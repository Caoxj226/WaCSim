"""Device ramp timing, controller precedence, trace and lock-gated wiring."""
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import pandas as pd
import pytest
import yaml

from wacsim.init_database import DatabaseInitializer
from wacsim.parser.config_parser import ConfigParser
from wacsim.replay.contract import ContractError, InjectionSpec
from wacsim.replay.device import DEVICE_ATTACK_NAME, FullDriverRamp
from wacsim.replay.full import FullPIControl
from wacsim.replay.scenario import file_sha, implementation_sha

ROOT = Path(__file__).resolve().parents[3]
SCENE = ROOT / "examples/crave_ctown"


def fake_plc(steps):
    plc = SimpleNamespace(clock=0, value=.8, writes=[], flags=[],
                          write_cache=pd.DataFrame(), cache={"T7": 2.0, "T7SP": 2.5},
                          tag_fresh=pd.DataFrame(True, index=range(steps), columns=["T7", "T7SP"]))
    plc.get_master_clock = lambda: plc.clock
    plc.get_tag = lambda tag: plc.value
    def set_tag(tag, value):
        assert tag == "PU10"
        plc.value = value
        plc.writes.append(value)
    plc.set_tag = set_tag
    plc.set_attack_flag = lambda flag, name: plc.flags.append((name, flag))
    return plc


def test_ramp_captures_post_pi_once_and_holds(tmp_path):
    driver = FullDriverRamp(InjectionSpec("PU10_SPEED_CMD", "driver", -.2, 3, 2, (0, 1)))
    plc = fake_plc(7)
    for step, value in enumerate([.1, .3, .8, 1.0, .9, .1, .7]):
        plc.clock, plc.value = step, value
        driver.apply(plc)
    assert plc.writes == pytest.approx([.8, .6, .4, .4, .4])
    assert driver.ramp.base_value == .8
    assert plc.write_cache["PU10_ATTACK_FLAG"].tolist() == [0, 0, 1, 1, 1, 1, 1]
    assert plc.write_cache["PU10_ATTACK_HOLDING"].tolist() == [0, 0, 0, 0, 0, 1, 1]
    assert pd.isna(plc.write_cache.loc[0, "PU10_ATTACK_BASE"])
    assert all(name == DEVICE_ATTACK_NAME for name, _ in plc.flags)
    driver.save_echo(tmp_path, dict(execution_id="test", run_key="rk_test"))
    echo = json.loads((tmp_path / "PLC5_driver_echo.json").read_text())
    assert echo["base_value"] == .8 and echo["last_observed_step"] == 6


@pytest.mark.parametrize("slope,terminal", [(-1., 0.), (1., 1.)])
def test_ramp_clamps_both_limits_and_holds(slope, terminal):
    driver = FullDriverRamp(InjectionSpec("PU10_SPEED_CMD", "driver", slope, 2, 0, (0, 1)))
    plc = fake_plc(4)
    for step in range(4):
        plc.clock, plc.value = step, .6
        driver.apply(plc)
    assert plc.writes == [.6, terminal, terminal, terminal]


def test_duplicate_and_skipped_master_clock_fail():
    driver = FullDriverRamp(InjectionSpec("PU10_SPEED_CMD", "driver", -.01, 2, 0, (0, 1)))
    plc = fake_plc(4)
    driver.apply(plc)
    with pytest.raises(ContractError, match="once per master-clock"):
        driver.apply(plc)
    plc.clock = 2
    with pytest.raises(ContractError, match="once per master-clock"):
        driver.apply(plc)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_ramp_rejects_invalid_upstream_command(value):
    driver = FullDriverRamp(InjectionSpec("PU10_SPEED_CMD", "driver", -.01, 2, 0, (0, 1)))
    plc = fake_plc(1)
    plc.value = value
    with pytest.raises(ContractError, match="finite, clamped"):
        driver.apply(plc)
    assert not plc.writes


def test_pi_keeps_computing_but_final_command_is_driver_override():
    pi = FullPIControl(dict(sp_m=2.5, kp=.2, ki_per_step=.04))
    driver = FullDriverRamp(InjectionSpec("PU10_SPEED_CMD", "driver", -.05, 2, 1, (0, 1)))
    plc = fake_plc(4)
    for step in range(4):
        plc.clock = step
        plc.cache["T7"] = 2.0 - .1 * step
        pi.apply(plc, step)
        driver.apply(plc)
    base = plc.write_cache.loc[1, "PU10_PI_CMD"]
    assert driver.ramp.base_value == base
    assert plc.write_cache.loc[3, "PU10_SPEED_CMD"] == pytest.approx(base - .05)
    assert plc.write_cache.loc[3, "PU10_PI_CMD"] != plc.write_cache.loc[3, "PU10_SPEED_CMD"]


def test_locked_driver_wiring_and_sqlite_flag(tmp_path):
    # Synthetic lock only in pytest's private fixture directory.
    network = ROOT / "examples/ctown_topology/ctown_map.inp"
    scenario = tmp_path / "scenario.yaml"
    declaration = yaml.safe_load((SCENE / "scenario.yaml").read_text())
    declaration["status"] = "locked"
    declaration["source"]["network_inp"] = str(network)
    scenario.write_text(yaml.safe_dump(declaration))
    lock = tmp_path / "calibration.lock.yaml"
    lock.write_text(yaml.safe_dump(dict(nominal_setpoint_m=2.5, Kp=.2, Ki_per_step=.04,
        theta_m=.1, scenario_sha256=file_sha(scenario), network_sha256=file_sha(network),
        implementation_sha256=implementation_sha(), purpose="synthetic_test_fixture")))
    parser = ConfigParser(SCENE / "full_driver.yaml")
    parser.data["output_path"] = tmp_path / "out"
    parser.data["crave_ctown"].update(scenario=str(scenario), calibration_lock=str(lock))
    path = parser.generate_intermediate_yaml()
    data = yaml.safe_load(path.read_text())
    assert data["crave_device_attacks"][0]["name"] == DEVICE_ATTACK_NAME
    DatabaseInitializer(path).write()
    with sqlite3.connect(data["db_path"]) as connection:
        assert connection.execute("SELECT flag FROM attack WHERE name=?", (DEVICE_ATTACK_NAME,)).fetchone() == (0,)
    dual = ConfigParser(SCENE / "full_driver_masker.yaml")
    dual.data["output_path"] = tmp_path / "dual_out"
    dual.data["crave_ctown"].update(scenario=str(scenario), calibration_lock=str(lock))
    dual_data = yaml.safe_load(dual.generate_intermediate_yaml().read_text())
    network_attack = dual_data["network_attacks"][-1]
    assert network_attack["name"] == "CRAVEMASK"
    assert network_attack["crave_masker"]["spec"]["role"] == "masker"


def test_device_adapter_rejects_masker_point():
    with pytest.raises(ContractError, match="PU10 actuator"):
        FullDriverRamp(InjectionSpec("T7_LEVEL_SP", "masker", -.01, 5, 24, (0, 5)))
