"""Audit full no-attack PLC/SCADA outputs and compare with fast EPANET replay."""
import json
from pathlib import Path
import pandas as pd
from wacsim.replay.control import PIController
from wacsim.replay.engine import run_epanet
from wacsim.replay.scenario import RunConfig, create_task
from wacsim.replay.full import check_full_completion


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "phase2_environment/full-94-smoke"
    import yaml
    intermediate = yaml.safe_load((output / "configuration/intermediate.yaml").read_text())
    check_full_completion(intermediate)
    ground = pd.read_csv(output / "ground_truth.csv")
    plc5 = pd.read_csv(output / "PLC5_values.csv")
    plc9 = pd.read_csv(output / "PLC9_values.csv")
    steps = len(plc5)
    assert steps == 24 and len(plc9) == steps and len(ground) == steps + 1
    assert ground["hydraulic_time_seconds"].tolist() == list(range(0, 300 * (steps + 1), 300))
    assert plc5["iteration"].tolist() == list(range(steps))
    assert (plc5["T7_LEVEL_SP"] == 2.5).all() and (plc9["T7SP"] == 2.5).all()
    config = RunConfig(2.5, .2, .04)
    pi = PIController(config)
    formula_errors = []
    for _, row in plc5.iterrows():
        raw, command = pi.step(row["T7_MEASURED"], row["T7_LEVEL_SP"])
        formula_errors.extend([abs(raw - row["PU10_PI_RAW"]),
                               abs(command - row["PU10_SPEED_CMD"])])
    network = root / "examples/ctown_topology/ctown_map.inp"
    scenario = root / "examples/crave_ctown/scenario.yaml"
    task = create_task(network, scenario, config, steps)
    fast = run_epanet(task, config, network, scenario)
    (root / "phase2_environment/full-94-fast-comparison.json").write_text(
        json.dumps({"task": task.to_dict(), "result": fast.to_dict()}, allow_nan=False))
    checks = {
        "full_ground_rows_including_initial": len(ground), "PLC5_rows": steps,
        "PLC9_rows": len(plc9), "hydraulic_step_seconds": 300,
        "initial_T7_m": float(ground.iloc[0]["T7_LEVEL"]),
        "initial_PLC5_T7_m": float(plc5.iloc[0]["T7_MEASURED"]),
        "nominal_SP_m": 2.5, "candidate_Kp": .2, "candidate_Ki_per_step": .04,
        "full_final_T7_m": float(ground.iloc[-1]["T7_LEVEL"]),
        "full_min_T7_m": float(ground["T7_LEVEL"].min()),
        "full_PU10_command_min": float(plc5["PU10_SPEED_CMD"].min()),
        "full_PU10_command_max": float(plc5["PU10_SPEED_CMD"].max()),
        "full_PI_formula_max_error": max(formula_errors),
        "network_measurement_max_error_m": max(abs(plc5.iloc[k]["T7_MEASURED"] - ground.iloc[k]["T7_LEVEL"]) for k in range(steps)),
        "fast_full_T7_max_abs_diff_m": max(abs(fast.rows[k]["T7_LEVEL"] - ground.iloc[k+1]["T7_LEVEL"]) for k in range(steps)),
        "fast_full_PU10_command_max_abs_diff": max(abs(fast.rows[k]["PU10_SPEED_CMD"] - plc5.iloc[k]["PU10_SPEED_CMD"]) for k in range(steps)),
        "fast_full_PU10_flow_max_abs_diff_m3h": max(abs(fast.rows[k]["PU10_FLOW"] - ground.iloc[k+1]["PU10_FLOW"]) for k in range(steps)),
        "nonempty_pcaps": len([p for p in output.glob("*.pcap") if p.stat().st_size > 24]),
        "alarm_status": "pending_full_oracle_step_9.7",
        "calibration_status": "exploratory_candidate_not_locked",
        "differential_status": "short_no_attack_diagnostic_not_full_acceptance",
    }
    (root / "phase2_environment/full-94-check.json").write_text(json.dumps(checks, indent=2, allow_nan=False))
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
