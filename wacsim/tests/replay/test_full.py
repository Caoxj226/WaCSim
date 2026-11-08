"""Full CTown configuration, actual SQLite initialization and shared PI tests."""
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import pandas as pd
import pytest
import yaml

from wacsim.init_database import DatabaseInitializer
from wacsim.parser.config_parser import ConfigParser
from wacsim.replay.contract import ContractError, ReplayTask
from wacsim.replay.control import PIController, apply_pump_speed
from wacsim.replay.full import FullPIControl, check_full_completion, evaluate_full_oracle
from wacsim.replay.scenario import RunConfig, file_sha, implementation_sha, validate_task

ROOT = Path(__file__).resolve().parents[3]
SCENE = ROOT / "examples/crave_ctown"


def intermediate(tmp_path, name="full_calibration_smoke.yaml"):
    parser = ConfigParser(SCENE / name)
    parser.data["output_path"] = tmp_path
    path = parser.generate_intermediate_yaml()
    return path, yaml.safe_load(path.read_text())


def test_full_configuration_and_real_database(tmp_path):
    path, data = intermediate(tmp_path)
    plcs = {p["name"]: p for p in data["plcs"]}
    assert plcs["PLC9"]["sensors"].count("T7SP") == 1
    assert set(("T7", "T7SP")) <= set(plcs["PLC5"]["dependent_sensors"])
    assert all(c["actuator"] != "PU10" for c in plcs["PLC5"]["controls"])
    pu11 = [c for c in plcs["PLC5"]["controls"] if c["actuator"] == "PU11"]
    assert {(c["type"], c["value"], c["action"]) for c in pu11} == {
        ("below", .5, "open"), ("above", 3.0, "closed")}
    initializer = DatabaseInitializer(path)
    initializer.write()
    with sqlite3.connect(data["db_path"]) as conn:
        values = dict(conn.execute("SELECT name,value FROM plant"))
    assert float(values["T7"]) == float(values["T7SP"]) == 2.5
    assert len(data["initial_tank_values"]) == 7
    assert data["crave_ctown_runtime"]["task"]["mode"] == "calibration"


@pytest.mark.parametrize("name", ["full_no_attack.yaml", "full_driver.yaml", "full_driver_masker.yaml"])
def test_formal_profiles_reject_missing_shared_lock(tmp_path, name):
    # The repository can legitimately contain a formal lock after calibration.
    # Point this negative test at an isolated scenario whose configured lock is absent.
    scenario = tmp_path / "scenario.yaml"
    declaration = yaml.safe_load((SCENE / "scenario.yaml").read_text())
    declaration["source"]["network_inp"] = str(ROOT / "examples/ctown_topology/ctown_map.inp")
    scenario.write_text(yaml.safe_dump(declaration))
    parser = ConfigParser(SCENE / name)
    parser.data["output_path"] = tmp_path / "outputs"
    parser.data["crave_ctown"]["scenario"] = str(scenario)
    parser.data["crave_ctown"]["calibration_lock"] = str(tmp_path / "missing.lock.yaml")
    with pytest.raises(ContractError, match="same calibration lock"):
        parser.generate_intermediate_yaml()


def test_full_and_fast_accept_the_same_test_lock_and_reject_tampering(tmp_path):
    # Synthetic reviewed/locked input lives only in pytest's temporary folder.
    # It tests the gate; it is not a calibration result for the research scene.
    scenario = tmp_path / "scenario.yaml"
    declaration = yaml.safe_load((SCENE / "scenario.yaml").read_text())
    network = ROOT / "examples/ctown_topology/ctown_map.inp"
    declaration["status"] = "locked"
    declaration["source"]["network_inp"] = str(network)
    scenario.write_text(yaml.safe_dump(declaration))
    lock = tmp_path / "calibration.lock.yaml"
    lock.write_text(yaml.safe_dump(dict(
        nominal_setpoint_m=2.5, Kp=.2, Ki_per_step=.04, theta_m=.1,
        scenario_sha256=file_sha(scenario), network_sha256=file_sha(network),
        implementation_sha256=implementation_sha())))
    parser = ConfigParser(SCENE / "full_no_attack.yaml")
    parser.data["output_path"] = tmp_path / "outputs"
    parser.data["crave_ctown"]["scenario"] = str(scenario)
    parser.data["crave_ctown"]["calibration_lock"] = str(lock)
    path = parser.generate_intermediate_yaml()
    data = yaml.safe_load(path.read_text())
    runtime = data["crave_ctown_runtime"]
    task = ReplayTask.from_dict(runtime["task"])
    config = RunConfig(**runtime["config"])
    validate_task(task, config, network, scenario)  # fast replay's exact gate
    assert task.configuration_sha256 == file_sha(lock)
    locked = yaml.safe_load(lock.read_text())
    locked["Kp"] = .4
    lock.write_text(yaml.safe_dump(locked))
    with pytest.raises(ContractError, match="fingerprint mismatch"):
        validate_task(task, config, network, scenario)


