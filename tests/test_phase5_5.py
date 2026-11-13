"""Tests for Phase 5.5 stability and search-to-patch diagnostics."""

import json

from app.agent.capabilities import AgentProfile
from app.agent.telemetry import ToolEvent
from app.evaluation.models import (
    BenchmarkReport,
    BenchmarkTask,
    BenchmarkTaskResult,
    ContextTelemetry,
)
from app.evaluation.phase5_5 import (
    _ci_planner_observation,
    _overlaps,
    _step_budget_records,
    _stability,
    _stability_summary,
    _timeline,
)
from app.evaluation.runner import BenchmarkRunner


def _task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        title=task_id,
        description=task_id,
        repository="unused",
        base_commit="unused",
        problem_statement="fix",
        suite="defects4j_v1",
        source="defects4j",
        external_difficulty="local",
        ground_truth_files=["src/main/java/example/Foo.java"],
    )


def _event(
    index: int,
    name: str,
    *,
    step: int,
    target: str | None = None,
    query: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> ToolEvent:
    return ToolEvent(
        tool_call_index=index,
        step=step,
        repair_round=0,
        tool_name=name,
        target_file=target,
        query=query,
        success=True,
        invoked=True,
        output_chars=100,
        argument_fingerprint=str(index),
        read_start_line=start,
        read_end_line=end,
        elapsed_seconds=float(step),
    )


def _result(task_id: str, events: list[ToolEvent], success: bool):
    return BenchmarkTaskResult(
        task_id=task_id,
        suite="defects4j_v1",
        success=success,
        final_test_passed=success,
        tool_calls=len(events),
        agent_tool_calls=len(events),
        tool_events=events,
        changed_files=["src/main/java/example/Foo.java"] if success else [],
        duration_seconds=10,
        llm_calls=5,
        context_telemetry=ContextTelemetry(),
    )


def _report(task: BenchmarkTask, result: BenchmarkTaskResult) -> BenchmarkReport:
    return BenchmarkReport(
        run_id="run",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        tasks=[task],
        results=[result],
        summary=BenchmarkRunner._summary([result]),
    )


def _multi_report(
    tasks: list[BenchmarkTask],
    results: list[BenchmarkTaskResult],
) -> BenchmarkReport:
    report = _report(tasks[0], results[0])
    report.tasks = tasks
    report.results = results
    report.summary = BenchmarkRunner._summary(results)
    return report


def test_timeline_detects_localization_patch_gap_and_repetition():
    task = _task("task-a")
    events = [
        _event(1, "search_code", step=1, query="Foo"),
        _event(
            2,
            "read_file",
            step=2,
            target="src/main/java/example/Foo.java",
            start=10,
            end=20,
        ),
        _event(3, "search_code", step=3, query=" Foo "),
        _event(
            4,
            "read_file",
            step=4,
            target="src/main/java/example/Foo.java",
            start=12,
            end=20,
        ),
        _event(
            5,
            "apply_patch",
            step=5,
            target="src/main/java/example/Foo.java",
        ),
    ]
    report = _report(task, _result(task.task_id, events, False))
    timeline = _timeline(task, report, report.results[0])
    assert timeline["first_relevant_file_step"] == 2
    assert timeline["first_patch_step"] == 5
    assert timeline["relevant_to_patch_gap"] == 3
    assert timeline["repeated_read_calls"] == 1
    assert timeline["repeated_search_calls"] == 1
    assert timeline["exploration_waste_ratio"] == 0.5
    assert timeline["query_telemetry_available"] is True


def test_overlap_and_stability_classification():
    assert _overlaps(10, 20, 12, 20)
    assert not _overlaps(10, 12, 18, 20)
    tasks = [_task("a"), _task("b"), _task("c")]
    first = [
        _result("a", [], True),
        _result("b", [], False),
        _result("c", [], True),
    ]
    second = [
        _result("a", [], True),
        _result("b", [], False),
        _result("c", [], False),
    ]
    run1 = {
        profile: _multi_report(tasks, first)
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    run2 = {
        profile: _multi_report(tasks, second)
        for profile in (AgentProfile.BASIC, AgentProfile.FULL)
    }
    result = _stability(run1, run2)
    assert result["basic"]["a"]["classification"] == "stable_pass"
    assert result["basic"]["b"]["classification"] == "stable_fail"
    assert result["basic"]["c"]["classification"] == "unstable"
    summary = _stability_summary(result)["basic"]
    assert summary == {
        "stable_pass": 1,
        "stable_fail": 1,
        "unstable": 1,
    }


def test_timeline_keeps_no_patch_runs_and_serializes():
    task = _task("task-a")
    events = [
        _event(
            1,
            "read_file",
            step=1,
            target="src/main/java/example/Foo.java",
            start=1,
            end=20,
        ),
        _event(2, "search_code", step=2, query="Foo"),
    ]
    report = _report(task, _result(task.task_id, events, False))
    timeline = _timeline(task, report, report.results[0])
    assert timeline["first_relevant_file_step"] == 1
    assert timeline["first_patch_step"] is None
    assert timeline["relevant_to_patch_gap"] is None
    assert timeline["patch_attempted"] is False
    assert timeline["post_relevant_search_calls"] == 1
    assert json.loads(json.dumps(timeline))["task_id"] == "task-a"


def test_ci_observation_excludes_runs_without_code_intelligence_tools():
    tasks = [_task("with-ci"), _task("without-ci")]
    with_ci = _result(
        "with-ci",
        [_event(1, "find_symbol", step=1, query="Foo")],
        False,
    )
    without_ci = _result(
        "without-ci",
        [_event(1, "read_file", step=1, target="README.md")],
        True,
    )
    report = _multi_report(tasks, [with_ci, without_ci])
    observation = _ci_planner_observation(report, tasks)
    assert observation["ci_used"]["task_runs"] == 1
    assert observation["ci_used"]["success_rate"] == 0.0
    assert observation["ci_not_used"]["task_runs"] == 1
    assert observation["ci_not_used"]["success_rate"] == 1.0


def test_30_step_results_remain_separate_from_20_step_results():
    task = _task("task-a")
    failed_20 = _result(task.task_id, [], False)
    success_30 = _result(
        task.task_id,
        [_event(1, "apply_patch", step=24, target="src/main/java/example/Foo.java")],
        True,
    )
    run20 = _report(task, failed_20)
    run30 = _report(task, success_30)
    records = _step_budget_records(run30, run20)
    assert records[0]["success_at_20"] is False
    assert records[0]["success_at_30"] is True
    assert records[0]["classification"] == "budget_limited"
    assert records[0]["patch_only_after_step_20"] is True
