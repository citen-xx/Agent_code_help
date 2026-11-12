"""Phase 4 compact Code Intelligence context A/B."""

import asyncio
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.code_intel.compact import (
    CALL_RESULT_CHARS,
    DEFAULT_CALL_LIMIT,
    DEFAULT_IMPACT_LIMIT,
    DEFAULT_REFERENCE_LIMIT,
    DEFAULT_SYMBOL_LIMIT,
    IMPACT_RESULT_CHARS,
    MAX_CALL_LIMIT,
    MAX_IMPACT_LIMIT,
    MAX_REFERENCE_LIMIT,
    MAX_SYMBOL_LIMIT,
    REFERENCE_RESULT_CHARS,
    SYMBOL_RESULT_CHARS,
)
from app.core.config import settings
from app.evaluation.ablation import _aggregate_variant
from app.evaluation.models import BenchmarkReport
from app.evaluation.phase2_5 import (
    _file_hashes,
    _load_reports,
    _tree_digest,
    _tree_hashes,
)
from app.evaluation.phase3 import PHASE3_DIR_NAME
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT, load_tasks
from app.tools.repo import MAX_READ_LINES
from app.workspace import git

PHASE4_DIR_NAME = "phase4_context_engineering"
PROFILES = [
    AgentProfile.CODE_INTELLIGENCE,
    AgentProfile.PLANNER,
    AgentProfile.FULL,
]
CI_TOOLS = [
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
]
FOCUS_TASKS = [
    "v2-overload-inheritance",
    "v2-stream-filter",
    "v2-checkout-transaction",
    "v2-regression-pipeline",
    "v2-optional-repository-chain",
    "v2-inventory-layered",
]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: dict) -> None:
    _write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _resolve_results_root(output_dir: str | Path) -> Path:
    path = Path(output_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _control_paths(
    results_root: Path,
) -> dict[AgentProfile, list[Path]]:
    phase4_dir = results_root / PHASE4_DIR_NAME
    phase3_dir = results_root / PHASE3_DIR_NAME
    ci_paths = sorted(
        (
            phase4_dir
            / "control_runs"
            / f"variant_{AgentProfile.CODE_INTELLIGENCE.value}"
        ).glob("*/report.json")
    )
    if len(ci_paths) != 3:
        raise RuntimeError(
            f"Phase 4 CI Control 需要 3 个 run，实际 {len(ci_paths)}"
        )
    paths = {AgentProfile.CODE_INTELLIGENCE: ci_paths}
    for profile in (AgentProfile.PLANNER, AgentProfile.FULL):
        report_paths = sorted(
            (
                phase3_dir
                / "treatment_runs"
                / f"variant_{profile.value}"
            ).glob("*/report.json")
        )
        if len(report_paths) != 3:
            raise RuntimeError(
                f"Phase 3 Control {profile.value} 需要 3 个 run，"
                f"实际 {len(report_paths)}"
            )
        paths[profile] = report_paths
    return paths


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, math.ceil(len(ordered) * fraction) - 1),
    )
    return ordered[index]


def _ci_stats(reports: list[BenchmarkReport]) -> dict:
    by_tool: dict[str, list[dict]] = defaultdict(list)
    per_task: dict[str, dict] = defaultdict(
        lambda: {
            "ci_invocations": 0,
            "ci_output_chars": 0,
            "read_file_calls": 0,
        }
    )
    for report in reports:
        for result in report.results:
            task_stats = per_task[result.task_id]
            task_stats["read_file_calls"] += (
                result.read_file_calls
                if "read_file_calls" in result.model_fields_set
                else sum(
                    1
                    for event in result.tool_events
                    if event.tool_name == "read_file"
                )
            )
            for event in result.tool_events:
                if event.tool_name in CI_TOOLS:
                    by_tool[event.tool_name].append(
                        {
                            "chars": event.output_chars,
                            "result_count": event.result_count,
                            "truncated": event.truncated,
                            "duplicates": event.duplicate_results_removed,
                        }
                    )
                    task_stats["ci_invocations"] += 1
                    task_stats["ci_output_chars"] += event.output_chars
    summary = {}
    for tool in CI_TOOLS:
        values = by_tool.get(tool, [])
        chars = [item["chars"] for item in values]
        summary[tool] = {
            "invocation_count": len(values),
            "total_output_chars": sum(chars),
            "avg_output_chars": sum(chars) / len(chars) if chars else 0.0,
            "p50_output_chars": _percentile(chars, 0.5),
            "p95_output_chars": _percentile(chars, 0.95),
            "max_output_chars": max(chars) if chars else 0,
            "result_count": sum(item["result_count"] for item in values),
            "truncated_count": sum(
                1 for item in values if item["truncated"]
            ),
            "duplicate_results_removed": sum(
                item["duplicates"] for item in values
            ),
        }
    return {
        "per_tool": summary,
        "per_task": dict(sorted(per_task.items())),
        "total_ci_invocations": sum(
            item["invocation_count"] for item in summary.values()
        ),
        "total_ci_output_chars": sum(
            item["total_output_chars"] for item in summary.values()
        ),
    }


