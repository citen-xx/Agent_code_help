"""Phase 4.5 context lifecycle accounting and deterministic compaction A/B."""

import asyncio
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.core.config import settings
from app.evaluation.ablation import _aggregate_variant
from app.evaluation.models import (
    BenchmarkReport,
    BenchmarkTaskResult,
    ContextCallTelemetry,
    ContextTelemetry,
    MessageLifecycleTelemetry,
)
from app.evaluation.phase2_5 import (
    _file_hashes,
    _load_reports,
    _tree_digest,
    _tree_hashes,
)
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT, load_tasks
from app.workspace import git

PHASE45_DIR_NAME = "phase4_5_context_lifecycle"
PROFILES = [
    AgentProfile.CODE_INTELLIGENCE,
    AgentProfile.PLANNER,
    AgentProfile.FULL,
]
CATEGORY_FIELDS = [
    "system_message_chars",
    "user_message_chars",
    "assistant_message_chars",
    "tool_call_chars",
    "ci_tool_chars",
    "read_file_chars",
    "test_output_chars",
    "patch_output_chars",
    "planner_chars",
    "other_chars",
]
CI_TOOL_NAMES = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
}
SOURCE_FILES = [
    "app/agent/capabilities.py",
    "app/agent/context_lifecycle.py",
    "app/agent/graph.py",
    "app/agent/state.py",
    "app/agent/telemetry.py",
    "app/code_intel/compact.py",
    "app/code_intel/tools.py",
    "app/evaluation/models.py",
    "app/evaluation/phase4_5.py",
    "app/evaluation/runner.py",
    "app/evaluation/usage.py",
    "app/repair/models.py",
    "app/repair/tools.py",
    "app/schemas/agent.py",
    "app/tools/build_tools.py",
    "app/tools/patch_tools.py",
    "app/tools/registry.py",
    "app/tools/repo.py",
    "app/workspace/build.py",
    "app/workspace/models.py",
    "app/workspace/patch.py",
    "evaluation/tasks_v2.json",
]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: dict) -> None:
    _write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _resolve_results_root(output_dir: str | Path) -> Path:
    path = Path(output_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _phase_dir(output_dir: str | Path) -> Path:
    return _resolve_results_root(output_dir) / PHASE45_DIR_NAME


def _source_snapshot() -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 4.5 实验要求 clean working tree；请先提交源码")
    source_hashes = _file_hashes(
        [PROJECT_ROOT / path for path in SOURCE_FILES]
    )
    hidden_hashes = _tree_hashes(
        PROJECT_ROOT / "evaluation" / "hidden_tests" / "v2"
    )
    return {
        "git_commit": git.head_commit(PROJECT_ROOT),
        "working_tree_clean": True,
        "source_file_hashes": source_hashes,
        "source_bundle_sha256": _tree_digest(source_hashes),
        "benchmark_tasks_sha256": source_hashes.get(
            "evaluation/tasks_v2.json"
        ),
        "hidden_tests_tree_sha256": _tree_digest(hidden_hashes),
    }


def _manifest(
    *,
    control_paths: dict[AgentProfile, list[Path]] | None = None,
    control_commit: str | None = None,
    control_snapshot: dict | None = None,
) -> dict:
    snapshot = _source_snapshot()
    if control_paths is not None:
        control = {
            **(control_snapshot or {}),
            "git_commit": control_commit,
            "working_tree_clean": True,
            "paths": {
                profile.value: [
                    path.relative_to(PROJECT_ROOT).as_posix()
                    for path in paths
                ]
                for profile, paths in control_paths.items()
            },
        }
        treatment = snapshot
    else:
        control = snapshot
        treatment = None
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": {
            "name": settings.LLM_MODEL,
            "temperature": 0,
        },
        "benchmark": "v2",
        "control": control,
        "treatment": treatment,
        "prompt_hashes": {
            profile.value: __import__("hashlib").sha256(
                build_system_prompt(PROFILE_CAPABILITIES[profile]).encode(
                    "utf-8"
                )
            ).hexdigest()
            for profile in PROFILES
        },
    }


def _completed_paths(directory: Path) -> list[Path]:
    candidates = []
    for path in directory.glob("*/report.json"):
        report = BenchmarkReport.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if (
            len(report.results) == 15
            and report.git_dirty is False
            and report.git_commit
        ):
            candidates.append((report.started_at, path, report.git_commit))
    candidates.sort()
    return candidates


