"""Opt-in CTown wiring for the full PLC/SCADA/Mininet execution path.

The full runner consumes the same scenario, PI, device ramp and network masker
classes as fast replay, under the same calibration-lock gate.
"""
from dataclasses import asdict
from pathlib import Path
import json
import math
import shutil
import uuid
import yaml

from .contract import ContractError, InjectionSpec
from .control import PIController
from .device import DEVICE_ATTACK_NAME
from .oracle import annotate_final_score, evaluate, rules_from_scenario
from .scenario import RunConfig, create_task, load_scenario, validate_task


def configure_intermediate(data: dict, settings: dict, config_path: Path) -> None:
    """Validate before launch, then wire virtual tags and shared PI into PLC5."""
    allowed = {"profile", "scenario", "calibration_lock", "candidate", "injections"}
    if set(settings) - allowed:
        raise ContractError("unknown full CTown configuration fields")
    root = config_path.resolve().parent
    scenario = (root / settings["scenario"]).resolve()
    declaration = load_scenario(scenario)
    network = Path(data["inp_file"]).resolve()
    if network != (scenario.parent / declaration["source"]["network_inp"]).resolve():
        raise ContractError("full CTown must use the scenario's original network")
    if data["mode"] != "plccontrol":
        raise ContractError("CTown CRAVE requires plccontrol: PU11 and other INP rules remain active")
    if data.get("network_attacks") or any(p.get("attacks") for p in data["plcs"]):
        raise ContractError("legacy attacks cannot be mixed with the CTown CRAVE contract")
    if data.get("network_events") or data.get("sensor_noise") or data.get("noise_scale_data"):
        raise ContractError("CTown CRAVE baseline must be deterministic and noise-free")
    if data.get("initial_tank_data") or data.get("demand_patterns_data"):
        raise ContractError("CTown CRAVE uses the original network's initial states and demands")
    specs = tuple(InjectionSpec.from_dict(s) for s in settings.get("injections", []))
    profile = settings["profile"]
    if profile == "calibration":
        if specs or "calibration_lock" in settings:
            raise ContractError("calibration profile permits only an unmodified candidate")
        config = RunConfig(**settings["candidate"])
        task = create_task(network, scenario, config, data["iterations"])
    else:
        roles = {s.role for s in specs}
        expected = {"no_attack": set(), "driver": {"driver"},
                    "driver_masker": {"driver", "masker"}}
        if profile not in expected or roles != expected[profile] or "candidate" in settings:
            raise ContractError("full CTown profile/injections mismatch")
        lock = (root / settings["calibration_lock"]).resolve()
        required_lock = scenario.parent / declaration["calibration"]["lock_file"]
        if lock != required_lock.resolve() or not lock.is_file():
            raise ContractError("full CTown requires the same calibration lock as fast replay")
        locked = yaml.safe_load(lock.read_text(encoding="utf-8"))
        config = RunConfig(locked["nominal_setpoint_m"], locked["Kp"],
                           locked["Ki_per_step"], locked["theta_m"])
        task = create_task(network, scenario, config, data["iterations"], specs, lock)
    validate_task(task, config, network, scenario)
    runtime = {"profile": profile, "config": asdict(config), "task": task.to_dict(),
               "run_key": task.run_key, "execution_id": uuid.uuid4().hex,
               "scenario_path": str(scenario),
               "alarm_status": "pending_full_oracle_step_9.7"}
    data["crave_ctown_runtime"] = runtime
    drivers = [s for s in specs if s.role == "driver"]
    if drivers:
        data["crave_device_attacks"] = [{"name": DEVICE_ATTACK_NAME, "plc": "PLC5",
                                         "actuator": "PU10", "spec": drivers[0].to_dict()}]
    maskers = [s for s in specs if s.role == "masker"]
    if maskers:
        if len(maskers) != 1 or maskers[0].point != "T7_LEVEL_SP":
            raise ContractError("full CTown supports only the reviewed T7_LEVEL_SP masker")
        masker = maskers[0]
        data["network_attacks"].append({
            "type": "seq_mitm", "name": "CRAVEMASK", "target": "PLC5", "server": "PLC9",
            # Install the passive transport before warm-up reads; only the
            # injected response changes at masker.start_step.
            "trigger": {"type": "time", "start": 0, "end": data["iterations"] - 1},
            "queue_num": len(data["network_attacks"]) + 1,
            "tags": [{"tag": "T7SP", "offset": 0.0,
                      "scaleParam": masker.slope, "scaleTime": 1}],
            "crave_masker": {"tag": "T7SP", "spec": masker.to_dict(),
                             "scale_interval": 1,
                             "audit_file": str(Path(data["output_path"]).resolve()
                                               / f"network_masker_{runtime['execution_id']}.jsonl"),
                             "ready_file": str(Path(data["output_path"]).resolve()
                                               / f"network_masker_{runtime['execution_id']}.ready.json"),
                             "execution_id": runtime["execution_id"]},
        })
    plcs = {p["name"]: p for p in data["plcs"]}
    if "T7" not in plcs["PLC9"]["sensors"] or "PU10" not in plcs["PLC5"]["actuators"]:
        raise ContractError("unexpected T7/PU10 PLC ownership")
    if any("T7SP" in p["sensors"] for p in data["plcs"]):
        raise ContractError("T7SP must be added exactly once by the CRAVE scene")
    plcs["PLC9"]["sensors"].append("T7SP")
    plc5 = plcs["PLC5"]
    dependencies = plc5.get("dependent_sensors", plc5.get("dependant_sensors"))
    for tag in ("T7", "T7SP"):
        if tag not in dependencies:
            dependencies.append(tag)
    old = plc5["controls"]
    if sum(c["actuator"] == "PU10" for c in old) != 2:
        raise ContractError("expected two original PU10 rules to replace")
    plc5["controls"] = [c for c in old if c["actuator"] != "PU10"]
    # Stock WaCSim defaults all unspecified tank tags to zero. Seed the actual
    # INP values so PLC9 reports T7=2.5 at the initial control transition.
    import wntr
    wn = wntr.network.WaterNetworkModel(str(network))
    data["initial_tank_values"] = {name: float(tank.init_level) for name, tank in wn.tanks()}
    data["virtual_tag_initial_values"] = {"T7SP": config.sp_m}