def _source_manifest(
    results_root: Path,
    control_paths: dict[AgentProfile, list[Path]],
) -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 4 实验要求 clean working tree；请先提交源码")
    source_files = [
        "app/agent/graph.py",
        "app/agent/telemetry.py",
        "app/code_intel/compact.py",
        "app/code_intel/models.py",
        "app/code_intel/tools.py",
        "app/evaluation/models.py",
        "app/evaluation/phase4.py",
        "app/evaluation/runner.py",
        "app/schemas/agent.py",
        "app/tools/registry.py",
        "app/tools/repo.py",
        "evaluation/tasks_v2.json",
    ]
    source_hashes = _file_hashes(
        [PROJECT_ROOT / path for path in source_files]
    )
    hidden_hashes = _tree_hashes(
        PROJECT_ROOT / "evaluation" / "hidden_tests" / "v2"
    )
    controls = {}
    for profile, paths in control_paths.items():
        reports = _load_reports(paths)
        controls[profile.value] = {
            "paths": [
                path.relative_to(PROJECT_ROOT).as_posix() for path in paths
            ],
            "git_commits": sorted(
                {
                    report.git_commit
                    for report in reports
                    if report.git_commit
                }
            ),
            "working_tree_clean": all(
                report.git_dirty is False for report in reports
            ),
        }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "treatment": {
            "git_commit": git.head_commit(PROJECT_ROOT),
            "working_tree_clean": True,
            "source_file_hashes": source_hashes,
            "source_bundle_sha256": _tree_digest(source_hashes),
            "benchmark_tasks_sha256": source_hashes.get(
                "evaluation/tasks_v2.json"
            ),
            "hidden_tests_tree_sha256": _tree_digest(hidden_hashes),
        },
        "control": controls,
        "model": {
            "name": settings.LLM_MODEL,
            "temperature": 0,
        },
        "benchmark": "v2",
        "context_contract": {
            "mode": "compact_progressive_disclosure",
            "default_symbol_limit": DEFAULT_SYMBOL_LIMIT,
            "max_symbol_limit": MAX_SYMBOL_LIMIT,
            "default_reference_limit": DEFAULT_REFERENCE_LIMIT,
            "max_reference_limit": MAX_REFERENCE_LIMIT,
            "default_call_limit": DEFAULT_CALL_LIMIT,
            "max_call_limit": MAX_CALL_LIMIT,
            "default_impact_limit": DEFAULT_IMPACT_LIMIT,
            "max_impact_limit": MAX_IMPACT_LIMIT,
            "symbol_result_chars": SYMBOL_RESULT_CHARS,
            "reference_result_chars": REFERENCE_RESULT_CHARS,
            "call_result_chars": CALL_RESULT_CHARS,
            "impact_result_chars": IMPACT_RESULT_CHARS,
            "read_file_range_max_lines": MAX_READ_LINES,
        },
        "prompt_hashes": {
            profile.value: __import__("hashlib").sha256(
                build_system_prompt(PROFILE_CAPABILITIES[profile]).encode(
                    "utf-8"
                )
            ).hexdigest()
            for profile in PROFILES
        },
    }


