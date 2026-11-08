"""Read-only phase-two validation helpers for fast and full CTown traces."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path


class ValidationError(ValueError):
    """A saved replay artifact cannot be used as phase-two evidence."""


def canonical_sha256(value: object) -> str:
    """Hash a finite JSON value in one platform-independent representation."""
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("validation artifact is not finite JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _finite(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValidationError(f"{name} must be finite")
    return result


def _score(score: object) -> dict:
    if not isinstance(score, dict):
        raise ValidationError("trace score must be an object")
    required = {"score_start_step", "first_alarm_step", "first_hazard_step",
                "prehazard_alarm_rule_ids"}
    if not required <= set(score):
        raise ValidationError("trace score lacks trigger metadata")
    result = dict(score)
    if type(result["score_start_step"]) is not int or result["score_start_step"] < 0:
        raise ValidationError("trace score has an invalid score origin")
    return result


def _project_rows(rows: object, *, source: str) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError(f"{source} trace has no rows")
    projected = []
    for expected_step, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("t_step") != expected_step:
            raise ValidationError(f"{source} trace is not consecutive from step zero")
        required = {"time_seconds", "T7_LEVEL", "T7_LEVEL_SP", "PU10_SPEED_CMD",
                    "alarm_rule_ids", "hazard_rule_ids", "alarm_margin_m", "hazard_margin_m"}
        if not required <= set(row):
            raise ValidationError(f"{source} trace lacks semantic validation fields")
        item = {"t_step": expected_step,
                "time_seconds": _finite(row["time_seconds"], f"{source}.time_seconds"),
                "T7_LEVEL": _finite(row["T7_LEVEL"], f"{source}.T7_LEVEL"),
                "T7_LEVEL_SP": _finite(row["T7_LEVEL_SP"], f"{source}.T7_LEVEL_SP"),
                "PU10_SPEED_CMD": _finite(row["PU10_SPEED_CMD"], f"{source}.PU10_SPEED_CMD"),
                "alarm_rule_ids": list(row["alarm_rule_ids"]),
                "hazard_rule_ids": list(row["hazard_rule_ids"]),
                "alarm_margin_m": (None if row["alarm_margin_m"] is None
                                   else _finite(row["alarm_margin_m"], f"{source}.alarm_margin_m")),
                "hazard_margin_m": _finite(row["hazard_margin_m"], f"{source}.hazard_margin_m")}
        if not all(isinstance(value, str) for value in item["alarm_rule_ids"] + item["hazard_rule_ids"]):
            raise ValidationError(f"{source} rule IDs must be text")
        projected.append(item)
    return projected


def load_fast_result(path: Path) -> dict:
    """Load the normalized subset of a successful fast replay JSON artifact."""
    data = json.loads(path.read_text(encoding="utf-8"))
    result = data.get("result")
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValidationError("fast replay artifact is not a successful result")
    return {"rows": _project_rows(result.get("rows"), source="fast"),
            "score": _score(result.get("score")), "run_key": result.get("run_key")}


def normalise_full_output(output: Path) -> dict:
    """Join full oracle rows with PLC5's actual PU10 command using master-clock steps."""
    summary_path = output / "oracle_summary.json"
    trace_path = output / "oracle_trace.jsonl"
    plc_path = output / "PLC5_values.csv"
    if not summary_path.is_file() or not trace_path.is_file() or not plc_path.is_file():
        raise ValidationError("full output needs oracle_summary.json, oracle_trace.jsonl, and PLC5_values.csv")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    oracle_rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line]
    commands: dict[int, float] = {}
    with plc_path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if "iteration" not in row or "PU10_SPEED_CMD" not in row:
                raise ValidationError("PLC5 CSV lacks iteration or PU10_SPEED_CMD")
            step = int(row["iteration"])
            if step in commands:
                raise ValidationError("PLC5 CSV has duplicate master-clock iterations")
            commands[step] = _finite(row["PU10_SPEED_CMD"], "full.PU10_SPEED_CMD")
    for row in oracle_rows:
        step = row.get("t_step")
        if step not in commands:
            raise ValidationError("full oracle row has no PLC5 PU10 command")
        row["PU10_SPEED_CMD"] = commands[step]
    return {"rows": _project_rows(oracle_rows, source="full"),
            "score": _score(summary.get("score")), "run_key": summary.get("run_key")}