def test_full_driver_masker_generates_a_separate_audited_network_attack(tmp_path):
    scenario = tmp_path / "scenario.yaml"
    declaration = yaml.safe_load((SCENE / "scenario.yaml").read_text())
    network = ROOT / "examples/ctown_topology/ctown_map.inp"
    declaration.update(status="locked")
    declaration["source"]["network_inp"] = str(network)
    scenario.write_text(yaml.safe_dump(declaration))
    lock = tmp_path / "calibration.lock.yaml"
    lock.write_text(yaml.safe_dump(dict(nominal_setpoint_m=2.5, Kp=.2, Ki_per_step=.04,
        theta_m=.1, scenario_sha256=file_sha(scenario), network_sha256=file_sha(network),
        implementation_sha256=implementation_sha())))
    parser = ConfigParser(SCENE / "full_driver_masker.yaml")
    parser.data["output_path"] = tmp_path / "outputs"
    parser.data["crave_ctown"]["scenario"] = str(scenario)
    parser.data["crave_ctown"]["calibration_lock"] = str(lock)
    data = yaml.safe_load(parser.generate_intermediate_yaml().read_text())
    attack = data["network_attacks"][-1]
    assert attack["type"] == "seq_mitm" and attack["tags"] == [{"tag": "T7SP", "offset": 0., "scaleParam": -.01, "scaleTime": 1}]
    assert attack["target"] == "PLC5" and attack["server"] == "PLC9"
    assert attack["crave_masker"]["spec"]["point"] == "T7_LEVEL_SP"
    assert attack["crave_masker"]["execution_id"] == data["crave_ctown_runtime"]["execution_id"]


def test_full_pi_calls_shared_formula_and_records_output():
    config = RunConfig(2.5, .2, .04)
    adapter, reference = FullPIControl(config.__dict__), PIController(config)
    commands = []
    plc = SimpleNamespace(cache={"T7": 2.5, "T7SP": 2.5},
                          tag_fresh=pd.DataFrame(True, index=range(4), columns=["T7", "T7SP"]),
                          write_cache=pd.DataFrame(), set_tag=lambda tag, value: commands.append((tag, value)))
    for step, measured in enumerate([2.5, 2.0, 1.5, 2.8]):
        plc.cache["T7"] = measured
        raw, command = reference.step(measured, 2.5)
        adapter.apply(plc, step)
        assert commands[-1] == ("PU10", command)
        assert plc.write_cache.loc[step, "PU10_PI_RAW"] == raw
        assert plc.write_cache.loc[step, "PI_INTEGRAL"] == reference.integral
    with pytest.raises(ContractError, match="exactly once"):
        adapter.apply(plc, 3)


def test_full_pi_rejects_missing_network_read():
    adapter = FullPIControl(dict(sp_m=2.5, kp=.2, ki_per_step=.04))
    plc = SimpleNamespace(cache={"T7": 0.0, "T7SP": 0.0},
                          tag_fresh=pd.DataFrame(False, index=[0], columns=["T7", "T7SP"]))
    with pytest.raises(ContractError, match="fresh PLC9"):
        adapter.apply(plc, 0)


def test_speed_write_order_preserves_fractional_setting():
    writes = []
    class Toolkit:
        STATUS = 1
        SETTING = 2

        @staticmethod
        def setlinkvalue(**kwargs):
            writes.append((kwargs["property"], kwargs["value"]))
    apply_pump_speed(Toolkit, None, 1, .63)
    assert writes == [(1, 1.0), (2, .63)]
    apply_pump_speed(Toolkit, None, 1, 0.0)
    assert writes[-1] == (1, 0.0)


def test_completion_check_rejects_missing_and_stale_markers(tmp_path):
    import json
    data = dict(output_path=str(tmp_path), iterations=24,
                crave_ctown_runtime=dict(execution_id="new", run_key="rk_test"))
    with pytest.raises(RuntimeError, match="did not complete"):
        check_full_completion(data)
    marker = tmp_path / "full_run_complete.json"
    marker.write_text(json.dumps(dict(execution_id="old", run_key="rk_test", completed_steps=24)))
    with pytest.raises(RuntimeError, match="stale"):
        check_full_completion(data)
    marker.write_text(json.dumps(dict(execution_id="new", run_key="rk_test", completed_steps=24)))
    check_full_completion(data)


