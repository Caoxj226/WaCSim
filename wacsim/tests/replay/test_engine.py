"""Deterministic control, attack, oracle and real EPANET integration checks."""
from pathlib import Path
import copy
import pytest
from wacsim.replay.contract import ContractError, InjectionSpec, ReplayTask
from wacsim.replay.control import PIController, PIParameters
from wacsim.replay.engine import run_epanet
from wacsim.replay.injections import Ramp
from wacsim.replay.oracle import OracleError, evaluate, rules_from_scenario, score_rows
from wacsim.replay.scenario import RunConfig, file_sha, implementation_sha, validate_task


ROOT = Path(__file__).resolve().parents[3]
INP = ROOT / "examples/ctown_topology/ctown_map.inp"
SCENARIO = ROOT / "examples/crave_ctown/scenario.yaml"


def task(config, *specs, steps=129):
    return ReplayTask(
        scenario_id="crave_ctown_t7_pu10", total_steps=steps, step_seconds=300,
        scenario_sha256=file_sha(SCENARIO), network_sha256=file_sha(INP),
        implementation_sha256=implementation_sha(),
        configuration_sha256=config.sha256(), score_start_step=24,
        mode="replay", injections=specs,
    )


def test_pi_conditional_integral_and_output_limits():
    pi = PIController(PIParameters(2.5, 0.4, 0.04))
    raw, command = pi.step(2.0, 2.5)
    assert pi.integral == 0.5
    assert raw == pytest.approx(0.22) and command == pytest.approx(0.22)
    raw, command = pi.step(0.0, 5.0)
    assert pi.integral == 0.5  # positive error would wind up while upper saturated
    assert raw > 1 and command == 1
    pi.step(2.51, 2.5)
    assert pi.integral == pytest.approx(.49)  # small negative error unwinds
    raw, command = pi.step(4.0, 2.5)
    assert pi.integral == pytest.approx(.49)  # lower saturation freezes further integration
    assert raw < 0 and command == 0


def test_ramp_capture_first_zero_hold_and_clamp():
    spec = InjectionSpec("PU10_SPEED_CMD", "driver", -.1, 3, 24, (0, 1))
    ramp = Ramp(spec)
    assert ramp.value(23, .9) == .9
    assert ramp.value(24, .8) == .8
    assert ramp.value(25, .2) == pytest.approx(.7)
    assert ramp.value(26, .2) == pytest.approx(.6)
    assert ramp.value(40, .2) == pytest.approx(.6)
    assert ramp.echo()["base_value"] == .8
    low = Ramp(InjectionSpec("T7_LEVEL_SP", "masker", -3, 4, 24, (0, 5)))
    assert low.value(24, 2.5) == 2.5 and low.value(25, 2.5) == 0
    assert low.value(30, 2.5) == 0


def test_alarm_hazard_and_same_step_ordering():
    both = evaluate(.5, 1.0, .1)
    assert both["alarm_rule_ids"] == ["A-T7-TRACK"]
    assert both["hazard_rule_ids"] == ["H-T7-LOW"]
    assert both["alarm_margin_m"] == pytest.approx(-.4)
    assert both["hazard_margin_m"] == 0
    score = score_rows([dict(t_step=0, alarm_rule_ids=[], hazard_rule_ids=[]),
                        dict(t_step=1, **{"alarm_rule_ids": both["alarm_rule_ids"],
                                               "hazard_rule_ids": both["hazard_rule_ids"]})])
    assert score["first_alarm_step"] == score["first_hazard_step"] == 1
    assert score["prehazard_alarm_rule_ids"] == []
    assert evaluate(.9, 1.0, .1)["alarm_rule_ids"] == []  # equality is safe


def test_score_retains_warmup_telemetry_but_excludes_it_from_crave_score():
    rows = [dict(t_step=8, alarm_rule_ids=["A-T7-TRACK"], hazard_rule_ids=[]),
            dict(t_step=24, alarm_rule_ids=[], hazard_rule_ids=[]),
            dict(t_step=25, alarm_rule_ids=["A-T7-TRACK"],
                 hazard_rule_ids=["H-T7-LOW"])]
    score = score_rows(rows, score_start_step=24)
    assert score == {"score_start_step": 24, "first_alarm_step": 25,
                     "first_hazard_step": 25,
                     "prehazard_alarm_rule_ids": [],
                     "prehazard_alarm_rule_ids_count": 0}


