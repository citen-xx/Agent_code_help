"""Offline tests for Benchmark Runner, reporting, and usage tracking."""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages import ToolMessage

from app.agent.graph import _execution_metrics
from app.agent.capabilities import AgentProfile
from app.evaluation.models import (
    AgentRunOutput,
    BenchmarkTask,
    FailureCategory,
    LLMUsage,
)
from app.evaluation.failure_analysis import generate_failure_analysis
from app.evaluation.runner import BenchmarkRunner
from app.evaluation.tasks import load_tasks
from app.evaluation.usage import UsageTracker, UsageTrackingChatModel
from app.schemas.agent import AnalyzeResponse
from app.workspace.models import BuildTool, TestRunResult as MavenTestResult
from tests.fakes import FakeChatModel


def _test_result(passed: bool, *, infrastructure: bool = False) -> MavenTestResult:
    if infrastructure:
        return MavenTestResult(
            tool=BuildTool.MAVEN,
            command=["mvn", "test"],
            exit_code=None,
            success=False,
            error="Docker daemon unavailable",
            duration_seconds=0.1,
        )
    return MavenTestResult(
        tool=BuildTool.MAVEN,
        command=["mvn", "test"],
        exit_code=0 if passed else 1,
        stdout="BUILD SUCCESS" if passed else "Failures: 1",
        stderr="",
        success=passed,
        duration_seconds=0.2,
    )


class SequenceTestRunner:
    def __init__(self, results: list[MavenTestResult]):
        self.results = list(results)
        self.calls = 0

    def __call__(self, repository_path, tool, timeout):
        self.calls += 1
        if not self.results:
            raise AssertionError("Unexpected test runner call")
        return self.results.pop(0)


class RecordingSequenceTestRunner(SequenceTestRunner):
    def __init__(self, results):
        super().__init__(results)
        self.paths: list[Path] = []

    def __call__(self, repository_path, tool, timeout):
        self.paths.append(Path(repository_path))
        return super().__call__(repository_path, tool, timeout)


class StubAgentRunner:
    def __init__(
        self,
        response: AnalyzeResponse | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0,
        usage: LLMUsage | None = None,
    ):
        self.response = response or AnalyzeResponse(answer="fixed", steps=2)
        self.error = error
        self.delay = delay
        self.usage = usage or LLMUsage()
        self.repositories: list[Path] = []
        self.calls = 0

    async def __call__(self, task, repository_path, workspace_id, manager):
        self.calls += 1
        self.repositories.append(Path(repository_path))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return AgentRunOutput(response=self.response, usage=self.usage)


def _tasks():
    return load_tasks()[:2]


async def test_single_task_success_and_report(tmp_path):
    task = _tasks()[0]
    agent = StubAgentRunner(
        AnalyzeResponse(
            answer="fixed",
            steps=3,
            tool_calls=5,
            last_agent_action='apply_patch {"changes":[]}',
            modified_files=["src/main/java/fixture/Calculator.java"],
            repair_rounds=1,
            diff="diff --git a/x b/x",
        ),
        usage=LLMUsage(
            llm_calls=3,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
        ),
    )
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner(
            [_test_result(False), _test_result(True)]
        ),
        agent_runner=agent,
        run_id="success-run",
        profile=AgentProfile.BASIC,
    )
    report, path = await runner.run()
    assert report.summary.passed_tasks == 1
    assert report.summary.failed_tasks == 0
    assert report.summary.success_rate == 1.0
    assert report.summary.total_tokens == 120
    assert report.results[0].success is True
    assert report.results[0].final_test_passed is True
    assert report.results[0].repair_rounds == 1
    assert report.results[0].llm_calls == 3
    assert report.results[0].tool_calls == 5
    assert report.results[0].last_agent_action == 'apply_patch {"changes":[]}'
    assert report.summary.average_llm_calls == 3.0
    assert report.summary.average_tokens == 120.0
    assert path.name == "report.json"
    assert path.parent.name == "success-run"
    assert report.variant == "basic"
    assert report.capabilities == {
        "code_intelligence": False,
        "repair_planner": False,
        "repair_loop": False,
    }
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"]["passed_tasks"] == 1
    assert payload["results"][0]["task_id"] == task.task_id
    summary_payload = json.loads(
        (path.parent / "summary.json").read_text(encoding="utf-8")
    )
    assert summary_payload["total_tasks"] == 1
    assert summary_payload["average_llm_calls"] == 3.0
    assert summary_payload["average_tokens"] == 120.0
    assert summary_payload["variant"] == "basic"
    tasks_payload = json.loads(
        (path.parent / "tasks.json").read_text(encoding="utf-8")
    )
    assert tasks_payload[0]["task_id"] == task.task_id
    assert tasks_payload[0]["tool_calls"] == 5
    markdown = (path.parent / "baseline_report.md").read_text(encoding="utf-8")
    assert "# Baseline Benchmark" in markdown
    assert "Average Tokens: 120.0" in markdown
    assert not any((tmp_path / "workspaces").iterdir())


