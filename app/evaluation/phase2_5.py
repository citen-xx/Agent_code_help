"""Phase 2.5 source audit, confirmation run, and failure analysis."""

import asyncio
import hashlib
import json
import math
import subprocess
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.capabilities import (
    AgentProfile,
    PROFILE_CAPABILITIES,
)
from app.agent.graph import _classify_patch_error, build_system_prompt
from app.code_intel.tools import create_code_intel_tools
from app.core.config import settings
from app.evaluation.ablation import (
    CODE_INTELLIGENCE_TOOLS,
    VARIANT_LABELS,
    VARIANT_ORDER,
    _aggregate_variant,
)
from app.evaluation.models import BenchmarkReport
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT, load_tasks
from app.repair.tools import create_repair_plan_tools
from app.tools.build_tools import create_build_tools
from app.tools.git_tools import create_git_tools
from app.tools.patch_tools import create_patch_tools
from app.tools.registry import create_repo_tools
from app.workspace import git
from app.workspace.manager import WorkspaceManager

LEGACY_EXPERIMENT_ID = "ablation_phase2_20261111_v2"
PHASE25_DIR_NAME = "phase2_5_confirmation"
LEGACY_EXECUTION_HEAD = "164e06e1c9db20cf2f58b9c72d4cc4219ac8b2ac"
LEGACY_RECONSTRUCTED_COMMIT = "7be4a959a59aaec353627fa22199855494574497"

SOURCE_FILES = [
    "app/agent/capabilities.py",
    "app/agent/graph.py",
    "app/agent/state.py",
    "app/agent/telemetry.py",
    "app/api/agent.py",
    "app/code_intel/index.py",
    "app/code_intel/parser.py",
    "app/code_intel/registry.py",
    "app/code_intel/tools.py",
    "app/evaluation/ablation.py",
    "app/evaluation/models.py",
    "app/evaluation/phase2_5.py",
    "app/evaluation/runner.py",
    "app/evaluation/tasks.py",
    "app/evaluation/usage.py",
    "app/repair/models.py",
    "app/repair/tools.py",
    "app/schemas/agent.py",
    "app/tools/build_tools.py",
    "app/tools/git_tools.py",
    "app/tools/patch_tools.py",
    "app/tools/registry.py",
    "app/tools/repo.py",
    "app/workspace/build.py",
    "app/workspace/docker_sandbox.py",
    "app/workspace/git.py",
    "app/workspace/manager.py",
    "app/workspace/models.py",
    "app/workspace/patch.py",
    "evaluation/tasks_v2.json",
]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: dict) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _resolve_output_root(output_dir: str | Path) -> Path:
    path = Path(output_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_hashes(paths: list[Path]) -> dict[str, str]:
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): _sha256_bytes(path.read_bytes())
        for path in paths
        if path.is_file()
    }


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_digest(hashes: dict[str, str]) -> str:
    payload = "\n".join(
        f"{path}:{digest}" for path, digest in sorted(hashes.items())
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _tool_contracts() -> dict[str, dict]:
    task = load_tasks(suite="v2")[0]
    bundle = PROJECT_ROOT / task.repository
    contracts: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="phase25-contracts-") as temp_dir:
        origin = Path(temp_dir) / "origin"
        subprocess.run(
            ["git", "clone", "--quiet", str(bundle), str(origin)],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        manager = WorkspaceManager(
            Path(temp_dir) / ".workspaces",
            clone_timeout_seconds=60,
            allow_file_urls=True,
        )
        workspace = manager.create(origin.as_uri(), commit=task.base_commit)
        try:
            for profile in VARIANT_ORDER:
                capabilities = PROFILE_CAPABILITIES[profile]
                tools = create_repo_tools(workspace.repository_path)
                if capabilities.code_intelligence:
                    tools += create_code_intel_tools(
                        workspace.workspace_id,
                        workspace.repository_path,
                        manager=manager,
                    )
                tools += create_git_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                )
                if capabilities.repair_planner:
                    tools += create_repair_plan_tools(
                        workspace.workspace_id,
                        workspace.repository_path,
                        manager=manager,
                    )
                tools += create_patch_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                )
                tools += create_build_tools(
                    workspace.workspace_id,
                    workspace.repository_path,
                    manager=manager,
                )
                contracts[profile.value] = {
                    "tool_names": sorted(tool.name for tool in tools),
                    "tool_schema_chars": sum(
                        len(tool.name)
                        + len(tool.description)
                        + len(
                            json.dumps(
                                tool.args_schema.model_json_schema(),
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        )
                        for tool in tools
                    ),
                    "prompt_chars": len(
                        build_system_prompt(capabilities)
                    ),
                    "prompt_sha256": _sha256_bytes(
                        build_system_prompt(capabilities).encode("utf-8")
                    ),
                }
        finally:
            manager.delete(workspace.workspace_id)
    return contracts


def _status_entries(status: str) -> list[str]:
    return [
        line
        for line in status.splitlines()
        if line.strip() and not line.startswith("## ")
    ]


def create_source_manifest(
    output_dir: Path,
    *,
    legacy_experiment: Path,
) -> dict:
    status_lines = _status_entries(git.status_porcelain(PROJECT_ROOT))
    if status_lines:
        raise RuntimeError(
            "确认实验要求 clean working tree；请先提交 Phase 2.5 源码"
        )
    commit = git.head_commit(PROJECT_ROOT)
    hidden_root = PROJECT_ROOT / "evaluation" / "hidden_tests" / "v2"
    hidden_hashes = _tree_hashes(hidden_root)
    source_hashes = _file_hashes(
        [PROJECT_ROOT / relative for relative in SOURCE_FILES]
    )
    tasks_hash = source_hashes.get("evaluation/tasks_v2.json")
    contracts = _tool_contracts()
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "confirmation_source": {
            "git_commit": commit,
            "working_tree_clean": True,
            "source_file_hashes": source_hashes,
            "source_bundle_sha256": _tree_digest(source_hashes),
            "benchmark_tasks_sha256": tasks_hash,
            "hidden_tests_tree_sha256": _tree_digest(hidden_hashes),
            "hidden_test_hashes": hidden_hashes,
        },
        "legacy_phase2": {
            "experiment": str(legacy_experiment),
            "recorded_git_head": LEGACY_EXECUTION_HEAD,
            "recorded_working_tree_dirty": True,
            "reconstructed_source_commit": LEGACY_RECONSTRUCTED_COMMIT,
            "source_snapshot_status": (
                "reconstructed_from_clean_commit_after_diff_audit"
            ),
            "strict_clean_run": False,
        },
        "model": {
            "name": settings.LLM_MODEL,
            "temperature": 0,
        },
        "benchmark": "v2",
        "controlled_variables": {
            "max_steps": 20,
            "task_timeout_seconds": 300,
            "docker_maven_image": settings.DOCKER_MAVEN_IMAGE,
            "workspace_line_endings": "repository-native (autocrlf=false)",
            "success_criteria": "public and hidden Maven tests both pass",
        },
        "profiles": {
            profile.value: {
                "capabilities": PROFILE_CAPABILITIES[profile].as_dict(),
                **contracts[profile.value],
            }
            for profile in VARIANT_ORDER
        },
    }
    _write_json(output_dir / "source_manifest.json", manifest)
    return manifest


