"""Benchmark CLI: python -m app.evaluation run [--task TASK]."""

import argparse
import asyncio
from pathlib import Path

from app.evaluation.failure_analysis import generate_failure_analysis
from app.evaluation.models import BenchmarkReport
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, load_tasks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Java Coding Agent Benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run all tasks or one task")
    run.add_argument("--task", help="Run a single benchmark task_id")
    run.add_argument(
        "--suite",
        choices=["v1", "v2", "all"],
        default="v1",
        help="Benchmark suite to run",
    )
    run.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help="Directory for JSON reports",
    )
    analyze = subparsers.add_parser(
        "analyze",
        help="Generate V2 failure-analysis.md from a JSON report",
    )
    analyze.add_argument("--report", required=True)
    analyze.add_argument(
        "--output",
        default="evaluation/failure-analysis.md",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "analyze":
        report = BenchmarkReport.model_validate_json(
            Path(args.report).read_text(encoding="utf-8")
        )
        output = generate_failure_analysis(report, args.output)
        print(f"Failure analysis: {output}")
        return 0
    tasks = load_tasks(task_id=args.task, suite=args.suite)
    runner = BenchmarkRunner(
        tasks,
        output_dir=Path(args.output_dir),
    )
    _, report_path = asyncio.run(runner.run())
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
