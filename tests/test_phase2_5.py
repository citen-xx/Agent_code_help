"""Phase 2.5 confirmation and patch-event analysis tests."""

from app.agent.telemetry import PatchErrorCategory, ToolEvent
from app.agent.capabilities import AgentProfile
from app.evaluation.models import (
    BenchmarkReport,
    BenchmarkTask,
    BenchmarkTaskResult,
    FailureCategory,
)
from app.evaluation.tasks import PROJECT_ROOT
from app.evaluation.phase2_5 import (
    _resolve_output_root,
    _patch_records,
    _status_entries,
    _wilson_interval,
)
from app.evaluation.runner import BenchmarkRunner


def _task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        title=task_id,
        description=task_id,
        repository="evaluation/fixtures/arithmetic-logic/repo.bundle",
        base_commit="unused",
        problem_statement=task_id,
        suite="v2",
        difficulty="hard",
    )


def _result(
    task: BenchmarkTask,
    *,
    success: bool,
    failures: int = 0,
    events: list[ToolEvent] | None = None,
) -> BenchmarkTaskResult:
    return BenchmarkTaskResult(
        task_id=task.task_id,
        suite="v2",
        difficulty=task.difficulty,
        success=success,
        public_tests_passed=True,
        hidden_tests_passed=success,
        final_test_passed=True,
        duration_seconds=1,
        failure_category=None if success else FailureCategory.PATCH_FAILURE,
        tool_failure_counts={"apply_patch": failures} if failures else {},
        tool_usage={"apply_patch": failures + 1},
        tool_calls=len(events) if events else failures + 1,
        apply_patch_attempts=failures + 1,
        apply_patch_failures=failures,
        apply_patch_failure_rate=(
            failures / (failures + 1) if failures + 1 else 0.0
        ),
        tool_events=events or [],
    )


def _report(
    run_id: str,
    results: list[BenchmarkTaskResult],
    tasks: list[BenchmarkTask],
) -> BenchmarkReport:
    return BenchmarkReport(
        run_id=run_id,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
        tasks=tasks,
        results=results,
        summary=BenchmarkRunner._summary(results),
    )


def test_wilson_interval_is_bounded_and_narrows():
    small = _wilson_interval(16, 18)
    large = _wilson_interval(160, 180)
    assert small is not None and large is not None
    assert 0 <= small[0] < small[1] <= 1
    assert large[1] - large[0] < small[1] - small[0]


def test_clean_tree_status_ignores_branch_header():
    assert _status_entries("## main...origin/main [ahead 2]\n") == []
    assert _status_entries("## main\n M app.py\n") == [" M app.py"]


def test_relative_output_root_is_project_absolute():
    resolved = _resolve_output_root("evaluation/results")
    assert resolved.is_absolute()
    assert resolved == (PROJECT_ROOT / "evaluation" / "results").resolve()


def test_patch_records_include_sequence_and_missing_history():
    task = _task("task-a")
    legacy = _report(
        "legacy",
        [_result(task, success=False, failures=1)],
        [task],
    )
    events = [
        ToolEvent(
            tool_call_index=1,
            step=2,
            repair_round=0,
            tool_name="create_repair_plan",
            success=True,
            output_chars=100,
            argument_fingerprint="plan-0",
        ),
        ToolEvent(
            tool_call_index=2,
            step=3,
            repair_round=0,
            tool_name="run_tests",
            success=False,
            error_category=PatchErrorCategory.OTHER,
            output_chars=50,
            argument_fingerprint="auto",
            auto_generated=True,
        ),
        ToolEvent(
            tool_call_index=3,
            step=4,
            repair_round=0,
            tool_name="create_repair_plan",
            success=True,
            output_chars=100,
            argument_fingerprint="plan-1",
        ),
        ToolEvent(
            tool_call_index=4,
            step=5,
            repair_round=0,
            tool_name="apply_patch",
            target_file="A.java",
            success=False,
            error_category=PatchErrorCategory.OLD_TEXT_NOT_FOUND,
            error_summary="old_text appears 0 times",
            patch_applied=None,
            output_chars=80,
            argument_fingerprint="failed-patch",
        ),
        ToolEvent(
            tool_call_index=5,
            step=6,
            repair_round=1,
            tool_name="apply_patch",
            target_file="A.java",
            success=True,
            patch_applied=True,
            output_chars=90,
            argument_fingerprint="successful-patch",
        ),
    ]
    confirmation = _report(
        "confirmation",
        [_result(task, success=True, failures=1, events=events)],
        [task],
    )
    legacy_reports = _empty_profiles()
    legacy_reports[AgentProfile.FULL] = [legacy]
    records = _patch_records(
        legacy_reports,
        {AgentProfile.FULL: [confirmation]},
    )
    historical = next(
        record
        for record in records
        if record["source"] == "phase2_exploratory"
    )
    detailed = next(
        record
        for record in records
        if record["source"] == "phase2_5_confirmation"
    )
    assert historical["error_category"] == "unavailable"
    assert historical["step"] is None
    assert detailed["step"] == 5
    assert detailed["error_category"] == "old_text_not_found"
    assert detailed["after_automatic_test"] is True
    assert detailed["after_replan"] is True
    assert detailed["retried_after_failure"] is True
    assert detailed["later_successful_patch"] is True
    assert detailed["final_task_passed"] is True


def _empty_profiles():
    return {
        AgentProfile.BASIC: [],
        AgentProfile.CODE_INTELLIGENCE: [],
        AgentProfile.PLANNER: [],
        AgentProfile.FULL: [],
    }