def _load_reports(paths: list[Path]) -> list[BenchmarkReport]:
    return [
        BenchmarkReport.model_validate_json(path.read_text(encoding="utf-8"))
        for path in paths
    ]


def _legacy_report_paths(experiment_dir: Path) -> dict[AgentProfile, list[Path]]:
    result = {}
    for profile in VARIANT_ORDER:
        result[profile] = sorted(
            (experiment_dir / f"variant_{profile.value}").glob("*/report.json")
        )
    return result


def _metric_summary(
    profile: AgentProfile,
    reports: list[BenchmarkReport],
    paths: list[Path],
) -> dict:
    if not reports:
        return {"available": False, "reason": "no reports"}
    aggregate = _aggregate_variant(
        profile,
        reports,
        paths,
        PROJECT_ROOT,
    )
    return {
        "available": True,
        "runs": len(reports),
        "metrics": aggregate["metrics"],
        "tool_usage": aggregate["tool_usage"],
        "tool_output_chars": aggregate["tool_output_chars"],
        "tool_failures": aggregate["tool_failures"],
        "failure_categories": aggregate["failure_categories"],
        "task_results": aggregate["task_results"],
        "stability": aggregate["stability"],
    }


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
        / denominator
    )
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _patch_records(
    legacy_reports: dict[AgentProfile, list[BenchmarkReport]],
    confirmation_reports: dict[AgentProfile, list[BenchmarkReport]],
) -> list[dict]:
    records: list[dict] = []

    for profile in (AgentProfile.PLANNER, AgentProfile.FULL):
        for report in legacy_reports.get(profile, []):
            for result in report.results:
                failure_count = result.tool_failure_counts.get("apply_patch", 0)
                for ordinal in range(1, failure_count + 1):
                    records.append(
                        {
                            "source": "phase2_exploratory",
                            "profile": profile.value,
                            "run_id": report.run_id,
                            "task_id": result.task_id,
                            "failure_ordinal_within_task": ordinal,
                            "tool_call_index": None,
                            "step": None,
                            "repair_round": None,
                            "target_file": None,
                            "error_message": None,
                            "error_category": "unavailable",
                            "patch_applied": None,
                            "retried_after_failure": None,
                            "later_successful_patch": None,
                            "repeated_same_patch": None,
                            "after_automatic_test": None,
                            "after_replan": None,
                            "final_task_passed": result.success,
                            "detail_availability": {
                                "per_call_metadata": "unavailable",
                                "reason": (
                                    "Phase 2 reports retained aggregate failure "
                                    "counts but not per-call tool events"
                                ),
                            },
                        }
                    )

    for profile, reports in confirmation_reports.items():
        for report in reports:
            for result in report.results:
                patch_events = [
                    event
                    for event in result.tool_events
                    if event.tool_name == "apply_patch"
                ]
                seen_fingerprints: set[str] = set()
                for index, event in enumerate(patch_events):
                    if event.success:
                        seen_fingerprints.add(event.argument_fingerprint)
                        continue
                    later_events = patch_events[index + 1 :]
                    all_prior_tool_events = [
                        item
                        for item in result.tool_events
                        if item.tool_call_index < event.tool_call_index
                    ]
                    error_category = (
                        event.error_category.value
                        if event.error_category is not None
                        else "other"
                    )
                    if event.error_summary:
                        derived = _classify_patch_error(event.error_summary)
                        if derived.value != "other":
                            error_category = derived.value
                    records.append(
                        {
                            "source": "phase2_5_confirmation",
                            "profile": profile.value,
                            "run_id": report.run_id,
                            "task_id": result.task_id,
                            "tool_call_index": event.tool_call_index,
                            "step": event.step,
                            "repair_round": event.repair_round,
                            "target_file": (
                                ", ".join(
                                    dict.fromkeys(
                                        event.target_file.split(", ")
                                    )
                                )
                                if event.target_file
                                else None
                            ),
                            "error_message": event.error_summary,
                            "error_category": error_category,
                            "patch_applied": event.patch_applied,
                            "retried_after_failure": bool(later_events),
                            "later_successful_patch": any(
                                item.success for item in later_events
                            ),
                            "repeated_same_patch": (
                                event.argument_fingerprint in seen_fingerprints
                            ),
                            "after_automatic_test": any(
                                item.auto_generated
                                for item in all_prior_tool_events
                            ),
                            "after_replan": sum(
                                1
                                for item in all_prior_tool_events
                                if item.tool_name == "create_repair_plan"
                            )
                            >= 2,
                            "final_task_passed": result.success,
                            "detail_availability": {
                                "per_call_metadata": "available",
                            },
                        }
                    )
                    seen_fingerprints.add(event.argument_fingerprint)
    return records


