"""Master-clock device ramp applied after the full PLC control calculation."""
import json
import math
from pathlib import Path

from .contract import ContractError, InjectionSpec
from .injections import Ramp

DEVICE_ATTACK_NAME = "CRAVE_PU10_RAMP"


class FullDriverRamp:
    def __init__(self, spec: InjectionSpec):
        if spec.role != "driver" or spec.point != "PU10_SPEED_CMD" or spec.range != (0.0, 1.0):
            raise ContractError("full device ramp requires the PU10 actuator driver")
        self.ramp = Ramp(spec)
        self.last_step = -1

    def apply(self, plc) -> None:
        # The step comes from the simulation DB, never wall time or packet count.
        step = plc.get_master_clock()
        if type(step) is not int or step != self.last_step + 1:
            raise ContractError("device ramp must run once per master-clock step")
        unmodified = float(plc.get_tag("PU10"))
        if not math.isfinite(unmodified) or not 0.0 <= unmodified <= 1.0:
            raise ContractError("device ramp needs the finite, clamped PI output")
        spec = self.ramp.spec
        active = step >= spec.start_step
        holding = step >= spec.start_step + spec.duration
        applied = self.ramp.value(step, unmodified)
        if active:
            plc.set_tag("PU10", applied)
        # Hold is still an attacker override, so its flag remains one.
        plc.set_attack_flag(active, DEVICE_ATTACK_NAME)
        for name, value in {
            "PU10_DRIVER_UNMODIFIED": unmodified,
            "PU10_ATTACK_FLAG": int(active),
            "PU10_ATTACK_RAMPING": int(active and not holding),
            "PU10_ATTACK_HOLDING": int(holding),
            "PU10_ATTACK_BASE": self.ramp.base_value,
            "PU10_ATTACK_APPLIED": applied,
            "PU10_SPEED_CMD": applied,
        }.items():
            plc.write_cache.loc[step, name] = value
        self.last_step = step

    def save_echo(self, output: Path, runtime: dict) -> None:
        if self.ramp.base_value is None:
            return
        echo = {"execution_id": runtime["execution_id"], "run_key": runtime["run_key"],
                "attack_name": DEVICE_ATTACK_NAME, "last_observed_step": self.last_step,
                "flag_includes_terminal_hold": True, **self.ramp.echo()}
        (output / "PLC5_driver_echo.json").write_text(json.dumps(echo, indent=2, allow_nan=False))
