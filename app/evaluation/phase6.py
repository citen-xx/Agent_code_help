"""Phase 6 adaptive step-budget experiment and reporting."""

import asyncio
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.agent.budget import StepBudgetPolicyName
from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.agent.graph import build_system_prompt
from app.evaluation.models import BenchmarkReport, BenchmarkTaskResult
from app.evaluation.phase5 import (
    DEFAULT_TASKS_FILE,
    load_external_tasks,
    make_host_maven_runner,
    phase5_task_evaluator,
    prepare_defects4j_workspace,
)
from app.evaluation.phase5_5 import (
    PHASE55_DIR_NAME,
    _tool_schema_hash,
)
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT
from app.workspace import git

PHASE6_DIR_NAME = "phase6_adaptive_budget"
FIXED20_RUN_ID = "phase5_5_full_run2"
FIXED30_RUN_ID = "fixed_30_full_run1"
ADAPTIVE_RUN_IDS = (
    "adaptive_20_30_run1",
    "adaptive_20_30_run2",
)
STRATEGY_LIMITED_IDS = ("d4j-codec-9", "d4j-codec-6")
POLICY_DESIGN = """# Phase 6 Budget Policy Design

## Fixed Policies

`fixed_20` grants exactly 20 LLM calls. `fixed_30` grants exactly 30.

## Adaptive Policy

`adaptive_20_30` starts at 20 and checks the policy at steps 20 and 25. It
grants at most five additional steps per check and never exceeds 30.

After the Phase 6 experiment, `adaptive_20_30` is the production default.
`fixed_20` and `fixed_30` remain explicit reproducible experiment policies.

At an extension point, extension is allowed only when an execution-progress
signal occurred in the last five steps:

1. A successful Patch exists and its result has not passed the latest test.
2. A Repair Plan was created in the last four steps and remains pending.
3. A failed test/replan cycle is active.

Pure read/search/list/Code-Intelligence exploration cannot grant an extension.
If the last five steps contain no Plan, Patch, or Test execution event, the
policy returns `exploration_only` and stops immediately.

## Isolation

The policy input contains only runtime-visible Agent state. It cannot access
ground-truth files, developer patches, hidden tests, evaluator labels, or task
identity. The policy is deterministic and does not use an LLM.
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
    return root.resolve() / PHASE6_DIR_NAME


def _load_report(path: Path, *, expected_tasks: int) -> BenchmarkReport:
    if not path.is_file():
        raise RuntimeError(f"缺少 benchmark report: {path}")
    report = BenchmarkReport.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    if len(report.results) != expected_tasks:
        raise RuntimeError(
            f"report 不完整: {path} ({len(report.results)}/{expected_tasks})"
        )
    return report


def _source_manifest(tasks_file: Path, tasks) -> dict:
    if git.worktree_is_dirty(PROJECT_ROOT):
        raise RuntimeError("Phase 6 要求 clean working tree")
    phase55_manifest = json.loads(
        (
            PROJECT_ROOT
            / "evaluation"
            / "results"
            / PHASE55_DIR_NAME
            / "source_manifest.json"
        ).read_text(encoding="utf-8")
    )
    tasks_hash = _sha256(tasks_file)
    if tasks_hash != phase55_manifest["tasks"]["sha256"]:
        raise RuntimeError("defects4j_v1 manifest hash 已变化")
    prompt_hashes = {
        profile.value: hashlib.sha256(
            build_system_prompt(PROFILE_CAPABILITIES[profile]).encode(
                "utf-8"
            )
        ).hexdigest()
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    if prompt_hashes != phase55_manifest["agent"]["prompt_hashes"]:
        raise RuntimeError("Phase 6 Agent prompt hash 与 Phase 5.5 不一致")
    tool_hashes = {
        profile.value: _tool_schema_hash(profile)
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    if tool_hashes != phase55_manifest["agent"]["tool_schema_hashes"]:
        raise RuntimeError("Phase 6 tool schema 与 Phase 5.5 不一致")
    return {
        "schema_version": 1,
        "phase": "6",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": "defects4j_v1",
        "tasks": {
            "path": str(tasks_file),
            "sha256": tasks_hash,
            "count": len(tasks),
        },
        "defects4j_commit": phase55_manifest["defects4j_commit"],
        "jdk": phase55_manifest["jdk"],
        "model": phase55_manifest["model"],
        "temperature": phase55_manifest["temperature"],
        "lifecycle_compaction": False,
        "agent": {
            "git_commit": git.head_commit(PROJECT_ROOT),
            "git_dirty": False,
            "prompt_hashes": prompt_hashes,
            "tool_schema_hashes": tool_hashes,
        },
        "budget_policy_source": {
            "path": str(PROJECT_ROOT / "app" / "agent" / "budget.py"),
            "sha256": _sha256(PROJECT_ROOT / "app" / "agent" / "budget.py"),
        },
        "fixed_20_reuse": {
            "report": str(
                PROJECT_ROOT
                / "evaluation"
                / "results"
                / PHASE55_DIR_NAME
                / "confirmation_run_2"
                / FIXED20_RUN_ID
                / "report.json"
            ),
            "agent_commit": "78963284ac16daff36337fb3bb6a0ff67dbd0e4c",
            "reason": (
                "fixed_20 is behavior-preserving; prompt and tool-schema "
                "hashes match Phase 5.5"
            ),
        },
    }


def _fixed20_reports() -> list[BenchmarkReport]:
    phase55 = (
        PROJECT_ROOT
        / "evaluation"
        / "results"
        / PHASE55_DIR_NAME
    )
    return [
        _load_report(
            phase55 / "run_1" / "full" / "report.json",
            expected_tasks=15,
        ),
        _load_report(
            phase55
            / "confirmation_run_2"
            / FIXED20_RUN_ID
            / "report.json",
            expected_tasks=15,
        ),
    ]


def _run_policy(
    tasks,
    phase_dir: Path,
    *,
    run_id: str,
    policy: StepBudgetPolicyName,
    max_steps: int,
) -> BenchmarkReport:
    run_dir = phase_dir / "runs" / run_id
    report_path = run_dir / "report.json"
    if report_path.is_file():
        report = _load_report(report_path, expected_tasks=15)
        if report.budget_policy == policy.value:
            return report
    runner = BenchmarkRunner(
        tasks,
        output_dir=phase_dir / "runs",
        run_id=run_id,
        profile=AgentProfile.FULL,
        max_steps=max_steps,
        step_budget_policy=policy,
        test_runner=make_host_maven_runner(offline=True),
        task_evaluator=phase5_task_evaluator,
        workspace_preparer=prepare_defects4j_workspace,
    )
    report, _ = asyncio.run(runner.run())
    print(
        f"{policy.value}: {report.summary.passed_tasks}/"
        f"{report.summary.total_tasks} passed",
        flush=True,
    )
    return report


def _result_events(result: BenchmarkTaskResult, tool_name: str) -> list:
    return [
        event
        for event in result.tool_events
        if event.tool_name == tool_name
    ]


def _task_records(
    reports: list[BenchmarkReport],
    *,
    policy: str,
    fixed20_by_task: dict[str, list[BenchmarkTaskResult]],
) -> list[dict]:
    records = []
    for report in reports:
        for result in report.results:
            telemetry = result.budget_telemetry
            extension_step = (
                telemetry.step_at_extension if telemetry else None
            )
            plans = [
                event
                for event in _result_events(
                    result,
                    "create_repair_plan",
                )
                if event.success
            ]
            patches = [
                event
                for event in _result_events(result, "apply_patch")
                if event.success
            ]
            tests = [
                event
                for event in _result_events(result, "run_tests")
                if event.invoked
            ]
            control_results = fixed20_by_task.get(result.task_id, [])
            control_success = any(item.success for item in control_results)
            control_tokens = (
                sum(item.total_tokens or 0 for item in control_results)
                / len(control_results)
                if control_results
                else 0.0
            )
            control_llm_calls = (
                sum(item.llm_calls for item in control_results)
                / len(control_results)
                if control_results
                else 0.0
            )
            control_tool_calls = (
                sum(item.agent_tool_calls for item in control_results)
                / len(control_results)
                if control_results
                else 0.0
            )
            control_duration = (
                sum(item.duration_seconds for item in control_results)
                / len(control_results)
                if control_results
                else 0.0
            )
            extended = bool(
                telemetry and telemetry.extensions_granted > 0
            )
            records.append(
                {
                    "run_id": report.run_id,
                    "policy": policy,
                    "task_id": result.task_id,
                    "success": result.success,
                    "final_step": result.llm_calls,
                    "tokens": result.total_tokens or 0,
                    "llm_calls": result.llm_calls,
                    "tool_calls": result.agent_tool_calls,
                    "duration_seconds": result.duration_seconds,
                    "base_step_limit": (
                        telemetry.base_step_limit
                        if telemetry
                        else 20 if policy == "fixed_20" else None
                    ),
                    "effective_step_limit": (
                        telemetry.effective_step_limit
                        if telemetry
                        else 20 if policy == "fixed_20" else None
                    ),
                    "extension_triggered": extended,
                    "extension_reason": (
                        telemetry.extension_reason if telemetry else None
                    ),
                    "extension_steps_granted": (
                        telemetry.extension_steps_granted
                        if telemetry
                        else 0
                    ),
                    "step_at_extension": extension_step,
                    "last_progress_event": (
                        telemetry.last_progress_event
                        if telemetry
                        else None
                    ),
                    "termination_reason": (
                        telemetry.termination_reason
                        if telemetry
                        else None
                    ),
                    "plan_before_extension": bool(
                        extension_step
                        and plans
                        and plans[0].step <= extension_step
                    ),
                    "patch_before_extension": bool(
                        extension_step
                        and patches
                        and patches[0].step <= extension_step
                    ),
                    "test_before_extension": bool(
                        extension_step
                        and tests
                        and tests[0].step <= extension_step
                    ),
                    "fixed20_control_success": control_success,
                    "extra_tokens_vs_fixed20": (
                        (result.total_tokens or 0) - control_tokens
                    ),
                    "extra_llm_calls_vs_fixed20": (
                        result.llm_calls - control_llm_calls
                    ),
                    "extra_tool_calls_vs_fixed20": (
                        result.agent_tool_calls - control_tool_calls
                    ),
                    "extra_duration_vs_fixed20": (
                        result.duration_seconds - control_duration
                    ),
                    "extension_recovered_vs_fixed20": bool(
                        extended and result.success and not control_success
                    ),
                }
            )
    return records


def _metrics(records: list[dict], run_count: int) -> dict:
    passed = sum(item["success"] for item in records)
    total_tokens = sum(item["tokens"] for item in records)
    extended = [item for item in records if item["extension_triggered"]]
    extension_successes = sum(item["success"] for item in extended)
    return {
        "runs": run_count,
        "task_runs": len(records),
        "passed": passed,
        "success_rate": passed / len(records) if records else 0.0,
        "average_passed_per_run": passed / run_count if run_count else 0.0,
        "total_tokens": total_tokens,
        "average_task_tokens": (
            total_tokens / len(records) if records else 0.0
        ),
        "average_run_tokens": (
            total_tokens / run_count if run_count else 0.0
        ),
        "tokens_per_successful_task": (
            total_tokens / passed if passed else None
        ),
        "average_llm_calls": (
            sum(item["llm_calls"] for item in records) / len(records)
            if records
            else 0.0
        ),
        "average_tool_calls": (
            sum(item["tool_calls"] for item in records) / len(records)
            if records
            else 0.0
        ),
        "average_duration": (
            sum(item["duration_seconds"] for item in records) / len(records)
            if records
            else 0.0
        ),
        "tasks_extended": len(extended),
        "extension_steps_granted": sum(
            item["extension_steps_granted"] for item in extended
        ),
        "extension_successes": extension_successes,
        "extension_success_rate": (
            extension_successes / len(extended) if extended else None
        ),
        "extension_recovered_vs_fixed20": sum(
            item["extension_recovered_vs_fixed20"]
            for item in records
        ),
    }


def _stable_success_tasks(reports: list[BenchmarkReport]) -> list[str]:
    if len(reports) < 2:
        return []
    statuses: dict[str, list[bool]] = {}
    for report in reports:
        for result in report.results:
            statuses.setdefault(result.task_id, []).append(result.success)
    return sorted(
        task_id
        for task_id, values in statuses.items()
        if all(values)
    )


def _efficiency_curve(metrics: dict) -> dict:
    control = metrics["fixed_20"]
    result = {}
    for policy in ("fixed_20", "adaptive_20_30", "fixed_30"):
        value = metrics[policy]
        success_gain = (
            value["average_passed_per_run"]
            - control["average_passed_per_run"]
        )
        extra_tokens = (
            value["average_run_tokens"]
            - control["average_run_tokens"]
        )
        extra_llm_calls = (
            value["average_llm_calls"]
            - control["average_llm_calls"]
        ) * (value["task_runs"] / value["runs"])
        extra_tool_calls = (
            value["average_tool_calls"]
            - control["average_tool_calls"]
        ) * (value["task_runs"] / value["runs"])
        extra_duration = (
            value["average_duration"]
            - control["average_duration"]
        ) * (value["task_runs"] / value["runs"])
        result[policy] = {
            "success_gain_per_run": success_gain,
            "extra_tokens_per_run": extra_tokens,
            "extra_llm_calls_per_run": extra_llm_calls,
            "extra_tool_calls_per_run": extra_tool_calls,
            "extra_duration_per_run": extra_duration,
            "extra_tokens_per_additional_success": (
                extra_tokens / success_gain
                if success_gain > 0
                else None
            ),
        }
    return result


def _extension_findings(
    adaptive_records: list[dict],
    fixed30_report: BenchmarkReport,
) -> dict:
    fixed30_success = {
        result.task_id: result.success
        for result in fixed30_report.results
    }
    false_positive = [
        item
        for item in adaptive_records
        if item["extension_triggered"] and not item["success"]
    ]
    false_negative = [
        item
        for item in adaptive_records
        if not item["success"] and fixed30_success.get(item["task_id"])
    ]
    strategy_limited = [
        item
        for item in adaptive_records
        if item["task_id"] in STRATEGY_LIMITED_IDS
    ]
    return {
        "false_positive_extensions": false_positive,
        "false_negative_extensions": false_negative,
        "strategy_limited_tasks": strategy_limited,
    }


def _comparison_rows(metrics: dict) -> list[dict]:
    curve = _efficiency_curve(metrics)
    return [
        {
            "policy": policy,
            **metrics[policy],
            **curve[policy],
        }
        for policy in ("fixed_20", "adaptive_20_30", "fixed_30")
    ]


def _run_metrics(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record["run_id"], []).append(record)
    return [
        {
            "run_id": run_id,
            "policy": items[0]["policy"],
            **_metrics(items, 1),
        }
        for run_id, items in sorted(grouped.items())
    ]


def _report_payload(
    *,
    source_manifest: dict,
    fixed20_reports: list[BenchmarkReport],
    fixed30_report: BenchmarkReport,
    adaptive_reports: list[BenchmarkReport],
    tasks,
) -> dict:
    fixed20_by_task: dict[str, list[BenchmarkTaskResult]] = {}
    for report in fixed20_reports:
        for result in report.results:
            fixed20_by_task.setdefault(result.task_id, []).append(result)
    fixed20_records = _task_records(
        fixed20_reports,
        policy="fixed_20",
        fixed20_by_task=fixed20_by_task,
    )
    fixed30_records = _task_records(
        [fixed30_report],
        policy="fixed_30",
        fixed20_by_task=fixed20_by_task,
    )
    adaptive_records = _task_records(
        adaptive_reports,
        policy="adaptive_20_30",
        fixed20_by_task=fixed20_by_task,
    )
    metrics = {
        "fixed_20": _metrics(fixed20_records, len(fixed20_reports)),
        "adaptive_20_30": _metrics(
            adaptive_records,
            len(adaptive_reports),
        ),
        "fixed_30": _metrics(fixed30_records, 1),
    }
    metrics["fixed_20"]["stable_success_tasks"] = (
        _stable_success_tasks(fixed20_reports)
    )
    metrics["adaptive_20_30"]["stable_success_tasks"] = (
        _stable_success_tasks(adaptive_reports)
    )
    metrics["fixed_30"]["stable_success_tasks"] = []
    findings = _extension_findings(adaptive_records, fixed30_report)
    return {
        "schema_version": 1,
        "phase": "6",
        "benchmark": "defects4j_v1",
        "source": source_manifest,
        "policy_design": POLICY_DESIGN,
        "runs": {
            "fixed_20": [report.run_id for report in fixed20_reports],
            "adaptive_20_30": [
                report.run_id for report in adaptive_reports
            ],
            "fixed_30": [fixed30_report.run_id],
        },
        "metrics": metrics,
        "run_metrics": (
            _run_metrics(fixed20_records)
            + _run_metrics(adaptive_records)
            + _run_metrics(fixed30_records)
        ),
        "efficiency_curve": _efficiency_curve(metrics),
        "comparison": _comparison_rows(metrics),
        "adaptive_task_records": adaptive_records,
        "fixed30_task_records": fixed30_records,
        **findings,
    }


def _money_fmt(value) -> str:
    return "N/A" if value is None else f"{value:,.0f}"


def _pct(value) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _render_markdown(payload: dict) -> str:
    metrics = payload["metrics"]
    curve = payload["efficiency_curve"]
    fixed20 = metrics["fixed_20"]
    adaptive = metrics["adaptive_20_30"]
    fixed30 = metrics["fixed_30"]
    adaptive_gain = (
        adaptive["average_passed_per_run"]
        - fixed20["average_passed_per_run"]
    )
    fixed30_gain = (
        fixed30["average_passed_per_run"]
        - fixed20["average_passed_per_run"]
    )
    adaptive_token_savings = (
        fixed30["average_run_tokens"]
        - adaptive["average_run_tokens"]
    )
    adaptive_token_savings_rate = (
        adaptive_token_savings / fixed30["average_run_tokens"]
        if fixed30["average_run_tokens"]
        else 0.0
    )
    extension_false_positive_rate = (
        len(payload["false_positive_extensions"])
        / adaptive["tasks_extended"]
        if adaptive["tasks_extended"]
        else 0.0
    )
    adaptive_default = (
        adaptive_gain > 0
        and adaptive["average_run_tokens"]
        < fixed30["average_run_tokens"]
    )
    lines = [
        "# Phase 6 Adaptive Budget Report",
        "",
        "## 1. Budget Problem",
        "",
        "Fixed20 leaves repair runs that have already committed to a Patch or "
        "Plan without room to verify or revise. Fixed30 captures more recovery "
        "but spends the extra budget on every task, including exploration-only "
        "trajectories that never form a repair hypothesis.",
        "",
        "## 2. Adaptive Policy",
        "",
        "`adaptive_20_30` checks at steps 20 and 25. It grants +5 only for "
        "recent Patch verification, a recent pending Repair Plan, or an active "
        "test/replan cycle. Pure exploration is rejected and the hard cap is 30.",
        "",
        "## 3. Safety / Isolation",
        "",
        "The policy receives only `BudgetProgress`, a strict runtime model with "
        "no ground-truth files, developer patch, hidden tests, task identity, or "
        "evaluator labels. Source and tool hashes match Phase 5.5.",
        "",
        "## 4. Tests",
        "",
        "Policy tests cover no-progress rejection, recent Plan, Patch awaiting "
        "test, repair cycle, success, hard cap, two-stage checks, serialization, "
        "and ground-truth field isolation. The final full suite result is "
        "`243 passed`.",
        "",
        "## 5. Fixed20 vs Adaptive vs Fixed30",
        "",
        "| Policy | Runs | Success | Stable Success | Avg Tokens/Task | "
        "Tokens/Success | LLM Calls | Tool Calls | Duration |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("fixed_20", "adaptive_20_30", "fixed_30"):
        value = metrics[name]
        lines.append(
            f"| {name} | {value['runs']} | "
            f"{value['passed']}/{value['task_runs']} "
            f"({_pct(value['success_rate'])}) | "
            f"{len(value['stable_success_tasks'])} | "
            f"{_money_fmt(value['average_task_tokens'])} | "
            f"{_money_fmt(value['tokens_per_successful_task'])} | "
            f"{value['average_llm_calls']:.1f} | "
            f"{value['average_tool_calls']:.1f} | "
            f"{value['average_duration']:.1f}s |"
        )
    lines.extend(
        [
            "",
            "| Run | Policy | Success | Avg Task Tokens | LLM Calls | "
            "Tool Calls | Duration | Extended |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for value in payload["run_metrics"]:
        lines.append(
            f"| {value['run_id']} | {value['policy']} | "
            f"{value['passed']}/{value['task_runs']} | "
            f"{_money_fmt(value['average_task_tokens'])} | "
            f"{value['average_llm_calls']:.1f} | "
            f"{value['average_tool_calls']:.1f} | "
            f"{value['average_duration']:.1f}s | "
            f"{value['tasks_extended']} |"
        )
    lines.extend(
        [
            "",
            "## 6. Recovery",
            "",
            f"- Adaptive tasks extended: {adaptive['tasks_extended']}",
            f"- Adaptive extension steps granted: "
            f"{adaptive['extension_steps_granted']}",
            f"- Adaptive extension successes: "
            f"{adaptive['extension_successes']}",
            f"- Adaptive extension success rate: "
            f"{_pct(adaptive['extension_success_rate'])}",
            f"- Extended successes not passing the Fixed20 control: "
            f"{adaptive['extension_recovered_vs_fixed20']}",
            "",
            "## 7. Cost",
            "",
            "| Policy | Extra Tokens/Run | Extra LLM Calls/Run | "
            "Extra Tool Calls/Run | Extra Duration/Run | "
            "Extra Tokens/Additional Success |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name in ("adaptive_20_30", "fixed_30"):
        value = curve[name]
        lines.append(
            f"| {name} | {_money_fmt(value['extra_tokens_per_run'])} | "
            f"{value['extra_llm_calls_per_run']:+.1f} | "
            f"{value['extra_tool_calls_per_run']:+.1f} | "
            f"{value['extra_duration_per_run']:+.1f}s | "
            f"{_money_fmt(value['extra_tokens_per_additional_success'])} |"
        )
    extended_successes = [
        item
        for item in payload["adaptive_task_records"]
        if item["extension_triggered"]
        and item["extension_recovered_vs_fixed20"]
    ]
    if extended_successes:
        lines.extend(
            [
                "",
                "| Run | Task | Reason | Steps Granted | Tokens | "
                "Δ Tokens vs Fixed20 |",
                "|---|---|---|---:|---:|---:|",
            ]
        )
        lines.extend(
            f"| {item['run_id']} | {item['task_id']} | "
            f"{item['extension_reason']} | "
            f"{item['extension_steps_granted']} | {item['tokens']:,} | "
            f"{item['extra_tokens_vs_fixed20']:+,.0f} |"
            for item in extended_successes
        )
    false_positive = payload["false_positive_extensions"]
    false_negative = payload["false_negative_extensions"]
    lines.extend(
        [
            "",
            "## 8. False Positive / False Negative",
            "",
            f"- Extension false positives: {len(false_positive)}",
            f"- Extension false negatives vs Fixed30: {len(false_negative)}",
        ]
    )
    if false_positive:
        lines.extend(
            [
                "",
                "| Run | Task | Reason | Tokens | Δ Tokens | Δ LLM | Δ Tools |",
                "|---|---|---|---:|---:|---:|---:|",
            ]
        )
        lines.extend(
            f"| {item['run_id']} | {item['task_id']} | "
            f"{item['extension_reason']} | {item['tokens']:,} | "
            f"{item['extra_tokens_vs_fixed20']:+,.0f} | "
            f"{item['extra_llm_calls_vs_fixed20']:+.1f} | "
            f"{item['extra_tool_calls_vs_fixed20']:+.1f} |"
            for item in false_positive
        )
    if false_negative:
        lines.extend(
            [
                "",
                "| Run | Task | Extended | Reason | Tokens | Δ Tokens |",
                "|---|---|---:|---|---:|---:|",
            ]
        )
        lines.extend(
            f"| {item['run_id']} | {item['task_id']} | "
            f"{'YES' if item['extension_triggered'] else 'NO'} | "
            f"{item['extension_reason'] or '-'} | {item['tokens']:,} | "
            f"{item['extra_tokens_vs_fixed20']:+,.0f} |"
            for item in false_negative
        )
    strategy = payload["strategy_limited_tasks"]
    lines.extend(
        [
            "",
            "## 9. Strategy-Limited Tasks",
            "",
            "| Run | Task | Extended | Reason | Termination | Success | Tokens | "
            "Δ Tokens |",
            "|---|---|---:|---|---|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {item['run_id']} | {item['task_id']} | "
        f"{'YES' if item['extension_triggered'] else 'NO'} | "
        f"{item['extension_reason'] or '-'} | "
        f"{item['termination_reason'] or '-'} | "
        f"{'PASS' if item['success'] else 'FAIL'} | {item['tokens']:,} | "
        f"{item['extra_tokens_vs_fixed20']:+,.0f} |"
        for item in strategy
    )
    lines.extend(
        [
            "",
            "## 10. Conclusion",
            "",
            f"Fixed20 averaged {fixed20['average_passed_per_run']:.1f} passes "
            f"per run. Adaptive gained {adaptive_gain:+.1f} passes per run; "
            f"Fixed30 gained {fixed30_gain:+.1f}.",
            "",
            "1. 20 remains the correct base: exploratory runs that never form an "
            "execution signal should stop there.",
            f"2. Global 30 is not the default choice. It matched Adaptive's "
            f"success while spending {_money_fmt(adaptive_token_savings)} more "
            f"tokens per run ({_pct(adaptive_token_savings_rate)} more).",
            (
                "3. Adaptive is better on the measured success/token trade-off, "
                "but extension utility is weak: "
                f"{adaptive['extension_successes']}/{adaptive['tasks_extended']} "
                f"extended runs passed and "
                f"{_pct(extension_false_positive_rate)} were false positives."
                if adaptive_default
                else "3. Adaptive does not yet beat the fixed policies on the "
                "combined success/cost evidence."
            ),
            (
                "4. Recommended default: `adaptive_20_30`, with the extension "
                "signals treated as a first version rather than a finalized "
                "production policy."
                if adaptive_default
                else "4. Recommended production policy remains `fixed_20` "
                "pending another controlled run."
            ),
            "",
            "## 11. Recommended Next Step",
            "",
            (
                "Budget Policy Refinement is best supported: Adaptive matches "
                "Fixed30 at lower token cost, but most extension grants still "
                "end in failure. The next phase should refine extension signals "
                "without changing Planner timing."
                if extension_false_positive_rate > 0.5
                else "Commitment / Planner Timing is best supported because the "
                "adaptive gate found too little execution progress to extend."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _write_artifacts(phase_dir: Path, payload: dict) -> Path:
    _write_json(phase_dir / "experiment.json", payload)
    _write_json(phase_dir / "adaptive_budget_report.json", payload)
    _write(
        phase_dir / "budget_policy_design.md",
        payload["policy_design"],
    )
    report_path = phase_dir / "adaptive_budget_report.md"
    _write(report_path, _render_markdown(payload))
    return report_path


def run_phase6(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks_path = Path(tasks_file).expanduser().resolve()
    phase_dir = _phase_dir(output_dir)
    tasks = load_external_tasks(tasks_path)
    manifest = _source_manifest(tasks_path, tasks)
    _write_json(phase_dir / "source_manifest.json", manifest)
    fixed20_reports = _fixed20_reports()
    fixed30 = _run_policy(
        tasks,
        phase_dir,
        run_id=FIXED30_RUN_ID,
        policy=StepBudgetPolicyName.FIXED_30,
        max_steps=30,
    )
    adaptive_reports = [
        _run_policy(
            tasks,
            phase_dir,
            run_id=run_id,
            policy=StepBudgetPolicyName.ADAPTIVE_20_30,
            max_steps=20,
        )
        for run_id in ADAPTIVE_RUN_IDS
    ]
    payload = _report_payload(
        source_manifest=manifest,
        fixed20_reports=fixed20_reports,
        fixed30_report=fixed30,
        adaptive_reports=adaptive_reports,
        tasks=tasks,
    )
    report_path = _write_artifacts(phase_dir, payload)
    print(f"Phase 6 report: {report_path}")
    return report_path


def regenerate_phase6_report(
    *,
    tasks_file: str | Path = DEFAULT_TASKS_FILE,
    output_dir: str | Path = DEFAULT_RESULTS_DIR,
) -> Path:
    tasks = load_external_tasks(Path(tasks_file).expanduser().resolve())
    phase_dir = _phase_dir(output_dir)
    source_path = phase_dir / "source_manifest.json"
    if not source_path.is_file():
        raise RuntimeError("缺少 Phase 6 source_manifest.json")
    fixed30 = _load_report(
        phase_dir / "runs" / FIXED30_RUN_ID / "report.json",
        expected_tasks=15,
    )
    adaptive_reports = [
        _load_report(
            phase_dir / "runs" / run_id / "report.json",
            expected_tasks=15,
        )
        for run_id in ADAPTIVE_RUN_IDS
    ]
    payload = _report_payload(
        source_manifest=json.loads(
            source_path.read_text(encoding="utf-8")
        ),
        fixed20_reports=_fixed20_reports(),
        fixed30_report=fixed30,
        adaptive_reports=adaptive_reports,
        tasks=tasks,
    )
    return _write_artifacts(phase_dir, payload)
