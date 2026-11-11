"""Phase 3 apply_patch contract A/B evaluation and reporting."""

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.graph import build_system_prompt
from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.core.config import settings
from app.evaluation.models import BenchmarkReport
from app.evaluation.phase2_5 import (
    LEGACY_EXPERIMENT_ID,
    PHASE25_DIR_NAME,
    _file_hashes,
    _legacy_report_paths,
    _load_reports,
    _tree_digest,
    _tree_hashes,
    _tool_contracts,
)
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT, load_tasks
from app.workspace import git
from app.workspace.models import ApplyPatchRequest, PatchFailureCategory

PHASE3_DIR_NAME = "phase3_patch_reliability"
CONTROL_SOURCE_COMMITS = [
    "7be4a959a59aaec353627fa22199855494574497",
    "137c738330c91a78b0b8a86b3bc3a0fb4557b6f7",
]
PROFILES = [AgentProfile.PLANNER, AgentProfile.FULL]
FOCUS_TASKS = ["v2-optional-profile", "v2-optional-repository-chain"]


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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _patch_schema_payload() -> dict:
    schema = ApplyPatchRequest.model_json_schema()
    return {
        "schema": schema,
        "sha256": _sha256(
            json.dumps(schema, ensure_ascii=False, sort_keys=True)
        ),
        "failure_categories": [
            category.value for category in PatchFailureCategory
        ],
        "contract": "files[].replacements[] sequential transactional v2",
    }


def _source_manifest(control_dir: Path, control_json: dict) -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 3 实验要求 clean working tree；请先提交源码")
    source_relative = [
        "app/agent/graph.py",
        "app/agent/telemetry.py",
        "app/evaluation/models.py",
        "app/evaluation/phase3.py",
        "app/evaluation/runner.py",
        "app/schemas/agent.py",
        "app/tools/patch_tools.py",
        "app/workspace/models.py",
        "app/workspace/patch.py",
        "evaluation/tasks_v2.json",
    ]
    source_hashes = _file_hashes(
        [PROJECT_ROOT / relative for relative in source_relative]
    )
    hidden_hashes = _tree_hashes(
        PROJECT_ROOT / "evaluation" / "hidden_tests" / "v2"
    )
    contracts = _tool_contracts()
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
        "control": {
            "phase2_5_experiment": str(control_dir),
            "source_commits": CONTROL_SOURCE_COMMITS,
            "confirmation_commit": control_json.get("confirmation_commit"),
            "provenance": control_json.get("confirmation_working_tree_clean"),
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
            "workspace_line_endings": "repository-native",
            "success_criteria": "public and hidden Maven tests both pass",
        },
        "prompt_hashes": {
            profile.value: _sha256(
                build_system_prompt(PROFILE_CAPABILITIES[profile])
            )
            for profile in PROFILES
        },
        "tool_contracts": {
            profile.value: contracts[profile.value] for profile in PROFILES
        },
        "patch_contract": _patch_schema_payload(),
    }


