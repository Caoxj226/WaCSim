"""Internal integration fixture, never a research calibration or attack result.

Creates a synthetic lock in a container temporary directory to exercise the
normal lock-gated driver path. The real research scenario and lock are untouched.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml
from wacsim.replay.scenario import file_sha, implementation_sha


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "examples/crave_ctown/scenario.yaml"
    source_sha = file_sha(source)
    network = root / "examples/ctown_topology/ctown_map.inp"
    output = root / "phase2_environment/full-95-driver"
    if output.exists():
        raise RuntimeError("fixture output already exists; use a new run directory")
    output.mkdir(parents=True)
    purpose = "synthetic_internal_fixture_not_calibration_or_RQ_result"
    with tempfile.TemporaryDirectory(prefix="ctown-device-fixture-") as temporary:
        fixture = Path(temporary)
        scenario = fixture / "scenario.yaml"
        declaration = yaml.safe_load(source.read_text())
        declaration.update(status="locked", test_fixture_only=purpose)
        declaration["source"]["network_inp"] = str(network)
        scenario.write_text(yaml.safe_dump(declaration))
        lock = fixture / "calibration.lock.yaml"
        lock.write_text(yaml.safe_dump(dict(purpose=purpose,
            nominal_setpoint_m=2.5, Kp=.2, Ki_per_step=.04, theta_m=.1,
            theta_source="synthetic_gate_value_full_oracle_not_implemented",
            scenario_sha256=file_sha(scenario), network_sha256=file_sha(network),
            implementation_sha256=implementation_sha())))
        configuration = dict(
            inp_file=str(network), iterations=149, output_path=str(output),
            plcs=yaml.safe_load((root / "examples/ctown_topology/ctown_plcs.yaml").read_text()),
            network_topology_type="complex", mode="plccontrol", demand="pdd",
            log_level="info", saving_interval=1,
            crave_ctown=dict(profile="driver", scenario=str(scenario), calibration_lock=str(lock),
                injections=[dict(point="PU10_SPEED_CMD", role="driver", slope=-.05,
                                 duration=25, start_step=24, range=[0., 1.])]))
        config = fixture / "full.yaml"
        config.write_text(yaml.safe_dump(configuration))
        (output / "fixture_manifest.json").write_text(json.dumps(dict(
            purpose=purpose, real_scenario_sha256_before=source_sha,
            total_steps=149, start_step=24, duration=25, slope=-.05), indent=2))
        with (output / "run.log").open("w") as log:
            completed = subprocess.run([sys.executable, "-m", "wacsim.command_line", str(config)],
                                       stdout=log, stderr=subprocess.STDOUT)
        if completed.returncode:
            raise RuntimeError(f"full fixture failed: exit {completed.returncode}; see run.log")
        if file_sha(source) != source_sha:
            raise RuntimeError("research scenario was changed during the fixture")
        if (source.parent / "calibration.lock.yaml").exists():
            raise RuntimeError("unexpected research calibration lock")
    print(f"Internal fixture completed: {output}")


if __name__ == "__main__":
    main()