async def test_final_test_failure_is_recorded(tmp_path):
    task = _tasks()[0]
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner(
            [_test_result(False), _test_result(False)]
        ),
        agent_runner=StubAgentRunner(),
        run_id="final-fail",
    )
    report, _ = await runner.run()
    result = report.results[0]
    assert result.success is False
    assert result.final_test_passed is False
    assert result.failure_category == FailureCategory.PLANNING_FAILURE


def test_public_failure_without_planner_does_not_report_planning_failure():
    category, _ = BenchmarkRunner._classify_public_failure(
        AnalyzeResponse(
            answer="tests failed",
            steps=2,
            modified_files=["src/main/java/fixture/Calculator.java"],
        ),
        ["src/main/java/fixture/Calculator.java"],
        planner_enabled=False,
    )
    assert category == FailureCategory.PUBLIC_TEST_FAILURE


async def test_initial_test_unexpectedly_passed(tmp_path):
    task = _tasks()[0]
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner([_test_result(True)]),
        agent_runner=StubAgentRunner(),
        run_id="initial-pass",
    )
    report, _ = await runner.run()
    assert (
        report.results[0].failure_category
        == FailureCategory.INITIAL_TEST_UNEXPECTEDLY_PASSED
    )
    assert report.results[0].success is False


async def test_agent_exception_is_classified(tmp_path):
    task = _tasks()[0]
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner(
            [_test_result(False), _test_result(False)]
        ),
        agent_runner=StubAgentRunner(error=RuntimeError("LLM exploded")),
        run_id="agent-error",
    )
    report, _ = await runner.run()
    result = report.results[0]
    assert result.failure_category == FailureCategory.AGENT_FAILED
    assert "LLM exploded" in result.failure_reason
    assert result.agent_answer == ""


async def test_agent_timeout_is_classified_and_workspace_cleaned(tmp_path):
    task = _tasks()[0].model_copy(update={"timeout": 0.01})
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner([_test_result(False)]),
        agent_runner=StubAgentRunner(delay=0.2),
        run_id="timeout",
    )
    report, _ = await runner.run()
    assert report.results[0].failure_category == FailureCategory.TIMEOUT
    assert report.results[0].final_test_passed is False
    assert not any((tmp_path / "workspaces").iterdir())


async def test_v2_agent_timeout_marks_hidden_result_unavailable(tmp_path):
    task = next(
        task
        for task in load_tasks(suite="v2")
        if task.task_id == "v2-account-repository"
    ).model_copy(update={"timeout": 0.01})
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner([_test_result(False)]),
        agent_runner=StubAgentRunner(delay=0.2),
        run_id="v2-timeout",
    )
    report, _ = await runner.run()
    result = report.results[0]
    assert result.failure_category == FailureCategory.TIMEOUT
    assert result.hidden_test_result is None
    assert result.hidden_tests_passed is None
    assert report.summary.hidden_tests_evaluated == 0


async def test_test_timeout_is_classified_as_timeout(tmp_path):
    task = _tasks()[0]
    timed_out_result = MavenTestResult(
        tool=BuildTool.MAVEN,
        command=["mvn", "test"],
        exit_code=None,
        success=False,
        timed_out=True,
        error="Maven Docker 测试超时",
        duration_seconds=0.1,
    )
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner([timed_out_result]),
        agent_runner=StubAgentRunner(),
        run_id="test-timeout",
    )
    report, _ = await runner.run()
    assert report.results[0].failure_category == FailureCategory.TIMEOUT


