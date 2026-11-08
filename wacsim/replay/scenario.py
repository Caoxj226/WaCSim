"""CTown scenario registry, fingerprints, and formal replay gate."""
from __future__ import annotations
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
import yaml
from .contract import ContractError, InjectionSpec, ReplayTask
from .control import PIParameters


MODULES = ("contract.py", "control.py", "injections.py", "oracle.py", "trace.py", "calibration.py",
           "scenario.py", "engine.py", "cli.py", "full.py", "device.py", "legacy.py",
           "network.py")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_sha() -> str:
    digest = hashlib.sha256()
    for name in MODULES:
        digest.update(name.encode())
        digest.update((Path(__file__).parent / name).read_bytes())
    for name in ("init_database.py", "parser/config_parser.py", "python2/generic_plc.py",
                 "physical_process.py", "command_line.py", "python2/entities/control.py",
                 "network_attacks/Seq_mitm_attack.py",
                 "network_attacks/Seq_mitm_netfilter_queue.py",
                 "network_attacks/mitm_netfilter_queue_subprocess.py",
                 "network_attacks/synced_attack.py",
                 "python2/automatic_attacker.py", "python2/topo/complex_topo.py",
                 "../examples/ctown_topology/ctown_plcs.yaml"):
        digest.update(name.encode())
        digest.update((Path(__file__).parent.parent / name).read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class RunConfig(PIParameters):
    theta_m: float | None = None

    def __post_init__(self):
        super().__post_init__()
        if self.theta_m is not None and (type(self.theta_m) not in (float, int)
                                         or not math.isfinite(self.theta_m) or self.theta_m < 0.1):
            raise ValueError("theta_m must be finite and >= 0.1 m")

    def sha256(self) -> str:
        content = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(content.encode()).hexdigest()


def load_scenario(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("scenario_id") != "crave_ctown_t7_pu10":
        raise ContractError("unexpected CTown scenario")
    if data["time"]["hydraulic_step_seconds"] != 300:
        raise ContractError("CTown replay requires 300-second hydraulic steps")
    time = data["time"]
    score_start = time.get("score_start_step")
    if type(score_start) is not int or score_start < 0:
        raise ContractError("CTown scenario must declare a non-negative score_start_step")
    if score_start != time.get("warmup_steps") or score_start != time.get("first_attack_transition"):
        raise ContractError("CTown score must start after warmup and at first attack transition")
    if time.get("score_start_semantics") != "retain_warmup_telemetry_exclude_warmup_from_CRAVE_score":
        raise ContractError("CTown scenario must declare retained warmup telemetry semantics")
    return data


def writable_points(data: dict) -> dict:
    return {v["id"]: {"role": v["role"], "range": v["engineering_range"]}
            for v in data["variables"] if v["attacker_writable"]}


def create_task(network: Path, scenario: Path, config: RunConfig, total_steps: int,
                injections: tuple[InjectionSpec, ...] = (),
                lock: Path | None = None) -> ReplayTask:
    data = load_scenario(scenario)
    mode = "replay" if lock is not None or injections else "calibration"
    if injections and lock is None:
        raise ContractError("attack tasks require an explicit calibration lock")
    return ReplayTask(
        scenario_id=data["scenario_id"], total_steps=total_steps, step_seconds=300,
        scenario_sha256=file_sha(scenario), network_sha256=file_sha(network),
        implementation_sha256=implementation_sha(),
        configuration_sha256=file_sha(lock) if lock else config.sha256(),
        score_start_step=data["time"]["score_start_step"], mode=mode, injections=injections,
    )


def validate_task(task: ReplayTask, config: RunConfig, network: Path, scenario_path: Path,
                  *, require_lock: bool = True) -> dict:
    data = load_scenario(scenario_path)
    if task.scenario_id != data["scenario_id"] or task.step_seconds != 300:
        raise ContractError("task scenario or step length does not match CTown")
    if task.score_start_step != data["time"]["score_start_step"]:
        raise ContractError("task score origin does not match CTown scenario")
    if task.scenario_sha256 != file_sha(scenario_path) or task.network_sha256 != file_sha(network):
        raise ContractError("scenario or INP fingerprint mismatch")
    if task.implementation_sha256 != implementation_sha():
        raise ContractError("replay implementation fingerprint mismatch")
    if task.mode == "calibration":
        if task.injections or config.theta_m is not None or task.configuration_sha256 != config.sha256():
            raise ContractError("calibration must be unmodified and use the declared candidate")
        grid = data["calibration"]
        if (config.sp_m not in grid["nominal_setpoint_candidates_m"] or
                config.kp not in grid["candidate_grid"]["Kp"] or
                config.ki_per_step not in grid["candidate_grid"]["Ki_per_step"]):
            raise ContractError("calibration parameters are not in the declared grid")
    else:
        if config.theta_m is None:
            raise ContractError("attack replay requires a calibrated alarm threshold")
        task.validate_points(writable_points(data))
        if len(task.injections) not in (0, 1, 2):
            raise ContractError("CTown replay accepts no attack, one driver, or driver+masker")
        roles = {x.role for x in task.injections}
        if roles not in (set(), {"driver"}, {"driver", "masker"}):
            raise ContractError("unsupported CTown attack roles")
        if len(task.injections) == 2 and len({(x.start_step, x.duration) for x in task.injections}) != 1:
            raise ContractError("driver and masker must share start and duration")
        for x in task.injections:
            allowed = data["attack"][x.role]["slope_range_per_step"]
            if not (allowed[0] <= x.slope <= allowed[1]):
                raise ContractError(f"slope outside CTown range: {x.point}")
            if x.start_step != data["time"]["first_attack_transition"] or not (5 <= x.duration <= 120):
                raise ContractError("CTown attack start or duration is out of range")
            if task.total_steps != x.start_step + x.duration + 100:
                raise ContractError("CTown task must contain exactly 100 post-ramp observation steps")
        if require_lock:
            lock = scenario_path.parent / data["calibration"]["lock_file"]
            if data["status"] != "locked" or not lock.is_file():
                raise ContractError("formal attack replay requires reviewed scenario and calibration lock")
            if file_sha(lock) != task.configuration_sha256:
                raise ContractError("calibration lock fingerprint mismatch")
            locked = yaml.safe_load(lock.read_text(encoding="utf-8"))
            if (locked.get("nominal_setpoint_m"), locked.get("Kp"), locked.get("Ki_per_step"),
                locked.get("theta_m")) != (config.sp_m, config.kp, config.ki_per_step, config.theta_m):
                raise ContractError("runtime parameters differ from calibration lock")
            if locked.get("scenario_sha256") != task.scenario_sha256 or locked.get("network_sha256") != task.network_sha256:
                raise ContractError("calibration lock uses another scenario or network")
            if locked.get("implementation_sha256") != task.implementation_sha256:
                raise ContractError("calibration lock uses another replay implementation")
        elif task.configuration_sha256 != config.sha256():
            raise ContractError("test replay configuration fingerprint mismatch")
    return data