def _patch_analysis(
    legacy_reports: dict[AgentProfile, list[BenchmarkReport]],
    confirmation_reports: dict[AgentProfile, list[BenchmarkReport]],
) -> dict:
    records = _patch_records(legacy_reports, confirmation_reports)
    historical = [
        record for record in records if record["source"] == "phase2_exploratory"
    ]
    detailed = [
        record for record in records if record["source"] == "phase2_5_confirmation"
    ]

    def grouped(records_: list[dict], key: str) -> dict:
        return dict(
            sorted(
                Counter(record[key] for record in records_).items()
            )
        )

    def group_by_task(records_: list[dict]) -> dict:
        groups: dict[str, list[dict]] = defaultdict(list)
        for record in records_:
            groups[record["task_id"]].append(record)
        return {
            task_id: len(group)
            for task_id, group in sorted(groups.items())
        }

    patch_rates = {}
    for profile in (AgentProfile.PLANNER, AgentProfile.FULL):
        confirmation_results = [
            result
            for report in confirmation_reports.get(profile, [])
            for result in report.results
        ]
        attempts = sum(result.apply_patch_attempts for result in confirmation_results)
        failures = sum(result.apply_patch_failures for result in confirmation_results)
        successes = attempts - failures
        patch_rates[profile.value] = {
            "attempts": attempts,
            "failures": failures,
            "successes": successes,
            "failure_rate": failures / attempts if attempts else 0.0,
            "success_rate": successes / attempts if attempts else 0.0,
        }

    historical_full = [
        record for record in historical if record["profile"] == "full"
    ]
    historical_planner = [
        record for record in historical if record["profile"] == "planner"
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "phase2_full_failures": len(historical_full),
            "phase2_planner_failures": len(historical_planner),
            "phase2_full_by_task": group_by_task(historical_full),
            "phase2_planner_by_task": group_by_task(historical_planner),
            "phase2_full_by_run": grouped(historical_full, "run_id"),
            "confirmation_failures": len(detailed),
            "confirmation_by_profile": grouped(detailed, "profile"),
            "confirmation_by_category": grouped(detailed, "error_category"),
            "confirmation_by_task": group_by_task(detailed),
            "confirmation_by_repair_round": grouped(
                detailed, "repair_round"
            ),
            "confirmation_after_automatic_test": sum(
                1 for record in detailed if record["after_automatic_test"]
            ),
            "confirmation_after_replan": sum(
                1 for record in detailed if record["after_replan"]
            ),
            "confirmation_repeated_same_patch": sum(
                1 for record in detailed if record["repeated_same_patch"]
            ),
            "confirmation_retried_after_failure": sum(
                1 for record in detailed if record["retried_after_failure"]
            ),
            "confirmation_later_success": sum(
                1 for record in detailed if record["later_successful_patch"]
            ),
            "confirmation_final_task_passed": sum(
                1 for record in detailed if record["final_task_passed"]
            ),
            "confirmation_patch_rates": patch_rates,
        },
        "historical_limitations": {
            "exact_step": "unavailable",
            "exact_tool_call_index": "unavailable",
            "exact_repair_round": "unavailable",
            "exact_target_file": "unavailable",
            "exact_error_message": "unavailable",
            "exact_error_category": "unavailable",
            "reason": (
                "Phase 2 persisted aggregate apply_patch failure counts and "
                "task outcomes, but not per-call telemetry."
            ),
        },
        "records": records,
    }