async def test_infrastructure_error_does_not_crash(tmp_path):
    task = _tasks()[0]
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner(
            [_test_result(False, infrastructure=True)]
        ),
        agent_runner=StubAgentRunner(),
        run_id="infra",
    )
    report, _ = await runner.run()
    assert (
        report.results[0].failure_category
        == FailureCategory.INFRASTRUCTURE_ERROR
    )


async def test_workspace_setup_failure_is_classified(tmp_path):
    task = _tasks()[0].model_copy(
        update={"repository": str(tmp_path / "missing.bundle")}
    )
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner([]),
        agent_runner=StubAgentRunner(),
        run_id="workspace-fail",
    )
    report, _ = await runner.run()
    assert (
        report.results[0].failure_category
        == FailureCategory.WORKSPACE_SETUP_FAILED
    )


async def test_multiple_tasks_continue_after_failure_and_are_isolated(tmp_path):
    tasks = _tasks()
    responses = [
        _test_result(False),
        _test_result(False),
        _test_result(False),
        _test_result(True),
    ]
    agent = StubAgentRunner(
        AnalyzeResponse(answer="ok", steps=1),
        usage=LLMUsage(llm_calls=1, total_tokens=10),
    )
    calls = {"count": 0}

    async def flaky_agent(task, repository_path, workspace_id, manager):
        calls["count"] += 1
        if calls["count"] == 1:
            agent.repositories.append(Path(repository_path))
            raise RuntimeError("first task fails")
        return await agent(task, repository_path, workspace_id, manager)

    runner = BenchmarkRunner(
        tasks,
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner(responses),
        agent_runner=flaky_agent,
        run_id="multi",
    )
    report, _ = await runner.run()
    assert report.summary.total_tasks == 2
    assert report.summary.failed_tasks == 1
    assert report.summary.passed_tasks == 1
    assert report.summary.total_llm_calls == 1
    assert report.summary.total_tokens == 10
    assert report.summary.average_tokens == 10.0
    assert report.summary.average_llm_calls == 0.5
    assert report.results[0].failure_category == FailureCategory.AGENT_FAILED
    assert report.results[1].success is True
    assert len(agent.repositories) == 2
    assert agent.repositories[0] != agent.repositories[1]
    assert not any((tmp_path / "workspaces").iterdir())


def test_usage_tracking_chat_model():
    tracker = UsageTracker()
    delegate = FakeChatModel(
        script=[
            AIMessage(
                content="usage",
                usage_metadata={
                    "input_tokens": 30,
                    "output_tokens": 7,
                    "total_tokens": 37,
                },
            )
        ]
    )
    tracked = UsageTrackingChatModel(delegate=delegate, tracker=tracker)
    response = tracked.invoke([HumanMessage(content="hello")])
    assert response.content == "usage"
    usage = tracker.snapshot()
    assert usage.llm_calls == 1
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 7
    assert usage.total_tokens == 37
    assert usage.estimated_cost_usd is None


def test_builtin_task_catalog_and_bundles():
    tasks = load_tasks()
    assert 8 <= len(tasks) <= 10
    assert len({task.task_id for task in tasks}) == len(tasks)
    for task in tasks:
        assert task.expected_test_command == "mvn test"
        assert task.base_commit
        assert Path(task.repository).exists()


def test_v2_catalog_has_difficulty_and_large_projects():
    tasks = load_tasks(suite="v2")
    assert len(tasks) == 15
    assert sum(1 for task in tasks if task.difficulty == "medium") >= 5
    assert sum(1 for task in tasks if task.difficulty == "hard") >= 5
    assert all(task.hidden_tests for task in tasks)
    for task in tasks:
        assert task.suite == "v2"
        assert Path(task.hidden_tests).is_dir()
        assert not Path(task.hidden_tests).resolve().is_relative_to(
            Path(task.repository).resolve().parent
        )
    large = [
        task
        for task in tasks
        if task.task_id in {"v2-catalog-layered", "v2-inventory-layered"}
    ]
    for task in large:
        fixture_dir = Path(task.repository).parent
        java_files = list(fixture_dir.rglob("*.java"))
        assert len(java_files) >= 30
    assert len(load_tasks(suite="all")) == 25


