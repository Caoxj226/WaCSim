"""Fixed-grid, no-attack CTown baseline calibration and offline verification."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import math
from pathlib import Path
import re
from statistics import mean
import subprocess
import tempfile
from typing import Iterable

import yaml

from .engine import run_epanet
from .scenario import RunConfig, create_task, file_sha, implementation_sha, load_scenario, validate_task


class CalibrationError(ValueError):
    """A candidate or its saved calibration evidence violates the protocol."""


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def _required(data: dict, *keys: str) -> None:
    if not all(key in data for key in keys):
        raise CalibrationError(f"calibration record lacks required fields: {keys}")


def nearest_rank(values: Iterable[float], quantile: float) -> float:
    """Return the declared 1-based nearest-rank quantile as a finite float."""
    sample = sorted(float(value) for value in values)
    if not sample or not 0 < quantile <= 1 or not all(math.isfinite(value) for value in sample):
        raise CalibrationError("nearest-rank quantile needs nonempty finite samples and 0 < q <= 1")
    return sample[math.ceil(quantile * len(sample)) - 1]


def tank_bounds_from_inp(inp: Path) -> dict[str, tuple[float, float]]:
    """Read CTown tank MinLevel/MaxLevel without relying on WNTR conversions."""
    section = ""
    bounds: dict[str, tuple[float, float]] = {}
    for raw in inp.read_text(encoding="utf-8").splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.upper()
            continue
        if section == "[TANKS]":
            cells = line.split()
            if len(cells) < 5:
                raise CalibrationError(f"malformed INP tank row: {line}")
            bounds[cells[0]] = (float(cells[3]), float(cells[4]))
    if set(bounds) != {"T1", "T2", "T3", "T4", "T5", "T6", "T7"}:
        raise CalibrationError("expected CTown T1--T7 bounds in INP")
    return bounds


def protocol_paths(scenario: Path, declaration: dict) -> tuple[Path, Path]:
    calibration = declaration["calibration"]
    multi = scenario.parent / calibration["multicycle_protocol"]["document"]
    candidates = scenario.parent / calibration["configuration_protocol"]
    if not multi.is_file() or not candidates.is_file():
        raise CalibrationError("calibration protocol documents are missing")
    return multi, candidates


def phase1_commit_matches_scenario(scenario: Path, commit: str) -> bool:
    """Require an independent commit whose Git-normalized scenario is current."""
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        return False
    scenario = scenario.resolve()
    try:
        root = Path(subprocess.run(["git", "-C", str(scenario.parents[2]), "rev-parse", "--show-toplevel"],
                                   check=True, capture_output=True, text=True).stdout.strip())
        relative = scenario.relative_to(root).as_posix()
        committed = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{relative}"],
                                   check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False
    return committed.replace(b"\r\n", b"\n") == scenario.read_bytes().replace(b"\r\n", b"\n")


def assess_candidate(rows: list[dict], declaration: dict,
                     tank_bounds: dict[str, tuple[float, float]]) -> dict:
    """Evaluate exactly the predeclared no-attack selection conditions."""
    calibration = declaration["calibration"]
    protocol = calibration["multicycle_protocol"]
    warmup = calibration["warmup_steps_excluded"]
    cycle_steps = calibration["demand_cycle_steps"]
    cycle_count = calibration["demand_cycles_to_run"]
    expected_total = declaration["time"]["calibration_transitions"]
    expected_scored = calibration["scored_sample_count"]
    if len(rows) != expected_total or expected_total != warmup + expected_scored:
        raise CalibrationError("candidate does not contain the declared calibration timeline")
    scored = rows[warmup:]
    weeks = [scored[index * cycle_steps:(index + 1) * cycle_steps]
             for index in range(cycle_count)]
    if len(scored) != expected_scored or any(len(week) != cycle_steps for week in weeks):
        raise CalibrationError("candidate cannot be divided into declared complete demand cycles")
    required_fields = {"T7_LEVEL", "T7_LEVEL_SP", "PU10_SPEED_CMD", "tank_levels_m",
                       "min_junction_pressure_m"}
    if any(not required_fields <= set(row) for row in rows):
        raise CalibrationError("candidate trace lacks calibration observations")
    if any(not math.isfinite(float(value)) for row in rows for value in (
            row["T7_LEVEL"], row["T7_LEVEL_SP"], row["PU10_SPEED_CMD"],
            row["min_junction_pressure_m"])):
        raise CalibrationError("candidate trace contains nonfinite observations")

    tolerance = float(protocol["tank_bound_numeric_tolerance_m"])
    pressure_tolerance = float(protocol["pressure_numeric_tolerance_m"])
    mae_limit = float(protocol["tracking_MAE_limit_m"])
    saturation_limit = float(calibration["eliminate_if_saturation_fraction_strictly_greater_than"])
    drift_limit = float(protocol["all_tank_weekly_mean_difference_limit_fraction_of_span"])
    hazard_threshold = float(declaration["hazards"][0]["threshold"])
    failures: list[str] = []
    if any(float(row["T7_LEVEL"]) <= hazard_threshold for row in rows):
        failures.append("T7_hazard")
    out_of_bounds = [name for row in rows for name, (low, high) in tank_bounds.items()
                     if not (low - tolerance <= float(row["tank_levels_m"].get(name, math.nan))
                             <= high + tolerance)]
    if out_of_bounds:
        failures.append("tank_bounds")
    if any(float(row["min_junction_pressure_m"]) < -pressure_tolerance for row in scored):
        failures.append("junction_pressure")
    weekly_mae = [mean(abs(float(row["T7_LEVEL_SP"]) - float(row["T7_LEVEL"])) for row in week)
                  for week in weeks]
    weekly_saturation = [sum(float(row["PU10_SPEED_CMD"]) in (0.0, 1.0) for row in week) / len(week)
                         for week in weeks]
    overall_mae = mean(abs(float(row["T7_LEVEL_SP"]) - float(row["T7_LEVEL"])) for row in scored)
    overall_saturation = (sum(float(row["PU10_SPEED_CMD"]) in (0.0, 1.0) for row in scored)
                          / len(scored))
    if overall_mae > mae_limit or any(value > mae_limit for value in weekly_mae):
        failures.append("tracking_MAE")
    if overall_saturation > saturation_limit or any(value > saturation_limit for value in weekly_saturation):
        failures.append("command_saturation")
    second, third = weeks[1], weeks[2]
    drifted = []
    for name, (low, high) in tank_bounds.items():
        difference = abs(mean(float(row["tank_levels_m"][name]) for row in second)
                         - mean(float(row["tank_levels_m"][name]) for row in third))
        if difference > drift_limit * (high - low):
            drifted.append(name)
    if drifted:
        failures.append("all_tank_weekly_mean_drift")
    required_pressure_samples = [float(value) for row in scored
                                 for value in row.get("junction_pressures_m", {}).values()]
    required_pressure_deficit = [value for value in required_pressure_samples if value < 20.0]
    return {
        "qualified": not failures,
        "fail_reasons": failures,
        "scoring_step_range_inclusive": [warmup, expected_total - 1],
        "T7_min_m": min(float(row["T7_LEVEL"]) for row in rows),
        "tracking_MAE_m": overall_mae,
        "weekly_MAE_m": weekly_mae,
        "saturation_fraction": overall_saturation,
        "weekly_saturation_fraction": weekly_saturation,
        "min_junction_pressure_m": min(float(row["min_junction_pressure_m"]) for row in scored),
        "all_tank_weekly_mean_drift": drifted,
        "tank_bound_violation_count": len(out_of_bounds),
        "required_pressure_threshold_m": 20.0,
        "required_pressure_deficit_sample_count": len(required_pressure_deficit),
        "required_pressure_deficit_fraction": (len(required_pressure_deficit) / len(required_pressure_samples)
                                               if required_pressure_samples else None),
    }


def select_candidate(entries: list[dict], declaration: dict) -> dict | None:
    """Apply the fixed SP order and the declared MAE/Kp/Ki tie-break exactly."""
    for sp in declaration["calibration"]["nominal_setpoint_candidates_m"]:
        qualifying = [entry for entry in entries
                      if entry.get("status") == "ok" and entry["SP_m"] == sp
                      and entry["metrics"]["qualified"]]
        if qualifying:
            return min(qualifying, key=lambda item: (item["metrics"]["tracking_MAE_m"],
                                                      item["Kp"], item["Ki_per_step"]))
    return None


def _selection_metrics(metrics: dict) -> dict:
    """Exclude optional per-junction diagnostics from deterministic candidate ranking."""
    fields = ("qualified", "fail_reasons", "scoring_step_range_inclusive", "T7_min_m",
              "tracking_MAE_m", "weekly_MAE_m", "saturation_fraction",
              "weekly_saturation_fraction", "min_junction_pressure_m",
              "all_tank_weekly_mean_drift", "tank_bound_violation_count")
    return {field: metrics[field] for field in fields}


def _candidate_entry(inp: Path, scenario: Path, declaration: dict,
                     bounds: dict[str, tuple[float, float]], sp: float, kp: float, ki: float,
                     *, include_network_detail: bool = False) -> tuple[dict, object | None, object | None]:
    config = RunConfig(sp, kp, ki)
    task = create_task(inp, scenario, config, declaration["time"]["calibration_transitions"])
    entry = {"SP_m": sp, "Kp": kp, "Ki_per_step": ki, "run_key": task.run_key}
    try:
        result = run_epanet(task, config, inp, scenario,
                            include_network_detail=include_network_detail)
        entry.update(status="ok", metrics=assess_candidate(result.rows, declaration, bounds),
                     epanet_warning_count=result.meta["epanet_warning_count"],
                     epanet_warning_kinds=result.meta["epanet_warning_kinds"])
        return entry, task, result
    except Exception as exc:
        entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
        return entry, None, None


def _candidate_worker(payload: dict) -> dict:
    """Run one independent no-attack candidate in a separate process."""
    entry, _, _ = _candidate_entry(Path(payload["inp"]), Path(payload["scenario"]),
                                   payload["declaration"], payload["bounds"],
                                   payload["sp"], payload["kp"], payload["ki"])
    return entry


def _candidate_lock(declaration: dict, scenario: Path, inp: Path, selected: dict,
                    task, result, baseline_trace: Path, *, formal: bool,
                    phase1_commit: str | None) -> dict:
    calibration = declaration["calibration"]
    multi_protocol, candidate_protocol = protocol_paths(scenario, declaration)
    rows = result.rows[calibration["warmup_steps_excluded"]:]
    residuals = [abs(float(row["T7_LEVEL_SP"]) - float(row["T7_LEVEL"])) for row in rows]
    rank_value = nearest_rank(residuals, float(declaration["alarms"][0]["threshold_algorithm"]
                                               ["empirical_quantile"]))
    floor = float(declaration["alarms"][0]["threshold_algorithm"]["floor_m"])
    theta = max(rank_value, floor)
    alarm_count = sum(value > theta for value in residuals)
    config = RunConfig(selected["SP_m"], selected["Kp"], selected["Ki_per_step"])
    lock = {
        "schema_version": 1,
        "kind": "calibration_lock" if formal else "preformal_calibration_candidate",
        "governance_status": ("formal_locked_contract" if formal
                              else "not_a_formal_lock_until_phase1_review_and_commit"),
        "source_commit": declaration["source"]["wacsim_commit"],
        "scenario_sha256": file_sha(scenario),
        "network_sha256": file_sha(inp),
        "implementation_sha256": implementation_sha(),
        "calibration_protocol_sha256": file_sha(multi_protocol),
        "candidate_configuration_protocol_sha256": file_sha(candidate_protocol),
        "nominal_setpoint_m": config.sp_m,
        "Kp": config.kp,
        "Ki_per_step": config.ki_per_step,
        "integral_initial": 0.0,
        "integral_limits_resolved": {"lower": 0.0, "upper": 1.0 / config.ki_per_step},
        "theta_m": theta,
        "theta_source": {"method": "nearest_rank", "quantile": .999,
                         "rank_one_based": math.ceil(.999 * len(residuals)),
                         "sample_count": len(residuals), "rank_value_m": rank_value,
                         "floor_m": floor, "baseline_alarm_count_strict_gt": alarm_count,
                         "baseline_alarm_fraction_strict_gt": alarm_count / len(residuals)},
        "baseline_run_key": task.run_key,
        "baseline_trace_sha256": file_sha(baseline_trace),
        "baseline_metrics": selected["metrics"],
    }
    if formal:
        lock["phase1_contract_commit"] = phase1_commit
    return lock


def calibrate(inp: Path, scenario: Path, output: Path,
              *, phase1_commit: str | None = None, workers: int = 1) -> dict:
    """Run all fixed no-attack candidates and write a self-contained candidate package."""
    if output.exists():
        raise CalibrationError(f"refusing to overwrite existing calibration output: {output}")
    if type(workers) is not int or workers < 1:
        raise CalibrationError("workers must be a positive integer")
    declaration = load_scenario(scenario)
    protocol_status = declaration["calibration"]["multicycle_protocol"]["status"]
    formal = declaration["status"] == "locked"
    if formal:
        if not protocol_status.startswith("approved"):
            raise CalibrationError("locked scenario requires an approved calibration protocol")
        if phase1_commit is None or not phase1_commit_matches_scenario(scenario, phase1_commit):
            raise CalibrationError("formal lock requires an independent commit matching the locked scenario")
    bounds = tank_bounds_from_inp(inp)
    calibration = declaration["calibration"]
    expected = (len(calibration["nominal_setpoint_candidates_m"]) * len(calibration["candidate_grid"]["Kp"])
                * len(calibration["candidate_grid"]["Ki_per_step"]))
    if expected != calibration["maximum_candidate_count"]:
        raise CalibrationError("candidate grid size contradicts scenario contract")
    output.mkdir(parents=True)
    entries = []
    partial = output / "candidate_metrics.partial.json"
    candidates = [(sp, kp, ki) for sp in calibration["nominal_setpoint_candidates_m"]
                  for kp in calibration["candidate_grid"]["Kp"]
                  for ki in calibration["candidate_grid"]["Ki_per_step"]]
    payloads = [{"inp": str(inp), "scenario": str(scenario), "declaration": declaration,
                 "bounds": bounds, "sp": sp, "kp": kp, "ki": ki}
                for sp, kp, ki in candidates]
    execution_workers = min(workers, len(payloads))
    if execution_workers == 1:
        iterator = map(_candidate_worker, payloads)
        for entry in iterator:
            entries.append(entry)
            _write_json(partial, {"status": "running", "completed_candidates": len(entries),
                                  "expected_candidates": expected, "workers": execution_workers,
                                  "entries": entries})
    else:
        with ProcessPoolExecutor(max_workers=execution_workers) as pool:
            for entry in pool.map(_candidate_worker, payloads):
                entries.append(entry)
                _write_json(partial, {"status": "running", "completed_candidates": len(entries),
                                      "expected_candidates": expected, "workers": execution_workers,
                                      "entries": entries})
    selected = select_candidate(entries, declaration)
    report = {
        "schema_version": 1,
        "kind": "formal_no_attack_calibration" if formal else "preformal_no_attack_calibration",
        "governance_status": ("formal_locked_contract" if formal
                              else "pending_phase1_external_review_and_independent_commit"),
        "scenario_status": declaration["status"],
        "scenario_sha256": file_sha(scenario), "network_sha256": file_sha(inp),
        "implementation_sha256": implementation_sha(),
        "candidate_count": len(entries), "candidate_execution_workers": execution_workers,
        "entries": entries,
    }
    if selected is None:
        report.update(status="no_qualifying_candidate", selected=None)
        _write_json(output / "candidate_metrics.json", report)
        partial.unlink()
        return report
    detailed, task, result = _candidate_entry(inp, scenario, declaration, bounds,
                                               selected["SP_m"], selected["Kp"],
                                               selected["Ki_per_step"], include_network_detail=True)
    if detailed.get("status") != "ok" or not detailed["metrics"]["qualified"]:
        raise CalibrationError("selected candidate did not reproduce during evidence capture")
    if _selection_metrics(detailed["metrics"]) != _selection_metrics(selected["metrics"]):
        raise CalibrationError("selected candidate metrics changed during evidence capture")
    baseline_trace = output / "baseline_trace.json"
    _write_json(baseline_trace, {"task": task.to_dict(), "result": result.to_dict()})
    selected = detailed
    lock = _candidate_lock(declaration, scenario, inp, selected, task, result, baseline_trace,
                           formal=formal, phase1_commit=phase1_commit)
    lock_path = output / (declaration["calibration"]["lock_file"] if formal
                          else "calibration.candidate.yaml")
    lock_path.write_text(yaml.safe_dump(lock, sort_keys=False, allow_unicode=True), encoding="utf-8")
    report.update(status="formal_calibration_complete" if formal else "technical_calibration_complete",
                  selected=selected, baseline_trace=baseline_trace.name,
                  calibration_lock=lock_path.name, formal_lock=formal)
    _write_json(output / "candidate_metrics.json", report)
    if formal:
        official_lock = scenario.parent / declaration["calibration"]["lock_file"]
        if official_lock.exists():
            raise CalibrationError(f"refusing to overwrite existing formal lock: {official_lock}")
        official_lock.write_text(lock_path.read_text(encoding="utf-8"), encoding="utf-8")
    partial.unlink()
    verification = verify_calibration(output, inp, scenario)
    _write_json(output / "calibration_verification.json", verification)
    return report


def verify_calibration(output: Path, inp: Path, scenario: Path) -> dict:
    """Independently recompute all 11.2 technical checks from saved artifacts only."""
    declaration = load_scenario(scenario)
    report_path = output / "candidate_metrics.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    lock_path = output / report.get("calibration_lock", report.get("candidate_lock", "calibration.candidate.yaml"))
    trace_path = output / "baseline_trace.json"
    checks: dict[str, dict] = {}

    def check(name: str, condition: bool, detail: str) -> None:
        checks[name] = {"passed": bool(condition), "detail": detail}

    check("candidate_record_exists", report_path.is_file() and lock_path.is_file() and trace_path.is_file(),
          "candidate calibration report, candidate lock, and full baseline trace are present")
    if not all(item["passed"] for item in checks.values()):
        return {"status": "failed", "checks": checks}
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    rows = trace["result"]["rows"]
    metrics = assess_candidate(rows, declaration, tank_bounds_from_inp(inp))
    _required(lock, "scenario_sha256", "network_sha256", "implementation_sha256", "nominal_setpoint_m",
              "Kp", "Ki_per_step", "theta_m", "baseline_run_key", "baseline_metrics",
              "calibration_protocol_sha256", "baseline_trace_sha256")
    check("no_attack_baseline_no_hazard", metrics["qualified"] and "T7_hazard" not in metrics["fail_reasons"],
          f"T7 minimum is {metrics['T7_min_m']:.9g} m")
    check("saturation_limit", metrics["saturation_fraction"] <= .25
          and all(value <= .25 for value in metrics["weekly_saturation_fraction"]),
          f"overall saturation={metrics['saturation_fraction']:.9g}")
    residuals = [abs(float(row["T7_LEVEL_SP"]) - float(row["T7_LEVEL"])) for row in rows[24:]]
    expected_theta = max(nearest_rank(residuals, .999), .1)
    check("threshold_formula", lock["theta_m"] == expected_theta,
          f"saved theta={lock['theta_m']:.17g}, independently recomputed theta={expected_theta:.17g}")
    metadata_ok = (lock["scenario_sha256"] == file_sha(scenario)
                   and lock["network_sha256"] == file_sha(inp)
                   and lock["implementation_sha256"] == implementation_sha()
                   and lock["baseline_run_key"] == trace["result"]["run_key"]
                   and lock["baseline_trace_sha256"] == file_sha(trace_path)
                   and lock["baseline_metrics"] == report["selected"]["metrics"])
    check("metadata_and_hashes", metadata_ok,
          "scenario, INP, implementation, trace, run key, and metrics match saved evidence")
    # This uses an isolated locked copy of the scenario.  The research scenario
    # stays ready_for_external_review, while the replay gate itself is exercised
    # once with a matching lock and once after that lock is changed.
    rejection_ok = False
    try:
        with tempfile.TemporaryDirectory(prefix="ctown-stale-lock-") as directory:
            temporary = Path(directory)
            copied_scenario = temporary / "scenario.yaml"
            copied = yaml.safe_load(scenario.read_text(encoding="utf-8"))
            copied["status"] = "locked"
            copied_scenario.write_text(yaml.safe_dump(copied, sort_keys=False), encoding="utf-8")
            copied_lock = dict(lock)
            copied_lock["scenario_sha256"] = file_sha(copied_scenario)
            copied_lock["network_sha256"] = file_sha(inp)
            copied_lock["implementation_sha256"] = implementation_sha()
            active_lock = temporary / copied["calibration"]["lock_file"]
            active_lock.write_text(yaml.safe_dump(copied_lock, sort_keys=False), encoding="utf-8")
            config = RunConfig(copied_lock["nominal_setpoint_m"], copied_lock["Kp"],
                               copied_lock["Ki_per_step"], copied_lock["theta_m"])
            task = create_task(inp, copied_scenario, config, 129, lock=active_lock)
            validate_task(task, config, inp, copied_scenario)
            copied_lock["theta_m"] = copied_lock["theta_m"] + .1
            active_lock.write_text(yaml.safe_dump(copied_lock, sort_keys=False), encoding="utf-8")
            try:
                validate_task(task, config, inp, copied_scenario)
            except Exception:
                rejection_ok = True
    except Exception:
        rejection_ok = False
    check("stale_lock_rejected", rejection_ok,
          "an isolated matching lock succeeds; modifying it causes validated replay to reject it")
    technical = all(item["passed"] for item in checks.values())
    official_lock = scenario.parent / declaration["calibration"]["lock_file"]
    formal = bool(report.get("formal_lock"))
    official_contents_match = official_lock.is_file() and file_sha(official_lock) == file_sha(lock_path)
    phase1_commit = lock.get("phase1_contract_commit")
    governance = (formal and declaration.get("status") == "locked"
                  and declaration["calibration"]["multicycle_protocol"]["status"].startswith("approved")
                  and official_contents_match and isinstance(phase1_commit, str)
                  and phase1_commit_matches_scenario(scenario, phase1_commit))
    checks["formal_11_2_gate"] = {
        "passed": governance,
        "detail": "formal pass requires a locked/approved committed contract and matching official lock path",
    }
    return {
        "status": "passed_technical_pending_governance" if technical and not governance
                  else ("passed" if technical else "failed"),
        "technical_checks_passed": technical,
        "formal_11_2_passed": technical and governance,
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fixed CTown no-attack calibration grid")
    parser.add_argument("--inp", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true", help="only verify an existing candidate package")
    parser.add_argument("--phase1-contract-commit", type=str,
                        help="required for a formal lock: commit containing the current locked scenario")
    parser.add_argument("--workers", type=int, default=1,
                        help="independent candidate replays to run concurrently (default: 1)")
    args = parser.parse_args()
    if args.verify:
        result = verify_calibration(args.output, args.inp, args.scenario)
        _write_json(args.output / "calibration_verification.json", result)
    else:
        result = calibrate(args.inp, args.scenario, args.output,
                           phase1_commit=args.phase1_contract_commit, workers=args.workers)
    print(json.dumps({key: result.get(key) for key in ("status", "candidate_count", "selected")},
                     ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
