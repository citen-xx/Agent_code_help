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
from app.evaluation.phase4 import (
    regenerate_phase4_report,
    run_context_ab,
)
from app.evaluation.phase4_5 import (
    regenerate_lifecycle_report,
    regenerate_lifecycle_audit,
    run_lifecycle_controls,
    run_lifecycle_treatment,
)
from app.evaluation.phase5 import (
    DEFAULT_TASKS_FILE as DEFAULT_DEFECTS4J_TASKS_FILE,
    regenerate_phase5_report,
    run_realworld_profiles,
    run_realworld_reproduction,
)
from app.evaluation.phase5_5 import run_phase5_5
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
    context_ab = subparsers.add_parser(
        "context-ab",
        help="Run Phase 4 compact Code Intelligence A/B",
    )
    context_ab.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    context_report = subparsers.add_parser(
        "context-report",
        help="Regenerate Phase 4 report from experiment.json",
    )
    context_report.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    lifecycle_control = subparsers.add_parser(
        "lifecycle-control",
        help="Run Phase 4.5 context-lifecycle Control and audit",
    )
    lifecycle_control.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    lifecycle_treatment = subparsers.add_parser(
        "lifecycle-treatment",
        help="Run Phase 4.5 context-lifecycle Treatment and report",
    )
    lifecycle_treatment.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    lifecycle_audit = subparsers.add_parser(
        "lifecycle-audit",
        help="Regenerate Phase 4.5 audit from existing Control runs",
    )
    lifecycle_audit.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    lifecycle_report = subparsers.add_parser(
        "lifecycle-report",
        help="Regenerate Phase 4.5 report from existing artifacts",
    )
    lifecycle_report.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    realworld_reproduce = subparsers.add_parser(
        "realworld-reproduce",
        help="Reproduce Defects4J buggy/fixed candidates",
    )
    realworld_reproduce.add_argument(
        "--tasks-file",
        default=str(DEFAULT_DEFECTS4J_TASKS_FILE),
    )
    realworld_reproduce.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    realworld_run = subparsers.add_parser(
        "realworld-run",
        help="Run Basic and Full on the Defects4J v1 benchmark",
    )
    realworld_run.add_argument(
        "--tasks-file",
        default=str(DEFAULT_DEFECTS4J_TASKS_FILE),
    )
    realworld_run.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    realworld_report = subparsers.add_parser(
        "realworld-report",
        help="Regenerate Phase 5 report from existing benchmark runs",
    )
    realworld_report.add_argument(
        "--tasks-file",
        default=str(DEFAULT_DEFECTS4J_TASKS_FILE),
    )
    realworld_report.add_argument(
        "--output-dir",
        default=str(DEFAULT_RESULTS_DIR),
    )
    phase5_5 = subparsers.add_parser(
        "phase5-5",
        help="Run Phase 5.5 confirmation and search-to-patch diagnosis",
    )
    phase5_5.add_argument(
        "--tasks-file",
        default=str(DEFAULT_DEFECTS4J_TASKS_FILE),
    )
    phase5_5.add_argument(
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
    if args.command == "context-ab":
        report = asyncio.run(
            run_context_ab(
                output_dir=Path(args.output_dir),
            )
        )
        print(f"Phase 4 report: {report}")
        return 0
    if args.command == "context-report":
        report = regenerate_phase4_report(
            output_dir=Path(args.output_dir),
        )
        print(f"Phase 4 report: {report}")
        return 0
    if args.command == "lifecycle-control":
        report = asyncio.run(
            run_lifecycle_controls(
                output_dir=Path(args.output_dir),
            )
        )
        print(f"Phase 4.5 audit: {report}")
        return 0
    if args.command == "lifecycle-treatment":
        report = asyncio.run(
            run_lifecycle_treatment(
                output_dir=Path(args.output_dir),
            )
        )
        print(f"Phase 4.5 report: {report}")
        return 0
    if args.command == "lifecycle-audit":
        report = regenerate_lifecycle_audit(
            output_dir=Path(args.output_dir),
        )
        print(f"Phase 4.5 audit: {report}")
        return 0
    if args.command == "lifecycle-report":
        report = regenerate_lifecycle_report(
            output_dir=Path(args.output_dir),
        )
        print(f"Phase 4.5 report: {report}")
        return 0
    if args.command == "realworld-reproduce":
        report = run_realworld_reproduction(
            tasks_file=args.tasks_file,
            output_dir=Path(args.output_dir),
        )
        print(f"Phase 5 candidate selection: {report}")
        return 0
    if args.command == "realworld-run":
        report = asyncio.run(
            run_realworld_profiles(
                tasks_file=args.tasks_file,
                output_dir=Path(args.output_dir),
            )
        )
        print(f"Phase 5 report: {report}")
        return 0
    if args.command == "realworld-report":
        report = regenerate_phase5_report(
            tasks_file=args.tasks_file,
            output_dir=Path(args.output_dir),
        )
        print(f"Phase 5 report: {report}")
        return 0
    if args.command == "phase5-5":
        report = run_phase5_5(
            tasks_file=args.tasks_file,
            output_dir=Path(args.output_dir),
        )
        print(f"Phase 5.5 report: {report}")
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
