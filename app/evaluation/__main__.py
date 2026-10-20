"""Benchmark CLI: python -m app.evaluation run [--task TASK]."""

import argparse
import asyncio
from pathlib import Path

from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, load_tasks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Java Coding Agent Benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run all tasks or one task")
    run.add_argument("--task", help="Run a single benchmark task_id")
    run.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory for JSON reports",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "run":
        return 2
    tasks = load_tasks(task_id=args.task)
    runner = BenchmarkRunner(
        tasks,
        output_dir=Path(args.output_dir),
    )
    _, report_path = asyncio.run(runner.run())
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
