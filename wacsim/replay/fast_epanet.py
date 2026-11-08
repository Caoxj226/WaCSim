"""Backward-compatible no-attack entry point for exploratory calibration runs."""
import argparse
import json
from pathlib import Path
from .control import PIParameters as Candidate
from .engine import prepared_inp as _prepared_inp, run_epanet
from .scenario import RunConfig, create_task


def run_no_attack(inp: Path, scenario: Path, candidate: Candidate, steps: int,
                  include_network_detail: bool = False):
    config = RunConfig(candidate.sp_m, candidate.kp, candidate.ki_per_step)
    task = create_task(inp, scenario, config, steps)
    return task, run_epanet(task, config, inp, scenario,
                            include_network_detail=include_network_detail)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inp", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--sp", type=float, required=True)
    parser.add_argument("--kp", type=float, required=True)
    parser.add_argument("--ki", type=float, required=True)
    parser.add_argument("--steps", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network-detail", action="store_true")
    args = parser.parse_args()
    task, result = run_no_attack(args.inp, args.scenario,
                                 Candidate(args.sp, args.kp, args.ki), args.steps,
                                 args.network_detail)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"task": task.to_dict(), "result": result.to_dict()},
                                      ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(f"steps={len(result.rows)} initial_T7={result.meta['initial_T7_LEVEL_m']:.4f} "
          f"last_T7={result.rows[-1]['T7_LEVEL']:.4f} hazard={result.score['first_hazard_step']}")


if __name__ == "__main__":
    main()
