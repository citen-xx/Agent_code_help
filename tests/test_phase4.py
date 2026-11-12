"""Phase 4 context telemetry and report tests."""

from app.agent.telemetry import ToolEvent
from app.agent.capabilities import AgentProfile
from app.evaluation.models import (
    BenchmarkReport,
    BenchmarkTask,
    BenchmarkTaskResult,
)
from app.evaluation.phase4 import (
    CI_TOOLS,
    _ci_stats,
    _risk_classification,
    render_context_audit,
    render_context_contract,
    render_phase4_report,
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
            "average_tokens": {"control": 1000, "treatment": 900, "delta": -100},
            "success_rate": {"control": 0.8, "treatment": 0.8, "delta": 0},
        },
        "planner": {
            "average_tokens": {"control": 1000, "treatment": 990, "delta": -10},
            "success_rate": {"control": 0.8, "treatment": 0.8, "delta": 0},
        },
    }
    control_context = {
        "code_intelligence": {
            "read_file_calls": 2,
            "total_ci_output_chars": 1000,
        },
        "planner": {
            "read_file_calls": 2,
            "total_ci_output_chars": 1000,
        },
    }
    treatment_context = {
        "code_intelligence": {
            "read_file_calls": 2,
            "total_ci_output_chars": 400,
        },
        "planner": {
            "read_file_calls": 10,
            "total_ci_output_chars": 400,
        },
    }
    risk = _risk_classification(
        comparison,
        control_context,
        treatment_context,
    )
    assert risk["code_intelligence"]["classification"] == "efficient_win"
    assert risk["planner"]["classification"] == "possible_tool_substitution"


def test_report_uses_measured_deltas_in_conclusion():
    comparison_keys = (
        "success_rate",
        "hard_success_rate",
        "hidden_test_pass_rate",
        "average_prompt_tokens",
        "average_completion_tokens",
        "average_tokens",
        "average_llm_calls",
        "average_agent_tool_calls",
        "average_duration",
    )
    profiles = ("code_intelligence", "planner", "full")
    comparison = {
        profile: {
            key: {
                "control": 1.0 if key.endswith("rate") else 100.0,
                "treatment": (
                    1.0
                    if key.endswith("rate")
                    else 90.0
                ),
                "delta": 0.0 if key.endswith("rate") else -10.0,
            }
            for key in comparison_keys
        }
        for profile in profiles
    }
    tool_stats = {
        tool: {
            "invocation_count": 3,
            "total_output_chars": 300,
            "avg_output_chars": 100.0,
            "p50_output_chars": 100,
            "p95_output_chars": 100,
            "max_output_chars": 100,
        }
        for tool in CI_TOOLS
    }
    context = {
        "total_ci_output_chars": 600,
        "read_file_calls": 3,
        "follow_up_source_reads": 1,
    }
    payload = {
        "experiment_id": "phase4",
        "model": "test",
        "benchmark": "v2",
        "source": {"treatment": {"git_commit": "abc"}},
        "control_audit": {
            "per_tool": tool_stats,
            "total_ci_output_chars": 600,
        },
        "treatment_audit": {"per_tool": tool_stats},
        "comparison": comparison,
        "control_metrics": {
            profile: {"metrics": {"task_runs": 45}}
            for profile in profiles
        },
        "control_context": {profile: dict(context) for profile in profiles},
        "treatment_context": {
            profile: {**context, "total_ci_output_chars": 300}
            for profile in profiles
        },
        "task_comparison": {profile: {} for profile in profiles},
        "risk_classification": {
            profile: {
                "classification": "efficient_win",
                "token_delta_percent": -0.1,
                "ci_output_delta_percent": -0.5,
                "read_file_call_delta": 0,
                "success_delta": 0.0,
            }
            for profile in profiles
        },
        "run_records": [],
    }
    report = render_phase4_report(payload)
    assert "CI output moved -50.0%" in report
    assert "Efficient-win profiles: code_intelligence, planner, full" in report