def test_full_oracle_aligns_effective_setpoint_with_post_hydraulic_truth(tmp_path):
    # PLC5 k is the control input for the k -> k+1 hydraulic transition.
    # The row at k=1 deliberately has both alarm and hazard: it is not a
    # pre-hazard alarm under CRAVE's strict-before-hazard tie rule.
    # A warmup alarm at step 8 remains in telemetry but must not enter the
    # CRAVE score.  The step-25 alarm/hazard tie is the first scored event.
    levels = [2.5] * 25 + [.6, .5, .4]
    setpoints = [2.5] * 27
    setpoints[8], setpoints[24], setpoints[25], setpoints[26] = 1.0, .6, 1.0, 1.0
    pd.DataFrame({"iteration": range(28), "T7_LEVEL": levels}).to_csv(
        tmp_path / "ground_truth.csv", index=False)
    pd.DataFrame({"iteration": range(27), "T7_LEVEL_SP": setpoints,
                  "T7_MEASURED": levels[:-1]}).to_csv(tmp_path / "PLC5_values.csv", index=False)
    data = {
        "output_path": str(tmp_path), "iterations": 27,
        "crave_ctown_runtime": {
            "scenario_path": str(SCENE / "scenario.yaml"), "run_key": "rk_test",
            "task": {"score_start_step": 24},
            "config": {"sp_m": 2.5, "kp": .2, "ki_per_step": .04, "theta_m": .1},
        },
    }
    summary = evaluate_full_oracle(data)
    rows = [json.loads(line) for line in (tmp_path / "oracle_trace.jsonl").read_text().splitlines()]
    assert rows[0]["source_PLC5_iteration"] == 0
    assert rows[0]["source_ground_truth_iteration"] == 1
    assert rows[8]["alarm_rule_ids"] == ["A-T7-TRACK"]
    assert rows[25]["alarm_rule_ids"] == ["A-T7-TRACK"]
    assert rows[25]["hazard_rule_ids"] == ["H-T7-LOW"]
    assert rows[0]["first_alarm_step"] == rows[26]["first_alarm_step"] == 25
    assert rows[0]["first_hazard_step"] == rows[26]["first_hazard_step"] == 25
    assert rows[0]["prehazard_alarm_rule_ids"] == []
    assert summary["score"] == {"score_start_step": 24,
                                 "first_alarm_step": 25, "first_hazard_step": 25,
                                 "prehazard_alarm_rule_ids": [],
                                 "prehazard_alarm_rule_ids_count": 0}
    assert json.loads((tmp_path / "oracle_summary.json").read_text()) == summary


def test_full_oracle_rejects_clock_or_semantic_column_mismatch(tmp_path):
    pd.DataFrame({"iteration": [0, 1], "T7_LEVEL": [2.5, 2.4]}).to_csv(
        tmp_path / "ground_truth.csv", index=False)
    pd.DataFrame({"iteration": [0], "T7_MEASURED": [2.5]}).to_csv(
        tmp_path / "PLC5_values.csv", index=False)
    data = {
        "output_path": str(tmp_path), "iterations": 1,
        "crave_ctown_runtime": {
            "scenario_path": str(SCENE / "scenario.yaml"), "run_key": "rk_test",
            "task": {"score_start_step": 24},
            "config": {"sp_m": 2.5, "kp": .2, "ki_per_step": .04, "theta_m": .1},
        },
    }
    with pytest.raises(RuntimeError, match="semantic T7"):
        evaluate_full_oracle(data)


def test_stock_ctown_configuration_keeps_original_rules_and_database(tmp_path):
    parser = ConfigParser(SCENE / "full_calibration_smoke.yaml")
    del parser.data["crave_ctown"]
    parser.data["output_path"] = tmp_path
    path = parser.generate_intermediate_yaml()
    data = yaml.safe_load(path.read_text())
    assert "crave_ctown_runtime" not in data and "virtual_tag_initial_values" not in data
    plc5 = next(p for p in data["plcs"] if p["name"] == "PLC5")
    assert sum(c["actuator"] == "PU10" for c in plc5["controls"]) == 2
    assert all("T7SP" not in p["sensors"] for p in data["plcs"])
    DatabaseInitializer(path).write()
    with sqlite3.connect(data["db_path"]) as conn:
        assert float(conn.execute("SELECT value FROM plant WHERE name='T7'").fetchone()[0]) == 0