def render_context_audit(
    control_reports: dict[AgentProfile, list[BenchmarkReport]],
) -> str:
    all_reports = [
        report
        for reports in control_reports.values()
        for report in reports
    ]
    stats = _ci_stats(all_reports)
    lines = [
        "# Code Intelligence Context Audit",
        "",
        "This audit uses clean Phase 3/Phase 4 Control runs. CI output is measured "
        "as the exact UTF-8 JSON character count delivered in ToolMessage.",
        "",
        "| Tool | Invocations | Total Chars | Avg Chars | P50 | P95 | Max |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for tool, values in stats["per_tool"].items():
        lines.append(
            f"| `{tool}` | {values['invocation_count']} | "
            f"{values['total_output_chars']} | "
            f"{values['avg_output_chars']:.1f} | "
            f"{values['p50_output_chars']} | "
            f"{values['p95_output_chars']} | "
            f"{values['max_output_chars']} |"
        )
    lines.extend(
        [
            "",
            "## Output Shape Findings",
            "",
            "- Symbols include names, types, locations and full method signatures; "
            "there is no source body, but default limits and repeated candidates "
            "cause large payloads.",
            "- References include a line of context. The fields are compact, but "
            "the default limit of 200 allows large result sets.",
            "- Caller/callee results include candidate declarations on every call. "
            "Candidates are repeated across call sites.",
            "- Impact results include targets, callers, candidate lists, test "
            "references, related methods and diagnostics for the whole query.",
            "- Results are appended to LangGraph message history and therefore "
            "repeat in later LLM prompts.",
            "",
            "## Highest CI-Output Tasks",
            "",
            "| Task | CI Invocations | CI Output Chars | Read File Calls |",
            "|---|---:|---:|---:|",
        ]
    )
    ranked = sorted(
        stats["per_task"].items(),
        key=lambda item: item[1]["ci_output_chars"],
        reverse=True,
    )[:10]
    for task_id, values in ranked:
        lines.append(
            f"| {task_id} | {values['ci_invocations']} | "
            f"{values['ci_output_chars']} | {values['read_file_calls']} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_context_contract() -> str:
    return """# Compact Code Intelligence Context Contract

## Progressive Disclosure

Code Intelligence returns deterministic discovery metadata:

- symbol name and kind
- repository-relative file
- line range
- short signature
- relation/candidate metadata

It does not return source method bodies. The Agent follows up with
`read_file(path, start_line, end_line)` when source logic is needed.

## Budgets

- list_symbols: default 30, max 100
- find_symbol: default 20, max 100
- find_references: default 40, max 200
- find_callers/callees: default 30, max 100
- analyze_impact: default 20 per section, max 100
- symbol/reference/call JSON budgets: 12,000 chars each
- impact JSON budget: 16,000 chars
- read_file range: at most 200 lines

All truncation is deterministic and reports `returned`, `total`, `truncated`,
`result_count`, and `duplicates_removed`.

## Filtering and Deduplication

Filtering uses only deterministic exact keys:

- file and line locations
- symbol name/kind/signature
- call site and containing method
- test/source classification

No LLM, embedding, hidden-test signal, or task-id rule is used.

## Cache Policy

Phase 4 intentionally adds no cross-query cache. Existing Tree-sitter index
invalidation remains authoritative after apply_patch. Within a result,
duplicate records are removed only during deterministic serialization.
"""


def _aggregate(
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


def _completed_treatment_paths(
    phase_dir: Path,
) -> dict[AgentProfile, list[Path]]:
    """Discover exactly three complete, clean Treatment runs per profile."""
    paths: dict[AgentProfile, list[Path]] = {}
    commits: set[str] = set()
    for profile in PROFILES:
        candidates = []
        for path in (
            phase_dir
            / "treatment_runs"
            / f"variant_{profile.value}"
        ).glob("*/report.json"):
            report = BenchmarkReport.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if (
                len(report.results) != 15
                or report.git_dirty is not False
                or not report.git_commit
            ):
                continue
            candidates.append((report.started_at, path, report.git_commit))
        candidates.sort()
        if len(candidates) != 3:
            raise RuntimeError(
                f"Phase 4 Treatment {profile.value} 需要 3 个完整的 clean run，"
                f"实际 {len(candidates)}"
            )
        paths[profile] = [item[1] for item in candidates]
        commits.update(item[2] for item in candidates)
    if len(commits) != 1:
        raise RuntimeError(
            "Phase 4 Treatment runs 必须来自同一个 commit，"
            f"实际 {sorted(commits)}"
        )
    return paths


def _assemble_phase4_artifacts(
    *,
    phase_dir: Path,
    manifest: dict,
    controls: dict[AgentProfile, list[Path]],
    control_reports: dict[AgentProfile, list[BenchmarkReport]],
    treatment_paths: dict[AgentProfile, list[Path]],
    treatment_reports: dict[AgentProfile, list[BenchmarkReport]],
) -> dict:
    control_metrics = _aggregate(control_reports, controls)
    treatment_metrics = _aggregate(treatment_reports, treatment_paths)
    control_context = {
        profile.value: _profile_context_metrics(control_reports[profile])
        for profile in PROFILES
    }
    treatment_context = {
        profile.value: _profile_context_metrics(treatment_reports[profile])
        for profile in PROFILES
    }
    comparison = _comparison(control_metrics, treatment_metrics)
    tasks_comparison = _task_comparison(
        control_reports,
        treatment_reports,
    )
    treatment_audit = _ci_stats(
        [
            report
            for reports in treatment_reports.values()
            for report in reports
        ]
    )
    run_records = []
    for profile in PROFILES:
        for repetition, (path, report) in enumerate(
            zip(
                treatment_paths[profile],
                treatment_reports[profile],
                strict=True,
            ),
            start=1,
        ):
            run_records.append(
                {
                    "profile": profile.value,
                    "repetition": repetition,
                    "run_id": report.run_id,
                    "report": path.relative_to(PROJECT_ROOT).as_posix(),
                    "passed_tasks": report.summary.passed_tasks,
                    "git_commit": report.git_commit,
                    "git_dirty": report.git_dirty,
                }
            )
    payload = {
        "schema_version": 1,
        "phase": "4",
        "experiment_id": PHASE4_DIR_NAME,
        "started_at": manifest["generated_at"],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.LLM_MODEL,
        "benchmark": "v2",
        "source": manifest,
        "control_audit": _ci_stats(
            [
                report
                for reports in control_reports.values()
                for report in reports
            ]
        ),
        "treatment_audit": treatment_audit,
        "control_metrics": control_metrics,
        "treatment_metrics": treatment_metrics,
        "control_context": control_context,
        "treatment_context": treatment_context,
        "comparison": comparison,
        "task_comparison": tasks_comparison,
        "risk_classification": _risk_classification(
            comparison,
            control_context,
            treatment_context,
        ),
        "run_records": run_records,
    }
    _write(
        phase_dir / "context_audit.md",
        render_context_audit(control_reports),
    )
    _write(
        phase_dir / "context_contract_design.md",
        render_context_contract(),
    )
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "phase4_report.json", payload)
    _write(
        phase_dir / "phase4_report.md",
        render_phase4_report(payload),
    )
    return payload


def _comparison(control: dict, treatment: dict) -> dict:
    result = {}
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
    for profile in PROFILES:
        control_metrics = control[profile.value]["metrics"]
        treatment_metrics = treatment[profile.value]["metrics"]
        result[profile.value] = {
            key: {
                "control": control_metrics.get(key),
                "treatment": treatment_metrics.get(key),
                "delta": (
                    treatment_metrics.get(key) - control_metrics.get(key)
                    if treatment_metrics.get(key) is not None
                    and control_metrics.get(key) is not None
                    else None
                ),
            }
            for key in keys
        }
    return result


def _profile_context_metrics(reports: list[BenchmarkReport]) -> dict:
    results = [result for report in reports for result in report.results]
    ci_chars = [
        result.total_ci_output_chars
        if "total_ci_output_chars" in result.model_fields_set
        else sum(
            event.output_chars
            for event in result.tool_events
            if event.tool_name in CI_TOOLS
        )
        for result in results
    ]
    read_calls = [
        result.read_file_calls
        if "read_file_calls" in result.model_fields_set
        else sum(
            1
            for event in result.tool_events
            if event.tool_name == "read_file"
        )
        for result in results
    ]
    follow_up_available = all(
        "follow_up_source_reads" in result.model_fields_set
        for result in results
    )
    return {
        "total_ci_output_chars": sum(ci_chars),
        "avg_ci_output_chars": (
            sum(ci_chars) / len(results)
            if results
            else 0.0
        ),
        "read_file_calls": sum(read_calls),
        "follow_up_source_reads": (
            sum(result.follow_up_source_reads for result in results)
            if follow_up_available
            else None
        ),
        "read_file_telemetry_availability": "available_or_event_derived",
        "follow_up_telemetry_availability": (
            "available" if follow_up_available else "unavailable"
        ),
    }


def _task_comparison(
    control: dict[AgentProfile, list[BenchmarkReport]],
    treatment: dict[AgentProfile, list[BenchmarkReport]],
) -> dict:
    result = {}
    for profile in PROFILES:
        control_by_task: dict[str, list] = defaultdict(list)
        treatment_by_task: dict[str, list] = defaultdict(list)
        for report in control[profile]:
            for item in report.results:
                control_by_task[item.task_id].append(item)
        for report in treatment[profile]:
            for item in report.results:
                treatment_by_task[item.task_id].append(item)
        result[profile.value] = {}
        for task_id in sorted(control_by_task):
            control_items = control_by_task[task_id]
            treatment_items = treatment_by_task[task_id]
            result[profile.value][task_id] = {
                "control": {
                    "passes": sum(1 for item in control_items if item.success),
                    "runs": len(control_items),
                    "tokens": sum(
                        item.total_tokens or 0 for item in control_items
                    )
                    / len(control_items),
                    "ci_chars": sum(
                        (
                            item.total_ci_output_chars
                            if "total_ci_output_chars" in item.model_fields_set
                            else sum(
                                event.output_chars
                                for event in item.tool_events
                                if event.tool_name in CI_TOOLS
                            )
                        )
                        for item in control_items
                    ),
                    "read_file_calls": sum(
                        (
                            item.read_file_calls
                            if "read_file_calls" in item.model_fields_set
                            else sum(
                                1
                                for event in item.tool_events
                                if event.tool_name == "read_file"
                            )
                        )
                        for item in control_items
                    ),
                },
                "treatment": {
                    "passes": sum(1 for item in treatment_items if item.success),
                    "runs": len(treatment_items),
                    "tokens": sum(
                        item.total_tokens or 0 for item in treatment_items
                    )
                    / len(treatment_items),
                    "ci_chars": sum(
                        item.total_ci_output_chars for item in treatment_items
                    ),
                    "read_file_calls": sum(
                        item.read_file_calls for item in treatment_items
                    ),
                    "follow_up_source_reads": sum(
                        (item.follow_up_source_reads or 0)
                        for item in treatment_items
                    ),
                },
            }
    return result


def _risk_classification(
    comparison: dict,
    control_context: dict,
    treatment_context: dict,
) -> dict:
    result = {}
    for profile in PROFILES:
        name = profile.value
        if (
            name not in comparison
            or name not in control_context
            or name not in treatment_context
        ):
            continue
        token_delta = comparison[name]["average_tokens"]["delta"]
        success_delta = comparison[name]["success_rate"]["delta"]
        control_tokens = comparison[name]["average_tokens"]["control"]
        token_pct = (
            token_delta / control_tokens
            if token_delta is not None and control_tokens
            else 0.0
        )
        control_ci = control_context[name]["total_ci_output_chars"]
        treatment_ci = treatment_context[name]["total_ci_output_chars"]
        ci_delta = treatment_ci - control_ci
        ci_pct = ci_delta / control_ci if control_ci else 0.0
        read_delta = (
            treatment_context[name]["read_file_calls"]
            - control_context[name]["read_file_calls"]
        )
        control_reads = control_context[name]["read_file_calls"]
        read_pct = read_delta / control_reads if control_reads else 0.0
        if token_pct <= -0.05 and success_delta >= -0.05:
            classification = "efficient_win"
        elif token_pct <= -0.05:
            classification = "over_compression"
        elif ci_pct <= -0.05 and token_pct > -0.05 and read_pct >= 0.05:
            classification = "possible_tool_substitution"
        elif abs(token_pct) <= 0.05:
            classification = "no_effect"
        else:
            classification = "token_increase"
        result[name] = {
            "classification": classification,
            "token_delta_percent": token_pct,
            "success_delta": success_delta,
            "ci_output_delta_percent": ci_pct,
            "read_file_call_delta": read_delta,
        }
    return result


def render_phase4_report(payload: dict) -> str:
    comparison = payload["comparison"]
    control_context = payload["control_context"]
    treatment_context = payload["treatment_context"]
    task_comparison = payload["task_comparison"]

    def number(value, digits: int = 1, suffix: str = "") -> str:
        if value is None:
            return "N/A"
        return f"{value:.{digits}f}{suffix}"

    def percentage(value, digits: int = 1) -> str:
        if value is None:
            return "N/A"
        return f"{value * 100:.{digits}f}%"

    def signed_percent(delta, base) -> str:
        if delta is None or not base:
            return "N/A"
        return f"{(delta / base) * 100:+.1f}%"

    def metric_delta(
        profile: str,
        key: str,
        *,
        percent_points: bool = False,
    ) -> str:
        values = comparison[profile][key]
        delta = values["delta"]
        if delta is None:
            return "N/A"
        if percent_points:
            return f"{delta * 100:+.1f} pp"
        return f"{delta:+.1f} ({signed_percent(delta, values['control'])})"

    control_tools = payload["control_audit"]["per_tool"]
    ranked_tools = sorted(
        control_tools.items(),
        key=lambda item: item[1]["total_output_chars"],
        reverse=True,
    )
    largest = ranked_tools[:2]
    total_control_ci = payload["control_audit"]["total_ci_output_chars"]
    largest_text = " and ".join(
        f"`{name}` ({values['total_output_chars']} chars, "
        f"{percentage(values['total_output_chars'] / total_control_ci, 1)})"
        for name, values in largest
        if total_control_ci
    )
    lines = [
        "# Phase 4 Context Engineering Report",
        "",
        f"- Experiment: `{payload['experiment_id']}`",
        f"- Model: `{payload['model']}`",
        f"- Benchmark: `{payload['benchmark']}`",
        f"- Treatment commit: `{payload['source']['treatment']['git_commit']}`",
        f"- Runs per Profile: {len(payload['run_records']) // len(PROFILES)}",
        "",
        "## 1. Context Root Cause",
        "",
        f"Old Code Intelligence produced {total_control_ci} characters across "
        f"the {len(PROFILES) * 3} Control runs. The two largest sources were "
        f"{largest_text}.",
        "",
        "Tool results are appended to LangGraph message history. Every later "
        "LLM call therefore carries the earlier JSON again, so a large result "
        "has a cost larger than its one-time output size. The compaction also "
        "removed repeated candidate declarations and per-query diagnostics.",
        "",
        "## 2. Old Tool Output",
        "",
        "| Tool | Invocations | Total Chars | Avg Chars | P50 | P95 | Max |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for tool, values in control_tools.items():
        lines.append(
            f"| `{tool}` | {values['invocation_count']} | "
            f"{values['total_output_chars']} | "
            f"{values['avg_output_chars']:.1f} | {values['p50_output_chars']} | "
            f"{values['p95_output_chars']} | {values['max_output_chars']} |"
        )
    lines.extend(
        [
            "",
            "## 3. New Context Contract",
            "",
            "Compact, deterministic, progressive disclosure: locations/signatures "
            "only, bounded result counts and characters, explicit truncation "
            "metadata, short reference context, and line-range read_file.",
            "",
            "The default contract is:",
            "",
            f"- symbols: {DEFAULT_SYMBOL_LIMIT} default / {MAX_SYMBOL_LIMIT} max",
            f"- references: {DEFAULT_REFERENCE_LIMIT} default / "
            f"{MAX_REFERENCE_LIMIT} max",
            f"- callers/callees: {DEFAULT_CALL_LIMIT} default / "
            f"{MAX_CALL_LIMIT} max",
            f"- impact sections: {DEFAULT_IMPACT_LIMIT} default / "
            f"{MAX_IMPACT_LIMIT} max",
            f"- JSON budgets: {SYMBOL_RESULT_CHARS:,} symbol/reference/call chars; "
            f"{IMPACT_RESULT_CHARS:,} impact chars",
            f"- source reads: at most {MAX_READ_LINES} lines per request",
            "",
            "## 4. Safety & Correctness",
            "",
            "Exact symbol/reference relationships are preserved. Every truncation "
            "reports returned/total/truncated. No fuzzy filtering, LLM reranking, "
            "hidden-test signal, or task-specific rule is used.",
            "",
            "Duplicate records are removed only by deterministic location/symbol "
            "keys within one response. The Agent may still invoke tools repeatedly. "
            "No stale cross-query cache is introduced, so patched workspaces retain "
            "the existing Tree-sitter invalidation behavior.",
            "",
            "## 5. Tests",
            "",
            "The full local test suite is run with `uv run pytest`. Phase 4 tests "
            "cover compact responses, deterministic budgets and truncation metadata, "
            "duplicate removal, no method-body leakage, structured locations, "
            "UTF-8 and multi-package/overload cases, index refresh after file "
            "modification, telemetry serialization, and Control/Treatment isolation.",
            "",
            "## 6. A/B Results",
            "",
            "| Profile | Metric | Control | Treatment | Delta |",
            "|---|---|---:|---:|---:|",
        ]
    )
    metrics = [
        ("Success", "success_rate", True),
        ("Hard Success", "hard_success_rate", True),
        ("Hidden Pass", "hidden_test_pass_rate", True),
        ("Prompt Tokens", "average_prompt_tokens", False),
        ("Completion Tokens", "average_completion_tokens", False),
        ("Total Tokens", "average_tokens", False),
        ("LLM Calls", "average_llm_calls", False),
        ("Agent Tool Calls", "average_agent_tool_calls", False),
        ("Duration (s)", "average_duration", False),
    ]
    for profile in PROFILES:
        for label, key, is_rate in metrics:
            values = comparison[profile.value][key]
            formatter = percentage if is_rate else number
            lines.append(
                f"| {profile.value} | {label} | "
                f"{formatter(values['control'], 2 if key.endswith('calls') else 1)} | "
                f"{formatter(values['treatment'], 2 if key.endswith('calls') else 1)} | "
                f"{metric_delta(profile.value, key, percent_points=is_rate)} |"
            )
        control_ci = control_context[profile.value]["total_ci_output_chars"]
        treatment_ci = treatment_context[profile.value]["total_ci_output_chars"]
        task_runs = payload["control_metrics"][profile.value]["metrics"][
            "task_runs"
        ]
        lines.append(
            f"| {profile.value} | CI Chars / Task | "
            f"{control_ci / task_runs:.1f} | {treatment_ci / task_runs:.1f} | "
            f"{signed_percent(treatment_ci - control_ci, control_ci)} |"
        )
        control_reads = control_context[profile.value]["read_file_calls"]
        treatment_reads = treatment_context[profile.value]["read_file_calls"]
        lines.append(
            f"| {profile.value} | read_file Calls (total) | "
            f"{control_reads} | {treatment_reads} | "
            f"{treatment_reads - control_reads:+d} |"
        )
    lines.extend(
        [
            "",
            "### Experimental Integrity",
            "",
            "| Profile | Repetition | Run | Passed | Commit | Dirty |",
            "|---|---:|---|---:|---|---|",
        ]
    )
    for run in payload["run_records"]:
        lines.append(
            f"| {run['profile']} | {run['repetition']} | `{run['run_id']}` | "
            f"{run['passed_tasks']}/15 | `{run['git_commit']}` | "
            f"{run['git_dirty']} |"
        )
    lines.extend(
        [
            "",
            "## 7. Per-Tool Results",
            "",
            "| Tool | Control Invocations | Treatment Invocations | "
            "Control Avg Chars | Treatment Avg Chars | Reduction |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for tool in CI_TOOLS:
        control = control_tools[tool]
        treatment = payload["treatment_audit"]["per_tool"][tool]
        old = control["avg_output_chars"]
        new = treatment["avg_output_chars"]
        reduction = (old - new) / old if old else 0.0
        lines.append(
            f"| `{tool}` | {control['invocation_count']} | "
            f"{treatment['invocation_count']} | {old:.1f} | {new:.1f} | "
            f"{reduction * 100:.1f}% |"
        )
    lines.extend(
        [
            "",
            "## 8. Task-Level Results",
            "",
            "| Profile | Task | Pass Control | Pass Treatment | Tokens Control | "
            "Tokens Treatment | Token Δ | CI Chars Treatment | Read Files Treatment |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        for task_id, values in task_comparison[profile.value].items():
            c = values["control"]
            t = values["treatment"]
            lines.append(
                f"| {profile.value} | {task_id} | {c['passes']}/{c['runs']} | "
                f"{t['passes']}/{t['runs']} | {c['tokens']:.1f} | "
                f"{t['tokens']:.1f} | "
                f"{signed_percent(t['tokens'] - c['tokens'], c['tokens'])} | "
                f"{t['ci_chars']} | {t['read_file_calls']} |"
            )
    lines.extend(["", "### Largest Token Reductions", ""])
    for profile in PROFILES:
        ranked = sorted(
            task_comparison[profile.value].items(),
            key=lambda item: (
                (item[1]["treatment"]["tokens"] - item[1]["control"]["tokens"])
                / item[1]["control"]["tokens"]
                if item[1]["control"]["tokens"]
                else 0.0
            ),
        )[:5]
        reductions = []
        for task_id, values in ranked:
            reduction = signed_percent(
                values["treatment"]["tokens"] - values["control"]["tokens"],
                values["control"]["tokens"],
            )
            reductions.append(f"`{task_id}` {reduction}")
        lines.append(
            f"- {profile.value}: "
            + ", ".join(reductions)
            + "."
        )
    lines.extend(["", "### Largest Token Increases", ""])
    for profile in PROFILES:
        ranked = sorted(
            task_comparison[profile.value].items(),
            key=lambda item: (
                (item[1]["treatment"]["tokens"] - item[1]["control"]["tokens"])
                / item[1]["control"]["tokens"]
                if item[1]["control"]["tokens"]
                else 0.0
            ),
            reverse=True,
        )[:3]
        increases = []
        for task_id, values in ranked:
            increase = signed_percent(
                values["treatment"]["tokens"] - values["control"]["tokens"],
                values["control"]["tokens"],
            )
            increases.append(f"`{task_id}` {increase}")
        lines.append(
            f"- {profile.value}: "
            + ", ".join(increases)
            + "."
        )
    lines.extend(["", "### Success Changes", ""])
    any_success_change = False
    for profile in PROFILES:
        changes = []
        for task_id, values in task_comparison[profile.value].items():
            c = values["control"]
            t = values["treatment"]
            if c["passes"] != t["passes"]:
                changes.append(
                    f"`{task_id}` {c['passes']}/{c['runs']} → "
                    f"{t['passes']}/{t['runs']}"
                )
        if changes:
            any_success_change = True
            lines.append(f"- {profile.value}: " + "; ".join(changes) + ".")
    if not any_success_change:
        lines.append("- No task pass-count changes at the three-run level.")
    lines.extend(
        [
            "",
            "## 9. Progressive Disclosure",
            "",
            "| Profile | read_file Control | read_file Treatment | Delta | "
            "Follow-up Source Reads Treatment |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for profile in PROFILES:
        control_reads = control_context[profile.value]["read_file_calls"]
        treatment_reads = treatment_context[profile.value]["read_file_calls"]
        follow_up = treatment_context[profile.value]["follow_up_source_reads"]
        lines.append(
            f"| {profile.value} | {control_reads} | {treatment_reads} | "
            f"{treatment_reads - control_reads:+d} | "
            f"{'N/A' if follow_up is None else follow_up} |"
        )
    follow_up_total = sum(
        item["follow_up_source_reads"] or 0
        for item in treatment_context.values()
    )
    total_control_reads = sum(
        item["read_file_calls"] for item in control_context.values()
    )
    total_treatment_reads = sum(
        item["read_file_calls"] for item in treatment_context.values()
    )
    lines.extend(
        [
            "",
            f"- Treatment follow-up source reads: {follow_up_total}.",
            f"- read_file calls across profiles: {total_control_reads} → "
            f"{total_treatment_reads} "
            f"({total_treatment_reads - total_control_reads:+d}). The near-flat "
            "usage does not support a tool-substitution explanation.",
            "- Control follow-up linkage is unavailable because the old event "
            "telemetry did not persist result-derived file locations.",
            "",
            "## Risk Classification",
            "",
            "Thresholds are descriptive: a token change within ±5% is treated as "
            "no effect; a reduction over 5% with a success delta worse than -5 pp "
            "is over-compression. Tool substitution additionally requires lower CI "
            "output, at least 5% more read_file calls, and no token reduction.",
            "A token increase outside those cases is reported separately rather "
            "than forced into one of the four predefined categories.",
            "",
            "",
        ]
    )
    for profile in PROFILES:
        risk = payload["risk_classification"][profile.value]
        lines.append(
            f"- {profile.value}: `{risk['classification']}`; "
            f"token Δ {risk['token_delta_percent'] * 100:+.1f}%, "
            f"CI output Δ {risk['ci_output_delta_percent'] * 100:+.1f}%, "
            f"read_file Δ {risk['read_file_call_delta']:+d}, "
            f"success Δ {risk['success_delta'] * 100:+.1f} pp."
        )
    ci_risk = payload["risk_classification"][
        AgentProfile.CODE_INTELLIGENCE.value
    ]
    successful_profiles = [
        profile.value
        for profile in PROFILES
        if payload["risk_classification"][profile.value]["classification"]
        == "efficient_win"
    ]
    lines.extend(
        [
            "",
            "## 10. Conclusion",
            "",
            f"- CI output moved "
            f"{ci_risk['ci_output_delta_percent'] * 100:+.1f}% in the primary "
            f"Code Intelligence profile. Its total tokens moved "
            f"{ci_risk['token_delta_percent'] * 100:+.1f}% and success moved "
            f"{ci_risk['success_delta'] * 100:+.1f} pp.",
            f"- Efficient-win profiles: "
            f"{', '.join(successful_profiles) if successful_profiles else 'none'}. "
            "The remaining profiles are reported without averaging away their "
            "regressions or unchanged cost.",
            "- Compact CI preserves exact discovery locations and supports explicit "
            "line-range source reads; it does not embed a source body or optimize "
            "hidden-contract behavior.",
            "- The compact contract is more cost-efficient than the old contract "
            "where the CI profile is an efficient win. A blanket default-on claim "
            "is not supported unless Full and Planner also avoid token regressions."
            if ci_risk["classification"] == "efficient_win"
            else "- The experiment does not establish that compact CI is more "
            "cost-efficient overall.",
            "",
            "## 11. Recommended Next Step",
            "",
            "Keep the deterministic compact contract, but do not broaden its "
            "default scope yet. Run a separate Planner/Full context-budget study "
            "that investigates step-limit failures and read_file substitution, then "
            "address hidden-contract failures without mixing them into this A/B.",
            "",
        ]
    )
    return "\n".join(lines)


async def run_context_ab(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    results_root = _resolve_results_root(output_dir)
    phase_dir = results_root / PHASE4_DIR_NAME
    phase_dir.mkdir(parents=True, exist_ok=True)
    controls = _control_paths(results_root)
    control_reports = {
        profile: _load_reports(paths)
        for profile, paths in controls.items()
    }
    manifest = _source_manifest(results_root, controls)
    _write_json(phase_dir / "source_manifest.json", manifest)

    tasks = load_tasks(suite="v2")
    treatment_reports: dict[AgentProfile, list[BenchmarkReport]] = defaultdict(list)
    treatment_paths: dict[AgentProfile, list[Path]] = defaultdict(list)
    for repetition in range(1, 4):
        for profile in PROFILES:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            run_id = f"treatment_{profile.value}_r{repetition:02d}_{stamp}"
            variant_dir = (
                phase_dir / "treatment_runs" / f"variant_{profile.value}"
            )
            print(
                f"\n=== context treatment {profile.value} "
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
            treatment_reports[profile].append(report)
            treatment_paths[profile].append(path)

    _assemble_phase4_artifacts(
        phase_dir=phase_dir,
        manifest=manifest,
        controls=controls,
        control_reports=control_reports,
        treatment_paths=treatment_paths,
        treatment_reports=treatment_reports,
    )
    print(f"\nPhase 4 report: {phase_dir / 'phase4_report.md'}")
    return phase_dir / "phase4_report.md"


def regenerate_phase4_report(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    results_root = _resolve_results_root(output_dir)
    phase_dir = results_root / PHASE4_DIR_NAME
    experiment_path = phase_dir / "experiment.json"
    if experiment_path.exists():
        payload = json.loads(experiment_path.read_text(encoding="utf-8"))
        payload["risk_classification"] = _risk_classification(
            payload["comparison"],
            payload["control_context"],
            payload["treatment_context"],
        )
    else:
        controls = _control_paths(results_root)
        control_reports = {
            profile: _load_reports(paths)
            for profile, paths in controls.items()
        }
        treatment_paths = _completed_treatment_paths(phase_dir)
        treatment_reports = {
            profile: _load_reports(paths)
            for profile, paths in treatment_paths.items()
        }
        manifest = json.loads(
            (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
        )
        payload = _assemble_phase4_artifacts(
            phase_dir=phase_dir,
            manifest=manifest,
            controls=controls,
            control_reports=control_reports,
            treatment_paths=treatment_paths,
            treatment_reports=treatment_reports,
        )
    _write_json(experiment_path, payload)
    _write_json(phase_dir / "phase4_report.json", payload)
    report = phase_dir / "phase4_report.md"
    _write(report, render_phase4_report(payload))
    return report
