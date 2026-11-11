"""Phase 4 context telemetry and report tests."""

from app.agent.telemetry import ToolEvent
from app.agent.capabilities import AgentProfile
from app.evaluation.models import (
    BenchmarkReport,
    BenchmarkTask,
    BenchmarkTaskResult,
)
from app.evaluation.phase4 import (
    _ci_stats,
    _risk_classification,
    render_context_audit,
    render_context_contract,
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
    )


def _event(
    index: int,
    name: str,
    chars: int,
    *,
    result_count: int,
    truncated: bool = False,
    duplicates: int = 0,
) -> ToolEvent:
    return ToolEvent(
        tool_call_index=index,
        step=index,
        repair_round=0,
        tool_name=name,
        success=True,
        invoked=True,
        output_chars=chars,
        result_count=result_count,
        truncated=truncated,
        duplicate_results_removed=duplicates,
        argument_fingerprint=str(index),
    )


def _report() -> BenchmarkReport:
    task = _task("task-a")
    events = [
        _event(1, "list_symbols", 1000, result_count=20, truncated=True),
        _event(2, "find_references", 300, result_count=5, duplicates=2),
        _event(3, "read_file", 200, result_count=0),
    ]
    result = BenchmarkTaskResult(
        task_id=task.task_id,
        suite="v2",
        difficulty="medium",
        success=True,
        public_tests_passed=True,
        hidden_tests_passed=True,
        final_test_passed=True,
        duration_seconds=1,
        tool_calls=3,
        tool_events=events,
        total_ci_output_chars=1300,
        read_file_calls=1,
        follow_up_source_reads=1,
    )
    return BenchmarkReport(
        run_id="run",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        tasks=[task],
        results=[result],
        summary=BenchmarkRunner._summary([result]),
    )


def test_ci_stats_measure_output_and_metadata():
    stats = _ci_stats([_report()])
    assert stats["per_tool"]["list_symbols"]["invocation_count"] == 1
    assert stats["per_tool"]["list_symbols"]["max_output_chars"] == 1000
    assert stats["per_tool"]["list_symbols"]["truncated_count"] == 1
    assert stats["per_tool"]["find_references"]["duplicate_results_removed"] == 2
    assert stats["total_ci_output_chars"] == 1300
    assert stats["per_task"]["task-a"]["read_file_calls"] == 1


def test_context_audit_and_design_are_progressive():
    audit = render_context_audit(
        {AgentProfile.CODE_INTELLIGENCE: [_report()]}
    )
    assert "list_symbols" in audit
    assert "Highest CI-Output Tasks" in audit
    design = render_context_contract()
    assert "default 30" in design
    assert "16,000" in design
    assert "No LLM" in design


def test_risk_classification_detects_tool_substitution():
    comparison = {
        "code_intelligence": {
            "average_tokens": {"control": 1000, "treatment": 990, "delta": -10},
            "success_rate": {"control": 0.8, "treatment": 0.8, "delta": 0},
        }
    }
    context = {
        "code_intelligence": {
            "read_file_calls": 10,
        }
    }
    risk = _risk_classification(comparison, context)
    assert risk["code_intelligence"]["classification"] == "possible_tool_substitution"
