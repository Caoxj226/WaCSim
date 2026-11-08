"""Replay wire format v1. No simulator, controller or alarm oracle is run here."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 1


class ContractError(ValueError):
    """A replay task or result violates the declared interface."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _integer(value: Any, name: str, minimum: int = 0) -> None:
    _require(type(value) is int and value >= minimum, f"{name}: expected integer >= {minimum}")


def _number(value: Any, name: str) -> float:
    _require(type(value) in (float, int), f"{name}: expected finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ContractError(f"{name}: number too large") from exc
    _require(math.isfinite(result), f"{name}: expected finite number")
    return result


def _text(value: Any, name: str) -> None:
    _require(isinstance(value, str) and bool(value.strip()), f"{name}: expected nonempty text")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ContractError("expected JSON-compatible data with finite numbers") from exc


def _object(value: Any, name: str) -> dict:
    _require(isinstance(value, Mapping), f"{name}: expected object")
    return json.loads(_json(dict(value)))


def _ids(value: Any, name: str) -> set[str]:
    _require(isinstance(value, list), f"{name}: expected list")
    for item in value:
        _text(item, name)
    _require(len(value) == len(set(value)), f"{name}: duplicate ID")
    return set(value)


@dataclass(frozen=True)
class InjectionSpec:
    point: str
    role: str
    slope: float
    duration: int
    start_step: int
    range: tuple[float, float]

    def __post_init__(self) -> None:
        _text(self.point, "point")
        _require(self.role in ("driver", "masker"), "role must be driver or masker")
        object.__setattr__(self, "slope", _number(self.slope, "slope"))
        _integer(self.duration, "duration", 1)
        _integer(self.start_step, "start_step")
        _require(isinstance(self.range, (tuple, list)) and len(self.range) == 2, "range needs two bounds")
        limits = tuple(_number(v, "range") for v in self.range)
        _require(limits[0] < limits[1], "range bounds must increase")
        object.__setattr__(self, "range", limits)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["range"] = list(self.range)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InjectionSpec:
        try:
            return cls(**dict(data))
        except TypeError as exc:
            raise ContractError(f"invalid InjectionSpec fields: {exc}") from exc


@dataclass(frozen=True)
class ReplayTask:
    scenario_id: str
    total_steps: int
    step_seconds: float
    scenario_sha256: str
    network_sha256: str
    implementation_sha256: str
    configuration_sha256: str
    score_start_step: int = 0
    injections: tuple[InjectionSpec, ...] = ()
    mode: str = "replay"
    meta: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _integer(self.schema_version, "schema_version", 1)
        _require(self.schema_version == SCHEMA_VERSION, "unsupported schema_version")
        _text(self.scenario_id, "scenario_id")
        _integer(self.total_steps, "total_steps", 1)
        _integer(self.score_start_step, "score_start_step")
        _require(self.score_start_step <= self.total_steps,
                 "score_start_step must not exceed the task horizon")
        seconds = _number(self.step_seconds, "step_seconds")
        _require(seconds > 0, "step_seconds must be positive")
        object.__setattr__(self, "step_seconds", seconds)
        _require(self.mode in ("replay", "calibration"), "unsupported mode")
        for name in ("scenario_sha256", "network_sha256", "implementation_sha256", "configuration_sha256"):
            value = getattr(self, name)
            _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
                     f"{name}: expected lowercase SHA-256")
        _require(isinstance(self.injections, (tuple, list)), "injections must be a list or tuple")
        specs = tuple(self.injections)
        _require(all(isinstance(s, InjectionSpec) for s in specs), "injections must contain InjectionSpec")
        _require(len({s.point for s in specs}) == len(specs), "duplicate injection point")
        for spec in specs:
            _require(spec.start_step + spec.duration <= self.total_steps, "attack extends beyond task")
        _require(self.mode != "calibration" or not specs, "calibration must have no attacks")
        object.__setattr__(self, "injections", tuple(sorted(specs, key=lambda s: s.point)))
        object.__setattr__(self, "meta", _object(self.meta, "meta"))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["injections"] = [spec.to_dict() for spec in self.injections]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayTask:
        values = dict(data)
        raw = values.get("injections", [])
        _require(isinstance(raw, (list, tuple)), "injections must be a list")
        values["injections"] = tuple(InjectionSpec.from_dict(spec) for spec in raw)
        try:
            return cls(**values)
        except TypeError as exc:
            raise ContractError(f"invalid ReplayTask fields: {exc}") from exc

    @property
    def run_key(self) -> str:
        payload = self.to_dict()
        # Labels and notes must never affect the physical run.
        payload.pop("meta")
        return "rk_" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def validate_points(self, writable_points: Mapping[str, Mapping[str, Any]]) -> None:
        """Check against the reviewed scenario's writable-point registry."""
        for spec in self.injections:
            _require(spec.point in writable_points, f"unknown or read-only point: {spec.point}")
            rule = writable_points[spec.point]
            _require(spec.role == rule["role"], f"role mismatch: {spec.point}")
            _require(spec.range == tuple(rule["range"]), f"range mismatch: {spec.point}")


