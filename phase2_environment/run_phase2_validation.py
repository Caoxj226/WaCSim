"""Run phase-two fast determinism/performance checks or compare saved full output."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
from statistics import median
import time

import yaml

from wacsim.replay.contract import ReplayTask
from wacsim.replay.engine import run_epanet
from wacsim.replay.scenario import RunConfig, create_task
from wacsim.replay.validation import canonical_sha256, compare_fast_and_full, load_fast_result, normalise_full_output


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def execute_once(payload: dict) -> dict:
    task = ReplayTask.from_dict(payload["task"])
    config = RunConfig(**payload["config"])
    result = run_epanet(task, config, Path(payload["inp"]), Path(payload["scenario"]))
    evidence = {"rows": result.rows, "score": result.score, "run_key": result.run_key}
    return {"run_key": result.run_key, "rows_sha256": canonical_sha256(evidence), "score": result.score}


def run_fast_validation(args) -> None:
    scenario, inp, lock = Path(args.scenario), Path(args.inp), Path(args.lock)
    locked = yaml.safe_load(lock.read_text(encoding="utf-8"))
    config = RunConfig(locked["nominal_setpoint_m"], locked["Kp"],
                       locked["Ki_per_step"], locked["theta_m"])
    task = create_task(inp, scenario, config, args.steps, lock=lock)
    payload = {"task": task.to_dict(), "config": config.__dict__, "inp": str(inp), "scenario": str(scenario)}
    workers = min(args.workers, args.repetitions)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        repeated = list(pool.map(execute_once, [payload] * args.repetitions))
    hashes = sorted({item["rows_sha256"] for item in repeated})
    run_keys = sorted({item["run_key"] for item in repeated})
    determinism = {"status": "passed" if len(hashes) == len(run_keys) == 1 else "failed",
                   "repetitions": args.repetitions, "workers": workers, "steps": args.steps,
                   "unique_rows_score_hashes": hashes, "unique_run_keys": run_keys,
                   "samples": repeated}
    timings, performance_runs = [], []
    for _ in range(args.performance_repetitions):
        started = time.perf_counter()
        performance_runs.append(execute_once(payload))
        timings.append(time.perf_counter() - started)
    performance = {"status": "passed" if median(timings) <= 1.0 else "failed",
                   "steps": args.steps, "repetitions": args.performance_repetitions,
                   "execution_mode": "single_process_serial", "threshold_seconds": 1.0,
                   "durations_seconds": timings, "median_seconds": median(timings),
                   "rows_score_hashes": [item["rows_sha256"] for item in performance_runs]}
    report = {"status": "passed" if determinism["status"] == performance["status"] == "passed" else "failed",
              "task": task.to_dict(), "config": config.__dict__,
              "determinism": determinism, "performance": performance}
    write_json(Path(args.output), report)
    print(json.dumps({"status": report["status"], "median_seconds": performance["median_seconds"]}))


def run_comparison(args) -> None:
    report = compare_fast_and_full(load_fast_result(Path(args.fast_result)),
                                   normalise_full_output(Path(args.full_output)),
                                   pu10_tolerance=args.pu10_tolerance)
    write_json(Path(args.output), report)
    print(json.dumps({"status": report["status"], "T7_max_abs_error_m": report["T7_max_abs_error_m"]}))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    fast = commands.add_parser("fast")
    fast.add_argument("--scenario", required=True)
    fast.add_argument("--inp", required=True)
    fast.add_argument("--lock", required=True)
    fast.add_argument("--output", required=True)
    fast.add_argument("--steps", type=int, default=300)
    fast.add_argument("--repetitions", type=int, default=100)
    fast.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    fast.add_argument("--performance-repetitions", type=int, default=5)
    fast.set_defaults(handler=run_fast_validation)
    compare = commands.add_parser("compare")
    compare.add_argument("--fast-result", required=True)
    compare.add_argument("--full-output", required=True)
    compare.add_argument("--pu10-tolerance", required=True, type=float)
    compare.add_argument("--output", required=True)
    compare.set_defaults(handler=run_comparison)
    args = parser.parse_args()
    if getattr(args, "workers", 1) < 1 or getattr(args, "repetitions", 1) < 1:
        parser.error("workers and repetitions must be positive")
    args.handler(args)


if __name__ == "__main__":
    main()