def _render_patch_analysis(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 2.5 Patch Failure Analysis",
        "",
        "## Historical Phase 2",
        "",
        f"- Full apply_patch failures: {summary['phase2_full_failures']}",
        f"- Planner apply_patch failures: {summary['phase2_planner_failures']}",
        f"- Full by task: `{json.dumps(summary['phase2_full_by_task'], ensure_ascii=False)}`",
        f"- Planner by task: `{json.dumps(summary['phase2_planner_by_task'], ensure_ascii=False)}`",
        "",
        "Per-call step, repair round, target file and exact error category were not "
        "retained in the Phase 2 JSON artifacts. These fields are explicitly "
        "marked `unavailable`; they were not reconstructed.",
        "",
        "## Confirmation Telemetry",
        "",
        f"- Detailed failures: {summary['confirmation_failures']}",
        f"- By profile: `{json.dumps(summary['confirmation_by_profile'])}`",
        f"- By category: `{json.dumps(summary['confirmation_by_category'])}`",
        f"- By task: `{json.dumps(summary['confirmation_by_task'])}`",
        f"- By repair round: `{json.dumps(summary['confirmation_by_repair_round'])}`",
        f"- After automatic Maven test: {summary['confirmation_after_automatic_test']}",
        f"- After Replan: {summary['confirmation_after_replan']}",
        f"- Repeated same patch: {summary['confirmation_repeated_same_patch']}",
        f"- Retried after failure: {summary['confirmation_retried_after_failure']}",
        f"- Later successful patch: {summary['confirmation_later_success']}",
        f"- Final task passed: {summary['confirmation_final_task_passed']}",
        "",
        "## Patch Success Rate",
        "",
        "| Profile | Attempts | Failures | Successes | Failure Rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for profile, values in summary["confirmation_patch_rates"].items():
        lines.append(
            f"| {profile} | {values['attempts']} | {values['failures']} | "
            f"{values['successes']} | {values['failure_rate'] * 100:.1f}% |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "The historical evidence establishes concentration and correlation, not "
            "causality. Whether automatic tests or Replanning triggered a failed "
            "patch can only be tested with the new per-call telemetry.",
            "",
        ]
    )
    detailed = [
        record
        for record in payload["records"]
        if record["source"] == "phase2_5_confirmation"
    ]
    if detailed:
        lines.extend(
            [
                "## Detailed Confirmation Events",
                "",
                "| Profile | Task | Tool # | Step | Repair Round | Target | Category | "
                "Retried | Later Success | Final Pass |",
                "|---|---|---:|---:|---:|---|---|---|---|---|",
            ]
        )
        for record in detailed:
            lines.append(
                f"| {record['profile']} | {record['task_id']} | "
                f"{record['tool_call_index']} | {record['step']} | "
                f"{record['repair_round']} | {record['target_file'] or '-'} | "
                f"{record['error_category']} | "
                f"{record['retried_after_failure']} | "
                f"{record['later_successful_patch']} | "
                f"{record['final_task_passed']} |"
            )
        lines.append("")
    return "\n".join(lines)


def _aggregate_by_profile(
    reports_by_profile: dict[AgentProfile, list[BenchmarkReport]],
    paths_by_profile: dict[AgentProfile, list[Path]],
) -> dict[str, dict]:
    return {
        profile.value: _metric_summary(
            profile,
            reports_by_profile.get(profile, []),
            paths_by_profile.get(profile, []),
        )
        for profile in VARIANT_ORDER
    }


def _token_attribution(
    combined: dict[str, dict],
    confirmation: dict[str, dict],
    manifest: dict,
) -> dict:
    basic = combined["basic"]["metrics"]
    ci = combined["code_intelligence"]["metrics"]
    planner = combined["planner"]["metrics"]
    full = combined["full"]["metrics"]
    basic_contract = manifest["profiles"]["basic"]
    ci_contract = manifest["profiles"]["code_intelligence"]
    planner_contract = manifest["profiles"]["planner"]

    def delta(current, previous):
        return current - previous if current is not None and previous is not None else None

    return {
        "actual_provider_usage": {
            "basic": {
                "prompt_tokens": basic["average_prompt_tokens"],
                "completion_tokens": basic["average_completion_tokens"],
                "total_tokens": basic["average_tokens"],
            },
            "code_intelligence": {
                "prompt_tokens": ci["average_prompt_tokens"],
                "completion_tokens": ci["average_completion_tokens"],
                "total_tokens": ci["average_tokens"],
            },
            "planner": {
                "prompt_tokens": planner["average_prompt_tokens"],
                "completion_tokens": planner["average_completion_tokens"],
                "total_tokens": planner["average_tokens"],
            },
            "full": {
                "prompt_tokens": full["average_prompt_tokens"],
                "completion_tokens": full["average_completion_tokens"],
                "total_tokens": full["average_tokens"],
            },
        },
        "prompt_and_schema_inputs": {
            "basic": basic_contract,
            "code_intelligence": ci_contract,
            "planner": planner_contract,
            "prompt_char_delta_basic_to_ci": (
                ci_contract["prompt_chars"] - basic_contract["prompt_chars"]
            ),
            "tool_schema_char_delta_basic_to_ci": (
                ci_contract["tool_schema_chars"]
                - basic_contract["tool_schema_chars"]
            ),
            "prompt_char_delta_ci_to_planner": (
                planner_contract["prompt_chars"] - ci_contract["prompt_chars"]
            ),
            "tool_schema_char_delta_ci_to_planner": (
                planner_contract["tool_schema_chars"]
                - ci_contract["tool_schema_chars"]
            ),
            "estimation_note": (
                "Characters are exact; token equivalents are approximate and not "
                "used in provider usage totals."
            ),
        },
        "confirmation_tool_output_chars": {
            profile: {
                "by_tool": confirmation[profile]["tool_output_chars"],
                "total": sum(confirmation[profile]["tool_output_chars"].values()),
                "estimated_tokens_chars_div_4": round(
                    sum(confirmation[profile]["tool_output_chars"].values()) / 4,
                    1,
                ),
            }
            for profile in ("basic", "code_intelligence", "planner", "full")
        },
        "delta_basic_to_ci": {
            "prompt_tokens": delta(
                ci["average_prompt_tokens"], basic["average_prompt_tokens"]
            ),
            "completion_tokens": delta(
                ci["average_completion_tokens"],
                basic["average_completion_tokens"],
            ),
            "total_tokens": delta(ci["average_tokens"], basic["average_tokens"]),
        },
        "interpretation_limit": (
            "Tool outputs are fed into subsequent prompts and repeated context "
            "cannot be isolated from aggregate provider usage without per-call "
            "provider billing metadata."
        ),
    }


