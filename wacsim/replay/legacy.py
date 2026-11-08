"""Evaluate CTown's original rules at the same sampled clock as full PLCs."""
import struct
from wacsim.python2.entities.control import AboveControl, BelowControl


def real32(value: float) -> float:
    """Remote PLC REAL readings use IEEE single precision on the wire."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


class SampledControls:
    def __init__(self, source: str, inventory: list[dict], commands: dict[str, float]):
        self.commands = dict(commands)
        self.controls = []
        owners = {a: set(p.get("sensors", [])) for p in inventory for a in p.get("actuators", [])}
        section = ""
        for line in source.splitlines():
            fields = line.split(";", 1)[0].split()
            if fields and fields[0].startswith("["):
                section = fields[0].upper()
                continue
            if section != "[CONTROLS]" or not fields:
                continue
            if (len(fields) != 8 or fields[0] != "LINK" or fields[3:5] != ["IF", "NODE"]
                    or fields[2] not in ("OPEN", "CLOSED") or fields[6] not in ("ABOVE", "BELOW")):
                raise ValueError("unsupported CTown control; update the shared sampled-rule adapter")
            actuator, action, node, limit = fields[1], fields[2].lower(), fields[5], float(fields[7])
            if actuator == "PU10":
                continue  # its two rules are replaced by the shared PI
            kind = AboveControl if fields[6] == "ABOVE" else BelowControl
            self.controls.append((kind(actuator, action, node, limit), node not in owners[actuator]))
        self.step_index = -1
        self.remote = False
        self.levels = {}

    def apply(self, step: int, levels: dict[str, float]) -> dict[str, float]:
        if step != self.step_index + 1:
            raise ValueError("sampled controls require consecutive master-clock steps")
        self.levels = levels
        self.step_index = step
        for control, self.remote in self.controls:
            control.apply(self)  # same AboveControl/BelowControl methods as full GenericPLC
        return self.commands

    def get_tag(self, tag):
        value = self.levels[tag]
        return real32(value) if self.remote else value

    def set_tag(self, tag, value):
        self.commands[tag] = {"open": 1., "closed": 0.}[value]
