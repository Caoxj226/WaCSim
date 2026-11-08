"""Deterministic ramp/hold injection; no packet-clock dependence."""
from dataclasses import dataclass
from .contract import InjectionSpec
from .control import clamp


@dataclass
class Ramp:
    spec: InjectionSpec
    base_value: float | None = None

    def value(self, step: int, unmodified: float) -> float:
        if step < self.spec.start_step:
            return unmodified
        if self.base_value is None:
            self.base_value = float(unmodified)
        offset = min(step - self.spec.start_step, self.spec.duration - 1)
        return clamp(self.base_value + self.spec.slope * offset, *self.spec.range)

    def echo(self) -> dict:
        if self.base_value is None:
            raise ValueError("injection base was never captured")
        return {**self.spec.to_dict(), "base_value": self.base_value}