def save_full_inputs(data: dict, intermediate: Path) -> None:
    runtime = data["crave_ctown_runtime"]
    output = Path(data["output_path"]) / "configuration"
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(intermediate, output / "intermediate.yaml")
    scenario = Path(runtime["scenario_path"])
    shutil.copyfile(scenario, output / "scenario.yaml")
    if runtime["profile"] != "calibration":
        declaration = load_scenario(scenario)
        shutil.copyfile(scenario.parent / declaration["calibration"]["lock_file"],
                        output / "calibration.lock.yaml")
    for name, value in (("runtime", runtime), ("task", runtime["task"]),
                        ("run_config", runtime["config"])):
        (output / f"{name}.json").write_text(json.dumps(value, indent=2, allow_nan=False))


def check_full_completion(data: dict) -> None:
    runtime = data["crave_ctown_runtime"]
    marker = Path(data["output_path"]) / "full_run_complete.json"
    if not marker.is_file():
        raise RuntimeError("full CTown physical process did not complete successfully; inspect its log")
    completed = json.loads(marker.read_text())
    if any(completed.get(key) != value for key, value in {
        "execution_id": runtime["execution_id"], "run_key": runtime["run_key"],
        "completed_steps": data["iterations"],
    }.items()):
        raise RuntimeError("full CTown completion record is stale or incomplete")


