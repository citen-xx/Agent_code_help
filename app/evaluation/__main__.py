"""Benchmark CLI: python -m app.evaluation run [--task TASK]."""

import argparse
import asyncio
from pathlib import Path

from app.agent.capabilities import AgentProfile
from app.evaluation.ablation import run_ablation_experiment
from app.evaluation.failure_analysis import generate_failure_analysis
from app.evaluation.models import BenchmarkReport
from app.evaluation.phase2_5 import (
    regenerate_confirmation_artifacts,
    run_confirmation_experiment,
)
from app.evaluation.phase3 import (
    regenerate_phase3_report,
    run_patch_reliability_experiment,
)
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
    run.add_argument(
        "--profile",
        choices=[profile.value for profile in AgentProfile],
        default=AgentProfile.FULL.value,
        help="Agent capability profile",
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
    ablation = subparsers.add_parser(
        "ablation",
        help="Run controlled capability ablation experiments",
    )
    ablation.add_argument(
        "--suite",
        choices=["v1", "v2", "all"],
        default="v2",
    )
    ablation.add_argument("--runs-per-variant", type=int, default=2)
    ablation.add_argument(
        "--variant",
        action="append",
        choices=[profile.value for profile in AgentProfile],
        help="Run only selected variants; repeat for multiple variants",
    )
    ablation.add_argument("--experiment-id")
    ablation.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    confirm = subparsers.add_parser(
        "confirm",
        help="Run Phase 2.5 clean confirmation and failure analysis",
    )
    confirm.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    confirm.add_argument("--legacy-experiment")
    confirm_analyze = subparsers.add_parser(
        "confirm-analyze",
        help="Regenerate Phase 2.5 reports from existing run artifacts",
    )
    confirm_analyze.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    confirm_analyze.add_argument("--legacy-experiment")
    patch_ab = subparsers.add_parser(
        "patch-ablation",
        help="Run Phase 3 v1/v2 apply_patch contract A/B",
    )
    patch_ab.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    patch_report = subparsers.add_parser(
        "patch-report",
        help="Regenerate Phase 3 report from experiment.json",
    )
    patch_report.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
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
    if args.command == "ablation":
        selected = (
            [AgentProfile(value) for value in args.variant]
            if args.variant
            else None
        )
        report = asyncio.run(
            run_ablation_experiment(
                suite=args.suite,
                runs_per_variant=args.runs_per_variant,
                output_dir=Path(args.output_dir),
                experiment_id=args.experiment_id,
                variants=selected,
            )
        )
        print(f"Ablation report: {report}")
        return 0
    if args.command == "confirm":
        report = asyncio.run(
            run_confirmation_experiment(
                output_dir=Path(args.output_dir),
                legacy_experiment=args.legacy_experiment,
            )
        )
        print(f"Confirmation report: {report}")
        return 0
    if args.command == "confirm-analyze":
        report = regenerate_confirmation_artifacts(
            output_dir=Path(args.output_dir),
            legacy_experiment=args.legacy_experiment,
        )
        print(f"Confirmation report: {report}")
        return 0
    if args.command == "patch-ablation":
        report = asyncio.run(
            run_patch_reliability_experiment(
                output_dir=Path(args.output_dir),
            )
        )
        print(f"Phase 3 report: {report}")
        return 0
    if args.command == "patch-report":
        report = regenerate_phase3_report(
            output_dir=Path(args.output_dir),
        )
        print(f"Phase 3 report: {report}")
        return 0
    tasks = load_tasks(task_id=args.task, suite=args.suite)
    runner = BenchmarkRunner(
        tasks,
        output_dir=Path(args.output_dir),
        profile=args.profile,
    )
    _, report_path = asyncio.run(runner.run())
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
