"""Discrete PI shared by the fast backend and the later full WaCSim adapter."""
from dataclasses import dataclass
import math


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


@dataclass(frozen=True)
class PIParameters:
    sp_m: float
    kp: float
    ki_per_step: float

    def __post_init__(self):
        if not all(type(v) in (float, int) and math.isfinite(v)
                   for v in (self.sp_m, self.kp, self.ki_per_step)):
            raise ValueError("PI parameters must be finite numbers")
        if not (0.5 < self.sp_m < 5.0 and self.kp > 0 and self.ki_per_step > 0):
            raise ValueError("PI parameters outside CTown candidate domain")


class PIController:
    def __init__(self, parameters: PIParameters):
        self.parameters = parameters
        self.integral = 0.0

    def step(self, measured_t7_m: float, effective_sp_m: float) -> tuple[float, float]:
        if not all(math.isfinite(v) for v in (measured_t7_m, effective_sp_m)):
            raise ValueError("nonfinite PI input")
        p = self.parameters
        error = effective_sp_m - measured_t7_m
        trial = clamp(self.integral + error, 0.0, 1.0 / p.ki_per_step)
        trial_raw = p.kp * error + p.ki_per_step * trial
        if not ((trial_raw > 1.0 and error > 0.0) or
                (trial_raw < 0.0 and error < 0.0)):
            self.integral = trial
        raw = p.kp * error + p.ki_per_step * self.integral
        return raw, clamp(raw, 0.0, 1.0)


def apply_pump_speed(toolkit, project, index: int, command: float) -> None:
    """Apply a CTown PU10 command identically in fast and full EPANET paths."""
    if not math.isfinite(command) or not 0.0 <= command <= 1.0:
        raise ValueError("PU10 relative speed must be finite and in [0, 1]")
    if command == 0.0:
        toolkit.setlinkvalue(ph=project, index=index, property=toolkit.STATUS, value=0.0)
    else:
        toolkit.setlinkvalue(ph=project, index=index, property=toolkit.STATUS, value=1.0)
        toolkit.setlinkvalue(ph=project, index=index, property=toolkit.SETTING, value=command)