async def _run_profile_set(
    *,
    phase_dir: Path,
    kind: str,
    reuse_existing: bool = False,
) -> tuple[dict[AgentProfile, list[Path]], dict[AgentProfile, list[BenchmarkReport]]]:
    tasks = load_tasks(suite="v2")
    paths: dict[AgentProfile, list[Path]] = defaultdict(list)
    reports: dict[AgentProfile, list[BenchmarkReport]] = defaultdict(list)
    if reuse_existing:
        for profile in PROFILES:
            candidates = _completed_paths(
                phase_dir / f"{kind}_runs" / f"variant_{profile.value}"
            )
            for _, path, _ in candidates:
                paths[profile].append(path)
                reports[profile].append(
                    BenchmarkReport.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                )
    for repetition in range(1, 4):
        for profile in PROFILES:
            if len(paths[profile]) >= repetition:
                print(
                    f"\n=== skip existing {kind} {profile.value} "
                    f"repetition {repetition}/3 ===",
                    flush=True,
                )
                continue
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            run_id = f"{kind}_{profile.value}_r{repetition:02d}_{stamp}"
            variant_dir = phase_dir / f"{kind}_runs" / f"variant_{profile.value}"
            print(
                f"\n=== context lifecycle {kind} {profile.value} "
                f"repetition {repetition}/3 ===",
                flush=True,
            )
            runner = BenchmarkRunner(
                tasks,
                output_dir=variant_dir,
                run_id=run_id,
                profile=profile,
            )
            report, path = await runner.run()
            paths[profile].append(path)
            reports[profile].append(report)
    return dict(paths), dict(reports)


def _control_paths(phase_dir: Path) -> dict[AgentProfile, list[Path]]:
    result = {}
    for profile in PROFILES:
        candidates = _completed_paths(
            phase_dir / "control_runs" / f"variant_{profile.value}"
        )
        if len(candidates) != 3:
            raise RuntimeError(
                f"Phase 4.5 Control {profile.value} 需要 3 个完整 clean run，"
                f"实际 {len(candidates)}"
            )
        result[profile] = [item[1] for item in candidates]
    return result


def _treatment_paths(phase_dir: Path) -> dict[AgentProfile, list[Path]]:
    result = {}
    for profile in PROFILES:
        candidates = _completed_paths(
            phase_dir / "treatment_runs" / f"variant_{profile.value}"
        )
        if len(candidates) != 3:
            raise RuntimeError(
                f"Phase 4.5 Treatment {profile.value} 需要 3 个完整 clean run，"
                f"实际 {len(candidates)}"
            )
        result[profile] = [item[1] for item in candidates]
    return result


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, math.ceil(len(ordered) * fraction) - 1),
    )
    return ordered[index]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _calls(reports: list[BenchmarkReport]) -> list[ContextCallTelemetry]:
    return [
        call
        for report in reports
        for result in report.results
        for call in result.context_telemetry.calls
    ]


def _messages(
    reports: list[BenchmarkReport],
) -> list[MessageLifecycleTelemetry]:
    return [
        message
        for report in reports
        for result in report.results
        for message in result.context_telemetry.messages
    ]


def _normalized_source(source: str) -> str:
    if source in CI_TOOL_NAMES:
        return "ci_tool_chars"
    return {
        "read_file": "read_file_chars",
        "run_tests": "test_output_chars",
        "apply_patch": "patch_output_chars",
        "create_repair_plan": "planner_chars",
        "system": "system_message_chars",
        "user_task": "user_message_chars",
        "assistant": "assistant_message_chars",
        "assistant_tool_call": "tool_call_chars",
    }.get(source, "other_chars")


def _context_summary(reports: list[BenchmarkReport]) -> dict:
    calls = _calls(reports)
    messages = _messages(reports)
    results = [result for report in reports for result in report.results]
    category_totals = {
        field: sum(getattr(call, field) for call in calls)
        for field in CATEGORY_FIELDS
    }
    exposure_by_category: dict[str, int] = defaultdict(int)
    raw_by_category: dict[str, int] = defaultdict(int)
    retained_by_category: dict[str, list[int]] = defaultdict(list)
    for message in messages:
        category = _normalized_source(message.source)
        exposure_by_category[category] += message.context_exposure_chars
        raw_by_category[category] += message.raw_chars
        retained_by_category[category].append(message.retained_call_count)
    prompt_values = [
        call.provider_prompt_tokens
        for call in calls
        if call.provider_prompt_tokens is not None
    ]
    return {
        "task_runs": len(results),
        "call_count": len(calls),
        "provider_prompt_tokens": sum(prompt_values),
        "average_prompt_tokens_per_call": _mean(prompt_values),
        "median_prompt_tokens_per_call": (
            statistics.median(prompt_values) if prompt_values else None
        ),
        "p95_prompt_tokens_per_call": _percentile(prompt_values, 0.95),
        "max_prompt_tokens_per_call": max(prompt_values) if prompt_values else 0,
        "category_totals": category_totals,
        "category_exposure": dict(exposure_by_category),
        "category_raw": dict(raw_by_category),
        "category_avg_retained_calls": {
            category: (
                exposure_by_category[category] / raw_by_category[category]
                if raw_by_category[category]
                else 0.0
            )
            for category in retained_by_category
        },
        "raw_test_output_chars": sum(
            len(output.content)
            for result in results
            for output in result.context_telemetry.raw_test_outputs
        ),
        "raw_test_output_count": sum(
            len(result.context_telemetry.raw_test_outputs)
            for result in results
        ),
        "context_available": all(
            bool(result.context_telemetry.calls) for result in results
        ),
    }