def test_oracle_rules_are_read_from_the_semantic_scenario_contract():
    import yaml
    declaration = yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))
    rules = rules_from_scenario(declaration, .1)
    assert (rules.alarm_rule_id, rules.hazard_rule_id) == ("A-T7-TRACK", "H-T7-LOW")
    assert (rules.process_variable, rules.setpoint_variable, rules.hazard_threshold_m) == (
        "T7_LEVEL", "T7_LEVEL_SP", .5)
    invalid = copy.deepcopy(declaration)
    invalid["alarms"][0]["process_var"] = "T7"
    with pytest.raises(OracleError, match="semantic T7"):
        rules_from_scenario(invalid, .1)


def test_formal_attack_rejected_without_reviewed_lock():
    config = RunConfig(2.5, .4, .01, .1)
    driver = InjectionSpec("PU10_SPEED_CMD", "driver", -.01, 5, 24, (0, 1))
    with pytest.raises(ContractError, match="calibration lock"):
        run_epanet(task(config, driver), config, INP, SCENARIO)


@pytest.mark.parametrize("spec,steps", [
    (InjectionSpec("PU10_SPEED_CMD", "driver", -.06, 5, 24, (0, 1)), 129),
    (InjectionSpec("PU10_SPEED_CMD", "driver", -.01, 5, 25, (0, 1)), 130),
    (InjectionSpec("PU10_SPEED_CMD", "driver", -.01, 4, 24, (0, 1)), 128),
    (InjectionSpec("T7_LEVEL_SP", "masker", -.01, 5, 24, (0, 5)), 129),
])
def test_scenario_rejects_out_of_contract_attack(spec, steps):
    config = RunConfig(2.5, .4, .01, .1)
    with pytest.raises(ContractError):
        validate_task(task(config, spec, steps=steps), config, INP, SCENARIO,
                      require_lock=False)


def test_single_and_dual_attack_step_order_with_real_epanet():
    config = RunConfig(2.5, .4, .01, .1)
    driver = InjectionSpec("PU10_SPEED_CMD", "driver", -.01, 5, 24, (0, 1))
    masker = InjectionSpec("T7_LEVEL_SP", "masker", -.02, 5, 24, (0, 5))
    single_task = task(config, driver)
    dual_task = task(config, driver, masker)
    single = run_epanet(single_task, config, INP, SCENARIO, allow_unreviewed=True)
    dual = run_epanet(dual_task, config, INP, SCENARIO, allow_unreviewed=True)
    single.validate_for(single_task)
    dual.validate_for(dual_task)
    assert len(single.rows) == len(dual.rows) == 129
    assert single.rows[24]["PU10_SPEED_CMD"] == pytest.approx(single.rows[24]["PU10_PI_CMD"])
    assert single.rows[25]["PU10_SPEED_CMD"] == pytest.approx(
        max(0, single.rows[24]["PU10_PI_CMD"] - .01))
    assert single.rows[29]["PU10_SPEED_CMD"] == pytest.approx(single.rows[28]["PU10_SPEED_CMD"])
    assert dual.rows[24]["T7_LEVEL_SP"] == 2.5
    assert dual.rows[25]["T7_LEVEL_SP"] == pytest.approx(2.48)
    assert dual.rows[29]["T7_LEVEL_SP"] == pytest.approx(2.42)
    assert dual.rows[25]["PU10_PI_CMD"] != pytest.approx(single.rows[25]["PU10_PI_CMD"])
    assert single.injection_echo[0]["base_value"] == pytest.approx(single.rows[24]["PU10_PI_CMD"])
    assert {echo["point"] for echo in dual.injection_echo} == {"PU10_SPEED_CMD", "T7_LEVEL_SP"}
    repeated = run_epanet(single_task, config, INP, SCENARIO, allow_unreviewed=True)
    assert repeated.rows == single.rows
    assert repeated.score == single.score
    assert repeated.run_key == single.run_key
    assert all(row["first_alarm_step"] == single.score["first_alarm_step"]
               and row["first_hazard_step"] == single.score["first_hazard_step"]
               and row["prehazard_alarm_rule_ids"] == single.score["prehazard_alarm_rule_ids"]
               for row in single.rows)
