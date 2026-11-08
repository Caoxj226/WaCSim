"""Contract-driven monitor-only alarm and true-state hazard oracle."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


class OracleError(ValueError):
    """The reviewed CTown oracle declaration is incomplete or inconsistent."""


@dataclass(frozen=True)
class OracleRules:
    alarm_rule_id: str
    hazard_rule_id: str
    hazard_threshold_m: float
    theta_m: float | None
    process_variable: str = "T7_LEVEL"
    setpoint_variable: str = "T7_LEVEL_SP"


def rules_from_scenario(data: dict, theta_m: float | None) -> OracleRules:
    """Validate the reviewed semantic variables before evaluating a trace."""
    alarms = data.get("alarms", [])
    hazards = data.get("hazards", [])
    if len(alarms) != 1 or len(hazards) != 1:
        raise OracleError("CTown oracle requires exactly one alarm and one hazard")
    alarm, hazard = alarms[0], hazards[0]
    if (alarm.get("process_var"), alarm.get("setpoint_var"), alarm.get("comparator"),
            alarm.get("monitor_only")) != ("T7_LEVEL", "T7_LEVEL_SP", ">", True):
        raise OracleError("unexpected semantic T7 tracking alarm declaration")
    if (hazard.get("variable"), hazard.get("comparator"), hazard.get("dwell_steps"),
            hazard.get("observation")) != ("T7_LEVEL", "<=", 1,
                                              "true_hydraulic_state_after_step"):
        raise OracleError("unexpected semantic T7 low-level hazard declaration")
    threshold = hazard.get("threshold")
    if type(threshold) not in (int, float) or not math.isfinite(float(threshold)):
        raise OracleError("hazard threshold must be finite")
    if theta_m is not None and (type(theta_m) not in (int, float)
                                or not math.isfinite(float(theta_m)) or theta_m < .1):
        raise OracleError("calibrated alarm threshold must be finite and at least 0.1 m")
    return OracleRules(alarm_rule_id=alarm["id"], hazard_rule_id=hazard["id"],
                       hazard_threshold_m=float(threshold), theta_m=theta_m)


def evaluate(true_t7_m: float, effective_sp_m: float, theta_m: float | None = None,
             *, rules: OracleRules | None = None) -> dict:
    """Evaluate one post-transition state using semantic CTown variables only."""
    if rules is None:
        rules = OracleRules("A-T7-TRACK", "H-T7-LOW", .5, theta_m)
    if not all(math.isfinite(float(value)) for value in (true_t7_m, effective_sp_m)):
        raise OracleError("oracle inputs must be finite")
    residual = abs(effective_sp_m - true_t7_m)
    hazard_margin = true_t7_m - rules.hazard_threshold_m
    alarm_margin = None if rules.theta_m is None else rules.theta_m - residual
    return {
        "tracking_residual_m": residual,
        "alarm_margin_m": alarm_margin,
        "hazard_margin_m": hazard_margin,
        "alarm_rule_ids": ([rules.alarm_rule_id]
                           if alarm_margin is not None and alarm_margin < 0 else []),
        "hazard_rule_ids": [rules.hazard_rule_id] if hazard_margin <= 0 else [],
    }


def score_rows(rows: Iterable[dict], *, score_start_step: int = 0) -> dict:
    """Score the declared window; retain CRAVE's strict-before-hazard tie rule."""
    if type(score_start_step) is not int or score_start_step < 0:
        raise OracleError("score_start_step must be a non-negative integer")
    first_alarm = first_hazard = None
    prior = set()
    for row in rows:
        k = row["t_step"]
        if k < score_start_step:
            continue
        if row["hazard_rule_ids"] and first_hazard is None:
            first_hazard = k
        if row["alarm_rule_ids"] and first_alarm is None:
            first_alarm = k
        if first_hazard is None:
            prior.update(row["alarm_rule_ids"])
    return {"score_start_step": score_start_step,
            "first_alarm_step": first_alarm, "first_hazard_step": first_hazard,
            "prehazard_alarm_rule_ids": sorted(prior),
            "prehazard_alarm_rule_ids_count": len(prior)}


def annotate_final_score(rows: list[dict], *, score_start_step: int = 0) -> dict:
    """Attach the completed trajectory's CRAVE-compatible score to every row.

    The rule IDs and margins are local to a transition; these four fields are
    final trajectory metadata repeated on each row so a JSONL consumer can
    read any row without a separate side channel.
    """
    score = score_rows(rows, score_start_step=score_start_step)
    for row in rows:
        row.update(score)
    return score
