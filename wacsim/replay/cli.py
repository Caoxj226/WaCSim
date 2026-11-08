"""Unified CTown replay CLI: task/config JSON in, validated result JSON out."""
import argparse
import json
from pathlib import Path
from .contract import ReplayResult, ReplayTask
from .engine import run_epanet
from .scenario import RunConfig


def main():
    parser = argparse.ArgumentParser(description="CTown fast EPANET replay")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--inp", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network-detail", action="store_true")
    args = parser.parse_args()
    task = ReplayTask.from_dict(json.loads(args.task.read_text(encoding="utf-8")))
    config = RunConfig(**json.loads(args.config.read_text(encoding="utf-8")))
    exit_code = 0
    try:
        result = run_epanet(task, config, args.inp, args.scenario,
                            include_network_detail=args.network_detail)
    except Exception as exc:
        result = ReplayResult(status="error", rows=[], score={}, injection_echo=[],
                              meta={}, run_key=task.run_key,
                              error=f"{type(exc).__name__}: {exc}")
        exit_code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"task": task.to_dict(), "result": result.to_dict()},
                                      ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(f"status={result.status} rows={len(result.rows)} run_key={task.run_key}")
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
