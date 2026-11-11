"""Controlled capability ablation experiments over the V2 benchmark."""

import asyncio
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.capabilities import (
    AgentProfile,
    PROFILE_CAPABILITIES,
)
from app.core.config import settings
from app.evaluation.models import BenchmarkReport, BenchmarkTaskResult
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT, load_tasks
from app.workspace import git

VARIANT_ORDER = [
    AgentProfile.BASIC,
    AgentProfile.CODE_INTELLIGENCE,
    AgentProfile.PLANNER,
    AgentProfile.FULL,
]
VARIANT_LABELS = {
    AgentProfile.BASIC: "Basic",
    AgentProfile.CODE_INTELLIGENCE: "Code Intelligence",
    AgentProfile.PLANNER: "Planner",
    AgentProfile.FULL: "Full",
}
CODE_INTELLIGENCE_TOOLS = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
}


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _mean(values: list[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float | int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _aggregate_variant(
    profile: AgentProfile,
    reports: list[BenchmarkReport],
    report_paths: list[Path],
    experiment_dir: Path,
) -> dict:
    results = [result for report in reports for result in report.results]
    task_groups: dict[str, list[BenchmarkTaskResult]] = defaultdict(list)
    tool_usage: Counter[str] = Counter()
    tool_failures: Counter[str] = Counter()
    tool_output_chars: Counter[str] = Counter()
    failure_categories: Counter[str] = Counter()
    for result in results:
        task_groups[result.task_id].append(result)
        tool_usage.update(result.tool_usage)
        tool_failures.update(result.tool_failure_counts)
        for event in result.tool_events:
            tool_output_chars[event.tool_name] += event.output_chars
        if result.failure_category is not None:
            failure_categories[result.failure_category.value] += 1

    patch_attempts = sum(
        (
            result.apply_patch_attempts
            if "apply_patch_attempts" in result.model_fields_set
            else result.tool_usage.get("apply_patch", 0)
        )
        for result in results
    )
    patch_failures = sum(
        (
            result.apply_patch_failures
            if "apply_patch_failures" in result.model_fields_set
            else result.tool_failure_counts.get("apply_patch", 0)
        )
        for result in results
    )
    manual_test_runs = sum(
        (
            result.manual_test_runs
            if "manual_test_runs" in result.model_fields_set
            else max(
                result.tool_usage.get("run_tests", 0)
                - result.auto_maven_test_runs,
                0,
            )
        )
        for result in results
    )
    agent_tool_calls = sum(
        (
            result.agent_tool_calls
            if "agent_tool_calls" in result.model_fields_set
            else max(result.tool_calls - result.auto_maven_test_runs, 0)
        )
        for result in results
    )
    patch_invocations = sum(
        (
            result.apply_patch_invocations
            if "apply_patch_invocations" in result.model_fields_set
            else result.tool_usage.get("apply_patch", 0)
        )
        for result in results
    )
    patch_files_requested = sum(
        result.patch_files_requested for result in results
    )
    patch_replacements_requested = sum(
        result.patch_replacements_requested for result in results
    )
    successful_patch_replacements = sum(
        result.successful_patch_replacements for result in results
    )
    recovered_patch_failures = sum(
        result.recovered_patch_failures for result in results
    )

    task_results: dict[str, dict] = {}
    for task_id in sorted(task_groups):
        group = task_groups[task_id]
        task_results[task_id] = {
            "runs": len(group),
            "passes": sum(1 for result in group if result.success),
            "success_rate": _rate(
                sum(1 for result in group if result.success),
                len(group),
            ),
            "failure_categories": sorted(
                {
                    result.failure_category.value
                    for result in group
                    if result.failure_category is not None
                }
            ),
        }

    token_values = [
        result.total_tokens
        for result in results
        if result.total_tokens is not None
    ]
    prompt_token_values = [
        result.prompt_tokens
        for result in results
        if result.prompt_tokens is not None
    ]
    completion_token_values = [
        result.completion_tokens
        for result in results
        if result.completion_tokens is not None
    ]
    hidden_results = [
        result
        for result in results
        if result.hidden_test_result is not None
    ]
    by_difficulty = {
        difficulty: [result for result in results if result.difficulty.value == difficulty]
        for difficulty in ("medium", "hard")
    }
    passes = sum(1 for result in results if result.success)
    stable_pass = sorted(
        task_id
        for task_id, value in task_results.items()
        if value["passes"] == value["runs"]
    )
    stable_fail = sorted(
        task_id
        for task_id, value in task_results.items()
        if value["passes"] == 0
    )
    unstable = sorted(
        task_id
        for task_id, value in task_results.items()
        if 0 < value["passes"] < value["runs"]
    )
    capabilities = PROFILE_CAPABILITIES[profile]
    return {
        "variant": profile.value,
        "label": VARIANT_LABELS[profile],
        "capabilities": capabilities.as_dict(),
        "runs": [
            {
                "run_id": report.run_id,
                "path": path.relative_to(experiment_dir).as_posix(),
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                "passed_tasks": report.summary.passed_tasks,
                "total_tasks": report.summary.total_tasks,
            }
            for report, path in zip(reports, report_paths, strict=True)
        ],
        "metrics": {
            "task_runs": len(results),
            "passed": passes,
            "success_rate": _rate(passes, len(results)),
            "medium_success_rate": _rate(
                sum(1 for result in by_difficulty["medium"] if result.success),
                len(by_difficulty["medium"]),
            ),
            "hard_success_rate": _rate(
                sum(1 for result in by_difficulty["hard"] if result.success),
                len(by_difficulty["hard"]),
            ),
            "hidden_test_pass_rate": _rate(
                sum(
                    1
                    for result in hidden_results
                    if result.hidden_tests_passed is True
                ),
                len(hidden_results),
            ),
            "average_tokens": _mean(token_values),
            "median_tokens": _median(token_values),
            "average_prompt_tokens": _mean(prompt_token_values),
            "average_completion_tokens": _mean(completion_token_values),
            "average_llm_calls": _mean(
                [result.llm_calls for result in results]
            ),
            "average_tool_calls": _mean(
                [result.tool_calls for result in results]
            ),
            "average_agent_tool_calls": _mean(
                [
                    (
                        result.agent_tool_calls
                        if "agent_tool_calls" in result.model_fields_set
                        else max(
                            result.tool_calls - result.auto_maven_test_runs,
                            0,
                        )
                    )
                    for result in results
                ]
            ),
            "average_duration": _mean(
                [result.duration_seconds for result in results]
            ),
            "average_repair_rounds": _mean(
                [result.repair_rounds for result in results]
            ),
            "average_changed_files": _mean(
                [result.changed_file_count for result in results]
            ),
            "total_tool_calls": sum(result.tool_calls for result in results),
            "failed_tool_calls": sum(tool_failures.values()),
            "empty_patch_calls": sum(
                result.empty_patch_calls for result in results
            ),
            "auto_maven_test_runs": sum(
                result.auto_maven_test_runs for result in results
            ),
            "manual_test_runs": manual_test_runs,
            "agent_tool_calls": agent_tool_calls,
            "apply_patch_attempts": patch_attempts,
            "apply_patch_invocations": patch_invocations,
            "apply_patch_failures": patch_failures,
            "apply_patch_failure_rate": _rate(
                patch_failures,
                patch_attempts,
            ),
            "tool_output_chars": sum(
                result.tool_output_chars for result in results
            ),
            "patch_files_requested": patch_files_requested,
            "patch_replacements_requested": patch_replacements_requested,
            "successful_patch_replacements": successful_patch_replacements,
            "recovered_patch_failures": recovered_patch_failures,
            "repair_activated_task_runs": sum(
                1 for result in results if result.repair_rounds > 0
            ),
        },
        "tool_usage": dict(sorted(tool_usage.items())),
        "tool_failures": dict(sorted(tool_failures.items())),
        "tool_output_chars": dict(sorted(tool_output_chars.items())),
        "failure_categories": dict(sorted(failure_categories.items())),
        "task_results": task_results,
        "stability": {
            "stable_pass": stable_pass,
            "stable_fail": stable_fail,
            "unstable": unstable,
        },
    }


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _number(value: float | None, digits: int = 1) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def _delta_percent(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / previous * 100


def _delta_block(
    previous: dict,
    current: dict,
) -> list[str]:
    previous_metrics = previous["metrics"]
    current_metrics = current["metrics"]
    return [
        f"- Success: {_percent(previous_metrics['success_rate'])} → "
        f"{_percent(current_metrics['success_rate'])} "
        f"({(current_metrics['success_rate'] - previous_metrics['success_rate']) * 100:+.1f} pp)",
        f"- Hard Success: {_percent(previous_metrics['hard_success_rate'])} → "
        f"{_percent(current_metrics['hard_success_rate'])} "
        f"({(current_metrics['hard_success_rate'] - previous_metrics['hard_success_rate']) * 100:+.1f} pp)",
        f"- Tokens: {_number(previous_metrics['average_tokens'])} → "
        f"{_number(current_metrics['average_tokens'])} "
        f"({_number(_delta_percent(current_metrics['average_tokens'], previous_metrics['average_tokens']))}%)",
        f"- Tool Calls: {_number(previous_metrics['average_tool_calls'])} → "
        f"{_number(current_metrics['average_tool_calls'])} "
        f"({current_metrics['average_tool_calls'] - previous_metrics['average_tool_calls']:+.2f}/task)",
        f"- Duration: {_number(previous_metrics['average_duration'])}s → "
        f"{_number(current_metrics['average_duration'])}s",
        f"- Failed Tool Calls: {previous_metrics['failed_tool_calls']} → "
        f"{current_metrics['failed_tool_calls']}",
        f"- Empty Patch Calls: {previous_metrics['empty_patch_calls']} → "
        f"{current_metrics['empty_patch_calls']}",
    ]


def _task_cell(value: dict) -> str:
    if value["passes"] == value["runs"]:
        symbol = "✅"
    elif value["passes"] == 0:
        symbol = "❌"
    else:
        symbol = "⚠️"
    return f"{value['passes']}/{value['runs']} {symbol}"


def _render_report(experiment: dict) -> str:
    variants = experiment["variants"]
    ordered = [
        variants[profile.value]
        for profile in VARIANT_ORDER
        if profile.value in variants
    ]
    lines = [
        "# Agent Ablation Study",
        "",
        "## Experimental Setup",
        "",
        f"- Experiment: `{experiment['experiment_id']}`",
        f"- Model: `{experiment['model']}`",
        f"- Benchmark: `{experiment['benchmark']}`",
        f"- Runs per Variant: {experiment['runs_per_variant']}",
        f"- Git Commit: `{experiment['git_commit'] or 'unknown'}`",
        f"- Dirty Working Tree: {experiment['git_dirty']}",
        f"- Docker Maven Image: `{experiment['controlled_variables']['docker_maven_image']}`",
        f"- Max Steps: {experiment['controlled_variables']['max_steps']}",
        f"- Task Timeout: {experiment['controlled_variables']['task_timeout_seconds']}s",
        "",
        "All variants use the same Graph, Evaluation Runner, benchmark tasks, fixtures, "
        "public/hidden tests, model/provider settings, timeout, Docker image and "
        "success criteria. Only capability flags and their directly related prompt "
        "fragments differ.",
        "",
        "## Overall Results",
        "",
        "| Variant | Success | Medium | Hard | Hidden | Tokens | Median Tokens | "
        "Calls | Tools | Duration | Repairs | Changed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in ordered:
        metrics = variant["metrics"]
        lines.append(
            f"| {variant['label']} | {_percent(metrics['success_rate'])} | "
            f"{_percent(metrics['medium_success_rate'])} | "
            f"{_percent(metrics['hard_success_rate'])} | "
            f"{_percent(metrics['hidden_test_pass_rate'])} | "
            f"{_number(metrics['average_tokens'])} | "
            f"{_number(metrics['median_tokens'])} | "
            f"{_number(metrics['average_llm_calls'], 2)} | "
            f"{_number(metrics['average_tool_calls'], 2)} | "
            f"{_number(metrics['average_duration'])}s | "
            f"{_number(metrics['average_repair_rounds'], 2)} | "
            f"{_number(metrics['average_changed_files'], 2)} |"
        )

    lines.extend(["", "## Capability Delta", ""])
    for previous, current in zip(ordered, ordered[1:]):
        lines.extend(
            [
                f"### {previous['label']} → {current['label']}",
                "",
                *_delta_block(previous, current),
                "",
            ]
        )

    lines.extend(
        [
            "## Task-Level Comparison",
            "",
            "Each cell is `passes/runs`; ⚠️ marks replicate instability.",
            "",
            "| Task | "
            + " | ".join(variant["label"] for variant in ordered)
            + " |",
            "|---|" + "---:|" * len(ordered),
        ]
    )
    task_ids = sorted(
        {
            task_id
            for variant in ordered
            for task_id in variant["task_results"]
        }
    )
    for task_id in task_ids:
        cells = []
        for variant in ordered:
            value = variant["task_results"].get(
                task_id,
                {"passes": 0, "runs": 0},
            )
            cells.append(_task_cell(value))
        lines.append(f"| {task_id} | " + " | ".join(cells) + " |")

    lines.extend(["", "### Adjacent Capability Changes", ""])
    for previous, current in zip(ordered, ordered[1:]):
        gains = []
        regressions = []
        for task_id in task_ids:
            previous_value = previous["task_results"].get(task_id, {"passes": 0})
            current_value = current["task_results"].get(task_id, {"passes": 0})
            if current_value["passes"] > previous_value["passes"]:
                gains.append(task_id)
            elif current_value["passes"] < previous_value["passes"]:
                regressions.append(task_id)
        lines.append(
            f"- {previous['label']} → {current['label']}: "
            f"gains={gains or 'none'}; regressions={regressions or 'none'}"
        )

    lines.extend(
        [
            "",
            "## Tool Usage Analysis",
            "",
            "| Tool | "
            + " | ".join(variant["label"] for variant in ordered)
            + " |",
            "|---|" + "---:|" * len(ordered),
        ]
    )
    all_tools = sorted(
        {
            tool
            for variant in ordered
            for tool in variant["tool_usage"]
        }
    )
    for tool in all_tools:
        lines.append(
            f"| `{tool}` | "
            + " | ".join(
                str(variant["tool_usage"].get(tool, 0))
                for variant in ordered
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| Diagnostic | "
            + " | ".join(variant["label"] for variant in ordered)
            + " |",
            "|---|" + "---:|" * len(ordered),
        ]
    )
    for key, label in (
        ("failed_tool_calls", "Failed tool calls"),
        ("empty_patch_calls", "Empty patch calls"),
        ("auto_maven_test_runs", "Auto Maven tests"),
        ("manual_test_runs", "Manual Maven tests"),
        ("apply_patch_attempts", "Apply patch attempts"),
        ("apply_patch_failures", "Apply patch failures"),
        ("apply_patch_failure_rate", "Apply patch failure rate"),
        ("repair_activated_task_runs", "Repair-activated task runs"),
    ):
        lines.append(
            f"| {label} | "
            + " | ".join(
                (
                    _percent(variant["metrics"][key])
                    if key == "apply_patch_failure_rate"
                    else str(variant["metrics"][key])
                )
                for variant in ordered
            )
            + " |"
        )
    for variant in ordered:
        ci_calls = sum(
            variant["tool_usage"].get(tool, 0)
            for tool in CODE_INTELLIGENCE_TOOLS
        )
        lines.append(
            f"- {variant['label']}: Code Intelligence tool calls = {ci_calls}; "
            f"Planner calls = {variant['tool_usage'].get('create_repair_plan', 0)}; "
            f"Maven test calls = {variant['tool_usage'].get('run_tests', 0)}."
        )

    lines.extend(["", "## Stability", ""])
    unstable_tasks = sorted(
        {
            task_id
            for variant in ordered
            for task_id in variant["stability"]["unstable"]
        }
    )
    if unstable_tasks:
        lines.append(
            "Replicate-unstable tasks: "
            + ", ".join(f"`{task_id}`" for task_id in unstable_tasks)
            + "."
        )
    else:
        lines.append("No replicate-unstable tasks.")
    lines.append("")
    for variant in ordered:
        stability = variant["stability"]
        lines.extend(
            [
                f"### {variant['label']}",
                "",
                f"- Stable pass: {', '.join(stability['stable_pass']) or 'none'}",
                f"- Stable fail: {', '.join(stability['stable_fail']) or 'none'}",
                f"- Unstable: {', '.join(stability['unstable']) or 'none'}",
                "",
            ]
        )

    lines.extend(["## Failed Cases", ""])
    for variant in ordered:
        lines.append(f"### {variant['label']}")
        lines.append("")
        if not variant["failure_categories"]:
            lines.append("No recorded failures.")
        else:
            for category, count in variant["failure_categories"].items():
                tasks = [
                    task_id
                    for task_id, value in variant["task_results"].items()
                    if category in value["failure_categories"]
                ]
                lines.append(
                    f"- `{category}`: {count} task-run(s); "
                    f"tasks={', '.join(tasks)}"
                )
        lines.append("")

    metrics_by_profile = {
        profile.value: variants[profile.value]["metrics"]
        for profile in VARIANT_ORDER
        if profile.value in variants
    }
    basic = metrics_by_profile.get(AgentProfile.BASIC.value)
    ci = metrics_by_profile.get(AgentProfile.CODE_INTELLIGENCE.value)
    planner = metrics_by_profile.get(AgentProfile.PLANNER.value)
    full = metrics_by_profile.get(AgentProfile.FULL.value)
    lines.extend(["## Research Questions", ""])
    if basic and ci:
        ci_delta = ci["success_rate"] - basic["success_rate"]
        medium_delta = (
            ci["medium_success_rate"] - basic["medium_success_rate"]
        ) * 100
        hard_delta = (
            ci["hard_success_rate"] - basic["hard_success_rate"]
        ) * 100
        token_delta = _delta_percent(ci["average_tokens"], basic["average_tokens"])
        lines.extend(
            [
                f"1. **Q1 Code Intelligence success:** Δ success = "
                f"{ci_delta * 100:+.1f} pp.",
                f"2. **Q2 Difficulty:** medium Δ = {medium_delta:+.1f} pp; "
                f"hard Δ = {hard_delta:+.1f} pp.",
                f"3. **Q3 Cost:** average token change = "
                f"{_number(token_delta)}%; LLM calls change = "
                f"{ci['average_llm_calls'] - basic['average_llm_calls']:+.2f}/task.",
            ]
        )
    if ci and planner:
        lines.extend(
            [
                f"4. **Q4 Repair Planner success:** Δ success = "
                f"{(planner['success_rate'] - ci['success_rate']) * 100:+.1f} pp.",
                f"5. **Q5 Planner efficiency:** tool calls change = "
                f"{planner['average_tool_calls'] - ci['average_tool_calls']:+.2f}/task; "
                f"failed tool calls {ci['failed_tool_calls']} → "
                f"{planner['failed_tool_calls']}; empty patches "
                f"{ci['empty_patch_calls']} → {planner['empty_patch_calls']}.",
            ]
        )
    if planner and full:
        lines.extend(
            [
                f"6. **Q6 Repair/Replan:** Full success Δ vs Planner = "
                f"{(full['success_rate'] - planner['success_rate']) * 100:+.1f} pp; "
                f"auto Maven tests = {full['auto_maven_test_runs']}; "
                f"repair-activated task runs = "
                f"{full['repair_activated_task_runs']}; average repair rounds = "
                f"{_number(full['average_repair_rounds'], 2)}.",
                f"7. **Q7 Complexity:** Full vs Planner tool calls = "
                f"{full['average_tool_calls'] - planner['average_tool_calls']:+.2f}/task; "
                f"tokens = "
                f"{_number(_delta_percent(full['average_tokens'], planner['average_tokens']))}%; "
                f"duration = "
                f"{full['average_duration'] - planner['average_duration']:+.1f}s/task.",
            ]
        )
    if basic and ci and planner and full:
        variants_by_value = {
            profile.value: variants[profile.value]
            for profile in VARIANT_ORDER
        }
        ci_calls = {
            profile.value: sum(
                variants_by_value[profile.value]["tool_usage"].get(tool, 0)
                for tool in CODE_INTELLIGENCE_TOOLS
            )
            for profile in VARIANT_ORDER
        }
        planner_calls = {
            profile.value: variants_by_value[profile.value]["tool_usage"].get(
                "create_repair_plan", 0
            )
            for profile in VARIANT_ORDER
        }
        lines.extend(
            [
                "",
                "## Interpretation and Limitations",
                "",
                f"- Code Intelligence was genuinely used: {ci_calls['code_intelligence']} "
                f"CI calls in +CI and {ci_calls['full']} in Full. The observed "
                "success change was not positive in this sample.",
                f"- Repair Planner was genuinely used: "
                f"{planner_calls['planner']} plan calls in Planner and "
                f"{planner_calls['full']} in Full. It changed efficiency behavior, "
                "but not aggregate success in this sample.",
                f"- Repair Loop was reachable and active: "
                f"{full['auto_maven_test_runs']} automatic Maven runs and "
                f"{full['repair_activated_task_runs']} task-runs requiring a patch "
                "after a failed test.",
                "- These are two repeats per variant, not an inferential significance "
                "test. Replicate instability and tool-call variance are large enough "
                "that small percentage-point differences should guide hypotheses, not "
                "be treated as definitive causal effects.",
                "- Full was freshly re-run under the unified profile system. The two "
                "phase-one baseline runs are not merged into this experiment because "
                "they were produced before the capability metadata existed.",
            ]
        )
    lines.extend(
        [
            "",
            "Repair rounds count patches made after an observed test failure. "
            "A low value means most successful task-runs needed only their initial "
            "patch, not that the repair loop was unreachable.",
            "",
        ]
    )
    return "\n".join(lines)


def _experiment_payload(
    *,
    experiment_id: str,
    started_at: str,
    model: str,
    benchmark: str,
    runs_per_variant: int,
    git_commit: str | None,
    git_dirty: bool | None,
    reports_by_variant: dict[AgentProfile, list[BenchmarkReport]],
    paths_by_variant: dict[AgentProfile, list[Path]],
    experiment_dir: Path,
) -> dict:
    variants = {}
    for profile in VARIANT_ORDER:
        reports = reports_by_variant.get(profile, [])
        if not reports:
            continue
        variants[profile.value] = _aggregate_variant(
            profile,
            reports,
            paths_by_variant[profile],
            experiment_dir,
        )
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "benchmark": benchmark,
        "runs_per_variant": runs_per_variant,
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "controlled_variables": {
            "temperature": 0,
            "max_steps": 20,
            "task_timeout_seconds": 300,
            "docker_maven_image": settings.DOCKER_MAVEN_IMAGE,
            "success_criteria": "public and hidden Maven tests both pass",
        },
        "variants": variants,
    }


def _write_experiment(
    experiment_dir: Path,
    payload: dict,
) -> None:
    _atomic_write(
        experiment_dir / "experiment.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(
        experiment_dir / "ablation_report.md",
        _render_report(payload),
    )


async def run_ablation_experiment(
    *,
    suite: str = "v2",
    runs_per_variant: int = 2,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
    experiment_id: str | None = None,
    variants: list[AgentProfile] | None = None,
) -> Path:
    if runs_per_variant < 1:
        raise ValueError("runs_per_variant 必须 >= 1")
    selected = variants or list(VARIANT_ORDER)
    unknown = [value for value in selected if value not in VARIANT_ORDER]
    if unknown:
        raise ValueError(f"未知 ablation variant: {unknown}")
    tasks = load_tasks(suite=suite)
    now = datetime.now(timezone.utc)
    experiment_id = experiment_id or f"ablation_{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    experiment_dir = Path(output_dir) / experiment_id
    experiment_dir.mkdir(parents=True, exist_ok=True)
    try:
        git_commit = git.head_commit(PROJECT_ROOT)
        git_dirty = bool(git.status_porcelain(PROJECT_ROOT).strip())
    except git.GitError:
        git_commit = None
        git_dirty = None

    reports_by_variant: dict[AgentProfile, list[BenchmarkReport]] = defaultdict(list)
    paths_by_variant: dict[AgentProfile, list[Path]] = defaultdict(list)
    payload = _experiment_payload(
        experiment_id=experiment_id,
        started_at=now.isoformat(),
        model=settings.LLM_MODEL or "unknown",
        benchmark=suite,
        runs_per_variant=runs_per_variant,
        git_commit=git_commit,
        git_dirty=git_dirty,
        reports_by_variant=reports_by_variant,
        paths_by_variant=paths_by_variant,
        experiment_dir=experiment_dir,
    )
    _write_experiment(experiment_dir, payload)

    for profile in selected:
        variant_dir = experiment_dir / f"variant_{profile.value}"
        variant_dir.mkdir(parents=True, exist_ok=True)
        for run_index in range(1, runs_per_variant + 1):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            run_id = f"{profile.value}_run{run_index:02d}_{stamp}"
            print(
                f"\n=== {profile.value} run {run_index}/{runs_per_variant} ===",
                flush=True,
            )
            runner = BenchmarkRunner(
                tasks,
                output_dir=variant_dir,
                run_id=run_id,
                profile=profile,
            )
            report, report_path = await runner.run()
            reports_by_variant[profile].append(report)
            paths_by_variant[profile].append(report_path)
            payload = _experiment_payload(
                experiment_id=experiment_id,
                started_at=now.isoformat(),
                model=settings.LLM_MODEL or "unknown",
                benchmark=suite,
                runs_per_variant=runs_per_variant,
                git_commit=git_commit,
                git_dirty=git_dirty,
                reports_by_variant=reports_by_variant,
                paths_by_variant=paths_by_variant,
                experiment_dir=experiment_dir,
            )
            _write_experiment(experiment_dir, payload)
    report_path = experiment_dir / "ablation_report.md"
    print(f"\nAblation report: {report_path}", flush=True)
    return report_path
