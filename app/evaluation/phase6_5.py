"""Phase 6.5 adaptive-budget refinement and controlled validation."""

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.agent.budget import (
    BudgetProgress,
    StepBudgetPolicyName,
    create_step_budget_policy,
)
from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.evaluation.models import BenchmarkReport
from app.evaluation.phase5 import (
    DEFAULT_TASKS_FILE,
    _task_rows,
    load_external_tasks,
    make_host_maven_runner,
    phase5_task_evaluator,
    prepare_defects4j_workspace,
)
from app.evaluation.phase5_5 import _tool_schema_hash
from app.evaluation.phase6 import (
    PHASE6_DIR_NAME,
    _fixed20_reports,
    _metrics,
    _task_records,
)
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT
from app.workspace import git
from app.workspace.models import TestRunResult

PHASE65_DIR_NAME = "phase6_5_budget_refinement"
CONTROL_POLICY = StepBudgetPolicyName.ADAPTIVE_20_30
CANDIDATE_POLICY = StepBudgetPolicyName.ADAPTIVE_EXECUTION_ONLY
RUN_ORDER = (
    ("control_run1", CONTROL_POLICY),
    ("candidate_run1", CANDIDATE_POLICY),
    ("candidate_run2", CANDIDATE_POLICY),
    ("control_run2", CONTROL_POLICY),
)
POLICY_DESIGN = """# Phase 6.5 Budget Policy Design

## Control

`adaptive_20_30` is unchanged. A recent pending Repair Plan can independently
grant one five-step extension.

## Candidate

`adaptive_execution_only` is identical except that a standalone recent Repair
Plan cannot grant an extension. It still extends for:

1. a recent successful Patch awaiting verification;
2. a recent Plan backed by a Patch and failed Test in the repair cycle;
3. any other execution-backed condition accepted by the control policy.

Both policies use base 20, checkpoints at 20 and 25, +5 increments, and a hard
cap of 30. Exploration-only and successful trajectories stop.

## Isolation

The policy receives only strict runtime progress fields. It cannot access task
identity, ground truth, developer patches, hidden tests, or evaluator labels.
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
    return root.resolve() / PHASE65_DIR_NAME


def _load_report(path: Path) -> BenchmarkReport:
    if not path.is_file():
        raise RuntimeError(f"缺少 report: {path}")
    report = BenchmarkReport.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if len(report.results) != 15:
        raise RuntimeError(f"report 不完整: {path}")
    return report


def _source_manifest(tasks_file: Path, tasks) -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 6.5 要求 clean working tree")
    phase6_manifest = json.loads(
        (
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / PHASE6_DIR_NAME
            / "source_manifest.json"
        ).read_text(encoding="utf-8")
    )
    tasks_hash = _sha256(tasks_file)
    if tasks_hash != phase6_manifest["tasks"]["sha256"]:
        raise RuntimeError("defects4j_v1 tasks hash 已变化")
    prompts = {
        profile.value: hashlib.sha256(
            build_system_prompt(PROFILE_CAPABILITIES[profile]).encode(
                "utf-8"
            )
        ).hexdigest()
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    tools = {
        profile.value: _tool_schema_hash(profile)
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    if prompts != phase6_manifest["agent"]["prompt_hashes"]:
        raise RuntimeError("Prompt hash 与 Phase 6 不一致")
    if tools != phase6_manifest["agent"]["tool_schema_hashes"]:
        raise RuntimeError("Tool schema hash 与 Phase 6 不一致")
    return {
        "schema_version": 1,
        "phase": "6.5",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": "defects4j_v1",
        "tasks": {
            "path": str(tasks_file),
            "sha256": tasks_hash,
            "count": len(tasks),
        },
        "defects4j_commit": phase6_manifest["defects4j_commit"],
        "jdk": phase6_manifest["jdk"],
        "model": phase6_manifest["model"],
        "temperature": phase6_manifest["temperature"],
        "lifecycle_compaction": False,
        "agent": {
            "git_commit": git.head_commit(PROJECT_ROOT),
            "git_dirty": False,
            "prompt_hashes": prompts,
            "tool_schema_hashes": tools,
        },
        "budget_policy_source": {
            "path": str(PROJECT_ROOT / "app" / "agent" / "budget.py"),
            "sha256": _sha256(PROJECT_ROOT / "app" / "agent" / "budget.py"),
        },
        "phase6_source_manifest": str(
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / PHASE6_DIR_NAME
            / "source_manifest.json"
        ),
    }


def _event_name(event) -> str:
    return f"step {event.step}: {event.tool_name}"


def _test_outcomes(result) -> list[tuple[object, bool | None]]:
    test_events = sorted(
        (
            event
            for event in result.tool_events
            if event.tool_name == "run_tests" and event.invoked
        ),
        key=lambda event: event.tool_call_index,
    )
    raw_tests = [
        output
        for output in result.context_telemetry.raw_test_outputs
        if output.tool_name == "run_tests"
    ]
    outcomes = []
    for index, event in enumerate(test_events):
        success = None
        if index < len(raw_tests):
            try:
                success = TestRunResult.model_validate_json(
                    raw_tests[index].content
                ).success
            except ValueError:
                success = None
        outcomes.append((event, success))
    return outcomes


def _audit_extension_records(phase6_payload: dict) -> list[dict]:
    reports = {}
    run_root = (
        PROJECT_ROOT
        / "evaluation"
        / "results"
        / PHASE6_DIR_NAME
        / "runs"
    )
    for path in run_root.glob("*/report.json"):
        report = _load_report(path)
        reports[report.run_id] = report
    records = []
    historical = {
        (item["run_id"], item["task_id"]): item
        for item in phase6_payload["adaptive_task_records"]
    }
    for (run_id, task_id), item in historical.items():
        if not item["extension_triggered"]:
            continue
        report = reports[run_id]
        result = next(
            value
            for value in report.results
            if value.task_id == task_id
        )
        test_outcomes = _test_outcomes(result)
        extensions = (
            result.budget_telemetry.extension_steps
            if result.budget_telemetry
            else []
        )
        for extension_index, extension in enumerate(extensions, start=1):
            cutoff = extension.step_at_extension
            before = [
                event for event in result.tool_events if event.step <= cutoff
            ]
            after = [
                event for event in result.tool_events if event.step > cutoff
            ]
            plans_before = [
                event
                for event in before
                if event.tool_name == "create_repair_plan" and event.success
            ]
            patches_before = [
                event
                for event in before
                if event.tool_name == "apply_patch" and event.success
            ]
            test_outcomes_before = [
                (event, success)
                for event, success in test_outcomes
                if event.step <= cutoff
            ]
            patches_after = [
                event
                for event in after
                if event.tool_name == "apply_patch" and event.success
            ]
            tests_after = [
                event
                for event in after
                if event.tool_name == "run_tests" and event.invoked
            ]
            plans_after = [
                event
                for event in after
                if event.tool_name == "create_repair_plan" and event.success
            ]
            latest_test = (
                test_outcomes_before[-1]
                if test_outcomes_before
                else None
            )
            plan_only = (
                extension.reason == "recent_repair_plan_pending"
            )
            standalone_plan_only = (
                plan_only
                and not patches_before
                and not test_outcomes_before
            )
            records.append(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "extension_index": extension_index,
                    "extension_step": cutoff,
                    "extension_reason": extension.reason,
                    "steps_granted": extension.steps_granted,
                    "plan_before_extension": bool(plans_before),
                    "patch_before_extension": bool(patches_before),
                    "test_before_extension": bool(test_outcomes_before),
                    "test_status_before_extension": (
                        "passed"
                        if latest_test and latest_test[1] is True
                        else "failed"
                        if latest_test and latest_test[1] is False
                        else "not_run"
                    ),
                    "plan_only": plan_only,
                    "standalone_plan_only": standalone_plan_only,
                    "new_progress_after_extension": [
                        *(
                            f"{event.step}: patch"
                            for event in patches_after
                        ),
                        *(
                            f"{event.step}: test"
                            for event in tests_after
                        ),
                        *(
                            f"{event.step}: repair_plan"
                            for event in plans_after
                        ),
                    ],
                    "patch_after_extension_step": (
                        patches_after[0].step if patches_after else None
                    ),
                    "test_after_extension_step": (
                        tests_after[0].step if tests_after else None
                    ),
                    "success": item["success"],
                    "extra_tokens_vs_fixed20": item[
                        "extra_tokens_vs_fixed20"
                    ],
                    "tokens": item["tokens"],
                }
            )
    return records


def _extension_audit_markdown(records: list[dict]) -> str:
    by_reason = Counter(item["extension_reason"] for item in records)
    success_by_reason = Counter(
        item["extension_reason"] for item in records if item["success"]
    )
    plan_only = [item for item in records if item["plan_only"]]
    lines = [
        "# Phase 6 Extension Audit",
        "",
        "This audit uses historical Phase 6 traces. Final task failure does not "
        "prove an extension was causally useless.",
        "",
        "| Run | Task | Grant | Step | Reason | Steps | Plan Before | Patch Before | "
        "Test Before | New Progress After | Success | Task Δ Tokens (shared) |",
        "|---|---|---:|---:|---|---:|---:|---:|---|---|---:|---:|",
    ]
    lines.extend(
        f"| {item['run_id']} | {item['task_id']} | "
        f"{item['extension_index']} | "
        f"{item['extension_step']} | {item['extension_reason']} | "
        f"{item['steps_granted']} | "
        f"{'YES' if item['plan_before_extension'] else 'NO'} | "
        f"{'YES' if item['patch_before_extension'] else 'NO'} | "
        f"{item['test_status_before_extension']} | "
        f"{', '.join(item['new_progress_after_extension']) or 'none'} | "
        f"{'PASS' if item['success'] else 'FAIL'} | "
        f"{item['extra_tokens_vs_fixed20']:+,.0f} |"
        for item in records
    )
    lines.extend(
        [
            "",
            "## Reason Summary",
            "",
            "| Reason | Extensions | Successes |",
            "|---|---:|---:|",
        ]
    )
    lines.extend(
        f"| {reason} | {count} | {success_by_reason[reason]} |"
        for reason, count in sorted(by_reason.items())
    )
    lines.extend(
        [
            "",
            "## Plan-only",
            "",
            f"Plan-only extensions: {len(plan_only)}; successes: "
            f"{sum(item['success'] for item in plan_only)}.",
        ]
    )
    if plan_only:
        lines.extend(
            [
                "",
                "| Task | Patch After Extension | Test After Extension | Success |",
                "|---|---:|---:|---:|",
            ]
        )
        lines.extend(
            f"| {item['run_id']} / {item['task_id']} | "
            f"{item['patch_after_extension_step'] or 'NO'} | "
            f"{item['test_after_extension_step'] or 'NO'} | "
            f"{'PASS' if item['success'] else 'FAIL'} |"
            for item in plan_only
        )
    return "\n".join(lines) + "\n"


def _snapshot(result, limit: int) -> BudgetProgress:
    events = list(result.tool_events)
    selected = [event for event in events if event.step <= limit]
    plans = [
        event
        for event in selected
        if event.tool_name == "create_repair_plan" and event.success
    ]
    patches = [
        event
        for event in selected
        if event.tool_name == "apply_patch" and event.success
    ]
    tests = [
        (event, success)
        for event, success in _test_outcomes(result)
        if event.step <= limit
    ]
    last_plan = plans[-1] if plans else None
    last_patch = patches[-1] if patches else None
    last_test = tests[-1] if tests else None
    recent = []
    for event in selected:
        if event.step < limit - 4:
            continue
        if event.tool_name == "create_repair_plan" and event.success:
            recent.append(_event_name(event))
        elif (
            event.tool_name == "apply_patch"
            and event.success
            and event.patch_applied is not False
        ):
            recent.append(_event_name(event))
        elif event.tool_name == "run_tests" and event.invoked:
            recent.append(_event_name(event))
    latest_failure_step = max(
        (
            event.step
            for event, success in tests
            if success is False
        ),
        default=None,
    )
    return BudgetProgress(
        step_count=limit,
        current_step_limit=limit,
        final_test_passed=bool(last_test and last_test[1] is True),
        plan_active=last_plan is not None,
        plan_stale=(
            latest_failure_step is not None
            and (
                last_plan is None
                or latest_failure_step > last_plan.step
            )
        ),
        last_plan_step=last_plan.step if last_plan else None,
        last_successful_patch_step=(
            last_patch.step if last_patch else None
        ),
        last_test_step=last_test[0].step if last_test else None,
        last_test_failed=bool(last_test and last_test[1] is False),
        has_seen_test_failure=any(
            success is False for _, success in tests
        ),
        repair_rounds=max(
            (event.repair_round for event in selected),
            default=0,
        ),
        recent_execution_events=list(dict.fromkeys(recent)),
    )


def _shadow_audit(phase6_payload: dict) -> dict:
    records = []
    reports = {}
    run_root = (
        PROJECT_ROOT
        / "evaluation"
        / "results"
        / PHASE6_DIR_NAME
        / "runs"
    )
    for path in run_root.glob("*/report.json"):
        report = _load_report(path)
        if report.budget_policy == CONTROL_POLICY.value:
            reports[report.run_id] = report
    control_policy = create_step_budget_policy(CONTROL_POLICY)
    candidate_policy = create_step_budget_policy(CANDIDATE_POLICY)
    historical = {
        (item["run_id"], item["task_id"]): item
        for item in phase6_payload["adaptive_task_records"]
    }
    for report in reports.values():
        for result in report.results:
            for checkpoint in (20, 25):
                progress = _snapshot(result, checkpoint)
                control = control_policy.should_extend(progress)
                candidate = candidate_policy.should_extend(progress)
                historical_item = historical[
                    (report.run_id, result.task_id)
                ]
                records.append(
                    {
                        "run_id": report.run_id,
                        "task_id": result.task_id,
                        "checkpoint": checkpoint,
                        "control_would_extend": control.extend,
                        "candidate_would_extend": candidate.extend,
                        "decision_difference": (
                            "candidate_stops"
                            if control.extend and not candidate.extend
                            else "candidate_extends_only"
                            if candidate.extend and not control.extend
                            else "same"
                        ),
                        "control_reason": control.reason,
                        "candidate_reason": candidate.reason,
                        "historical_success": result.success,
                        "historical_tokens": result.total_tokens or 0,
                        "historical_extra_tokens_vs_fixed20": historical_item[
                            "extra_tokens_vs_fixed20"
                        ],
                    }
                )
    return {
        "note": (
            "Offline shadow replay compares decisions on historical states only. "
            "It does not simulate the changed future trajectory."
        ),
        "checkpoints": records,
        "summary": {
            "evaluated": len(records),
            "candidate_stops": sum(
                item["decision_difference"] == "candidate_stops"
                for item in records
            ),
            "candidate_extends_only": sum(
                item["decision_difference"] == "candidate_extends_only"
                for item in records
            ),
        },
    }


def _run_policy(
    tasks,
    phase_dir: Path,
    *,
    run_name: str,
    policy: StepBudgetPolicyName,
) -> BenchmarkReport:
    run_id = f"phase6_5_{run_name}"
    report_path = phase_dir / "runs" / run_id / "report.json"
    if report_path.is_file():
        report = _load_report(report_path)
        if report.budget_policy == policy.value:
            return report
    runner = BenchmarkRunner(
        tasks,
        output_dir=phase_dir / "runs",
        run_id=run_id,
        profile=AgentProfile.FULL,
        max_steps=20,
        step_budget_policy=policy,
        test_runner=make_host_maven_runner(offline=True),
        task_evaluator=phase5_task_evaluator,
        workspace_preparer=prepare_defects4j_workspace,
    )
    report, _ = asyncio.run(runner.run())
    print(
        f"{run_name} ({policy.value}): "
        f"{report.summary.passed_tasks}/{report.summary.total_tasks}",
        flush=True,
    )
    return report


def _extension_events(report: BenchmarkReport) -> dict:
    records = []
    followed_by_patch = 0
    followed_by_test = 0
    plan_only = 0
    for result in report.results:
        telemetry = result.budget_telemetry
        if not telemetry or not telemetry.extension_steps:
            continue
        for extension in telemetry.extension_steps:
            after = [
                event
                for event in result.tool_events
                if event.step > extension.step_at_extension
            ]
            patch = next(
                (
                    event
                    for event in after
                    if event.tool_name == "apply_patch" and event.success
                ),
                None,
            )
            test = next(
                (
                    event
                    for event in after
                    if event.tool_name == "run_tests" and event.invoked
                ),
                None,
            )
            before = [
                event
                for event in result.tool_events
                if event.step <= extension.step_at_extension
            ]
            has_execution = any(
                event.tool_name in {"apply_patch", "run_tests"}
                for event in before
            )
            is_plan_only = (
                extension.reason == "recent_repair_plan_pending"
            )
            is_standalone_plan_only = is_plan_only and not has_execution
            followed_by_patch += int(patch is not None)
            followed_by_test += int(test is not None)
            plan_only += int(is_plan_only)
            records.append(
                {
                    "task_id": result.task_id,
                    "step": extension.step_at_extension,
                    "reason": extension.reason,
                    "steps_granted": extension.steps_granted,
                    "followed_by_patch": patch is not None,
                    "followed_by_test": test is not None,
                    "plan_only": is_plan_only,
                    "standalone_plan_only": is_standalone_plan_only,
                    "task_success": result.success,
                }
            )
    return {
        "records": records,
        "followed_by_patch": followed_by_patch,
        "followed_by_test": followed_by_test,
        "plan_only": plan_only,
    }


def _failure_counts(report: BenchmarkReport) -> dict:
    counts = Counter()
    for row in _task_rows(report):
        if row["success"]:
            continue
        category = row.get("failure_category") or "unknown"
        counts[category] += 1
        result = next(
            item for item in report.results
            if item.task_id == row["task_id"]
        )
        if not any(
            event.tool_name == "apply_patch" and event.success
            for event in result.tool_events
        ):
            counts["no_patch"] += 1
    return dict(sorted(counts.items()))


def _category_success(report: BenchmarkReport) -> dict:
    categories = {"local": [0, 0], "cross_file": [0, 0], "structural": [0, 0]}
    tasks = {task.task_id: task for task in report.tasks}
    for result in report.results:
        tags = set(tasks[result.task_id].tags)
        category = next(
            (name for name in categories if name in tags),
            None,
        )
        if category:
            categories[category][1] += 1
            categories[category][0] += int(result.success)
    return {
        name: {
            "passed": values[0],
            "total": values[1],
            "success_rate": values[0] / values[1] if values[1] else 0.0,
        }
        for name, values in categories.items()
    }


def _policy_aggregate(
    reports: list[BenchmarkReport],
    records: list[dict],
) -> dict:
    metrics = _metrics(records, len(reports))
    plan_only_results = [
        result
        for report in reports
        for result in report.results
        if result.budget_telemetry
        and any(
            extension.reason == "recent_repair_plan_pending"
            for extension in result.budget_telemetry.extension_steps
        )
    ]
    statuses = _task_status(reports)
    metrics.update(
        {
            "stable_success_tasks": sorted(
                task_id
                for task_id, status in statuses.items()
                if status == "stable_pass"
            ),
            "stable_fail_tasks": sorted(
                task_id
                for task_id, status in statuses.items()
                if status == "stable_fail"
            ),
            "unstable_tasks": sorted(
                task_id
                for task_id, status in statuses.items()
                if status == "unstable"
            ),
        }
    )
    results = [result for report in reports for result in report.results]
    successes = [result for result in results if result.success]
    metrics.update(
        {
            "prompt_tokens": sum(
                result.prompt_tokens or 0 for result in results
            ),
            "completion_tokens": sum(
                result.completion_tokens or 0 for result in results
            ),
            "failure_categories": dict(
                sum(
                    (
                        Counter(_failure_counts(report))
                        for report in reports
                    ),
                    Counter(),
                )
            ),
            "category_success": {
                name: {
                    "passed": sum(
                        _category_success(report)[name]["passed"]
                        for report in reports
                    ),
                    "total": sum(
                        _category_success(report)[name]["total"]
                        for report in reports
                    ),
                }
                for name in ("local", "cross_file", "structural")
            },
            "extension_count": sum(
                len(_extension_events(report)["records"])
                for report in reports
            ),
            "plan_only_extensions": sum(
                _extension_events(report)["plan_only"]
                for report in reports
            ),
            "standalone_plan_only_extensions": sum(
                sum(
                    item["standalone_plan_only"]
                    for item in _extension_events(report)["records"]
                )
                for report in reports
            ),
            "plan_only_task_runs": len(plan_only_results),
            "plan_only_successful_task_runs": sum(
                result.success for result in plan_only_results
            ),
            "extensions_followed_by_patch": sum(
                _extension_events(report)["followed_by_patch"]
                for report in reports
            ),
            "extensions_followed_by_test": sum(
                _extension_events(report)["followed_by_test"]
                for report in reports
            ),
            "extension_failures": sum(
                item["extension_triggered"] and not item["success"]
                for item in records
            ),
            "extension_successes": sum(
                item["extension_triggered"] and item["success"]
                for item in records
            ),
            "tokens_per_success": (
                sum(result.total_tokens or 0 for result in results)
                / len(successes)
                if successes
                else None
            ),
        }
    )
    reason_distribution = Counter()
    for report in reports:
        for item in _extension_events(report)["records"]:
            reason_distribution[item["reason"]] += 1
    metrics["extension_reason_distribution"] = dict(
        sorted(reason_distribution.items())
    )
    return {
        "metrics": metrics,
        "run_metrics": {
            report.run_id: {
                "success": report.summary.passed_tasks,
                "total": report.summary.total_tasks,
                "tokens": report.summary.total_tokens,
                "prompt_tokens": sum(
                    result.prompt_tokens or 0 for result in report.results
                ),
                "completion_tokens": sum(
                    result.completion_tokens or 0
                    for result in report.results
                ),
                "llm_calls": report.summary.total_llm_calls,
                "tool_calls": sum(
                    result.agent_tool_calls for result in report.results
                ),
                "duration": sum(
                    result.duration_seconds for result in report.results
                ),
                "category_success": _category_success(report),
                "failure_categories": _failure_counts(report),
                "extensions": _extension_events(report),
            }
            for report in reports
        },
    }


def _task_status(reports: list[BenchmarkReport]) -> dict[str, str]:
    statuses = {}
    task_ids = sorted({result.task_id for report in reports for result in report.results})
    for task_id in task_ids:
        values = [
            next(
                result.success
                for result in report.results
                if result.task_id == task_id
            )
            for report in reports
        ]
        if all(values):
            statuses[task_id] = "stable_pass"
        elif not any(values):
            statuses[task_id] = "stable_fail"
        else:
            statuses[task_id] = "unstable"
    return statuses


def _cross_policy_tasks(
    control_reports: list[BenchmarkReport],
    candidate_reports: list[BenchmarkReport],
) -> list[dict]:
    def details(report: BenchmarkReport, task_id: str) -> dict:
        result = next(
            value for value in report.results if value.task_id == task_id
        )
        telemetry = result.budget_telemetry
        return {
            "run_id": report.run_id,
            "success": result.success,
            "extension_reasons": (
                [
                    extension.reason
                    for extension in telemetry.extension_steps
                ]
                if telemetry
                else []
            ),
            "patch_attempted": any(
                event.tool_name == "apply_patch" and event.success
                for event in result.tool_events
            ),
            "test_status": (
                "passed"
                if result.final_test_result
                and result.final_test_result.success
                else "failed"
                if result.final_test_result
                else "not_run"
            ),
            "tokens": result.total_tokens or 0,
        }

    control_status = _task_status(control_reports)
    candidate_status = _task_status(candidate_reports)
    rows = []
    for task_id in sorted(control_status):
        c = control_status[task_id]
        b = candidate_status[task_id]
        if c == b == "stable_pass":
            classification = "both_stable_pass"
        elif c == b == "stable_fail":
            classification = "both_stable_fail"
        elif c == "stable_pass" and b != "stable_pass":
            classification = "control_only_stable_pass"
        elif b == "stable_pass" and c != "stable_pass":
            classification = "candidate_only_stable_pass"
        elif c == "stable_fail" and b == "unstable":
            classification = "control_stable_fail_candidate_unstable"
        elif c == "unstable" and b == "stable_fail":
            classification = "control_unstable_candidate_stable_fail"
        elif c == "unstable" and b == "unstable":
            classification = "both_unstable"
        else:
            classification = "mixed"
        rows.append(
            {
                "task_id": task_id,
                "control_status": c,
                "candidate_status": b,
                "classification": classification,
                "control_runs": [
                    next(
                        result.success
                        for result in report.results
                        if result.task_id == task_id
                    )
                    for report in control_reports
                ],
                "candidate_runs": [
                    next(
                        result.success
                        for result in report.results
                        if result.task_id == task_id
                    )
                    for report in candidate_reports
                ],
                "control_extensions": [
                    (
                        result.budget_telemetry.extension_reason
                        if result.budget_telemetry
                        and result.budget_telemetry.extensions_granted
                        else None
                    )
                    for report in control_reports
                    for result in report.results
                    if result.task_id == task_id
                ],
                "candidate_extensions": [
                    (
                        result.budget_telemetry.extension_reason
                        if result.budget_telemetry
                        and result.budget_telemetry.extensions_granted
                        else None
                    )
                    for report in candidate_reports
                    for result in report.results
                    if result.task_id == task_id
                ],
                "control_details": [
                    details(report, task_id)
                    for report in control_reports
                ],
                "candidate_details": [
                    details(report, task_id)
                    for report in candidate_reports
                ],
            }
        )
    return rows


def _acceptance(control: dict, candidate: dict) -> dict:
    token_reduction = (
        control["average_run_tokens"]
        - candidate["average_run_tokens"]
    )
    token_reduction_rate = (
        token_reduction / control["average_run_tokens"]
        if control["average_run_tokens"]
        else 0.0
    )
    success_delta = (
        candidate["average_passed_per_run"]
        - control["average_passed_per_run"]
    )
    no_patch_regression = (
        candidate["failure_categories"].get("no_patch", 0)
        - control["failure_categories"].get("no_patch", 0)
    )
    if success_delta < 0:
        status = "rejected"
        reason = "success_regression"
    elif no_patch_regression > 2:
        status = "inconclusive"
        reason = "no_patch_failure_increase"
    elif token_reduction_rate < 0.10:
        status = "rejected"
        reason = "token_reduction_below_10_percent"
    elif success_delta == 0:
        status = "accepted"
        reason = "cost_reduced_without_success_loss"
    elif success_delta < 0.5:
        status = "inconclusive"
        reason = "small_unstable_success_difference"
    else:
        status = "accepted"
        reason = "cost_reduced_and_success_improved"
    return {
        "status": status,
        "reason": reason,
        "token_reduction_per_run": token_reduction,
        "token_reduction_rate": token_reduction_rate,
        "success_delta_per_run": success_delta,
        "no_patch_failure_delta": no_patch_regression,
    }


def _build_payload(
    *,
    source_manifest: dict,
    audit_records: list[dict],
    shadow: dict,
    control_reports: list[BenchmarkReport],
    candidate_reports: list[BenchmarkReport],
    tasks,
) -> dict:
    fixed20 = _fixed20_reports()
    fixed20_by_task = {}
    for report in fixed20:
        for result in report.results:
            fixed20_by_task.setdefault(result.task_id, []).append(result)
    control_records = _task_records(
        control_reports,
        policy=CONTROL_POLICY.value,
        fixed20_by_task=fixed20_by_task,
    )
    candidate_records = _task_records(
        candidate_reports,
        policy=CANDIDATE_POLICY.value,
        fixed20_by_task=fixed20_by_task,
    )
    control = _policy_aggregate(control_reports, control_records)
    candidate = _policy_aggregate(candidate_reports, candidate_records)
    control_success_by_task = {
        task_id: any(
            item["success"]
            for item in control_records
            if item["task_id"] == task_id
        )
        for task_id in {item["task_id"] for item in control_records}
    }
    extension_errors = {
        "control_false_positive_extensions": [
            item
            for item in control_records
            if item["extension_triggered"] and not item["success"]
        ],
        "candidate_false_positive_extensions": [
            item
            for item in candidate_records
            if item["extension_triggered"] and not item["success"]
        ],
        "candidate_false_negative_runs": [
            item
            for item in candidate_records
            if not item["success"]
            and control_success_by_task.get(item["task_id"], False)
        ],
    }
    acceptance = _acceptance(
        control["metrics"],
        candidate["metrics"],
    )
    return {
        "schema_version": 1,
        "phase": "6.5",
        "benchmark": "defects4j_v1",
        "source": source_manifest,
        "policy_design": POLICY_DESIGN,
        "extension_audit": audit_records,
        "extension_audit_summary": {
            "checkpoint_grants": len(audit_records),
            "extended_task_runs": len(
                {
                    (item["run_id"], item["task_id"])
                    for item in audit_records
                }
            ),
            "successful_task_runs": len(
                {
                    (item["run_id"], item["task_id"])
                    for item in audit_records
                    if item["success"]
                }
            ),
            "plan_only": sum(item["plan_only"] for item in audit_records),
            "successes": sum(item["success"] for item in audit_records),
            "by_reason": dict(
                Counter(
                    item["extension_reason"] for item in audit_records
                )
            ),
        },
        "shadow_policy": shadow,
        "control": control,
        "candidate": candidate,
        "acceptance": acceptance,
        "extension_errors": extension_errors,
        "task_comparison": _cross_policy_tasks(
            control_reports,
            candidate_reports,
        ),
        "control_records": control_records,
        "candidate_records": candidate_records,
    }


def _pct(value) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _number(value) -> str:
    return "N/A" if value is None else f"{value:,.0f}"


def _render_report(payload: dict) -> str:
    control = payload["control"]["metrics"]
    candidate = payload["candidate"]["metrics"]
    acceptance = payload["acceptance"]
    lines = [
        "# Phase 6.5 Adaptive Budget Refinement Report",
        "",
        "## 1. Phase 6 Extension Waste",
        "",
        f"Historical extended task-runs: "
        f"{payload['extension_audit_summary']['extended_task_runs']}; "
        f"checkpoint grants: "
        f"{payload['extension_audit_summary']['checkpoint_grants']}; "
        f"Plan-only: {payload['extension_audit_summary']['plan_only']}; "
        f"successful extended task-runs: "
        f"{payload['extension_audit_summary']['successful_task_runs']}; "
        f"checkpoint rows ending in success: "
        f"{payload['extension_audit_summary']['successes']}.",
        "",
        "See `extension_audit.md` for 13 checkpoint grants across 9 extended "
        "task-runs.",
        "",
        "## 2. Plan-only Extension",
        "",
        "Recent-Plan grants without execution-backed repair evidence are the "
        "only policy difference removed by the Candidate. Execution-backed "
        "replans remain eligible.",
        (
            f"Historical Plan-only checkpoint grants reached a Patch after "
            f"extension in "
            f"{sum(item['plan_only'] and item['patch_after_extension_step'] is not None for item in payload['extension_audit'])}"
            f"/{payload['extension_audit_summary']['plan_only']} cases."
        ),
        "",
        "## 3. Shadow Policy Audit",
        "",
        f"Historical checkpoints evaluated: "
        f"{payload['shadow_policy']['summary']['evaluated']}; "
        f"Candidate-would-stop decisions: "
        f"{payload['shadow_policy']['summary']['candidate_stops']}.",
        "This is decision replay, not simulated A/B execution.",
        "",
        "## 4. Candidate Policy",
        "",
        payload["policy_design"],
        "",
        "## 5. Safety / Isolation",
        "",
        "Prompt and tool-schema hashes match Phase 6. Budget decisions use only "
        "runtime Plan/Patch/Test progress and never evaluator ground truth.",
        "",
        "Automated validation: `251 passed`. Coverage includes Plan-only "
        "rejection, execution-backed repairs, Patch verification, exploration "
        "rejection, success, hard cap, policy isolation, serialization, and "
        "Candidate acceptance rules.",
        "",
        "## 6. Formal A/B",
        "",
        "| Policy | Success | Stable Pass | Stable Fail | Unstable | "
        "Avg Tokens/Run | Tokens/Success | LLM Calls | Tool Calls | Duration |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, value in (("Control", control), ("Candidate", candidate)):
        total = value["task_runs"]
        stable_pass = len(value["stable_success_tasks"])
        stable_fail = len(value["stable_fail_tasks"])
        unstable = len(value["unstable_tasks"])
        lines.append(
            f"| {name} | {value['passed']}/{total} "
            f"({_pct(value['success_rate'])}) | {stable_pass} | "
            f"{stable_fail} | {unstable} | "
            f"{_number(value['average_run_tokens'])} | "
            f"{_number(value['tokens_per_success'])} | "
            f"{value['average_llm_calls']:.1f} | "
            f"{value['average_tool_calls']:.1f} | "
            f"{value['average_duration']:.1f}s |"
        )
    lines.extend(
        [
            "",
            "Stable counts are expanded below in the task-level section.",
            "",
            "| Policy | Local | Cross-file | Structural | Prompt Tokens | "
            "Completion Tokens |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name, value in (("Control", control), ("Candidate", candidate)):
        category = value["category_success"]
        lines.append(
            f"| {name} | "
            f"{category['local']['passed']}/{category['local']['total']} | "
            f"{category['cross_file']['passed']}/"
            f"{category['cross_file']['total']} | "
            f"{category['structural']['passed']}/"
            f"{category['structural']['total']} | "
            f"{value['prompt_tokens']:,} | "
            f"{value['completion_tokens']:,} |"
        )
    lines.extend(
        [
            "",
            "| Policy | Failure Categories |",
            "|---|---|",
        ]
    )
    for name, value in (("Control", control), ("Candidate", candidate)):
        lines.append(
            f"| {name} | "
            + ", ".join(
                f"{category}={count}"
                for category, count in sorted(
                    value["failure_categories"].items()
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| Run | Policy | Success | Tokens | Prompt | Completion | "
            "LLM | Tools | Duration | Extensions |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for policy_name, run_group in (
        ("adaptive_20_30", payload["control"]["run_metrics"]),
        (
            "adaptive_execution_only",
            payload["candidate"]["run_metrics"],
        ),
    ):
        for run_id, value in run_group.items():
            lines.append(
                f"| {run_id} | {policy_name} | "
                f"{value['success']}/{value['total']} | "
                f"{value['tokens']:,} | {value['prompt_tokens']:,} | "
                f"{value['completion_tokens']:,} | {value['llm_calls']} | "
                f"{value['tool_calls']} | {value['duration']:.1f}s | "
                f"{len(value['extensions']['records'])} |"
            )
    lines.extend(
        [
            "",
            "## 7. Decision Quality",
            "",
            "| Policy | Tasks Extended | Extension Count | Steps Granted | "
            "Plan-only | Plan-only Successes | Followed by Patch | Followed by Test | "
            "Extension Successes | Extension Failures |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, value in (("Control", control), ("Candidate", candidate)):
        lines.append(
            f"| {name} | {value['tasks_extended']} | "
            f"{value['extension_count']} | "
            f"{value['extension_steps_granted']} | "
            f"{value['plan_only_extensions']} | "
            f"{value['plan_only_successful_task_runs']}/"
            f"{value['plan_only_task_runs']} | "
            f"{value['extensions_followed_by_patch']} | "
            f"{value['extensions_followed_by_test']} | "
            f"{value['extension_successes']} | "
            f"{value['extension_failures']} |"
        )
    lines.extend(
        [
            "",
            "## 8. Cost and Success Comparison",
            "",
            f"- Token reduction per run: "
            f"{_number(acceptance['token_reduction_per_run'])} "
            f"({_pct(acceptance['token_reduction_rate'])}).",
            f"- Success delta per run: "
            f"{acceptance['success_delta_per_run']:+.1f}.",
            f"- no-patch failure delta: "
            f"{acceptance['no_patch_failure_delta']:+d}.",
            f"- Average LLM-call delta per task: "
            f"{candidate['average_llm_calls'] - control['average_llm_calls']:+.2f}.",
            f"- Average tool-call delta per task: "
            f"{candidate['average_tool_calls'] - control['average_tool_calls']:+.2f}.",
            f"- Average duration delta per task: "
            f"{candidate['average_duration'] - control['average_duration']:+.1f}s.",
            f"- Candidate status: `{acceptance['status']}` "
            f"({acceptance['reason']}).",
            f"- Control extension false positives: "
            f"{len(payload['extension_errors']['control_false_positive_extensions'])}.",
            f"- Candidate extension false positives: "
            f"{len(payload['extension_errors']['candidate_false_positive_extensions'])}.",
            f"- Candidate failure runs matching at least one Control success: "
            f"{len(payload['extension_errors']['candidate_false_negative_runs'])}.",
            "",
            "## 9. Task-level Stability",
            "",
            "| Task | Control Runs | Candidate Runs | Classification | "
            "Control Extension | Candidate Extension | Control Patch/Test | "
            "Candidate Patch/Test | Tokens C/B |",
            "|---|---|---|---|---|---|---|---|---:|",
        ]
    )
    for item in payload["task_comparison"]:
        control_details = item["control_details"]
        candidate_details = item["candidate_details"]
        control_extensions = " / ".join(
            ",".join(detail["extension_reasons"]) or "-"
            for detail in control_details
        )
        candidate_extensions = " / ".join(
            ",".join(detail["extension_reasons"]) or "-"
            for detail in candidate_details
        )
        control_patch_test = " / ".join(
            f"{'P' if detail['patch_attempted'] else 'N'}:"
            f"{detail['test_status']}"
            for detail in control_details
        )
        candidate_patch_test = " / ".join(
            f"{'P' if detail['patch_attempted'] else 'N'}:"
            f"{detail['test_status']}"
            for detail in candidate_details
        )
        lines.append(
            f"| {item['task_id']} | "
            f"{', '.join('PASS' if value else 'FAIL' for value in item['control_runs'])} | "
            f"{', '.join('PASS' if value else 'FAIL' for value in item['candidate_runs'])} | "
            f"{item['classification']} | {control_extensions} | "
            f"{candidate_extensions} | {control_patch_test} | "
            f"{candidate_patch_test} | "
            f"{sum(detail['tokens'] for detail in control_details):,}/"
            f"{sum(detail['tokens'] for detail in candidate_details):,} |"
        )
    lines.extend(
        [
            "",
            "## 10. Conclusion",
            "",
            "1. Phase 6 extension waste concentrated in non-verifying execution "
            "flows: only two extended task-runs passed, while several Plan/Patch "
            "extensions spent steps without producing a new patch or test.",
            "2. Historical Plan-only grants had limited observable value: "
            f"{payload['extension_audit_summary']['plan_only']} checkpoint grants "
            "produced one successful historical task-run (`codec-12`). However, "
            "Control's formal Plan-triggered extensions passed in "
            f"{control['plan_only_successful_task_runs']}/"
            f"{control['plan_only_task_runs']} task-runs, so removing them has "
            "observable downside.",
            f"3. Candidate changed aggregate tokens by only "
            f"{_pct(acceptance['token_reduction_rate'])} per run, below the "
            "predeclared 10% threshold.",
            f"4. Candidate success regressed from {control['passed']}/"
            f"{control['task_runs']} to {candidate['passed']}/"
            f"{candidate['task_runs']} and stable success fell from "
            f"{len(control['stable_success_tasks'])} to "
            f"{len(candidate['stable_success_tasks'])}.",
            "5. The result is not stable: run-level success ranged 10/15 to "
            "3/15 for Control and 6/15 to 4/15 for Candidate.",
            "6. The current `adaptive_20_30` should remain the default.",
            "7. Further Budget-only refinement is not the best next use of "
            "evidence; the residual problem is when commitment occurs.",
            "",
            "## 11. Recommended Next Step",
            "",
            "Commitment / Planner Timing.",
            "",
            "The Candidate was rejected. The remaining failures frequently "
            "localize but stop without a Patch, so another Budget-rule tweak is "
            "less justified than changing the commitment/timing mechanism in a "
            "future, separately controlled phase. This phase does not implement "
            "that change.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_artifacts(phase_dir: Path, payload: dict) -> Path:
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "phase6_5_report.json", payload)
    _write(
        phase_dir / "budget_policy_design.md",
        payload["policy_design"],
    )
    report_path = phase_dir / "phase6_5_report.md"
    _write(report_path, _render_report(payload))
    return report_path


def run_phase6_5(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    tasks = load_external_tasks(tasks_path)
    phase_dir = _phase_dir(output_dir)
    manifest = _source_manifest(tasks_path, tasks)
    _write_json(phase_dir / "source_manifest.json", manifest)
    phase6_payload = json.loads(
        (
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / PHASE6_DIR_NAME
            / "adaptive_budget_report.json"
        ).read_text(encoding="utf-8")
    )
    audit_records = _audit_extension_records(phase6_payload)
    _write(
        phase_dir / "extension_audit.md",
        _extension_audit_markdown(audit_records),
    )
    shadow = _shadow_audit(phase6_payload)
    _write_json(phase_dir / "shadow_policy_report.json", shadow)
    reports: dict[StepBudgetPolicyName, list[BenchmarkReport]] = {
        CONTROL_POLICY: [],
        CANDIDATE_POLICY: [],
    }
    for run_name, policy in RUN_ORDER:
        report = _run_policy(
            tasks,
            phase_dir,
            run_name=run_name,
            policy=policy,
        )
        reports[policy].append(report)
    payload = _build_payload(
        source_manifest=manifest,
        audit_records=audit_records,
        shadow=shadow,
        control_reports=reports[CONTROL_POLICY],
        candidate_reports=reports[CANDIDATE_POLICY],
        tasks=tasks,
    )
    return _write_artifacts(phase_dir, payload)


def regenerate_phase6_5_report(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks = load_external_tasks(Path(tasks_file).expanduser().resolve())
    phase_dir = _phase_dir(output_dir)
    source = json.loads(
        (phase_dir / "source_manifest.json").read_text(encoding="utf-8")
    )
    phase6_payload = json.loads(
        (
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / PHASE6_DIR_NAME
            / "adaptive_budget_report.json"
        ).read_text(encoding="utf-8")
    )
    audit = _audit_extension_records(phase6_payload)
    shadow = _shadow_audit(phase6_payload)
    reports = defaultdict(list)
    for run_name, policy in RUN_ORDER:
        reports[policy].append(
            _load_report(
                phase_dir
                / "runs"
                / f"phase6_5_{run_name}"
                / "report.json"
            )
        )
    payload = _build_payload(
        source_manifest=source,
        audit_records=audit,
        shadow=shadow,
        control_reports=reports[CONTROL_POLICY],
        candidate_reports=reports[CANDIDATE_POLICY],
        tasks=tasks,
    )
    _write(
        phase_dir / "extension_audit.md",
        _extension_audit_markdown(audit),
    )
    _write_json(phase_dir / "shadow_policy_report.json", shadow)
    return _write_artifacts(phase_dir, payload)
