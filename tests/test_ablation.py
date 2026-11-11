"""Aggregation and report tests for capability ablation experiments."""

import json
from pathlib import Path

from app.agent.capabilities import AgentProfile, PROFILE_CAPABILITIES
from app.evaluation.ablation import (
    _aggregate_variant,
    _experiment_payload,
    _write_experiment,
)
from app.evaluation.models import (
    BenchmarkReport,
    BenchmarkTask,
    BenchmarkTaskResult,
    FailureCategory,
)
from app.evaluation.runner import BenchmarkRunner


def _task(task_id: str, difficulty: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        title=task_id,
        description=task_id,
        repository="evaluation/fixtures/arithmetic-logic/repo.bundle",
        base_commit="unused",
        problem_statement=task_id,
        suite="v2",
        difficulty=difficulty,
    )


def _result(
    task: BenchmarkTask,
    *,
    success: bool,
    tokens: int,
    tool_usage: dict[str, int],
    tool_failures: dict[str, int] | None = None,
) -> BenchmarkTaskResult:
    return BenchmarkTaskResult(
        task_id=task.task_id,
        suite="v2",
        difficulty=task.difficulty,
        success=success,
        public_tests_passed=success,
        hidden_tests_passed=success,
        final_test_passed=success,
        duration_seconds=10,
        failure_category=None if success else FailureCategory.PUBLIC_TEST_FAILURE,
        llm_calls=4,
        total_tokens=tokens,
        tool_calls=sum(tool_usage.values()),
        tool_usage=tool_usage,
        tool_failure_counts=tool_failures or {},
        changed_file_count=1,
    )


def _report(
    run_id: str,
    profile: AgentProfile,
    tasks: list[BenchmarkTask],
    results: list[BenchmarkTaskResult],
) -> BenchmarkReport:
    return BenchmarkReport(
        run_id=run_id,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
        model="test-model",
        benchmark="v2",
        variant=profile.value,
        capabilities=PROFILE_CAPABILITIES[profile].as_dict(),
        tasks=tasks,
        results=results,
        summary=BenchmarkRunner._summary(results),
    )


def test_variant_aggregation_tracks_stability_and_tools(tmp_path):
    easy = _task("task-a", "medium")
    hard = _task("task-b", "hard")
    reports = [
        _report(
            "run-1",
            AgentProfile.BASIC,
            [easy, hard],
            [
                _result(
                    easy,
                    success=True,
                    tokens=100,
                    tool_usage={"read_file": 2, "apply_patch": 1},
                ),
                _result(
                    hard,
                    success=False,
                    tokens=300,
                    tool_usage={"read_file": 4, "apply_patch": 2},
                    tool_failures={"apply_patch": 1},
                ),
            ],
        ),
        _report(
            "run-2",
            AgentProfile.BASIC,
            [easy, hard],
            [
                _result(
                    easy,
                    success=True,
                    tokens=120,
                    tool_usage={"read_file": 1, "apply_patch": 1},
                ),
                _result(
                    hard,
                    success=True,
                    tokens=200,
                    tool_usage={"read_file": 2, "apply_patch": 1},
                ),
            ],
        ),
    ]
    paths = []
    for report in reports:
        path = tmp_path / report.run_id / "report.json"
        path.parent.mkdir()
        path.write_text(report.model_dump_json(), encoding="utf-8")
        paths.append(path)

    aggregate = _aggregate_variant(
        AgentProfile.BASIC,
        reports,
        paths,
        tmp_path,
    )
    assert aggregate["metrics"]["task_runs"] == 4
    assert aggregate["metrics"]["passed"] == 3
    assert aggregate["metrics"]["success_rate"] == 0.75
    assert aggregate["metrics"]["median_tokens"] == 160
    assert aggregate["metrics"]["total_tool_calls"] == 14
    assert aggregate["metrics"]["failed_tool_calls"] == 1
    assert aggregate["tool_usage"] == {"apply_patch": 5, "read_file": 9}
    assert aggregate["task_results"]["task-a"]["passes"] == 2
    assert aggregate["task_results"]["task-b"]["passes"] == 1
    assert aggregate["stability"]["stable_pass"] == ["task-a"]
    assert aggregate["stability"]["stable_fail"] == []
    assert aggregate["stability"]["unstable"] == ["task-b"]


def test_experiment_artifacts_include_profile_metadata(tmp_path):
    task = _task("task-a", "medium")
    profile = AgentProfile.CODE_INTELLIGENCE
    results = [
        _result(
            task,
            success=True,
            tokens=100,
            tool_usage={"find_symbol": 1, "apply_patch": 1},
        )
    ]
    report = _report("run-1", profile, [task], results)
    report_path = tmp_path / "variant_code_intelligence" / "run-1" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(report.model_dump_json(), encoding="utf-8")
    payload = _experiment_payload(
        experiment_id="ablation-test",
        started_at="2026-01-01T00:00:00+00:00",
        model="test-model",
        benchmark="v2",
        runs_per_variant=2,
        git_commit="abc123",
        git_dirty=True,
        reports_by_variant={profile: [report]},
        paths_by_variant={profile: [report_path]},
        experiment_dir=tmp_path,
    )
    _write_experiment(tmp_path, payload)

    saved = json.loads((tmp_path / "experiment.json").read_text(encoding="utf-8"))
    variant = saved["variants"]["code_intelligence"]
    assert saved["git_commit"] == "abc123"
    assert saved["runs_per_variant"] == 2
    assert variant["capabilities"] == {
        "code_intelligence": True,
        "repair_planner": False,
        "repair_loop": False,
    }
    assert variant["runs"][0]["path"] == (
        "variant_code_intelligence/run-1/report.json"
    )
    markdown = (tmp_path / "ablation_report.md").read_text(encoding="utf-8")
    assert "# Agent Ablation Study" in markdown
    assert "Code Intelligence" in markdown
    assert "Research Questions" in markdown