@dataclass(frozen=True)
class ReplayResult:
    status: str
    rows: list[dict]
    score: dict
    injection_echo: list[dict]
    meta: dict
    run_key: str
    error: str | None = None

    def __post_init__(self) -> None:
        _require(self.status in ("ok", "error"), "status must be ok or error")
        _require(isinstance(self.run_key, str) and re.fullmatch(r"rk_[0-9a-f]{64}", self.run_key) is not None,
                 "invalid run_key")
        for name in ("rows", "injection_echo"):
            value = getattr(self, name)
            _require(isinstance(value, list), f"{name}: expected list")
            object.__setattr__(self, name, [_object(item, name) for item in value])
        for name in ("score", "meta"):
            object.__setattr__(self, name, _object(getattr(self, name), name))
        if self.status == "error":
            _text(self.error, "error")
            _require(not self.score, "failed run must not publish a success score")
        else:
            _require(self.error is None, "successful run must not contain error")

    def validate_for(self, task: ReplayTask) -> None:
        """Validate completeness and score consistency, not physical alarm formulas."""
        _require(self.run_key == task.run_key, "result belongs to another task")
        _require(len(self.rows) <= task.total_steps, "too many trace rows")
        if self.status == "ok":
            _require(len(self.rows) == task.total_steps, "incomplete trace cannot be successful")
        first_alarm = None
        first_hazard = None
        prior_alarms: set[str] = set()
        for index, row in enumerate(self.rows):
            _integer(row.get("t_step"), "row.t_step")
            _require(row["t_step"] == index, "trace steps must be consecutive from zero")
            seconds = _number(row.get("time_seconds"), "row.time_seconds")
            _require(seconds == (index + 1) * task.step_seconds, "row time must describe post-step state")
            alarms = _ids(row.get("alarm_rule_ids"), "alarm_rule_ids")
            hazards = _ids(row.get("hazard_rule_ids"), "hazard_rule_ids")
            if index >= task.score_start_step:
                if hazards and first_hazard is None:
                    first_hazard = index
                if alarms and first_alarm is None:
                    first_alarm = index
                if first_hazard is None:
                    prior_alarms.update(alarms)
        if self.status == "error":
            return
        _require(self.score.get("score_start_step") == task.score_start_step,
                 "score origin contradicts task")
        for name, expected in (("first_alarm_step", first_alarm), ("first_hazard_step", first_hazard)):
            _require(name in self.score, f"score missing {name}")
            actual = self.score[name]
            if actual is not None:
                _integer(actual, name)
            _require(actual == expected, f"score contradicts trace: {name}")
        _require(_ids(self.score.get("prehazard_alarm_rule_ids"), "prehazard_alarm_rule_ids") == prior_alarms,
                 "prehazard alarms contradict trace (same-step alarm is not prior)")
        count = self.score.get("prehazard_alarm_rule_ids_count")
        _integer(count, "prehazard_alarm_rule_ids_count")
        _require(count == len(prior_alarms), "prehazard alarm count mismatch")
        _require(len(self.injection_echo) == len(task.injections), "missing injection echo")
        echoes = {echo.get("point"): echo for echo in self.injection_echo}
        _require(len(echoes) == len(self.injection_echo), "duplicate injection echo")
        for spec in task.injections:
            echo = echoes.get(spec.point)
            _require(echo is not None, f"missing echo: {spec.point}")
            for key, value in spec.to_dict().items():
                _require(echo.get(key) == value, f"injection echo mismatch: {spec.point}.{key}")
            _number(echo.get("base_value"), "echo.base_value")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayResult:
        try:
            return cls(**dict(data))
        except TypeError as exc:
            raise ContractError(f"invalid ReplayResult fields: {exc}") from exc
