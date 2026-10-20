"""Offline tests for Benchmark Runner, reporting, and usage tracking."""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.evaluation.models import (
    AgentRunOutput,
    BenchmarkTask,
    FailureCategory,
    LLMUsage,
)
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
    assert path.name == "success-run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"]["passed_tasks"] == 1
    assert payload["results"][0]["task_id"] == task.task_id
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
    assert result.failure_category == FailureCategory.FINAL_TEST_FAILED


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
