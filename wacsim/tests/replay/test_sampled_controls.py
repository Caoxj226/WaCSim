"""A PLC backup pump reacts at a master-clock sample, not mid-transition."""
from pathlib import Path
import yaml
from wacsim.replay.engine import prepared_inp, run_epanet
from wacsim.replay.legacy import SampledControls
from wacsim.replay.contract import InjectionSpec, ReplayTask
from wacsim.replay.scenario import RunConfig, file_sha, implementation_sha

ROOT = Path(__file__).resolve().parents[3]
INP = ROOT / "examples/ctown_topology/ctown_map.inp"
SCENE = ROOT / "examples/crave_ctown/scenario.yaml"


def test_sampled_backup_pump_threshold_and_hysteresis(tmp_path):
    inventory = yaml.safe_load(INP.with_name("ctown_plcs.yaml").read_text())
    sampled = SampledControls(INP.read_text(), inventory, dict(PU11=0.))
    # Exercise PU11 alone using the exact rule objects retained from full WaCSim.
    sampled.controls = [(c, remote) for c, remote in sampled.controls if c.actuator == "PU11"]
    assert sampled.apply(0, {"T7": .51})["PU11"] == 0
    assert sampled.apply(1, {"T7": .45})["PU11"] == 1
    assert sampled.apply(2, {"T7": 2.9})["PU11"] == 1
    assert sampled.apply(3, {"T7": 3.1})["PU11"] == 0
    prepared = tmp_path / "sampled.inp"
    prepared_inp(INP, prepared)
    controls = prepared.read_text().split("[CONTROLS]", 1)[1].split("[", 1)[0]
    assert "LINK" not in controls  # original rules execute in the shared PLC classes


def test_fast_driver_can_cross_hazard_before_sampled_PU11_reacts():
    config = RunConfig(2.5, .2, .04, .1)
    spec = InjectionSpec("PU10_SPEED_CMD", "driver", -.05, 25, 24, (0, 1))
    task = ReplayTask(scenario_id="crave_ctown_t7_pu10", total_steps=149, step_seconds=300,
        scenario_sha256=file_sha(SCENE), network_sha256=file_sha(INP),
        implementation_sha256=implementation_sha(), configuration_sha256=config.sha256(),
        score_start_step=24, mode="replay", injections=(spec,))
    result = run_epanet(task, config, INP, SCENE, allow_unreviewed=True)
    assert result.score["first_hazard_step"] == 49
    assert result.rows[49]["T7_LEVEL"] < .5 and result.rows[49]["PU11_STATUS"] == 0
    assert result.rows[50]["PU11_STATUS"] == 1
