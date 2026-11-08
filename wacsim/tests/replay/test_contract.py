"""Interface checks using synthetic rows, not CTown simulation results."""

from dataclasses import replace
import json

import pytest

from wacsim.replay import ContractError, InjectionSpec, ReplayResult, ReplayTask


def task(**changes):
    fields = dict(
        scenario_id="contract-test", total_steps=3, step_seconds=300,
        scenario_sha256="a" * 64, network_sha256="b" * 64,
        implementation_sha256="c" * 64, configuration_sha256="d" * 64,
    )
    fields.update(changes)
    return ReplayTask(**fields)


def spec(**changes):
    fields = dict(point="PU10_SPEED_CMD", role="driver", slope=-0.01,
                  duration=2, start_step=1, range=(0, 1))
    fields.update(changes)
    return InjectionSpec(**fields)


def result(t, alarm_step=None, hazard_step=None):
    rows = [dict(t_step=k, time_seconds=(k + 1) * t.step_seconds,
                 alarm_rule_ids=["A"] if k == alarm_step else [],
                 hazard_rule_ids=["H"] if k == hazard_step else [])
            for k in range(t.total_steps)]
    scored_alarm = alarm_step if alarm_step is not None and alarm_step >= t.score_start_step else None
    scored_hazard = hazard_step if hazard_step is not None and hazard_step >= t.score_start_step else None
    prior = ["A"] if scored_alarm is not None and (scored_hazard is None or scored_alarm < scored_hazard) else []
    return ReplayResult(
        status="ok", rows=rows,
        score=dict(score_start_step=t.score_start_step,
                   first_alarm_step=scored_alarm, first_hazard_step=scored_hazard,
                   prehazard_alarm_rule_ids=prior, prehazard_alarm_rule_ids_count=len(prior)),
        injection_echo=[dict(**s.to_dict(), base_value=0.6) for s in t.injections],
        meta={"synthetic": True}, run_key=t.run_key,
    )


@pytest.mark.parametrize("mode", ["no_attack", "single", "dual"])
def test_json_roundtrip(mode):
    specs = () if mode == "no_attack" else (spec(),)
    if mode == "dual":
        specs += (spec(point="T7_LEVEL_SP", role="masker", range=(0, 5)),)
    t = task(injections=specs)
    restored = ReplayTask.from_dict(json.loads(json.dumps(t.to_dict())))
    assert restored == t
    assert restored.run_key == t.run_key
    r = ReplayResult.from_dict(json.loads(json.dumps(result(t).to_dict())))
    r.validate_for(t)


@pytest.mark.parametrize("changes", [
    {"duration": 0}, {"duration": 1.5}, {"start_step": -1}, {"start_step": True},
    {"slope": float("nan")}, {"slope": float("inf")}, {"range": [1, 0]},
    {"range": [0, float("inf")]}, {"role": "hidden"}, {"point": ""},
])
def test_bad_attack_parameters(changes):
    with pytest.raises(ContractError):
        spec(**changes)


def test_reject_conflicting_or_out_of_horizon_injections():
    with pytest.raises(ContractError):
        task(injections=(spec(), spec()))
    with pytest.raises(ContractError):
        task(injections=(spec(duration=3),))


def test_unknown_fields_are_not_silently_ignored():
    data = task().to_dict()
    data["duraton"] = 30
    with pytest.raises(ContractError):
        ReplayTask.from_dict(data)


@pytest.mark.parametrize("name", ["scenario_sha256", "network_sha256", "implementation_sha256", "configuration_sha256"])
def test_configuration_changes_cannot_reuse_run_key(name):
    t = task()
    assert replace(t, **{name: "e" * 64}).run_key != t.run_key


def test_metadata_and_injection_list_order_do_not_change_task_identity():
    a, b = spec(), spec(point="T7_LEVEL_SP", role="masker", range=(0, 5))
    t = task(injections=(a, b))
    assert replace(t, injections=(b, a), meta={"note": "another label"}).run_key == t.run_key
    assert replace(t, injections=(replace(a, slope=-0.02), b)).run_key != t.run_key
    assert replace(t, score_start_step=1).run_key != t.run_key


def test_calibration_is_possible_without_a_finished_lock_but_not_with_attack():
    # configuration_sha256 identifies a candidate config, not a calibration lock, in this mode.
    t = task(mode="calibration")
    assert t.run_key != replace(t, mode="replay").run_key
    with pytest.raises(ContractError):
        task(mode="calibration", injections=(spec(),))


def test_scenario_registry_enforces_write_permission_role_and_range():
    t = task(injections=(spec(),))
    t.validate_points({"PU10_SPEED_CMD": {"role": "driver", "range": [0, 1]}})
    for registry in ({}, {"PU10_SPEED_CMD": {"role": "masker", "range": [0, 1]}},
                     {"PU10_SPEED_CMD": {"role": "driver", "range": [0, 2]}}):
        with pytest.raises(ContractError):
            t.validate_points(registry)


@pytest.mark.parametrize("alarm,hazard", [(None, None), (0, None), (0, 2), (2, 2), (2, 1), (None, 1)])
def test_score_agrees_with_trace_and_same_step_is_not_prior(alarm, hazard):
    t = task()
    result(t, alarm, hazard).validate_for(t)


def test_same_step_alarm_cannot_be_reported_as_prior():
    t = task()
    r = result(t, 2, 2)
    wrong = dict(r.score, prehazard_alarm_rule_ids=["A"], prehazard_alarm_rule_ids_count=1)
    with pytest.raises(ContractError):
        replace(r, score=wrong).validate_for(t)


def test_score_origin_excludes_telemetry_before_the_declared_window():
    t = task(score_start_step=1)
    result(t, 0, 2).validate_for(t)
    wrong = result(t, 0, 2)
    with pytest.raises(ContractError, match="score contradicts trace"):
        replace(wrong, score=dict(wrong.score, first_alarm_step=0)).validate_for(t)


def test_partial_trace_and_wrong_time_cannot_report_success():
    t = task()
    r = result(t)
    with pytest.raises(ContractError):
        replace(r, rows=r.rows[:-1]).validate_for(t)
    with pytest.raises(ContractError):
        replace(r, rows=[dict(r.rows[0], time_seconds=0)] + r.rows[1:]).validate_for(t)
    with pytest.raises(ContractError):
        r.validate_for(replace(t, total_steps=4))


def test_failed_run_preserves_partial_trace_without_a_success_score():
    t = task()
    r = replace(result(t), status="error", rows=result(t).rows[:1], score={}, error="backend stopped")
    r.validate_for(t)
    with pytest.raises(ContractError):
        replace(r, error=None)
    with pytest.raises(ContractError):
        replace(r, score={"first_hazard_step": 0})


def test_echo_must_match_actual_task_settings():
    t = task(injections=(spec(),))
    r = result(t)
    with pytest.raises(ContractError):
        replace(r, injection_echo=[]).validate_for(t)
    with pytest.raises(ContractError):
        replace(r, injection_echo=[dict(r.injection_echo[0], slope=-0.03)]).validate_for(t)


def test_nonfinite_trace_cannot_be_serialized_as_success():
    t = task()
    r = result(t)
    with pytest.raises(ContractError):
        replace(r, rows=[dict(r.rows[0], T7_LEVEL=float("nan"))] + r.rows[1:])
