"""Independently verify the saved full driver trace against its closed form."""
import json
from pathlib import Path
import pandas as pd
import yaml
from wacsim.replay.contract import ReplayTask
from wacsim.replay.engine import run_epanet
from wacsim.replay.full import check_full_completion
from wacsim.replay.scenario import RunConfig, create_task, file_sha, implementation_sha


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "phase2_environment/full-95-driver"
    inputs = output / "configuration"
    intermediate = yaml.safe_load((inputs / "intermediate.yaml").read_text())
    check_full_completion(intermediate)
    fixture = json.loads((output / "fixture_manifest.json").read_text())
    assert file_sha(root / "examples/crave_ctown/scenario.yaml") == fixture["real_scenario_sha256_before"]
    task = ReplayTask.from_dict(json.loads((inputs / "task.json").read_text()))
    config = RunConfig(**json.loads((inputs / "run_config.json").read_text()))
    plc = pd.read_csv(output / "PLC5_values.csv")
    ground = pd.read_csv(output / "ground_truth.csv")
    assert len(plc) == 149 and len(ground) == 150
    assert plc["iteration"].tolist() == list(range(149))
    assert ground["hydraulic_time_seconds"].tolist() == list(range(0, 150*300, 300))
    start, duration, slope = 24, 25, -.05
    base = float(plc.iloc[start]["PU10_DRIVER_UNMODIFIED"])
    errors = []
    for step, row in plc.iterrows():
        expected = (row["PU10_PI_CMD"] if step < start else
                    min(1., max(0., base + slope * min(step-start, duration-1))))
        errors.append(abs(row["PU10_SPEED_CMD"]-expected))
        assert abs(row["PU10_ATTACK_APPLIED"]-expected) < 1e-12
        assert abs(row["PU10_DRIVER_UNMODIFIED"]-row["PU10_PI_CMD"]) < 1e-12
        assert row["PU10_ATTACK_FLAG"] == int(step >= start)
        assert row["PU10_ATTACK_HOLDING"] == int(step >= start+duration)
        assert ground.iloc[step+1]["CRAVE_PU10_RAMP"] == int(step >= start)
        if step < start:
            assert pd.isna(row["PU10_ATTACK_BASE"])
        else:
            assert abs(row["PU10_ATTACK_BASE"]-base) < 1e-12
    assert max(errors) < 1e-12
    # Compare zero increment with the captured pre-injection tag value.
    # MiniCPS DB serialization can round the in-memory PI value by a few ulps.
    assert plc.iloc[start]["PU10_SPEED_CMD"] == plc.iloc[start]["PU10_DRIVER_UNMODIFIED"]
    assert (plc.iloc[start+duration:]["PU10_SPEED_CMD"] == 0).all()
    echo = json.loads((output / "PLC5_driver_echo.json").read_text())
    assert echo["base_value"] == base and echo["last_observed_step"] == 148
    assert echo["execution_id"] == intermediate["crave_ctown_runtime"]["execution_id"]
    network = root / "examples/ctown_topology/ctown_map.inp"
    # Retain the actual full inputs. Only fast replay's sampled-control adapter
    # changed after that run; create a separately identified test comparison.
    corrected = root / "phase2_environment/full-95-clock-corrected-inputs"
    corrected.mkdir(exist_ok=True)
    scenario = corrected / "scenario.yaml"
    scenario.write_bytes((inputs / "scenario.yaml").read_bytes())
    lock = corrected / "calibration.lock.yaml"
    locked = yaml.safe_load((inputs / "calibration.lock.yaml").read_text())
    locked["implementation_sha256"] = implementation_sha()
    locked["reference_full_run_key"] = task.run_key
    lock.write_text(yaml.safe_dump(locked))
    corrected_task = create_task(network, scenario, config, 149, task.injections, lock)
    fast = run_epanet(corrected_task, config, network, scenario)
    (root / "phase2_environment/full-95-fast-comparison.json").write_text(json.dumps(
        {"task": corrected_task.to_dict(), "reference_full_task": task.to_dict(),
         "result": fast.to_dict()}, allow_nan=False))
    difference = [abs(fast.rows[k]["PU10_SPEED_CMD"]-plc.iloc[k]["PU10_SPEED_CMD"]) for k in range(149)]
    full_hazard = next((k for k in range(149) if ground.iloc[k+1]["T7_LEVEL"] <= .5), None)
    summary = dict(purpose=fixture["purpose"], completed_steps=149, post_ramp_hold_steps=100,
        attack_start_step=24, last_ramp_step=48, first_hold_step=49,
        base_value=base, first_increment=float(plc.iloc[start]["PU10_SPEED_CMD"]-base), terminal_command=0.,
        max_closed_form_error=max(errors), command_min=float(plc["PU10_SPEED_CMD"].min()),
        command_max=float(plc["PU10_SPEED_CMD"].max()),
        flag_one_steps=int(plc["PU10_ATTACK_FLAG"].sum()),
        full_first_true_T7_low_step=full_hazard,
        full_min_T7_m=float(ground["T7_LEVEL"].min()),
        fast_full_command_max_abs_diff=max(difference),
        PI_to_DB_serialization_max_abs_diff=float(abs(plc["PU10_DRIVER_UNMODIFIED"]-plc["PU10_PI_CMD"]).max()),
        fast_full_T7_max_abs_diff_m=max(abs(fast.rows[k]["T7_LEVEL"]-ground.iloc[k+1]["T7_LEVEL"]) for k in range(149)),
        full_alarm_status="pending_step_9.7_no_ReachAvoid_claim",
        synthetic_theta_m=config.theta_m,
        reference_full_implementation_sha256=task.implementation_sha256,
        fast_implementation_sha256=corrected_task.implementation_sha256,
        comparison_note="clock_fix_comparison_against_archived_full_run_not_final_frozen_version_acceptance",
        nonempty_pcaps=len([p for p in output.glob("*.pcap") if p.stat().st_size > 24]))
    (root / "phase2_environment/full-95-check.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
