"""Phase 7 evidence-driven repair commitment experiment."""

import asyncio
import hashlib
import json
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.agent.budget import StepBudgetPolicyName
from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.code_intel.tools import create_code_intel_tools
from app.evaluation.models import BenchmarkReport
from app.evaluation.phase5 import (
    DEFAULT_TASKS_FILE,
    _task_rows,
    load_external_tasks,
    make_host_maven_runner,
    phase5_task_evaluator,
    prepare_defects4j_workspace,
)
from app.evaluation.phase5_5 import _timeline
from app.evaluation.phase6_5 import PHASE65_DIR_NAME, _test_outcomes
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT
from app.repair.tools import create_repair_plan_tools
from app.tools.build_tools import create_build_tools
from app.tools.git_tools import create_git_tools
from app.tools.patch_tools import create_patch_tools
from app.tools.registry import create_repo_tools
from app.workspace import git

PHASE7_DIR_NAME = "phase7_repair_commitment"
TREATMENT_RUNS = ("treatment_run1", "treatment_run2")
COMMITMENT_DESIGN = """# Evidence-driven Repair Commitment

## Existing Implementation Audit

The Planner is model-triggered; no graph rule creates a Plan automatically.
The existing RepairPlan contains target symbols, impacted files/tests, planned
changes, risks, and a Maven validation plan. It does not contain structured
evidence, an explicit root-cause hypothesis, execution status, or an enforced
file-level association with the eventual Patch. Plan revisions are stored, and
a failed test marks the Plan stale, but hypothesis validity is not classified.

## Control

The existing model-driven Planner is unchanged. It may create a Repair Plan
from free-form planning fields, and apply_patch requires a non-stale plan.

## Treatment

`evidence_commitment=True` keeps the same Agent Graph, budget policy, model,
Code Intelligence tools, Patch Contract, and benchmark. It adds:

1. Structured `RepairHypothesis` and `EvidenceReference` fields to the
   `create_repair_plan` schema.
2. Runtime validation that every evidence reference was actually observed
   through read_file, test failure, Code Intelligence, or read-only Git tools.
3. A dynamic commitment signal after a failed test and source read establish
   sufficient evidence.
4. Plan-to-Patch file matching.
5. Blocking of broad exploration after a committed Plan has no unresolved
   questions; targeted reads of Plan files remain available.
6. Structured Plan execution status history and hypothesis revision telemetry.

The commitment signal is based on a failed test plus an observed Java source
read. No fixed step, task ID, evaluator file label, hidden test, or developer
patch participates in this decision.

The controller never receives task identity, evaluator files, hidden tests,
developer patches, or expected repairs.
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload) -> None:
    _write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _phase_dir(output_dir: str | Path) -> Path:
    root = Path(output_dir).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve() / PHASE7_DIR_NAME


def _tool_schema_hash(
    profile: AgentProfile,
    *,
    evidence_commitment: bool,
) -> str:
    workspace_id = "schema-snapshot"
    with tempfile.TemporaryDirectory(prefix="phase7-schema-") as temporary:
        repository = Path(temporary) / workspace_id / "repository"
        repository.mkdir(parents=True)
        tools = create_repo_tools(repository)
        tools += create_git_tools(workspace_id, repository)
        capabilities = PROFILE_CAPABILITIES[profile]
        if capabilities.code_intelligence:
            tools += create_code_intel_tools(workspace_id, repository)
        if capabilities.repair_planner:
            tools += create_repair_plan_tools(
                workspace_id,
                repository,
                require_evidence=evidence_commitment,
            )
        tools += create_patch_tools(workspace_id, repository)
        tools += create_build_tools(workspace_id, repository)
        snapshot = []
        for tool in sorted(tools, key=lambda item: item.name):
            schema = getattr(tool, "args_schema", None)
            snapshot.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "schema": (
                        schema.model_json_schema()
                        if schema is not None
                        else None
                    ),
                }
            )
    return hashlib.sha256(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _source_manifest(tasks_file: Path, tasks) -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 7 要求 clean working tree")
    phase65_manifest = json.loads(
        (
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / PHASE65_DIR_NAME
            / "source_manifest.json"
        ).read_text(encoding="utf-8")
    )
    tasks_hash = _sha256(tasks_file)
    if tasks_hash != phase65_manifest["tasks"]["sha256"]:
        raise RuntimeError("defects4j_v1 tasks hash 已变化")
    prompts = {
        profile.value: hashlib.sha256(
            build_system_prompt(PROFILE_CAPABILITIES[profile]).encode(
                "utf-8"
            )
        ).hexdigest()
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    control_tools = {
        profile.value: _tool_schema_hash(
            profile,
            evidence_commitment=False,
        )
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    treatment_tools = {
        profile.value: _tool_schema_hash(
            profile,
            evidence_commitment=True,
        )
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    if prompts != phase65_manifest["agent"]["prompt_hashes"]:
        raise RuntimeError("Prompt hash 与 Phase 6.5 不一致")
    if control_tools != phase65_manifest["agent"]["tool_schema_hashes"]:
        raise RuntimeError("Control tool schema 与 Phase 6.5 不一致")
    if _sha256(PROJECT_ROOT / "app/agent/budget.py") != (
        phase65_manifest["budget_policy_source"]["sha256"]
    ):
        raise RuntimeError("Budget Policy source 已变化")
    return {
        "schema_version": 1,
        "phase": "7",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": "defects4j_v1",
        "tasks": {
            "path": str(tasks_file),
            "sha256": tasks_hash,
            "count": len(tasks),
        },
        "defects4j_commit": phase65_manifest["defects4j_commit"],
        "jdk": phase65_manifest["jdk"],
        "model": phase65_manifest["model"],
        "temperature": phase65_manifest["temperature"],
        "budget_policy": StepBudgetPolicyName.ADAPTIVE_20_30.value,
        "lifecycle_compaction": False,
        "agent": {
            "git_commit": git.head_commit(PROJECT_ROOT),
            "git_dirty": False,
            "prompt_hashes": prompts,
            "control_tool_schema_hashes": control_tools,
            "treatment_tool_schema_hashes": treatment_tools,
        },
        "commitment_sources": {
            name: {
                "path": str(PROJECT_ROOT / "app" / "agent" / name),
                "sha256": _sha256(PROJECT_ROOT / "app" / "agent" / name),
            }
            for name in ("commitment.py", "graph.py")
        },
        "control_reuse": {
            "reason": (
                "Prompt, Control tool schemas, benchmark, budget source, and "
                "default Planner behavior match Phase 6.5"
            ),
            "runs": [
                "phase6_5_control_run1",
                "phase6_5_control_run2",
            ],
            "agent_commit": "094ac42043b0905ea9db987bec6cce3922a98086",
        },
    }


def _load_report(path: Path) -> BenchmarkReport:
    if not path.is_file():
        raise RuntimeError(f"缺少 report: {path}")
    report = BenchmarkReport.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if len(report.results) != 15:
        raise RuntimeError(f"report 不完整: {path}")
    return report


def _control_reports() -> list[BenchmarkReport]:
    base = (
        PROJECT_ROOT
        / "evaluation"
        / "results"
        / PHASE65_DIR_NAME
        / "runs"
    )
    return [
        _load_report(base / "phase6_5_control_run1" / "report.json"),
        _load_report(base / "phase6_5_control_run2" / "report.json"),
    ]


def _run_treatment(
    tasks,
    phase_dir: Path,
    run_name: str,
) -> BenchmarkReport:
    run_id = f"phase7_{run_name}"
    path = phase_dir / "runs" / run_id / "report.json"
    if path.is_file():
        report = _load_report(path)
        if report.capabilities.get("evidence_commitment") is True:
            return report
    runner = BenchmarkRunner(
        tasks,
        output_dir=phase_dir / "runs",
        run_id=run_id,
        profile=AgentProfile.FULL,
        max_steps=20,
        step_budget_policy=StepBudgetPolicyName.ADAPTIVE_20_30,
        evidence_commitment=True,
        test_runner=make_host_maven_runner(offline=True),
        task_evaluator=phase5_task_evaluator,
        workspace_preparer=prepare_defects4j_workspace,
    )
    report, _ = asyncio.run(runner.run())
    print(
        f"{run_name}: {report.summary.passed_tasks}/"
        f"{report.summary.total_tasks}",
        flush=True,
    )
    return report


def _first_step(events, predicate) -> int | None:
    values = [event.step for event in events if predicate(event)]
    return min(values) if values else None


def _task_records(
    reports: list[BenchmarkReport],
    tasks,
    *,
    variant: str,
) -> list[dict]:
    task_map = {task.task_id: task for task in tasks}
    records = []
    for report in reports:
        rows = {row["task_id"]: row for row in _task_rows(report)}
        for result in report.results:
            timeline = _timeline(
                task_map[result.task_id],
                report,
                result,
            )
            tests = _test_outcomes(result)
            failures = [
                event.step
                for event, success in tests
                if success is False
            ]
            plans = [
                event.step
                for event in result.tool_events
                if event.tool_name == "create_repair_plan"
                and event.success
            ]
            patches = [
                event
                for event in result.tool_events
                if event.tool_name == "apply_patch"
            ]
            sources = [
                event.step
                for event in result.tool_events
                if event.tool_name == "read_file" and event.success
            ]
            commitment = result.commitment_telemetry
            first_hypothesis = (
                commitment.first_hypothesis_step
                if commitment
                else None
            )
            first_plan = min(plans) if plans else None
            first_patch = patches[0].step if patches else None
            first_test = tests[0][0].step if tests else None
            relevant = timeline["first_relevant_file_step"]
            records.append(
                {
                    "run_id": report.run_id,
                    "variant": variant,
                    "task_id": result.task_id,
                    "success": result.success,
                    "failure_category": rows[result.task_id].get(
                        "failure_category"
                    ),
                    "tokens": result.total_tokens or 0,
                    "prompt_tokens": result.prompt_tokens or 0,
                    "completion_tokens": result.completion_tokens or 0,
                    "llm_calls": result.llm_calls,
                    "tool_calls": result.agent_tool_calls,
                    "duration_seconds": result.duration_seconds,
                    "first_failure_observation_step": (
                        min(failures) if failures else None
                    ),
                    "first_source_read_step": (
                        min(sources) if sources else None
                    ),
                    "first_hypothesis_step": first_hypothesis,
                    "first_plan_step": first_plan,
                    "first_patch_step": first_patch,
                    "first_test_step": first_test,
                    "relevant_file_first_seen_step": relevant,
                    "relevant_to_hypothesis_gap": (
                        first_hypothesis - relevant
                        if first_hypothesis is not None
                        and relevant is not None
                        else None
                    ),
                    "relevant_to_plan_gap": (
                        first_plan - relevant
                        if first_plan is not None and relevant is not None
                        else None
                    ),
                    "relevant_to_patch_gap": timeline[
                        "relevant_to_patch_gap"
                    ],
                    "plan_to_patch_gap": (
                        first_patch - first_plan
                        if first_patch is not None
                        and first_plan is not None
                        else None
                    ),
                    "hypothesis_to_patch_gap": (
                        commitment.hypothesis_to_patch_gap
                        if commitment
                        else None
                    ),
                    "plan_revision_count": len(result.plan_revisions),
                    "hypothesis_revision_count": (
                        commitment.hypothesis_revision_count
                        if commitment
                        else 0
                    ),
                    "plan_execution_success": bool(
                        result.success and result.plan_revisions
                    ),
                    "patch_attempts": len(patches),
                    "patch_successes": sum(
                        event.success for event in patches
                    ),
                    "post_plan_exploration_calls": (
                        commitment.post_plan_exploration_calls
                        if commitment
                        else 0
                    ),
                    "no_patch_failure": bool(
                        not result.success
                        and not any(event.success for event in patches)
                    ),
                    "repeated_exploration_calls": timeline[
                        "redundant_exploration_calls"
                    ],
                    "plan_status_history": (
                        [
                            item.model_dump(mode="json")
                            for item in commitment.plan_status_history
                        ]
                        if commitment
                        else []
                    ),
                    "evidence_references": (
                        commitment.evidence_references
                        if commitment
                        else []
                    ),
                    "commitment_validation_errors": (
                        commitment.validation_errors
                        if commitment
                        else []
                    ),
                }
            )
    return records


def _mean(values):
    filtered = [value for value in values if value is not None]
    return sum(filtered) / len(filtered) if filtered else None


def _category_success(reports: list[BenchmarkReport]) -> dict:
    buckets = {
        "local": [0, 0],
        "cross_file": [0, 0],
        "structural": [0, 0],
    }
    for report in reports:
        task_map = {task.task_id: task for task in report.tasks}
        for result in report.results:
            tags = set(task_map[result.task_id].tags)
            category = next(
                (name for name in buckets if name in tags),
                None,
            )
            if category:
                buckets[category][1] += 1
                buckets[category][0] += int(result.success)
    return {
        name: {
            "passed": values[0],
            "total": values[1],
            "success_rate": values[0] / values[1] if values[1] else 0.0,
        }
        for name, values in buckets.items()
    }


def _status_tasks(records: list[dict], run_count: int) -> dict:
    grouped = {}
    for record in records:
        grouped.setdefault(record["task_id"], []).append(record["success"])
    stable_pass = sorted(
        task_id
        for task_id, values in grouped.items()
        if len(values) == run_count and all(values)
    )
    stable_fail = sorted(
        task_id
        for task_id, values in grouped.items()
        if len(values) == run_count and not any(values)
    )
    unstable = sorted(
        set(grouped) - set(stable_pass) - set(stable_fail)
    )
    return {
        "stable_pass": stable_pass,
        "stable_fail": stable_fail,
        "unstable": unstable,
    }


def _policy_metrics(
    records: list[dict],
    reports: list[BenchmarkReport],
) -> dict:
    passed = sum(item["success"] for item in records)
    total_tokens = sum(item["tokens"] for item in records)
    base = {
        "runs": len(reports),
        "task_runs": len(records),
        "passed": passed,
        "success_rate": passed / len(records) if records else 0.0,
        "average_passed_per_run": (
            passed / len(reports) if reports else 0.0
        ),
        "total_tokens": total_tokens,
        "average_task_tokens": (
            total_tokens / len(records) if records else 0.0
        ),
        "average_run_tokens": (
            total_tokens / len(reports) if reports else 0.0
        ),
        "average_llm_calls": _mean(
            [item["llm_calls"] for item in records]
        ) or 0.0,
        "average_tool_calls": _mean(
            [item["tool_calls"] for item in records]
        ) or 0.0,
        "average_duration": _mean(
            [item["duration_seconds"] for item in records]
        ) or 0.0,
    }
    located = [
        item
        for item in records
        if item["relevant_file_first_seen_step"] is not None
    ]
    plans = [item for item in records if item["first_plan_step"] is not None]
    hypotheses = [
        item
        for item in records
        if item["first_hypothesis_step"] is not None
    ]
    patched = [
        item for item in records if item["patch_successes"] > 0
    ]
    no_patch = [item for item in records if item["no_patch_failure"]]
    successful = [item for item in records if item["success"]]
    failures = Counter(
        item["failure_category"] or "unknown"
        for item in records
        if not item["success"]
    )
    failures["no_patch"] += len(no_patch)
    return {
        **base,
        "stable_success_tasks": _status_tasks(
            records,
            len(reports),
        )["stable_pass"],
        "stable_fail_tasks": _status_tasks(
            records,
            len(reports),
        )["stable_fail"],
        "unstable_tasks": _status_tasks(
            records,
            len(reports),
        )["unstable"],
        "localization_rate": (
            len(located) / len(records) if records else 0.0
        ),
        "runs_with_hypothesis": len(hypotheses),
        "runs_with_plan": len(plans),
        "runs_with_patch": len(patched),
        "patch_conversion_rate": (
            len(patched) / len(located) if located else 0.0
        ),
        "successful_patch_conversion": (
            len(successful) / len(located) if located else 0.0
        ),
        "hypothesis_to_patch_conversion": (
            sum(
                item["patch_successes"] > 0
                for item in hypotheses
            )
            / len(hypotheses)
            if hypotheses
            else None
        ),
        "patch_success_rate": (
            sum(item["success"] for item in patched) / len(patched)
            if patched
            else 0.0
        ),
        "test_pass_rate": (
            sum(item["success"] for item in records) / len(records)
            if records
            else 0.0
        ),
        "no_patch_failures": len(no_patch),
        "invalid_plan_runs": sum(
            any(
                "validation failed" in error
                or "not been observed" in error
                or "evidence" in error
                for error in item["commitment_validation_errors"]
            )
            for item in records
        ),
        "patch_plan_mismatch_runs": sum(
            "patch_target_mismatch"
            in item["commitment_validation_errors"]
            for item in records
        ),
        "broad_exploration_block_runs": sum(
            "broad_exploration_blocked"
            in item["commitment_validation_errors"]
            for item in records
        ),
        "average_first_failure_step": _mean(
            [
                item["first_failure_observation_step"]
                for item in records
            ]
        ),
        "average_first_hypothesis_step": _mean(
            [item["first_hypothesis_step"] for item in records]
        ),
        "average_first_plan_step": _mean(
            [item["first_plan_step"] for item in records]
        ),
        "average_first_patch_step": _mean(
            [item["first_patch_step"] for item in records]
        ),
        "average_relevant_to_hypothesis_gap": _mean(
            [item["relevant_to_hypothesis_gap"] for item in records]
        ),
        "average_relevant_to_plan_gap": _mean(
            [item["relevant_to_plan_gap"] for item in records]
        ),
        "average_plan_to_patch_gap": _mean(
            [item["plan_to_patch_gap"] for item in records]
        ),
        "average_hypothesis_to_patch_gap": _mean(
            [item["hypothesis_to_patch_gap"] for item in records]
        ),
        "average_relevant_to_patch_gap": _mean(
            [item["relevant_to_patch_gap"] for item in records]
        ),
        "average_post_plan_exploration": _mean(
            [item["post_plan_exploration_calls"] for item in records]
        ),
        "plan_completion_rate": (
            sum(item["plan_execution_success"] for item in plans)
            / len(plans)
            if plans
            else 0.0
        ),
        "hypothesis_revision_rate": (
            sum(
                item["hypothesis_revision_count"] > 0
                for item in hypotheses
            )
            / len(hypotheses)
            if hypotheses
            else None
        ),
        "tokens_per_success": (
            sum(item["tokens"] for item in records) / len(successful)
            if successful
            else None
        ),
        "category_success": _category_success(reports),
        "failure_categories": dict(sorted(failures.items())),
    }


def _task_comparison(
    control_records: list[dict],
    treatment_records: list[dict],
) -> list[dict]:
    control = {}
    treatment = {}
    for item in control_records:
        control.setdefault(item["task_id"], []).append(item)
    for item in treatment_records:
        treatment.setdefault(item["task_id"], []).append(item)
    rows = []
    for task_id in sorted(control):
        c = control[task_id]
        t = treatment[task_id]
        control_stable = all(item["success"] for item in c)
        treatment_stable = all(item["success"] for item in t)
        if control_stable and treatment_stable:
            classification = "both_stable_pass"
        elif not any(item["success"] for item in c) and not any(
            item["success"] for item in t
        ):
            classification = "both_stable_fail"
        elif treatment_stable and not control_stable:
            classification = "treatment_improved"
        elif control_stable and not treatment_stable:
            classification = "treatment_regressed"
        else:
            classification = "mixed_or_unstable"
        rows.append(
            {
                "task_id": task_id,
                "classification": classification,
                "control": c,
                "treatment": t,
            }
        )
    return rows


def _write_design(phase_dir: Path) -> None:
    _write(phase_dir / "commitment_design.md", COMMITMENT_DESIGN)


def _render_report(payload: dict) -> str:
    control = payload["metrics"]["control"]
    treatment = payload["metrics"]["treatment"]
    comparison = payload["comparison"]
    lines = [
        "# Phase 7 Evidence-driven Repair Commitment",
        "",
        "| Metric | Control | Treatment |",
        "|---|---:|---:|",
        f"| Success | {control['passed']}/{control['task_runs']} "
        f"({control['success_rate'] * 100:.1f}%) | "
        f"{treatment['passed']}/{treatment['task_runs']} "
        f"({treatment['success_rate'] * 100:.1f}%) |",
        f"| Patches | {control['runs_with_patch']} | "
        f"{treatment['runs_with_patch']} |",
        f"| No-Patch failures | {control['no_patch_failures']} | "
        f"{treatment['no_patch_failures']} |",
        f"| Avg Tokens/Task | {control['average_task_tokens']:,.0f} | "
        f"{treatment['average_task_tokens']:,.0f} |",
        "",
        "## 1. Hypothesis Timing",
        "",
        f"Treatment formed a structured Hypothesis in "
        f"{treatment['runs_with_hypothesis']}/{treatment['task_runs']} runs. "
        f"Average first Hypothesis step: "
        f"{_fmt(treatment['average_first_hypothesis_step'])}; average "
        f"relevant-to-Hypothesis gap: "
        f"{_fmt(treatment['average_relevant_to_hypothesis_gap'])}.",
        "Control has no structured Hypothesis field, so its Hypothesis timing "
        "is not available rather than zero.",
        "",
        "## 2. Plan Timing",
    ]
    lines.extend(
        [
            "",
            f"Average first Plan step fell from "
            f"{_fmt(control['average_first_plan_step'])} to "
            f"{_fmt(treatment['average_first_plan_step'])}. "
            f"Average relevant-to-Plan gap changed from "
            f"{_fmt(control['average_relevant_to_plan_gap'])} to "
            f"{_fmt(treatment['average_relevant_to_plan_gap'])}.",
            "",
            "## 3. Plan-to-Patch Gap",
            "",
            f"First Patch moved from step "
            f"{_fmt(control['average_first_patch_step'])} to "
            f"{_fmt(treatment['average_first_patch_step'])}. "
            f"Plan-to-Patch gap changed from "
            f"{_fmt(control['average_plan_to_patch_gap'])} to "
            f"{_fmt(treatment['average_plan_to_patch_gap'])}; "
            f"Hypothesis-to-Patch gap was "
            f"{_fmt(treatment['average_hypothesis_to_patch_gap'])}; "
            f"relevant-to-Patch gap changed from "
            f"{_fmt(control['average_relevant_to_patch_gap'])} to "
            f"{_fmt(treatment['average_relevant_to_patch_gap'])}.",
            "",
            "## 4. No-Patch Failure",
            "",
            f"No-Patch failures fell from {control['no_patch_failures']} to "
            f"{treatment['no_patch_failures']}. Patch conversion changed from "
            f"{control['patch_conversion_rate'] * 100:.1f}% to "
            f"{treatment['patch_conversion_rate'] * 100:.1f}%.",
            "",
            "## 5. Patch Correctness",
            "",
            f"Patch success rate changed from "
            f"{control['patch_success_rate'] * 100:.1f}% to "
            f"{treatment['patch_success_rate'] * 100:.1f}%. The Treatment "
            "attempted more Patches but a larger share were incorrect; this is "
            "a negative quality signal even though overall success improved.",
            "",
            "| Category | Control | Treatment |",
            "|---|---:|---:|",
        ]
    )
    for category in ("local", "cross_file", "structural"):
        c = control["category_success"][category]
        t = treatment["category_success"][category]
        lines.append(
            f"| {category} | {c['passed']}/{c['total']} | "
            f"{t['passed']}/{t['total']} |"
        )
    lines.extend(
        [
            "",
            "| Failure Category | Control | Treatment |",
            "|---|---:|---:|",
        ]
    )
    failure_keys = sorted(
        set(control["failure_categories"])
        | set(treatment["failure_categories"])
    )
    lines.extend(
        f"| {category} | "
        f"{control['failure_categories'].get(category, 0)} | "
        f"{treatment['failure_categories'].get(category, 0)} |"
        for category in failure_keys
    )
    lines.extend(
        [
            "",
            "## 6. Exploration Trade-off",
            "",
            f"Plan completion: {control['plan_completion_rate'] * 100:.1f}% → "
            f"{treatment['plan_completion_rate'] * 100:.1f}%. "
            f"Hypothesis-to-Patch conversion: "
            f"{_pct(treatment['hypothesis_to_patch_conversion'])}. "
            f"Hypothesis revision rate: "
            f"{_pct(treatment['hypothesis_revision_rate'])}. "
            f"Average post-Plan exploration: "
            f"{_fmt(control['average_post_plan_exploration'])} → "
            f"{_fmt(treatment['average_post_plan_exploration'])}. "
            f"Broad-exploration blocks: "
            f"{treatment['broad_exploration_block_runs']}. "
            f"Invalid Plan attempts: {treatment['invalid_plan_runs']}; "
            f"Patch/Plan mismatches: "
            f"{treatment['patch_plan_mismatch_runs']}.",
            f"Stable Pass/Fail/Unstable: Control "
            f"{len(control['stable_success_tasks'])}/"
            f"{len(control['stable_fail_tasks'])}/"
            f"{len(control['unstable_tasks'])}; Treatment "
            f"{len(treatment['stable_success_tasks'])}/"
            f"{len(treatment['stable_fail_tasks'])}/"
            f"{len(treatment['unstable_tasks'])}.",
            "",
            "## 7. Cost",
            "",
            "| Variant | Tokens | Prompt | Completion | LLM | Tools | Duration | "
            "Tokens/Success |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name in ("control", "treatment"):
        value = payload["metrics"][name]
        records = payload["records"][name]
        lines.append(
            f"| {name.title()} | {value['average_task_tokens']:,.0f} | "
            f"{sum(item['prompt_tokens'] for item in records):,} | "
            f"{sum(item['completion_tokens'] for item in records):,} | "
            f"{value['average_llm_calls']:.1f} | "
            f"{value['average_tool_calls']:.1f} | "
            f"{value['average_duration']:.1f}s | "
            f"{_num(value['tokens_per_success'])} |"
        )
    lines.extend(
        [
            "",
            "## 8. Improvements",
            "",
            "Tasks meeting a stable Treatment improvement: "
            + (
                ", ".join(
                    f"`{task_id}`"
                    for task_id in comparison["improved_tasks"]
                )
                or "none"
            )
            + ".",
            "",
            "## 9. Regressions",
            "",
            "Stable Treatment regressions: "
            + (
                ", ".join(
                    f"`{task_id}`"
                    for task_id in comparison["regressed_tasks"]
                )
                or "none"
            )
            + ".",
            "",
            "| Task | Control Runs | Treatment Runs | Classification |",
            "|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {item['task_id']} | "
        f"{', '.join('PASS' if value['success'] else 'FAIL' for value in item['control'])} | "
        f"{', '.join('PASS' if value['success'] else 'FAIL' for value in item['treatment'])} | "
        f"{item['classification']} |"
        for item in payload["task_comparison"]
    )
    lines.extend(
        [
            "",
            "## 10. Default Capability",
            "",
            payload["conclusion"],
            "",
            "The capability remains opt-in because incorrect-Patch risk and "
            "structural-task success regressed.",
            "",
            "## Recommended Next Step",
            "",
            payload["recommendation"],
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(value) -> str:
    return "N/A" if value is None else f"{value:.1f}"


def _num(value) -> str:
    return "N/A" if value is None else f"{value:,.0f}"


def _pct(value) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _build_payload(
    *,
    manifest: dict,
    control_reports: list[BenchmarkReport],
    treatment_reports: list[BenchmarkReport],
    tasks,
) -> dict:
    control_records = _task_records(
        control_reports,
        tasks,
        variant="control",
    )
    treatment_records = _task_records(
        treatment_reports,
        tasks,
        variant="treatment",
    )
    control = _policy_metrics(control_records, control_reports)
    treatment = _policy_metrics(treatment_records, treatment_reports)
    comparison = _task_comparison(control_records, treatment_records)
    success_delta = treatment["success_rate"] - control["success_rate"]
    no_patch_delta = (
        treatment["no_patch_failures"] - control["no_patch_failures"]
    )
    patch_conversion_delta = (
        treatment["patch_conversion_rate"]
        - control["patch_conversion_rate"]
    )
    gap_delta = (
        (treatment["average_relevant_to_patch_gap"] or 0.0)
        - (control["average_relevant_to_patch_gap"] or 0.0)
    )
    token_delta = (
        treatment["average_task_tokens"]
        - control["average_task_tokens"]
    )
    improved = [
        item["task_id"]
        for item in comparison
        if item["classification"] == "treatment_improved"
    ]
    regressed = [
        item["task_id"]
        for item in comparison
        if item["classification"] == "treatment_regressed"
    ]
    positive = (
        success_delta >= 0
        and no_patch_delta <= 0
        and (
            patch_conversion_delta > 0
            or gap_delta < 0
        )
    )
    structural_control = control["category_success"]["structural"]
    structural_treatment = treatment["category_success"]["structural"]
    structural_regression = (
        structural_treatment["success_rate"]
        < structural_control["success_rate"]
    )
    patch_accuracy_regression = (
        treatment["patch_success_rate"]
        < control["patch_success_rate"]
    )
    if success_delta < 0:
        conclusion = (
            f"Treatment reduced success by "
            f"{abs(success_delta) * 100:.1f} percentage points. Faster "
            "commitment or smaller gaps cannot be counted as a success."
        )
        recommendation = (
            "Retain the Control and refine failure-feedback quality before "
            "another commitment experiment."
        )
    elif positive and (
        structural_regression or patch_accuracy_regression
    ):
        conclusion = (
            "Treatment materially improved success, Plan timing, Patch "
            "conversion, and no-patch failures, but the stricter commitment "
            "also increased incorrect Patch risk: patch success rate fell and "
            "structural-task success regressed. This is a promising but mixed "
            "result, not sufficient for default rollout."
        )
        recommendation = (
            "Advance Repair Hypothesis Quality: validate why a proposed change "
            "follows from the observed source behavior before committing, while "
            "retaining the earlier evidence signal."
        )
    elif positive:
        conclusion = (
            "Treatment preserved or improved success while improving at least "
            "one conversion metric without increasing no-patch failures."
        )
        recommendation = (
            "Advance Evidence-driven Commitment as a default-capability "
            "candidate in the next controlled phase."
        )
    else:
        conclusion = (
            "The commitment mechanism did not produce a clear net repair-quality "
            "improvement; the result is exploratory and inconclusive."
        )
        recommendation = (
            "Keep the Control default and continue targeted repair-hypothesis "
            "quality research rather than adding more commitment gates."
        )
    return {
        "schema_version": 1,
        "phase": "7",
        "benchmark": "defects4j_v1",
        "source": manifest,
        "design": COMMITMENT_DESIGN,
        "records": {
            "control": control_records,
            "treatment": treatment_records,
        },
        "metrics": {
            "control": control,
            "treatment": treatment,
        },
        "comparison": {
            "success_delta": success_delta,
            "no_patch_delta": no_patch_delta,
            "patch_conversion_delta": patch_conversion_delta,
            "relevant_to_patch_gap_delta": gap_delta,
            "average_task_token_delta": token_delta,
            "improved_tasks": improved,
            "regressed_tasks": regressed,
        },
        "task_comparison": comparison,
        "conclusion": conclusion,
        "recommendation": recommendation,
        "default_capability_recommended": bool(
            positive
            and not structural_regression
            and not patch_accuracy_regression
        ),
        "regression_signals": {
            "structural_success_regression": structural_regression,
            "patch_success_rate_regression": patch_accuracy_regression,
        },
    }


def _write_artifacts(phase_dir: Path, payload: dict) -> Path:
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "task_level_analysis.json", {
        "metrics": payload["metrics"],
        "comparison": payload["comparison"],
        "tasks": payload["task_comparison"],
    })
    _write_json(phase_dir / "phase7_report.json", payload)
    _write(phase_dir / "commitment_design.md", COMMITMENT_DESIGN)
    path = phase_dir / "phase7_report.md"
    _write(path, _render_report(payload))
    return path


def run_phase7(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    tasks = load_external_tasks(tasks_path)
    phase_dir = _phase_dir(output_dir)
    manifest = _source_manifest(tasks_path, tasks)
    _write_json(phase_dir / "source_manifest.json", manifest)
    _write_design(phase_dir)
    control_reports = _control_reports()
    treatment_reports = [
        _run_treatment(tasks, phase_dir, run_name)
        for run_name in TREATMENT_RUNS
    ]
    payload = _build_payload(
        manifest=manifest,
        control_reports=control_reports,
        treatment_reports=treatment_reports,
        tasks=tasks,
    )
    path = _write_artifacts(phase_dir, payload)
    print(f"Phase 7 report: {path}")
    return path


def regenerate_phase7_report(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks = load_external_tasks(Path(tasks_file).expanduser().resolve())
    phase_dir = _phase_dir(output_dir)
    manifest = json.loads(
        (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
    )
    treatment_reports = [
        _load_report(
            phase_dir
            / "runs"
            / f"phase7_{run_name}"
            / "report.json"
        )
        for run_name in TREATMENT_RUNS
    ]
    payload = _build_payload(
        manifest=manifest,
        control_reports=_control_reports(),
        treatment_reports=treatment_reports,
        tasks=tasks,
    )
    return _write_artifacts(phase_dir, payload)