def evaluate_full_oracle(data: dict) -> dict:
    """Score saved full-platform outputs with the same oracle as fast replay.

    PLC5 row k records the effective setpoint used for transition k. Ground
    truth row k+1 is the true hydraulic state after that transition.
    """
    import pandas as pd

    runtime = data["crave_ctown_runtime"]
    output = Path(data["output_path"])
    scenario = Path(runtime["scenario_path"])
    if not scenario.is_file():  # lets preserved internal fixtures be rechecked
        scenario = output / "configuration" / "scenario.yaml"
    declaration = load_scenario(scenario)
    config = RunConfig(**runtime["config"])
    rules = rules_from_scenario(declaration, config.theta_m)
    steps = int(data["iterations"])
    ground = pd.read_csv(output / "ground_truth.csv").set_index("iteration")
    plc5 = pd.read_csv(output / "PLC5_values.csv").set_index("iteration")
    expected_ground = list(range(steps + 1))
    expected_plc = list(range(steps))
    if list(ground.index) != expected_ground or list(plc5.index) != expected_plc:
        raise RuntimeError("full oracle input rows do not describe the declared CTown clock")
    required_ground = {"T7_LEVEL"}
    required_plc = {"T7_LEVEL_SP", "T7_MEASURED"}
    if not required_ground <= set(ground.columns) or not required_plc <= set(plc5.columns):
        raise RuntimeError("full oracle input is missing a semantic T7 variable")
    rows = []
    for step in expected_plc:
        true_level = float(ground.loc[step + 1, "T7_LEVEL"])
        effective_sp = float(plc5.loc[step, "T7_LEVEL_SP"])
        measured = float(plc5.loc[step, "T7_MEASURED"])
        row = {"t_step": step, "time_seconds": (step + 1) * 300,
               "T7_LEVEL": true_level, "T7_LEVEL_SP": effective_sp,
               "T7_MEASUREMENT_PRE_STEP": measured,
               "source_ground_truth_iteration": step + 1,
               "source_PLC5_iteration": step}
        row.update(evaluate(true_level, effective_sp, rules=rules))
        rows.append(row)
    score = annotate_final_score(rows, score_start_step=runtime["task"]["score_start_step"])
    trace_path = output / "oracle_trace.jsonl"
    trace_path.write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in rows),
                          encoding="utf-8")
    summary = {"oracle_status": "theta_not_calibrated" if config.theta_m is None else "evaluated",
               "rules": {"alarm_rule_id": rules.alarm_rule_id,
                         "hazard_rule_id": rules.hazard_rule_id,
                         "process_variable": rules.process_variable,
                         "setpoint_variable": rules.setpoint_variable,
                         "hazard_threshold_m": rules.hazard_threshold_m,
                         "theta_m": rules.theta_m},
               "score": score,
               "score_start_step": runtime["task"]["score_start_step"],
               "score_start_semantics": "retain_warmup_telemetry_exclude_warmup_from_CRAVE_score",
               "trace_file": trace_path.name,
               "row_count": len(rows), "run_key": runtime["run_key"]}
    (output / "oracle_summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False),
                                                  encoding="utf-8")
    return summary


class FullPIControl:
    """PLC5 adapter; the PI formula itself lives only in control.PIController."""

    def __init__(self, parameters: dict):
        self.controller = PIController(RunConfig(**parameters))
        self.last_step = -1

    def apply(self, plc, step: int) -> None:
        if step != self.last_step + 1:
            raise ContractError("full PI must run exactly once per master-clock step")
        for tag in ("T7", "T7SP"):
            if not plc.tag_fresh.loc[step, tag] or not math.isfinite(plc.cache[tag]):
                raise ContractError(f"PLC5 has no fresh PLC9 {tag} sample at step {step}")
        measured, sp = plc.cache["T7"], plc.cache["T7SP"]
        raw, command = self.controller.step(measured, sp)
        plc.set_tag("PU10", command)
        for name, value in {"T7_MEASURED": measured, "T7_LEVEL_SP": sp,
                            "PU10_PI_RAW": raw, "PU10_PI_CMD": command,
                            "PU10_SPEED_CMD": command,
                            "PI_INTEGRAL": self.controller.integral}.items():
            plc.write_cache.loc[step, name] = value
        self.last_step = step