async def test_hidden_tests_pass_and_are_isolated_from_agent_workspace(tmp_path):
    task = next(
        task
        for task in load_tasks(suite="v2")
        if task.task_id == "v2-account-repository"
    )
    test_runner = RecordingSequenceTestRunner(
        [_test_result(False), _test_result(True), _test_result(True)]
    )

    async def agent(task, repository_path, workspace_id, manager):
        assert not list(Path(repository_path).rglob("*HiddenTest.java"))
        return AgentRunOutput(
            response=AnalyzeResponse(answer="fixed", steps=2),
            usage=LLMUsage(llm_calls=2, total_tokens=20),
        )

    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=test_runner,
        agent_runner=agent,
        run_id="hidden-pass",
    )
    report, _ = await runner.run()
    result = report.results[0]
    assert result.success is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is True
    assert result.hidden_test_result is not None
    assert len(test_runner.paths) == 3
    assert test_runner.paths[0] == test_runner.paths[1]
    assert test_runner.paths[2] != test_runner.paths[1]
    assert report.summary.hidden_test_success_rate == 1.0
    assert report.summary.hidden_test_pass_rate == 1.0


async def test_hidden_test_failure_prevents_success(tmp_path):
    task = next(
        task
        for task in load_tasks(suite="v2")
        if task.task_id == "v2-account-repository"
    )
    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner(
            [_test_result(False), _test_result(True), _test_result(False)]
        ),
        agent_runner=StubAgentRunner(
            AnalyzeResponse(
                answer="hidden regression",
                steps=2,
                last_agent_action='apply_patch {"changes":[]}',
            )
        ),
        run_id="hidden-fail",
    )
    report, report_path = await runner.run()
    result = report.results[0]
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is False
    assert result.success is False
    assert result.failure_category == FailureCategory.HIDDEN_TEST_FAILURE
    assert result.hidden_failure_summary
    assert report.summary.hidden_test_success_rate == 0.0
    markdown = (report_path.parent / "baseline_report.md").read_text(
        encoding="utf-8"
    )
    assert "Last Agent Action:" in markdown
    assert 'apply_patch {"changes":[]}' in markdown
    analysis = generate_failure_analysis(
        report, tmp_path / "failure-analysis.md"
    )
    text = analysis.read_text(encoding="utf-8")
    assert "Benchmark V2 Failure Analysis" in text
    assert "Hidden regression | 1" in text
    assert "v2-account-repository" in text


async def test_patch_quality_metrics_are_recorded(tmp_path):
    task = _tasks()[0]

    async def agent(task, repository_path, workspace_id, manager):
        path = Path(repository_path) / "src/main/java/fixture/Calculator.java"
        path.write_text(path.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
        target = Path(repository_path) / "target" / "classes"
        target.mkdir(parents=True)
        (target / "Calculator.class").write_bytes(b"generated")
        return AgentRunOutput(
            response=AnalyzeResponse(
                answer="changed",
                steps=1,
                modified_files=["src/main/java/fixture/Calculator.java"],
            )
        )

    runner = BenchmarkRunner(
        [task],
        output_dir=tmp_path / "results",
        workspace_root=tmp_path / "workspaces",
        test_runner=SequenceTestRunner(
            [_test_result(False), _test_result(True)]
        ),
        agent_runner=agent,
        run_id="quality",
    )
    report, _ = await runner.run()
    result = report.results[0]
    assert result.changed_file_count == 1
    assert result.changed_files == ["src/main/java/fixture/Calculator.java"]
    assert result.diff_lines_added >= 1
    assert result.diff_size >= 1
    assert result.modified_test_files is False
    assert result.modified_build_files is False
    assert result.modified_unrelated_files is True


def test_execution_metrics_count_tools_and_last_action():
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "A.java"},
                    "id": "call-1",
                }
            ],
        ),
        ToolMessage(content="ok", tool_call_id="call-1"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "apply_patch",
                    "args": {"changes": [{"path": "A.java"}]},
                    "id": "call-2",
                }
            ],
        ),
        ToolMessage(content="ok", tool_call_id="call-2"),
    ]
    (
        count,
        last_action,
        tool_usage,
        failures,
        empty_patch_calls,
        auto_maven_test_runs,
    ) = _execution_metrics(messages)
    assert count == 2
    assert last_action.startswith("apply_patch ")
    assert '"path": "A.java"' in last_action
    assert tool_usage == {"apply_patch": 1, "read_file": 1}
    assert failures == {}
    assert empty_patch_calls == 0
    assert auto_maven_test_runs == 0