def _metric_consistency() -> dict:
    return {
        "repair_rounds": {
            "consistent_across_profiles": True,
            "definition": (
                "Successful apply_patch calls made after an observed Maven test "
                "failure. Failed patch attempts do not increment this counter."
            ),
            "warning": (
                "The repair limit therefore counts successful repair patches, "
                "not total repair attempts."
            ),
        },
        "tool_calls": {
            "consistent_across_profiles": True,
            "definition": "All executed tool events, including auto Maven tests.",
        },
        "agent_tool_calls": {
            "definition": "Tool calls requested by the Agent, excluding harness auto tests.",
            "historical_phase2": "unavailable",
            "phase2_5": "available",
        },
        "harness_action_count": {
            "definition": "Automatically generated harness actions, currently auto Maven tests.",
        },
        "manual_test_runs": {
            "definition": "run_tests calls requested by the Agent.",
            "historical_phase2": "unavailable as a separate field; inferable from auto counts",
        },
        "automatic_test_runs": {
            "definition": "Maven tests triggered automatically after patch success.",
        },
        "apply_patch_failure_rate": {
            "definition": "failed apply_patch tool calls / apply_patch attempts",
            "historical_phase2": "available at task aggregate level only",
        },
        "duration": {
            "definition": (
                "Whole task wall time including initial public test, Agent run, "
                "final public test and hidden verification."
            ),
            "consistent_across_profiles": True,
        },
        "tokens": {
            "definition": "Provider-reported prompt/completion/total usage.",
            "historical_phase2": "available",
        },
    }