def _growth_curve(reports: list[BenchmarkReport]) -> dict[str, dict[int, dict]]:
    grouped: dict[str, dict[int, list[ContextCallTelemetry]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for call in _calls(reports):
        grouped[call.profile][call.call_index].append(call)
    result = {}
    for profile, by_index in grouped.items():
        result[profile] = {}
        for index, calls in sorted(by_index.items()):
            prompts = [
                call.provider_prompt_tokens
                for call in calls
                if call.provider_prompt_tokens is not None
            ]
            result[profile][index] = {
                "count": len(calls),
                "average": _mean(prompts),
                "median": statistics.median(prompts) if prompts else None,
                "p95": _percentile(prompts, 0.95),
                "average_history_chars": _mean(
                    [call.total_history_chars for call in calls]
                ),
            }
    return result


def _task_context_rows(
    reports: list[BenchmarkReport],
) -> list[dict]:
    rows = []
    for report in reports:
        for result in report.results:
            calls = result.context_telemetry.calls
            messages = result.context_telemetry.messages
            prompts = [
                call.provider_prompt_tokens
                for call in calls
                if call.provider_prompt_tokens is not None
            ]
            rows.append(
                {
                    "task_id": result.task_id,
                    "profile": calls[0].profile if calls else "unknown",
                    "success": result.success,
                    "llm_calls": result.llm_calls,
                    "prompt_tokens": sum(prompts),
                    "max_prompt_tokens": max(prompts) if prompts else 0,
                    "max_history_chars": max(
                        (call.total_history_chars for call in calls),
                        default=0,
                    ),
                    "context_exposure_chars": sum(
                        message.context_exposure_chars
                        for message in messages
                    ),
                    "test_output_chars": sum(
                        call.test_output_chars for call in calls
                    ),
                    "patch_output_chars": sum(
                        call.patch_output_chars for call in calls
                    ),
                    "planner_chars": sum(
                        call.planner_chars for call in calls
                    ),
                    "ci_tool_chars": sum(
                        call.ci_tool_chars for call in calls
                    ),
                    "raw_test_output_chars": sum(
                        len(output.content)
                        for output in result.context_telemetry.raw_test_outputs
                    ),
                }
            )
    return rows


def _metrics(
    reports: dict[AgentProfile, list[BenchmarkReport]],
    paths: dict[AgentProfile, list[Path]],
) -> dict[str, dict]:
    result = {}
    for profile in PROFILES:
        aggregate = _aggregate_variant(
            profile,
            reports[profile],
            paths[profile],
            PROJECT_ROOT,
        )
        result[profile.value] = {
            "metrics": aggregate["metrics"],
            "tool_usage": aggregate["tool_usage"],
            "task_results": aggregate["task_results"],
        }
    return result


def _comparison(control: dict, treatment: dict) -> dict:
    keys = [
        "success_rate",
        "hard_success_rate",
        "hidden_test_pass_rate",
        "average_prompt_tokens",
        "average_completion_tokens",
        "average_tokens",
        "average_llm_calls",
        "average_agent_tool_calls",
        "average_duration",
    ]
    result = {}
    for profile in PROFILES:
        result[profile.value] = {}
        for key in keys:
            control_value = control[profile.value]["metrics"].get(key)
            treatment_value = treatment[profile.value]["metrics"].get(key)
            result[profile.value][key] = {
                "control": control_value,
                "treatment": treatment_value,
                "delta": (
                    treatment_value - control_value
                    if control_value is not None
                    and treatment_value is not None
                    else None
                ),
            }
    return result


def _risk_classification(comparison: dict) -> dict:
    result = {}
    for profile in PROFILES:
        name = profile.value
        prompt = comparison[name]["average_prompt_tokens"]
        success = comparison[name]["success_rate"]
        calls = comparison[name]["average_llm_calls"]
        prompt_pct = (
            prompt["delta"] / prompt["control"]
            if prompt["delta"] is not None and prompt["control"]
            else 0.0
        )
        calls_pct = (
            calls["delta"] / calls["control"]
            if calls["delta"] is not None and calls["control"]
            else 0.0
        )
        if prompt_pct <= -0.05 and success["delta"] < -0.05:
            classification = "context_loss"
        elif prompt_pct <= -0.05 and calls_pct >= 0.20:
            classification = "behavioral_shift"
        elif prompt_pct <= -0.05 and success["delta"] >= -0.05:
            classification = "efficient_win"
        elif abs(prompt_pct) <= 0.05:
            classification = "no_improvement"
        else:
            classification = "prompt_increase"
        result[name] = {
            "classification": classification,
            "prompt_token_delta_percent": prompt_pct,
            "llm_call_delta_percent": calls_pct,
            "success_delta": success["delta"],
        }
    return result


def _step_pressure(
    reports: dict[AgentProfile, list[BenchmarkReport]],
) -> dict:
    flat_reports = [
        report
        for profile_reports in reports.values()
        for report in profile_reports
    ]
    rows = [
        result
        for report in flat_reports
        for result in report.results
    ]
    return {
        "task_runs": len(rows),
        "calls_15_plus": sum(1 for result in rows if result.llm_calls >= 15),
        "calls_20": sum(1 for result in rows if result.llm_calls >= 20),
        "max_calls": max((result.llm_calls for result in rows), default=0),
        "average_calls": _mean([result.llm_calls for result in rows]),
        "profiles": {
            profile.value: {
                "task_runs": sum(
                    len(report.results) for report in reports[profile]
                ),
                "calls_15_plus": sum(
                    result.llm_calls >= 15
                    for report in reports[profile]
                    for result in report.results
                ),
                "calls_20": sum(
                    result.llm_calls >= 20
                    for report in reports[profile]
                    for result in report.results
                ),
                "max_calls": max(
                    (
                        result.llm_calls
                        for report in reports[profile]
                        for result in report.results
                    ),
                    default=0,
                ),
            }
            for profile in PROFILES
        },
    }


def _task_comparison(
    control: dict[AgentProfile, list[BenchmarkReport]],
    treatment: dict[AgentProfile, list[BenchmarkReport]],
) -> dict:
    result = {}
    for profile in PROFILES:
        control_rows = _task_context_rows(control[profile])
        treatment_rows = _task_context_rows(treatment[profile])
        control_by_task: dict[str, list[dict]] = defaultdict(list)
        treatment_by_task: dict[str, list[dict]] = defaultdict(list)
        for row in control_rows:
            control_by_task[row["task_id"]].append(row)
        for row in treatment_rows:
            treatment_by_task[row["task_id"]].append(row)
        result[profile.value] = {}
        for task_id in sorted(control_by_task):
            c = control_by_task[task_id]
            t = treatment_by_task[task_id]
            result[profile.value][task_id] = {
                "control": {
                    "runs": len(c),
                    "passes": sum(row["success"] for row in c),
                    "prompt_tokens": sum(row["prompt_tokens"] for row in c),
                    "llm_calls": sum(row["llm_calls"] for row in c),
                    "test_output_chars": sum(
                        row["test_output_chars"] for row in c
                    ),
                    "patch_output_chars": sum(
                        row["patch_output_chars"] for row in c
                    ),
                    "planner_chars": sum(
                        row["planner_chars"] for row in c
                    ),
                    "context_exposure_chars": sum(
                        row["context_exposure_chars"] for row in c
                    ),
                },
                "treatment": {
                    "runs": len(t),
                    "passes": sum(row["success"] for row in t),
                    "prompt_tokens": sum(row["prompt_tokens"] for row in t),
                    "llm_calls": sum(row["llm_calls"] for row in t),
                    "test_output_chars": sum(
                        row["test_output_chars"] for row in t
                    ),
                    "patch_output_chars": sum(
                        row["patch_output_chars"] for row in t
                    ),
                    "planner_chars": sum(
                        row["planner_chars"] for row in t
                    ),
                    "context_exposure_chars": sum(
                        row["context_exposure_chars"] for row in t
                    ),
                },
            }
    return result


def _build_payload(
    *,
    phase_dir: Path,
    manifest: dict,
    control_paths: dict[AgentProfile, list[Path]],
    control_reports: dict[AgentProfile, list[BenchmarkReport]],
    treatment_paths: dict[AgentProfile, list[Path]],
    treatment_reports: dict[AgentProfile, list[BenchmarkReport]],
) -> dict:
    control_metrics = _metrics(control_reports, control_paths)
    treatment_metrics = _metrics(treatment_reports, treatment_paths)
    comparison = _comparison(control_metrics, treatment_metrics)
    payload = {
        "schema_version": 1,
        "phase": "4.5",
        "experiment_id": PHASE45_DIR_NAME,
        "started_at": manifest["generated_at"],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.LLM_MODEL,
        "benchmark": "v2",
        "source": manifest,
        "control": {
            "metrics": control_metrics,
            "context": {
                profile.value: _context_summary(control_reports[profile])
                for profile in PROFILES
            },
            "growth_curve": _growth_curve(
                [
                    report
                    for reports in control_reports.values()
                    for report in reports
                ]
            ),
            "task_rows": {
                profile.value: _task_context_rows(control_reports[profile])
                for profile in PROFILES
            },
            "step_pressure": _step_pressure(control_reports),
        },
        "treatment": {
            "metrics": treatment_metrics,
            "context": {
                profile.value: _context_summary(treatment_reports[profile])
                for profile in PROFILES
            },
            "growth_curve": _growth_curve(
                [
                    report
                    for reports in treatment_reports.values()
                    for report in reports
                ]
            ),
            "task_rows": {
                profile.value: _task_context_rows(treatment_reports[profile])
                for profile in PROFILES
            },
            "step_pressure": _step_pressure(treatment_reports),
        },
        "comparison": comparison,
        "task_comparison": _task_comparison(
            control_reports,
            treatment_reports,
        ),
        "risk_classification": _risk_classification(comparison),
    }
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "phase4_5_report.json", payload)
    _write(
        phase_dir / "context_lifecycle_audit.md",
        render_lifecycle_audit(payload["control"]),
    )
    _write(
        phase_dir / "context_lifecycle_design.md",
        render_lifecycle_design(),
    )
    _write(
        phase_dir / "phase4_5_report.md",
        render_lifecycle_report(payload),
    )
    return payload


def render_lifecycle_audit(control: dict) -> str:
    lines = [
        "# Phase 4.5 Context Lifecycle Audit",
        "",
        "Provider tokens are actual values. Message-category values are exact "
        "UTF-8 character counts and are not presented as token attribution.",
        "",
        "## Token Accounting",
        "",
        "| Profile | Task Runs | LLM Calls | Prompt Tokens | Avg Prompt/Call | "
        "P95 Prompt/Call | Max Prompt/Call |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for profile in PROFILES:
        values = control["context"][profile.value]
        lines.append(
            f"| {profile.value} | {values['task_runs']} | "
            f"{values['call_count']} | {values['provider_prompt_tokens']} | "
            f"{values['average_prompt_tokens_per_call'] or 0:.1f} | "
            f"{values['p95_prompt_tokens_per_call']:.1f} | "
            f"{values['max_prompt_tokens_per_call']} |"
        )
    lines.extend(
        [
            "",
            "## Category Chars and Exposure",
            "",
            "Exposure equals raw message chars multiplied by the number of "
            "later prompts that retain the same message.",
            "",
            "| Profile | Category | Raw Chars | Context Exposure Chars | Avg Retained Calls |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        values = control["context"][profile.value]
        for category in CATEGORY_FIELDS:
            lines.append(
                f"| {profile.value} | `{category}` | "
                f"{values['category_raw'].get(category, 0)} | "
                f"{values['category_exposure'].get(category, 0)} | "
                f"{values['category_avg_retained_calls'].get(category) or 0:.2f} |"
            )
    lines.extend(
        [
            "",
            "## Prompt Growth Curve",
            "",
            "| Profile | Call | Samples | Avg Prompt | Median | P95 | Avg History Chars |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        for index, values in control["growth_curve"].get(
            profile.value,
            {},
        ).items():
            lines.append(
                f"| {profile.value} | {index} | {values['count']} | "
                f"{values['average'] or 0:.1f} | {values['median'] or 0:.1f} | "
                f"{values['p95']:.1f} | "
                f"{values['average_history_chars'] or 0:.1f} |"
            )
    rows = [
        row
        for profile_rows in control["task_rows"].values()
        for row in profile_rows
    ]
    lines.extend(
        [
            "",
            "## Largest Prompt-Context Task Runs",
            "",
            "| Profile | Task | Prompt Tokens | Calls | Max Prompt | "
            "Max History Chars | Exposure |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(
        rows,
        key=lambda item: item["prompt_tokens"],
        reverse=True,
    )[:10]:
        lines.append(
            f"| {row['profile']} | {row['task_id']} | "
            f"{row['prompt_tokens']} | {row['llm_calls']} | "
            f"{row['max_prompt_tokens']} | {row['max_history_chars']} | "
            f"{row['context_exposure_chars']} |"
        )
    lines.extend(
        [
            "",
            "## Raw Test and Patch Accounting",
            "",
            "| Profile | Raw Test Outputs | Raw Test Chars | "
            "Injected Test Chars | Patch Chars | Planner Chars |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        values = control["context"][profile.value]
        lines.append(
            f"| {profile.value} | {values['raw_test_output_count']} | "
            f"{values['raw_test_output_chars']} | "
            f"{values['category_totals']['test_output_chars']} | "
            f"{values['category_totals']['patch_output_chars']} | "
            f"{values['category_totals']['planner_chars']} |"
        )
    lines.extend(
        [
            "",
            "## Step Pressure",
            "",
            "| Profile | Task Runs | Calls >= 15 | Calls >= 20 | Max Calls |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        values = control["step_pressure"]["profiles"][profile.value]
        lines.append(
            f"| {profile.value} | {values['task_runs']} | "
            f"{values['calls_15_plus']} | {values['calls_20']} | "
            f"{values['max_calls']} |"
        )
    planner = control["context"]["planner"]
    full = control["context"]["full"]
    lines.extend(
        [
            "",
            "## Audit Findings",
            "",
            f"- Planner executed {planner['call_count']} LLM calls across "
            f"{planner['task_runs']} task-runs and retained "
            f"{planner['category_exposure']['planner_chars']} planner-result "
            "char-exposures.",
            f"- Full retained {full['category_exposure']['test_output_chars']} "
            "test-result char-exposures and "
            f"{full['category_exposure']['patch_output_chars']} patch-result "
            "char-exposures.",
            "- Selected optimization 1: compact older successful exploration "
            "results while retaining the latest six.",
            "- Selected optimization 2: retain the latest test result and compact "
            "older test results to deterministic failure metadata.",
            "- Selected optimization 3: retain the active plan and compact "
            "superseded plan results and their duplicated tool-call arguments.",
            "- Patch success is not selected because its measured exposure is only "
            "15,240 chars for Full and 99,231 chars for Planner, which is much "
            "smaller than CI/source/test lifecycle cost.",
            "- The user task, current repair outcome, latest relevant failure, "
            "latest source-read locations, and active plan semantics must remain.",
            "- No history is removed merely for age; compaction is selected by "
            "message source and deterministic content shape.",
            "",
        ]
    )
    return "\n".join(lines)


def render_lifecycle_design() -> str:
    return """# Context Lifecycle Design

## Accounting First

Every LLM call records provider usage, exact per-category message characters,
the active repair round, and message count. Message lifecycle records retain
introduced call, retained call count, and cumulative char exposure.

Provider usage is authoritative for tokens. Category token values are never
invented; categories remain exact characters.

## Lifecycle Classes

- Persistent: user task, constraints, current plan, latest failure.
- Working: current CI locations, source reads, current test result.
- Ephemeral: old successful patch acknowledgement, obsolete test stdout,
  previous plan revisions, and superseded metadata.

## Treatment Contract

The model receives a derived history; persistent Agent state and execution
telemetry retain the original messages.

- Keep the latest six successful exploration results. Older CI, source,
  search, file-list, and Git-read results are replaced with deterministic
  compact markers.
- Keep the latest Maven result verbatim. Older results keep status, exit code,
  failed tests, relevant failure lines, duration, and original size.
- Keep the active repair plan and its latest tool-call arguments. Superseded
  plan results and duplicated older plan arguments are compacted.

Patch output is not compacted in this treatment because measured exposure was
small relative to exploration, test, and plan history.

## Safety

No recency-only message truncation is allowed. Raw Maven output remains in the
evaluation trace even when the model-visible result is compact. Patch failures
keep their Phase 3 structured detail. The active repair plan and current failure
must survive all compaction.
"""


def render_lifecycle_report(payload: dict) -> str:
    comparison = payload["comparison"]
    lines = [
        "# Phase 4.5 Context Lifecycle Report",
        "",
        f"- Model: `{payload['model']}`",
        f"- Benchmark: `{payload['benchmark']}`",
        f"- Control commit: `{payload['source']['control']['git_commit']}`",
        f"- Treatment commit: `{payload['source']['treatment']['git_commit']}`",
        "",
        "## A/B Results",
        "",
        "| Profile | Metric | Control | Treatment | Delta |",
        "|---|---|---:|---:|---:|",
    ]
    for profile in PROFILES:
        for label, key in (
            ("Success", "success_rate"),
            ("Hard Success", "hard_success_rate"),
            ("Hidden Pass", "hidden_test_pass_rate"),
            ("Prompt Tokens", "average_prompt_tokens"),
            ("Completion Tokens", "average_completion_tokens"),
            ("Total Tokens", "average_tokens"),
            ("LLM Calls", "average_llm_calls"),
            ("Agent Tool Calls", "average_agent_tool_calls"),
            ("Duration", "average_duration"),
        ):
            values = comparison[profile.value][key]
            if key.endswith("rate"):
                control_value = f"{values['control'] * 100:.1f}%"
                treatment_value = f"{values['treatment'] * 100:.1f}%"
                delta = f"{values['delta'] * 100:+.1f} pp"
            else:
                control_value = f"{values['control']:.1f}"
                treatment_value = f"{values['treatment']:.1f}"
                delta = f"{values['delta']:+.1f}"
            lines.append(
                f"| {profile.value} | {label} | {control_value} | "
                f"{treatment_value} | {delta} |"
            )
    lines.extend(
        [
            "",
            "## Per-Category Reduction",
            "",
            "| Profile | Category | Control Chars | Treatment Chars | Delta |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        control = payload["control"]["context"][profile.value]["category_totals"]
        treatment = payload["treatment"]["context"][profile.value][
            "category_totals"
        ]
        for category in CATEGORY_FIELDS:
            old = control.get(category, 0)
            new = treatment.get(category, 0)
            change = (new - old) / old if old else 0.0
            lines.append(
                f"| {profile.value} | `{category}` | {old} | {new} | "
                f"{change * 100:+.1f}% |"
            )
    lines.extend(
        [
            "",
            "## Exposure Reduction",
            "",
            "| Profile | Control Exposure | Treatment Exposure | Delta |",
            "|---|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        old = sum(
            payload["control"]["context"][profile.value][
                "category_exposure"
            ].values()
        )
        new = sum(
            payload["treatment"]["context"][profile.value][
                "category_exposure"
            ].values()
        )
        change = (new - old) / old if old else 0.0
        lines.append(
            f"| {profile.value} | {old} | {new} | {change * 100:+.1f}% |"
        )
    lines.extend(
        [
            "",
            "## Task-Level Changes",
            "",
            "| Profile | Task | Pass Control | Pass Treatment | "
            "Prompt Tokens Control | Prompt Tokens Treatment |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        for task_id, values in payload["task_comparison"][profile.value].items():
            c = values["control"]
            t = values["treatment"]
            lines.append(
                f"| {profile.value} | {task_id} | {c['passes']}/{c['runs']} | "
                f"{t['passes']}/{t['runs']} | {c['prompt_tokens']} | "
                f"{t['prompt_tokens']} |"
            )
    lines.extend(
        [
            "",
            "## Step Pressure",
            "",
            "| Profile | Phase | Calls >= 15 | Calls >= 20 | Max Calls |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for phase in ("control", "treatment"):
        for profile in PROFILES:
            values = payload[phase]["step_pressure"]["profiles"][
                profile.value
            ]
            lines.append(
                f"| {profile.value} | {phase} | {values['calls_15_plus']} | "
                f"{values['calls_20']} | {values['max_calls']} |"
            )
    lines.extend(["", "## Risk Classification", ""])
    for profile in PROFILES:
        values = payload["risk_classification"][profile.value]
        lines.append(
            f"- {profile.value}: `{values['classification']}`; prompt tokens "
            f"{values['prompt_token_delta_percent'] * 100:+.1f}%, LLM calls "
            f"{values['llm_call_delta_percent'] * 100:+.1f}%, success "
            f"{values['success_delta'] * 100:+.1f} pp."
        )
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            "The conformance of this result is summarized by the per-profile "
            "classification above. Context compaction is accepted only where "
            "prompt-token exposure falls without a material success regression.",
            "",
        ]
    )
    return "\n".join(lines)


def regenerate_lifecycle_audit(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    phase_dir = _phase_dir(output_dir)
    paths = _control_paths(phase_dir)
    reports = {
        profile: _load_reports(profile_paths)
        for profile, profile_paths in paths.items()
    }
    control = {
        "metrics": _metrics(reports, paths),
        "context": {
            profile.value: _context_summary(reports[profile])
            for profile in PROFILES
        },
        "growth_curve": _growth_curve(
            [
                report
                for profile_reports in reports.values()
                for report in profile_reports
            ]
        ),
        "task_rows": {
            profile.value: _task_context_rows(reports[profile])
            for profile in PROFILES
        },
        "step_pressure": _step_pressure(reports),
    }
    audit = phase_dir / "context_lifecycle_audit.md"
    _write(audit, render_lifecycle_audit(control))
    _write(
        phase_dir / "context_lifecycle_design.md",
        render_lifecycle_design(),
    )
    return audit


async def run_lifecycle_controls(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    phase_dir = _phase_dir(output_dir)
    phase_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest()
    _write_json(phase_dir / "source_manifest.json", manifest)
    paths, reports = await _run_profile_set(phase_dir=phase_dir, kind="control")
    control = {
        "metrics": _metrics(reports, paths),
        "context": {
            profile.value: _context_summary(reports[profile])
            for profile in PROFILES
        },
        "growth_curve": _growth_curve(
            [
                report
                for profile_reports in reports.values()
                for report in profile_reports
            ]
        ),
        "task_rows": {
            profile.value: _task_context_rows(reports[profile])
            for profile in PROFILES
        },
        "step_pressure": _step_pressure(reports),
    }
    _write(
        phase_dir / "context_lifecycle_audit.md",
        render_lifecycle_audit(control),
    )
    _write(
        phase_dir / "context_lifecycle_design.md",
        render_lifecycle_design(),
    )
    audit = phase_dir / "context_lifecycle_audit.md"
    print(f"\nPhase 4.5 audit: {audit}")
    return audit


async def run_lifecycle_treatment(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    phase_dir = _phase_dir(output_dir)
    control_paths = _control_paths(phase_dir)
    control_reports = {
        profile: _load_reports(paths)
        for profile, paths in control_paths.items()
    }
    control_commits = {
        report.git_commit
        for reports in control_reports.values()
        for report in reports
    }
    if len(control_commits) != 1:
        raise RuntimeError(
            f"Control runs 必须来自同一 commit，实际 {control_commits}"
        )
    previous_manifest = json.loads(
        (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
    )
    existing_treatment = any(
        _completed_paths(
            phase_dir / "treatment_runs" / f"variant_{profile.value}"
        )
        for profile in PROFILES
    )
    if existing_treatment:
        manifest = previous_manifest
        manifest["control"]["paths"] = {
            profile.value: [
                path.relative_to(PROJECT_ROOT).as_posix()
                for path in paths
            ]
            for profile, paths in control_paths.items()
        }
    else:
        manifest = _manifest(
            control_paths=control_paths,
            control_commit=next(iter(control_commits)),
            control_snapshot=previous_manifest.get("control"),
        )
    _write_json(phase_dir / "source_manifest.json", manifest)
    treatment_paths, treatment_reports = await _run_profile_set(
        phase_dir=phase_dir,
        kind="treatment",
        reuse_existing=True,
    )
    payload = _build_payload(
        phase_dir=phase_dir,
        manifest=manifest,
        control_paths=control_paths,
        control_reports=control_reports,
        treatment_paths=treatment_paths,
        treatment_reports=treatment_reports,
    )
    report = phase_dir / "phase4_5_report.md"
    print(f"\nPhase 4.5 report: {report}")
    return report


def regenerate_lifecycle_report(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    phase_dir = _phase_dir(output_dir)
    experiment_path = phase_dir / "experiment.json"
    if experiment_path.exists():
        payload = json.loads(experiment_path.read_text(encoding="utf-8"))
    else:
        manifest = json.loads(
            (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
        )
        control_paths = _control_paths(phase_dir)
        treatment_paths = _treatment_paths(phase_dir)
        payload = _build_payload(
            phase_dir=phase_dir,
            manifest=manifest,
            control_paths=control_paths,
            control_reports={
                profile: _load_reports(paths)
                for profile, paths in control_paths.items()
            },
            treatment_paths=treatment_paths,
            treatment_reports={
                profile: _load_reports(paths)
                for profile, paths in treatment_paths.items()
            },
        )
    _write_json(phase_dir / "phase4_5_report.json", payload)
    report = phase_dir / "phase4_5_report.md"
    _write(report, render_lifecycle_report(payload))
    return report