def _trigger_error(fast: int | None, full: int | None, offset: int) -> int | None:
    if fast is None and full is None:
        return 0
    if fast is None or full is None:
        return None
    return abs((full - fast) - offset)


def compare_fast_and_full(fast: dict, full: dict, *, pu10_tolerance: float) -> dict:
    """Evaluate the §11.4 criteria after selecting the best allowed clock offset."""
    if pu10_tolerance < 0 or not math.isfinite(pu10_tolerance):
        raise ValidationError("PU10 comparison tolerance must be finite and nonnegative")
    fast_rows = _project_rows(fast.get("rows"), source="fast")
    full_rows = _project_rows(full.get("rows"), source="full")
    fast_score, full_score = _score(fast.get("score")), _score(full.get("score"))
    if fast_score["score_start_step"] != full_score["score_start_step"]:
        raise ValidationError("fast and full traces use different score origins")
    candidates = []
    for offset in (-1, 0, 1):
        pairs = [(fast_rows[step], full_rows[step + offset]) for step in range(len(fast_rows))
                 if 0 <= step + offset < len(full_rows)]
        if not pairs:
            continue
        t7_errors = [abs(left["T7_LEVEL"] - right["T7_LEVEL"]) for left, right in pairs]
        command_errors = [abs(left["PU10_SPEED_CMD"] - right["PU10_SPEED_CMD"])
                          for left, right in pairs]
        candidates.append({"offset": offset, "pairs": pairs, "t7_errors": t7_errors,
                           "command_errors": command_errors})
    if not candidates:
        raise ValidationError("fast and full traces have no allowed alignment overlap")
    chosen = min(candidates, key=lambda item: (max(item["t7_errors"]),
                                                sum(item["t7_errors"]) / len(item["t7_errors"]),
                                                abs(item["offset"])))
    offset = chosen["offset"]
    alarm_error = _trigger_error(fast_score["first_alarm_step"], full_score["first_alarm_step"], offset)
    hazard_error = _trigger_error(fast_score["first_hazard_step"], full_score["first_hazard_step"], offset)
    command_agreement = (sum(error <= pu10_tolerance for error in chosen["command_errors"])
                         / len(chosen["command_errors"]))
    prehazard_equal = (set(fast_score["prehazard_alarm_rule_ids"])
                       == set(full_score["prehazard_alarm_rule_ids"]))
    trigger_ok = alarm_error is not None and hazard_error is not None and alarm_error <= 1 and hazard_error <= 1
    checks = {
        "allowed_time_offset": abs(offset) <= 1,
        "T7_max_abs_error_le_0_01_m": max(chosen["t7_errors"]) <= .01,
        "PU10_command_agreement_ge_0_99": command_agreement >= .99,
        "alarm_first_step_error_le_1": alarm_error is not None and alarm_error <= 1,
        "hazard_first_step_error_le_1": hazard_error is not None and hazard_error <= 1,
        "prehazard_alarm_set_equal": prehazard_equal,
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "time_alignment_offset_steps": offset,
        "overlap_rows": len(chosen["pairs"]),
        "T7_max_abs_error_m": max(chosen["t7_errors"]),
        "T7_mean_abs_error_m": sum(chosen["t7_errors"]) / len(chosen["t7_errors"]),
        "PU10_tolerance": pu10_tolerance,
        "PU10_command_agreement_fraction": command_agreement,
        "PU10_max_abs_error": max(chosen["command_errors"]),
        "alarm_first_step_error": alarm_error,
        "hazard_first_step_error": hazard_error,
        "score_start_step": fast_score["score_start_step"],
        "fast_run_key": fast.get("run_key"), "full_run_key": full.get("run_key"),
    }