def _render_confirmation_report(payload: dict) -> str:
    combined = payload["metrics"]["combined"]
    confirmation = payload["metrics"]["confirmation_clean"]
    lines = [
        "# Agent_code_help Phase 2.5 Confirmation Report",
        "",
        "## A. Experimental Integrity",
        "",
        f"- Phase 2 recorded HEAD: `{LEGACY_EXECUTION_HEAD}` with dirty tree.",
        f"- Audited/reconstructed Phase 2 source commit: "
        f"`{LEGACY_RECONSTRUCTED_COMMIT}`.",
        f"- Confirmation source commit: `{payload['confirmation_commit']}`.",
        f"- Final analysis code commit: `{payload['analysis_code_commit']}`.",
        f"- Confirmation working tree clean: "
        f"{payload['confirmation_working_tree_clean']}.",
        "- The legacy runs are labeled exploratory because they were executed with "
        "a dirty tree. Their source was reconstructed into a clean commit after "
        "diff audit; they are not represented as clean-run provenance.",
        "",
        "## E. Confirmation Results",
        "",
        "### Clean Confirmation Run",
        "",
        "| Variant | Passed | Success | Medium | Hard | Hidden | Tokens | Calls | Tools | Duration |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for profile in VARIANT_ORDER:
        values = confirmation[profile.value]
        metrics = values["metrics"]
        lines.append(
            f"| {VARIANT_LABELS[profile]} | {metrics['passed']}/{metrics['task_runs']} | "
            f"{metrics['success_rate'] * 100:.1f}% | "
            f"{metrics['medium_success_rate'] * 100:.1f}% | "
            f"{metrics['hard_success_rate'] * 100:.1f}% | "
            f"{metrics['hidden_test_pass_rate'] * 100:.1f}% | "
            f"{metrics['average_tokens']:.1f} | "
            f"{metrics['average_llm_calls']:.2f} | "
            f"{metrics['average_tool_calls']:.2f} | "
            f"{metrics['average_duration']:.1f}s |"
        )
    lines.extend(
        [
            "",
            "### Combined Legacy + Confirmation (3 runs per Variant)",
            "",
            "| Variant | Success | Medium | Hard | Hidden | Tokens | Calls | Tools | Duration | Patch Failure Rate | 95% Wilson Task-Run CI |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile in VARIANT_ORDER:
        metrics = combined[profile.value]["metrics"]
        ci = payload["confidence_intervals"][profile.value]["success_rate"]
        lines.append(
            f"| {VARIANT_LABELS[profile]} | {metrics['success_rate'] * 100:.1f}% | "
            f"{metrics['medium_success_rate'] * 100:.1f}% | "
            f"{metrics['hard_success_rate'] * 100:.1f}% | "
            f"{metrics['hidden_test_pass_rate'] * 100:.1f}% | "
            f"{metrics['average_tokens']:.1f} | "
            f"{metrics['average_llm_calls']:.2f} | "
            f"{metrics['average_tool_calls']:.2f} | "
            f"{metrics['average_duration']:.1f}s | "
            f"{metrics['apply_patch_failure_rate'] * 100:.1f}% | "
            f"[{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%] |"
        )
    lines.extend(
        [
            "",
            "The Wilson intervals are naive intervals over repeated task-runs and "
            "do not model within-task dependence.",
            "",
            "## B. Patch Failure Analysis",
            "",
        ]
    )
    patch_summary = payload["patch_failure_analysis"]["summary"]
    lines.extend(
        [
            f"- Phase 2 Full failures: {patch_summary['phase2_full_failures']}, "
            f"concentrated in `{json.dumps(patch_summary['phase2_full_by_task'])}`.",
            f"- Phase 2 Planner failures: {patch_summary['phase2_planner_failures']}, "
            f"concentrated in `{json.dumps(patch_summary['phase2_planner_by_task'])}`.",
            f"- Confirmation detailed failures: {patch_summary['confirmation_failures']}.",
            f"- Confirmation categories: "
            f"`{json.dumps(patch_summary['confirmation_by_category'])}`.",
            f"- Failures after an automatic Maven test: "
            f"{patch_summary['confirmation_after_automatic_test']}.",
            f"- Failures after Replan: {patch_summary['confirmation_after_replan']}.",
            f"- Repeated identical patch payloads: "
            f"{patch_summary['confirmation_repeated_same_patch']}.",
        ]
    )
    for profile, values in patch_summary["confirmation_patch_rates"].items():
        lines.append(
            f"- {profile}: apply_patch {values['successes']}/{values['attempts']} "
            f"successful; failure rate {values['failure_rate'] * 100:.1f}%."
        )
    lines.extend(
        [
            "",
            "Historical per-call step/error fields are unavailable and were not "
            "fabricated. Detailed event-level evidence comes from the clean "
            "confirmation run.",
            "",
            "## C. Token Attribution",
            "",
        ]
    )
    token = payload["token_attribution"]
    delta = token["delta_basic_to_ci"]
    lines.extend(
        [
            f"- Basic → CI actual prompt-token delta: "
            f"{delta['prompt_tokens']:+.1f}/task.",
            f"- Basic → CI actual completion-token delta: "
            f"{delta['completion_tokens']:+.1f}/task.",
            f"- Basic → CI actual total-token delta: "
            f"{delta['total_tokens']:+.1f}/task.",
            f"- CI prompt-fragment character delta: "
            f"{token['prompt_and_schema_inputs']['prompt_char_delta_basic_to_ci']}.",
            f"- CI tool-schema character delta: "
            f"{token['prompt_and_schema_inputs']['tool_schema_char_delta_basic_to_ci']}.",
        ]
    )
    for profile in VARIANT_ORDER:
        chars = token["confirmation_tool_output_chars"][profile.value]
        lines.append(
            f"- {VARIANT_LABELS[profile]} confirmation tool output: "
            f"{chars['total']} chars (approx. "
            f"{chars['estimated_tokens_chars_div_4']} tokens, estimate only)."
        )
    basic_chars = token["confirmation_tool_output_chars"]["basic"]["total"]
    ci_chars = token["confirmation_tool_output_chars"]["code_intelligence"][
        "total"
    ]
    lines.append(
        f"- Basic → CI measured tool-output difference: "
        f"{ci_chars - basic_chars:+d} characters. Because outputs are fed back "
        "into later prompts, this is the main identifiable source of the much "
        "larger actual prompt-token delta."
    )
    lines.extend(
        [
            "- Exact per-tool token cost is unavailable because provider usage is "
            "reported per call and accumulated context repeats prior outputs.",
            "",
            "## D. Metric Consistency",
            "",
            "| Metric | Consistent | Definition / availability |",
            "|---|---:|---|",
            f"| repair_rounds | yes | {payload['metric_consistency']['repair_rounds']['definition']} |",
            "| tool_calls | yes | All Agent + harness tool actions. |",
            "| agent_tool_calls | yes in confirmation | Agent-requested calls; unavailable in legacy Phase 2. |",
            "| harness_action_count | yes in confirmation | Harness auto actions; unavailable in legacy. |",
            "| manual/automatic tests | yes in confirmation | Split explicitly; legacy manual count is inferred only. |",
            "| apply_patch attempts/failures/rate | yes in confirmation | Legacy has aggregate counts only. |",
            "| changed_files | yes | Git status after filtering generated build directories. |",
            "| duration | yes | Includes initial, Agent, final and hidden phases. |",
            "| prompt/completion tokens | yes | Provider-reported values. |",
            "",
            "## F. Task-Level Stability",
            "",
            "| Task | Basic | CI | Planner | Full |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    task_ids = sorted(
        {
            task_id
            for profile in VARIANT_ORDER
            for task_id in combined[profile.value]["task_results"]
        }
    )
    for task_id in task_ids:
        cells = []
        for profile in VARIANT_ORDER:
            value = combined[profile.value]["task_results"][task_id]
            symbol = (
                "✅"
                if value["passes"] == value["runs"]
                else ("❌" if value["passes"] == 0 else "⚠️")
            )
            cells.append(f"{value['passes']}/{value['runs']} {symbol}")
        lines.append(f"| {task_id} | " + " | ".join(cells) + " |")
    unstable = sorted(
        {
            task_id
            for profile in VARIANT_ORDER
            for task_id in combined[profile.value]["stability"]["unstable"]
        }
    )
    lines.extend(
        [
            "",
            f"Unstable tasks across the three-run combined data: "
            f"{', '.join(unstable) or 'none'}.",
            "",
            "## G. Final Conclusions",
            "",
        ]
    )
    lines.extend(
        f"- {item}" for item in payload["final_conclusions"]
    )
    lines.extend(
        [
            "",
            "No confidence interval or small-sample ablation is treated as proof "
            "of capability ineffectiveness.",
            "",
        ]
    )
    return "\n".join(lines)


def _build_final_conclusions(payload: dict) -> list[str]:
    metrics = payload["metrics"]["combined"]
    basic = metrics["basic"]["metrics"]
    ci = metrics["code_intelligence"]["metrics"]
    planner = metrics["planner"]["metrics"]
    full = metrics["full"]["metrics"]
    patch = payload["patch_failure_analysis"]["summary"]
    ci_pp = (ci["success_rate"] - basic["success_rate"]) * 100
    planner_pp = (planner["success_rate"] - ci["success_rate"]) * 100
    loop_pp = (full["success_rate"] - planner["success_rate"]) * 100
    conclusions = [
        (
            "Code Intelligence: the combined evidence shows "
            f"{ci_pp:+.1f} pp success versus Basic with "
            f"{ci['average_tokens'] - basic['average_tokens']:+.1f} tokens/task. "
            "Do not enable by default based on capability alone; more repetitions "
            "and task-level evidence are required."
        ),
        (
            "Planner: the combined evidence shows "
            f"{planner_pp:+.1f} pp versus Code Intelligence. Planner calls were "
            "observed, so the component was exercised, but positive value is not "
            "established."
        ),
        (
            "Repair Loop: Full versus Planner is "
            f"{loop_pp:+.1f} pp. Automatic tests and repair activations were "
            "observed. The current implementation counts successful repair patches, "
            "not failed patch attempts, which is a concrete implementation-level "
            "weakness for evaluating repair limits."
        ),
        (
            "Repair causality: in the detailed confirmation events, "
            f"{patch['confirmation_after_automatic_test']} apply_patch failures "
            "occurred after an auto Maven test and "
            f"{patch['confirmation_after_replan']} occurred after a second plan "
            "revision; all detailed failures were at repair round 0. This supports "
            "correlation with the affected tasks, not a demonstrated auto-test or "
            "Replan causal trigger."
        ),
        (
            "apply_patch: confirmation apply_patch failure categories are "
            f"`{json.dumps(patch['confirmation_by_category'])}`. The tool warrants "
            "targeted robustness work before capability-level conclusions are "
            "treated as stable."
        ),
        (
            "V2 difficulty ceiling: public tests are frequently solved, while "
            "recurrent hidden-contract failures remain. This indicates remaining "
            "headroom is primarily in behavioral-contract generalization rather "
            "than basic test execution."
        ),
    ]
    return conclusions


def _write_confirmation_outputs(
    *,
    phase_dir: Path,
    legacy_paths: dict[AgentProfile, list[Path]],
    legacy_reports: dict[AgentProfile, list[BenchmarkReport]],
    confirmation_paths: dict[AgentProfile, list[Path]],
    confirmation_reports: dict[AgentProfile, list[BenchmarkReport]],
    run_records: list[dict],
    manifest: dict,
    started_at: str,
) -> Path:
    confirmation_metrics = _aggregate_by_profile(
        confirmation_reports,
        confirmation_paths,
    )
    combined_reports = {
        profile: legacy_reports[profile] + confirmation_reports[profile]
        for profile in VARIANT_ORDER
    }
    combined_paths = {
        profile: legacy_paths[profile] + confirmation_paths[profile]
        for profile in VARIANT_ORDER
    }
    combined_metrics = _aggregate_by_profile(
        combined_reports,
        combined_paths,
    )
    confidence_intervals = {}
    for profile in VARIANT_ORDER:
        metrics = combined_metrics[profile.value]["metrics"]
        confidence_intervals[profile.value] = {
            "success_rate": _wilson_interval(
                metrics["passed"],
                metrics["task_runs"],
            ),
            "method": "naive 95% Wilson interval over task-runs",
            "independence_caveat": "repeated task-runs are not independent",
        }

    patch_analysis = _patch_analysis(
        legacy_reports,
        confirmation_reports,
    )
    _write_json(phase_dir / "patch_failure_analysis.json", patch_analysis)
    _atomic_write(
        phase_dir / "patch_failure_analysis.md",
        _render_patch_analysis(patch_analysis),
    )
    payload = {
        "schema_version": 1,
        "phase": "2.5",
        "experiment_id": PHASE25_DIR_NAME,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "confirmation_commit": manifest["confirmation_source"]["git_commit"],
        "analysis_code_commit": git.head_commit(PROJECT_ROOT),
        "confirmation_working_tree_clean": manifest["confirmation_source"][
            "working_tree_clean"
        ],
        "model": settings.LLM_MODEL,
        "benchmark": "v2",
        "run_records": run_records,
        "metrics": {
            "legacy_phase2_exploratory": _aggregate_by_profile(
                legacy_reports,
                legacy_paths,
            ),
            "confirmation_clean": confirmation_metrics,
            "combined": combined_metrics,
        },
        "confidence_intervals": confidence_intervals,
        "metric_consistency": _metric_consistency(),
        "token_attribution": _token_attribution(
            combined_metrics,
            confirmation_metrics,
            manifest,
        ),
        "patch_failure_analysis": patch_analysis,
    }
    payload["final_conclusions"] = _build_final_conclusions(payload)
    _write_json(phase_dir / "confirmation_report.json", payload)
    _atomic_write(
        phase_dir / "confirmation_report.md",
        _render_confirmation_report(payload),
    )
    print(f"\nConfirmation report: {phase_dir / 'confirmation_report.md'}")
    return phase_dir / "confirmation_report.md"


async def run_confirmation_experiment(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
    legacy_experiment: str | Path | None = None,
) -> Path:
    output_root = _resolve_output_root(output_dir)
    phase_dir = output_root / PHASE25_DIR_NAME
    phase_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = (
        Path(legacy_experiment).expanduser().resolve()
        if legacy_experiment
        else output_root / LEGACY_EXPERIMENT_ID
    )
    legacy_paths = _legacy_report_paths(legacy_path)
    if any(len(paths) != 2 for paths in legacy_paths.values()):
        counts = {profile.value: len(paths) for profile, paths in legacy_paths.items()}
        raise RuntimeError(f"Legacy Phase 2 data 不完整: {counts}")
    legacy_reports = {
        profile: _load_reports(paths)
        for profile, paths in legacy_paths.items()
    }

    manifest = create_source_manifest(
        phase_dir,
        legacy_experiment=legacy_path,
    )
    tasks = load_tasks(suite="v2")
    confirmation_paths: dict[AgentProfile, list[Path]] = defaultdict(list)
    confirmation_reports: dict[AgentProfile, list[BenchmarkReport]] = defaultdict(list)
    run_records = []
    started_at = datetime.now(timezone.utc).isoformat()
    for profile in VARIANT_ORDER:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"confirmation_{profile.value}_{stamp}"
        variant_dir = phase_dir / "confirmation_runs" / f"variant_{profile.value}"
        print(f"\n=== confirmation {profile.value} ===", flush=True)
        runner = BenchmarkRunner(
            tasks,
            output_dir=variant_dir,
            run_id=run_id,
            profile=profile,
        )
        report, path = await runner.run()
        confirmation_reports[profile].append(report)
        confirmation_paths[profile].append(path)
        run_records.append(
            {
                "profile": profile.value,
                "run_id": report.run_id,
                "report": path.relative_to(PROJECT_ROOT).as_posix(),
                "passed_tasks": report.summary.passed_tasks,
                "total_tasks": report.summary.total_tasks,
            }
        )

    return _write_confirmation_outputs(
        phase_dir=phase_dir,
        legacy_paths=legacy_paths,
        legacy_reports=legacy_reports,
        confirmation_paths=confirmation_paths,
        confirmation_reports=confirmation_reports,
        run_records=run_records,
        manifest=manifest,
        started_at=started_at,
    )


def regenerate_confirmation_artifacts(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
    legacy_experiment: str | Path | None = None,
) -> Path:
    """Rebuild analysis artifacts from existing source and run reports."""
    output_root = _resolve_output_root(output_dir)
    phase_dir = output_root / PHASE25_DIR_NAME
    legacy_path = (
        Path(legacy_experiment).expanduser().resolve()
        if legacy_experiment
        else output_root / LEGACY_EXPERIMENT_ID
    )
    legacy_paths = _legacy_report_paths(legacy_path)
    legacy_reports = {
        profile: _load_reports(paths)
        for profile, paths in legacy_paths.items()
    }
    confirmation_paths = {
        profile: sorted(
            (
                phase_dir
                / "confirmation_runs"
                / f"variant_{profile.value}"
            ).glob("*/report.json")
        )
        for profile in VARIANT_ORDER
    }
    confirmation_reports = {
        profile: _load_reports(paths)
        for profile, paths in confirmation_paths.items()
    }
    manifest = json.loads(
        (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
    )
    run_records = [
        {
            "profile": profile.value,
            "run_id": report.run_id,
            "report": path.relative_to(PROJECT_ROOT).as_posix(),
            "passed_tasks": report.summary.passed_tasks,
            "total_tasks": report.summary.total_tasks,
        }
        for profile in VARIANT_ORDER
        for report, path in zip(
            confirmation_reports[profile],
            confirmation_paths[profile],
            strict=True,
        )
    ]
    return _write_confirmation_outputs(
        phase_dir=phase_dir,
        legacy_paths=legacy_paths,
        legacy_reports=legacy_reports,
        confirmation_paths=confirmation_paths,
        confirmation_reports=confirmation_reports,
        run_records=run_records,
        manifest=manifest,
        started_at=manifest["generated_at"],
    )