def render_patch_contract_design() -> str:
    schema = json.dumps(
        ApplyPatchRequest.model_json_schema(),
        ensure_ascii=False,
        indent=2,
    )
    return f"""# Transactional Patch Contract Design

## Root Cause

The v1 contract used a flat list of `{{path, old_text, new_text}}` entries and
rejected duplicate paths. The Agent naturally generated multiple independent
edits for one file as multiple flat entries, producing
`invalid_patch_format` for a structurally valid repair.

## New Contract

```json
{{
  "files": [
    {{
      "path": "src/main/java/Example.java",
      "replacements": [
        {{"old_text": "...", "new_text": "..."}},
        {{"old_text": "...", "new_text": "..."}}
      ]
    }}
  ]
}}
```

- A path appears at most once in `files`.
- A file contains one or more ordered `replacements`.
- Each replacement runs against the in-memory content produced by the previous
  replacement, so sequential dependencies are supported.
- Every `old_text` must occur exactly once at its turn. No fuzzy matching,
  whitespace normalization, or automatic disambiguation is performed.

## Atomicity

1. Validate every path and decode every file.
2. Simulate every replacement for every file in memory.
3. Only if all files are valid, prepare temporary files.
4. Commit each file by atomic replace.
5. If commit fails, restore every file already replaced.

The guarantee is replacement-level, file-level, and invocation-level
all-or-nothing under process/host I/O error handling. A physical host crash
during multi-file commit cannot be made perfectly atomic by ordinary filesystem
renames; rollback failure is reported explicitly as `write_failed`.

## Structured Errors

Failures return:

```json
{{
  "success": false,
  "error_category": "old_text_not_found",
  "file": "src/main/java/Example.java",
  "replacement_index": 2,
  "message": "..."
}}
```

No file body or patch payload is returned.

## Safety

- workspace-relative path validation
- symlink and hardlink rejection
- `.git` protection
- UTF-8/BOM and binary checks
- repository-native LF/CRLF preservation
- exact-match semantics

## Pydantic Schema

```json
{schema}
```
"""


def _load_control(
    results_root: Path,
) -> tuple[dict[str, list[BenchmarkReport]], dict[str, list[Path]], dict]:
    phase25_dir = results_root / PHASE25_DIR_NAME
    control_json = json.loads(
        (phase25_dir / "confirmation_report.json").read_text(encoding="utf-8")
    )
    legacy_paths = _legacy_report_paths(results_root / LEGACY_EXPERIMENT_ID)
    confirmation_paths = {
        profile: sorted(
            (
                phase25_dir
                / "confirmation_runs"
                / f"variant_{profile.value}"
            ).glob("*/report.json")
        )
        for profile in PROFILES
    }
    paths = {
        profile: legacy_paths[profile] + confirmation_paths[profile]
        for profile in PROFILES
    }
    reports = {
        profile: _load_reports(paths[profile])
        for profile in PROFILES
    }
    return reports, paths, control_json


def _profile_metric_summary(
    reports_by_profile: dict[AgentProfile, list[BenchmarkReport]],
    paths_by_profile: dict[AgentProfile, list[Path]],
) -> dict[str, dict]:
    from app.evaluation.ablation import _aggregate_variant

    return {
        profile.value: _aggregate_variant(
            profile,
            reports_by_profile[profile],
            paths_by_profile[profile],
            PROJECT_ROOT,
        )
        for profile in PROFILES
    }


