"""Sequential Benchmark Runner using the production Agent pipeline."""

import asyncio
import logging
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from app.agent.graph import run_agent
from app.evaluation.models import (
    AgentRunOutput,
    BenchmarkReport,
    BenchmarkSummary,
    BenchmarkTask,
    BenchmarkTaskResult,
    FailureCategory,
    LLMUsage,
)
from app.evaluation.tasks import DEFAULT_RESULTS_DIR, PROJECT_ROOT
from app.evaluation.usage import UsageTracker, UsageTrackingChatModel
from app.llm.client import build_chat_model
from app.schemas.agent import AnalyzeResponse
from app.workspace import git
from app.workspace.build import run_workspace_tests
from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

TestRunner = Callable[[str | Path, str, float], object]
AgentRunner = Callable[
    [BenchmarkTask, Path, str, WorkspaceManager],
    Awaitable[AgentRunOutput],
]


class BenchmarkRunner:
    def __init__(
        self,
        tasks: list[BenchmarkTask],
        *,
        output_dir: str | Path = DEFAULT_RESULTS_DIR,
        workspace_root: str | Path | None = None,
        test_runner: TestRunner = run_workspace_tests,
        agent_runner: AgentRunner | None = None,
        run_id: str | None = None,
    ):
        self.tasks = tasks
        self.output_dir = Path(output_dir)
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.test_runner = test_runner
        self.agent_runner = agent_runner or self._default_agent_runner
        self.run_id = run_id or datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        self.started_at = datetime.now(timezone.utc).isoformat()

    async def _default_agent_runner(
        self,
        task: BenchmarkTask,
        repository_path: Path,
        workspace_id: str,
        manager: WorkspaceManager,
    ) -> AgentRunOutput:
        tracker = UsageTracker()
        model = UsageTrackingChatModel(
            delegate=build_chat_model(),
            tracker=tracker,
        )
        response = await run_agent(
            task.problem_statement,
            repository_path,
            model=model,
            run_timeout_seconds=None,
            workspace_id=workspace_id,
            workspace_manager=manager,
        )
        return AgentRunOutput(response=response, usage=tracker.snapshot())

    def _repository_path(self, task: BenchmarkTask) -> Path:
        path = Path(task.repository)
        return path if path.is_absolute() else PROJECT_ROOT / path

    def _create_workspace(
        self,
        manager: WorkspaceManager,
        repository: Path,
        base_commit: str,
    ):
        if repository.suffix != ".bundle":
            return manager.create(
                repository.as_uri(),
                commit=base_commit,
                branch=None,
            )
        with tempfile.TemporaryDirectory(prefix="benchmark-bundle-") as temp_dir:
            origin = Path(temp_dir) / "origin"
            subprocess.run(
                ["git", "clone", "--quiet", str(repository), str(origin)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return manager.create(
                origin.as_uri(),
                commit=base_commit,
                branch=None,
            )

    def _result(
        self,
        *,
        task: BenchmarkTask,
        workspace_id: str | None,
        started: float,
        initial_test,
        final_test,
        response: AnalyzeResponse | None,
        usage: LLMUsage,
        category: FailureCategory | None,
        reason: str | None,
        final_diff: str = "",
    ) -> BenchmarkTaskResult:
        final_test_passed = bool(final_test is not None and final_test.success)
        success = final_test_passed and category is None
        return BenchmarkTaskResult(
            task_id=task.task_id,
            success=success,
            final_test_passed=final_test_passed,
            modified_files=list(response.modified_files) if response else [],
            repair_rounds=response.repair_rounds if response else 0,
            plan_revisions=list(response.plan_revisions) if response else [],
            duration_seconds=round(time.monotonic() - started, 3),
            final_diff=final_diff,
            failure_reason=reason,
            failure_category=category,
            agent_answer=response.answer if response else "",
            workspace_id=workspace_id,
            initial_test_result=initial_test,
            final_test_result=final_test,
            llm_calls=usage.llm_calls,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_usd=usage.estimated_cost_usd,
        )

    def _failure_result(
        self,
        *,
        task: BenchmarkTask,
        started: float,
        category: FailureCategory,
        reason: str,
        workspace_id: str | None = None,
    ) -> BenchmarkTaskResult:
        return self._result(
            task=task,
            workspace_id=workspace_id,
            started=started,
            initial_test=None,
            final_test=None,
            response=None,
            usage=LLMUsage(),
            category=category,
            reason=reason,
        )

    @staticmethod
    def _agent_reported_failure(response: AnalyzeResponse) -> str | None:
        prefixes = (
            "LLM 调用失败",
            "Agent 执行异常",
            "Agent 执行超时",
            "已达到最大步骤数",
        )
        if response.answer.startswith(prefixes):
            return response.answer[:1000]
        return None

    async def run_task(self, task: BenchmarkTask, manager: WorkspaceManager) -> BenchmarkTaskResult:
        started = time.monotonic()
        workspace = None
        initial_test = None
        final_test = None
        response: AnalyzeResponse | None = None
        usage = LLMUsage()
        category: FailureCategory | None = None
        reason: str | None = None
        final_diff = ""
        try:
            repository = self._repository_path(task)
            if not repository.exists():
                return self._failure_result(
                    task=task,
                    started=started,
                    category=FailureCategory.WORKSPACE_SETUP_FAILED,
                    reason=f"Fixture repository 不存在: {repository}",
                )
            try:
                workspace = self._create_workspace(
                    manager,
                    repository,
                    task.base_commit,
                )
            except Exception as exc:
                return self._failure_result(
                    task=task,
                    started=started,
                    category=FailureCategory.WORKSPACE_SETUP_FAILED,
                    reason=f"Workspace 准备失败: {exc}",
                )

            repository_path = Path(workspace.repository_path)
            try:
                initial_test = self.test_runner(
                    repository_path,
                    "maven",
                    task.timeout,
                )
            except Exception as exc:
                return self._result(
                    task=task,
                    workspace_id=workspace.workspace_id,
                    started=started,
                    initial_test=None,
                    final_test=None,
                    response=None,
                    usage=usage,
                    category=FailureCategory.INFRASTRUCTURE_ERROR,
                    reason=f"Initial test 执行异常: {exc}",
                )
            if initial_test.success:
                return self._result(
                    task=task,
                    workspace_id=workspace.workspace_id,
                    started=started,
                    initial_test=initial_test,
                    final_test=None,
                    response=None,
                    usage=usage,
                    category=FailureCategory.INITIAL_TEST_UNEXPECTEDLY_PASSED,
                    reason="初始 Maven test 意外通过，Benchmark Fixture 无效",
                )
            if initial_test.timed_out or (
                initial_test.error and initial_test.exit_code is None
            ):
                return self._result(
                    task=task,
                    workspace_id=workspace.workspace_id,
                    started=started,
                    initial_test=initial_test,
                    final_test=None,
                    response=None,
                    usage=usage,
                    category=FailureCategory.INFRASTRUCTURE_ERROR,
                    reason=f"Initial test 基础设施错误: {initial_test.error}",
                )

            try:
                agent_output = await asyncio.wait_for(
                    self.agent_runner(
                        task,
                        repository_path,
                        workspace.workspace_id,
                        manager,
                    ),
                    timeout=task.timeout,
                )
                response = agent_output.response
                usage = agent_output.usage
                agent_failure = self._agent_reported_failure(response)
                if agent_failure:
                    category = FailureCategory.AGENT_FAILED
                    reason = agent_failure
            except TimeoutError:
                category = FailureCategory.TIMEOUT
                reason = f"Agent 执行超过 task timeout {task.timeout:.0f}s"
            except Exception as exc:
                category = FailureCategory.AGENT_FAILED
                reason = f"Agent 执行异常: {exc}"

            if category is not FailureCategory.TIMEOUT:
                try:
                    final_test = self.test_runner(
                        repository_path,
                        "maven",
                        task.timeout,
                    )
                except Exception as exc:
                    category = category or FailureCategory.INFRASTRUCTURE_ERROR
                    reason = reason or f"Final test 执行异常: {exc}"
                else:
                    if final_test.timed_out or (
                        final_test.error and final_test.exit_code is None
                    ):
                        category = category or FailureCategory.INFRASTRUCTURE_ERROR
                        reason = reason or f"Final test 基础设施错误: {final_test.error}"
                    elif not final_test.success:
                        category = category or FailureCategory.FINAL_TEST_FAILED
                        reason = reason or "最终 Maven test 失败"

            try:
                final_diff = git.diff(repository_path)
            except git.GitError:
                logger.warning("Failed to get final diff for %s", task.task_id)

            return self._result(
                task=task,
                workspace_id=workspace.workspace_id,
                started=started,
                initial_test=initial_test,
                final_test=final_test,
                response=response,
                usage=usage,
                category=category,
                reason=reason,
                final_diff=final_diff,
            )
        except Exception as exc:
            logger.exception("Benchmark task infrastructure failure: %s", task.task_id)
            return self._result(
                task=task,
                workspace_id=workspace.workspace_id if workspace else None,
                started=started,
                initial_test=initial_test,
                final_test=final_test,
                response=response,
                usage=usage,
                category=FailureCategory.INFRASTRUCTURE_ERROR,
                reason=f"Benchmark infrastructure error: {exc}",
            )
        finally:
            if workspace is not None:
                try:
                    manager.delete(workspace.workspace_id)
                except Exception:
                    logger.exception("Failed to clean benchmark workspace %s", workspace.workspace_id)

    @staticmethod
    def _summary(results: list[BenchmarkTaskResult]) -> BenchmarkSummary:
        total = len(results)
        passed = sum(1 for result in results if result.success)
        token_values = [
            result.total_tokens
            for result in results
            if result.total_tokens is not None
        ]
        cost_values = [
            result.estimated_cost_usd
            for result in results
            if result.estimated_cost_usd is not None
        ]
        return BenchmarkSummary(
            total_tasks=total,
            passed_tasks=passed,
            failed_tasks=total - passed,
            success_rate=(passed / total) if total else 0.0,
            average_duration=(
                sum(result.duration_seconds for result in results) / total
                if total
                else 0.0
            ),
            average_repair_rounds=(
                sum(result.repair_rounds for result in results) / total
                if total
                else 0.0
            ),
            total_llm_calls=sum(result.llm_calls for result in results),
            total_tokens=sum(token_values) if token_values else None,
            estimated_cost_usd=sum(cost_values) if cost_values else None,
        )

    def _report(self, results: list[BenchmarkTaskResult]) -> BenchmarkReport:
        return BenchmarkReport(
            run_id=self.run_id,
            started_at=self.started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            tasks=self.tasks,
            results=results,
            summary=self._summary(results),
        )

    def _write_report(self, report: BenchmarkReport) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{self.run_id}.json"
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return path

    @staticmethod
    def print_summary(report: BenchmarkReport) -> None:
        summary = report.summary
        print(f"Tasks: {summary.total_tasks}")
        print(f"Passed: {summary.passed_tasks}")
        print(f"Failed: {summary.failed_tasks}")
        print(f"Success rate: {summary.success_rate * 100:.1f}%")
        print(f"Average repair rounds: {summary.average_repair_rounds:.1f}")
        print(f"Average duration: {summary.average_duration:.1f}s")
        print(f"LLM calls: {summary.total_llm_calls}")
        print(
            "Tokens: "
            + (
                str(summary.total_tokens)
                if summary.total_tokens is not None
                else "unavailable"
            )
        )
        print(
            "Estimated cost USD: "
            + (
                f"{summary.estimated_cost_usd:.4f}"
                if summary.estimated_cost_usd is not None
                else "unavailable"
            )
        )
        for result in report.results:
            if not result.success:
                category = (
                    result.failure_category.value
                    if result.failure_category is not None
                    else "unknown"
                )
                print(
                    f"FAILED {result.task_id} [{category}]: "
                    f"{result.failure_reason or 'no reason'}"
                )

    async def run(self) -> tuple[BenchmarkReport, Path]:
        results: list[BenchmarkTaskResult] = []
        if self.workspace_root is not None:
            manager = WorkspaceManager(
                self.workspace_root,
                clone_timeout_seconds=60,
                allow_file_urls=True,
            )
            for task in self.tasks:
                result = await self.run_task(task, manager)
                results.append(result)
                self._write_report(self._report(results))
            report = self._report(results)
            path = self._write_report(report)
        else:
            with tempfile.TemporaryDirectory(
                prefix="java-code-agent-benchmark-"
            ) as temp_dir:
                manager = WorkspaceManager(
                    Path(temp_dir) / ".workspaces",
                    clone_timeout_seconds=60,
                    allow_file_urls=True,
                )
                for task in self.tasks:
                    result = await self.run_task(task, manager)
                    results.append(result)
                    self._write_report(self._report(results))
                report = self._report(results)
                path = self._write_report(report)
        self.print_summary(report)
        return report, path