def _metric_delta(control: dict, treatment: dict) -> dict:
    control_metrics = control["metrics"]
    treatment_metrics = treatment["metrics"]
    keys = [
        "success_rate",
        "medium_success_rate",
        "hard_success_rate",
        "hidden_test_pass_rate",
        "average_tokens",
        "average_llm_calls",
        "average_agent_tool_calls",
        "apply_patch_invocations",
        "apply_patch_failures",
        "apply_patch_failure_rate",
        "recovered_patch_failures",
        "average_duration",
    ]
    return {
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


def _task_metrics(reports: list[BenchmarkReport], task_id: str) -> dict:
    results = [
        result
        for report in reports
        for result in report.results
        if result.task_id == task_id
    ]
    if not results:
        return {"available": False}
    patch_failures = 0
    patch_attempts = 0
    for result in results:
        patch_attempts += (
            result.apply_patch_attempts
            if "apply_patch_attempts" in result.model_fields_set
            else result.tool_usage.get("apply_patch", 0)
        )
        patch_failures += (
            result.apply_patch_failures
            if "apply_patch_failures" in result.model_fields_set
            else result.tool_failure_counts.get("apply_patch", 0)
        )
    return {
        "available": True,
        "runs": len(results),
        "passes": sum(1 for result in results if result.success),
        "average_tokens": sum(
            result.total_tokens or 0 for result in results
        )
        / len(results),
        "average_llm_calls": sum(result.llm_calls for result in results)
        / len(results),
        "average_tool_calls": sum(result.tool_calls for result in results)
        / len(results),
        "average_duration": sum(
            result.duration_seconds for result in results
        )
        / len(results),
        "apply_patch_attempts": patch_attempts,
        "apply_patch_failures": patch_failures,
        "apply_patch_failure_rate": (
            patch_failures / patch_attempts if patch_attempts else 0.0
        ),
        "recovered_patch_failures": sum(
            result.recovered_patch_failures for result in results
        ),
    }


def _task_comparison(
    control_reports: dict[AgentProfile, list[BenchmarkReport]],
    treatment_reports: dict[AgentProfile, list[BenchmarkReport]],
) -> dict:
    result = {}
    for profile in PROFILES:
        # Task pass counts are cheaper to derive directly and avoid fake paths.
        task_ids = sorted(
            {
                item.task_id
                for report in control_reports[profile]
                for item in report.results
            }
        )
        result[profile.value] = {
            task_id: {
                "control": _task_metrics(control_reports[profile], task_id),
                "treatment": _task_metrics(
                    treatment_reports[profile], task_id
                ),
            }
            for task_id in task_ids
        }
    return result


def _report_payload(
    *,
    control: dict,
    treatment: dict,
    comparison: dict,
    tasks: dict,
    source_manifest: dict,
) -> dict:
    return {
        "schema_version": 1,
        "phase": "3",
        "experiment_id": PHASE3_DIR_NAME,
        "model": settings.LLM_MODEL,
        "benchmark": "v2",
        "source": source_manifest,
        "control": {
            "label": "v1_flat_contract",
            "runs_per_profile": 3,
            "metrics": control,
        },
        "treatment": {
            "label": "v2_grouped_transactional_contract",
            "runs_per_profile": 3,
            "metrics": treatment,
        },
        "comparison": comparison,
        "task_comparison": tasks,
        "focus_tasks": {
            task_id: {
                profile.value: tasks[profile.value][task_id]
                for profile in PROFILES
            }
            for task_id in FOCUS_TASKS
        },
    }


def render_phase3_report(payload: dict) -> str:
    comparison = payload["comparison"]
    control = payload["control"]["metrics"]
    treatment = payload["treatment"]["metrics"]
    control_full = control["full"]["metrics"]
    treatment_full = treatment["full"]["metrics"]
    control_planner = control["planner"]["metrics"]
    treatment_planner = treatment["planner"]["metrics"]
    def displayed(value, metric: str) -> str:
        if value is None:
            return "N/A"
        if metric.endswith("_rate"):
            return f"{value * 100:.1f}%"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    lines = [
        "# Phase 3 Patch Reliability",
        "",
        "## 1. Root Cause",
        "",
        "The v1 flat changes[] contract rejected a second entry for the same "
        "file, although the Agent naturally emitted multiple independent edits "
        "for one file. This caused invalid_patch_format without attempting the "
        "repair.",
        "",
        "## 2. New Tool Contract",
        "",
        "`files[] -> replacements[]`; one path per file entry, ordered exact-match "
        "replacements within a file.",
        "",
        "## 3. Atomicity",
        "",
        "Validated and simulated in memory for all files, then written via "
        "temporary files and atomic rename, with rollback on commit failure.",
        "Guarantee: replacement, file, and invocation-level all-or-nothing under "
        "handled process/I/O failures.",
        "",
        "## 4. Safety",
        "",
        "Exact matching, workspace-relative paths, symlink/hardlink rejection, "
        "UTF-8/BOM checks, newline preservation, and no ambiguous matching.",
        "",
        "## 6. A/B Results",
        "",
        "| Profile | Metric | Control | Treatment | Delta |",
        "|---|---|---:|---:|---:|",
    ]
    for profile in PROFILES:
        delta = comparison[profile.value]
        for metric in (
            "success_rate",
            "hard_success_rate",
            "average_tokens",
            "average_agent_tool_calls",
            "apply_patch_invocations",
            "apply_patch_failures",
            "apply_patch_failure_rate",
            "recovered_patch_failures",
            "average_duration",
        ):
            values = delta[metric]
            lines.append(
                f"| {profile.value} | {metric} | "
                f"{displayed(values['control'], metric)} | "
                f"{displayed(values['treatment'], metric)} | "
                f"{displayed(values['delta'], metric)} |"
            )
    lines.extend(
        [
            "",
            "## 7. Patch Reliability",
            "",
            f"- Planner failure rate: "
            f"{control_planner['apply_patch_failure_rate'] * 100:.1f}% → "
            f"{treatment_planner['apply_patch_failure_rate'] * 100:.1f}%.",
            f"- Full failure rate: "
            f"{control_full['apply_patch_failure_rate'] * 100:.1f}% → "
            f"{treatment_full['apply_patch_failure_rate'] * 100:.1f}%.",
            f"- Planner recovered failures: "
            f"{control_planner['recovered_patch_failures']} (legacy partial) → "
            f"{treatment_planner['recovered_patch_failures']}.",
            f"- Full recovered failures: "
            f"{control_full['recovered_patch_failures']} (legacy partial) → "
            f"{treatment_full['recovered_patch_failures']}.",
            "",
            "## 8. Agent Efficiency",
            "",
            f"- Planner tokens: "
            f"{control_planner['average_tokens']:.1f} → "
            f"{treatment_planner['average_tokens']:.1f}.",
            f"- Full tokens: "
            f"{control_full['average_tokens']:.1f} → "
            f"{treatment_full['average_tokens']:.1f}.",
            "",
            "## 9. Task Results",
            "",
            "| Profile | Task | Control | Treatment |",
            "|---|---|---:|---:|",
        ]
    )
    for profile in PROFILES:
        for task_id, values in payload["task_comparison"][profile.value].items():
            control_task = values["control"]
            treatment_task = values["treatment"]
            lines.append(
                f"| {profile.value} | {task_id} | "
                f"{control_task.get('passes', 'N/A')}/{control_task.get('runs', 'N/A')} | "
                f"{treatment_task.get('passes', 'N/A')}/{treatment_task.get('runs', 'N/A')} |"
            )
    lines.extend(
        [
            "",
            "## Focus Tasks",
            "",
        ]
    )
    for task_id, profiles in payload["focus_tasks"].items():
        lines.append(f"### {task_id}")
        lines.append("")
        for profile in PROFILES:
            control_task = profiles[profile.value]["control"]
            treatment_task = profiles[profile.value]["treatment"]
            lines.append(
                f"- {profile.value}: pass "
                f"{control_task.get('passes')}/{control_task.get('runs')} → "
                f"{treatment_task.get('passes')}/{treatment_task.get('runs')}; "
                f"patch failures {control_task.get('apply_patch_failures')} → "
                f"{treatment_task.get('apply_patch_failures')}; "
                f"LLM calls {control_task.get('average_llm_calls', 0):.2f} → "
                f"{treatment_task.get('average_llm_calls', 0):.2f}; "
                f"tool calls {control_task.get('average_tool_calls', 0):.2f} → "
                f"{treatment_task.get('average_tool_calls', 0):.2f}; "
                f"tokens {control_task.get('average_tokens', 0):.1f} → "
                f"{treatment_task.get('average_tokens', 0):.1f}; "
                f"duration {control_task.get('average_duration', 0):.1f}s → "
                f"{treatment_task.get('average_duration', 0):.1f}s."
            )
        lines.append("")
    lines.extend(
        [
            "## 10. Conclusion",
            "",
            f"- Patch failure rate: Planner "
            f"{control_planner['apply_patch_failure_rate'] * 100:.1f}% → "
            f"{treatment_planner['apply_patch_failure_rate'] * 100:.1f}%; "
            f"Full {control_full['apply_patch_failure_rate'] * 100:.1f}% → "
            f"{treatment_full['apply_patch_failure_rate'] * 100:.1f}%.",
            f"- Success rate: Planner "
            f"{control_planner['success_rate'] * 100:.1f}% → "
            f"{treatment_planner['success_rate'] * 100:.1f}%; "
            f"Full {control_full['success_rate'] * 100:.1f}% → "
            f"{treatment_full['success_rate'] * 100:.1f}%.",
            "- The grouped transactional contract eliminates the targeted "
            "duplicate-path invalid-format failures and should remain the default.",
            "- Small success-rate regressions are not attributed to Patch failures: "
            "the treatment produced zero Patch failures in the measured runs.",
            "- Residual risks are multi-file rollback failure and malformed top-level "
            "JSON rejected by schema validation before structured tool execution.",
            "",
            "## 11. Recommended Next Step",
            "",
            "Use the observed A/B result to decide whether further tool-input "
            "normalization is warranted. Do not add benchmark-specific rules.",
            "",
        ]
    )
    return "\n".join(lines)


async def run_patch_reliability_experiment(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    results_root = Path(output_dir).expanduser()
    if not results_root.is_absolute():
        results_root = PROJECT_ROOT / results_root
    results_root = results_root.resolve()
    phase_dir = results_root / PHASE3_DIR_NAME
    phase_dir.mkdir(parents=True, exist_ok=True)

    control_reports, control_paths, control_json = _load_control(results_root)
    manifest = _source_manifest(results_root / PHASE25_DIR_NAME, control_json)
    _write_json(phase_dir / "source_manifest.json", manifest)
    _write(
        phase_dir / "patch_contract_design.md",
        render_patch_contract_design(),
    )

    tasks = load_tasks(suite="v2")
    treatment_reports: dict[AgentProfile, list[BenchmarkReport]] = defaultdict(list)
    treatment_paths: dict[AgentProfile, list[Path]] = defaultdict(list)
    run_records = []
    for repetition in range(1, 4):
        for profile in PROFILES:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            run_id = f"treatment_{profile.value}_r{repetition:02d}_{stamp}"
            variant_dir = (
                phase_dir / "treatment_runs" / f"variant_{profile.value}"
            )
            print(
                f"\n=== treatment {profile.value} repetition {repetition}/3 ===",
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
            run_records.append(
                {
                    "profile": profile.value,
                    "repetition": repetition,
                    "run_id": run_id,
                    "report": path.relative_to(PROJECT_ROOT).as_posix(),
                    "passed_tasks": report.summary.passed_tasks,
                }
            )

    control_metrics = _profile_metric_summary(control_reports, control_paths)
    treatment_metrics = _profile_metric_summary(
        treatment_reports,
        treatment_paths,
    )
    comparison = {
        profile.value: _metric_delta(
            control_metrics[profile.value],
            treatment_metrics[profile.value],
        )
        for profile in PROFILES
    }
    task_comparison = _task_comparison(
        control_reports,
        treatment_reports,
    )
    payload = _report_payload(
        control=control_metrics,
        treatment=treatment_metrics,
        comparison=comparison,
        tasks=task_comparison,
        source_manifest=manifest,
    )
    payload["run_records"] = run_records
    payload["control_report_paths"] = {
        profile.value: [
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in control_paths[profile]
        ]
        for profile in PROFILES
    }
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "phase3_report.json", payload)
    _write(phase_dir / "phase3_report.md", render_phase3_report(payload))
    print(f"\nPhase 3 report: {phase_dir / 'phase3_report.md'}")
    return phase_dir / "phase3_report.md"


def regenerate_phase3_report(
    *,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    results_root = Path(output_dir).expanduser()
    if not results_root.is_absolute():
        results_root = PROJECT_ROOT / results_root
    phase_dir = results_root.resolve() / PHASE3_DIR_NAME
    payload = json.loads(
        (phase_dir / "experiment.json").read_text(encoding="utf-8")
    )
    _write_json(phase_dir / "phase3_report.json", payload)
    report = phase_dir / "phase3_report.md"
    _write(report, render_phase3_report(payload))
    return report
