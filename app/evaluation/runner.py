"""Sequential Benchmark Runner using the production Agent pipeline."""

import asyncio
import logging
import shutil
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
from app.workspace.models import BuildTool, TestRunResult

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

    @staticmethod
    def _changed_files(repository_path: Path) -> list[str]:
        try:
            output = git.status_porcelain(repository_path)
        except git.GitError:
            return []
        files: list[str] = []
        for line in output.splitlines():
            if not line or line.startswith("## "):
                continue
            raw = line[3:].strip()
            if " -> " in raw:
                raw = raw.split(" -> ", 1)[1]
            files.append(raw.strip('"').replace("\\", "/"))
        return sorted(set(files))

    @staticmethod
    def _diff_metrics(diff: str) -> tuple[int, int]:
        added = 0
        removed = 0
        for line in diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return added, removed

    @staticmethod
    def _quality_metrics(
        changed_files: list[str],
        response: AnalyzeResponse | None,
    ) -> dict:
        normalized = {path.replace("\\", "/") for path in changed_files}
        test_files = {
            path
            for path in normalized
            if "/src/test/" in f"/{path}" or path.endswith("Test.java")
        }
        build_files = {
            path
            for path in normalized
            if path in {"pom.xml", "build.gradle", "settings.gradle"}
            or path.startswith(".mvn/")
        }
        allowed = set()
        if response and response.repair_plan:
            allowed.update(
                change.file_path.replace("\\", "/")
                for change in response.repair_plan.planned_changes
            )
            allowed.update(
                path.replace("\\", "/")
                for path in response.repair_plan.impacted_files
            )
        unrelated = {
            path
            for path in normalized
            if path not in allowed
            and path not in test_files
            and path not in build_files
        }
        return {
            "modified_unrelated_files": bool(unrelated),
            "modified_test_files": bool(test_files),
            "modified_build_files": bool(build_files),
        }

    @staticmethod
    def _hidden_failure_summary(result) -> str:
        if result is None:
            return ""
        parts = [
            result.error or "",
            (result.stderr or "")[-2000:],
            (result.stdout or "")[-2000:],
        ]
        return "\n".join(part for part in parts if part).strip()[:4000]

    def _run_hidden_tests(
        self,
        task: BenchmarkTask,
        repository_path: Path,
    ):
        if not task.hidden_tests:
            return None
        hidden_dir = Path(task.hidden_tests)
        hidden_dir = (
            hidden_dir if hidden_dir.is_absolute() else PROJECT_ROOT / hidden_dir
        )
        if not hidden_dir.is_dir():
            raise FileNotFoundError(f"Hidden tests directory missing: {hidden_dir}")
        with tempfile.TemporaryDirectory(
            prefix=f"benchmark-hidden-{task.task_id}-"
        ) as temp_dir:
            verification_repo = Path(temp_dir) / "repository"
            shutil.copytree(
                repository_path,
                verification_repo,
                ignore=shutil.ignore_patterns(".git", "target"),
            )
            shutil.copytree(
                hidden_dir,
                verification_repo,
                dirs_exist_ok=True,
            )
            return self.test_runner(
                verification_repo,
                "maven",
                task.timeout,
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
        hidden_test=None,
        changed_files: list[str] | None = None,
    ) -> BenchmarkTaskResult:
        changed_files = changed_files or []
        public_test_passed = bool(final_test is not None and final_test.success)
        hidden_test_passed = bool(hidden_test is None or hidden_test.success)
        success = public_test_passed and hidden_test_passed and category is None
        added, removed = self._diff_metrics(final_diff)
        quality = self._quality_metrics(changed_files, response)
        return BenchmarkTaskResult(
            task_id=task.task_id,
            suite=task.suite,
            difficulty=task.difficulty,
            tags=task.tags,
            success=success,
            public_tests_passed=public_test_passed,
            hidden_tests_passed=hidden_test_passed,
            hidden_failure_summary=(
                self._hidden_failure_summary(hidden_test) if hidden_test else None
            ),
            final_test_passed=public_test_passed,
            modified_files=list(response.modified_files) if response else [],
            repair_rounds=response.repair_rounds if response else 0,
            plan_revisions=list(response.plan_revisions) if response else [],
            duration_seconds=round(time.monotonic() - started, 3),
            final_diff=final_diff,
            changed_file_count=len(changed_files),
            diff_lines_added=added,
            diff_lines_removed=removed,
            diff_size=added + removed,
            **quality,
            failure_reason=reason,
            failure_category=category,
            agent_answer=response.answer if response else "",
            workspace_id=workspace_id,
            initial_test_result=initial_test,
            final_test_result=final_test,
            hidden_test_result=hidden_test,
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
            hidden_test=None,
            response=None,
            usage=LLMUsage(),
            category=category,
            reason=reason,
        )

    @staticmethod
    def _agent_reported_failure(
        response: AnalyzeResponse,
    ) -> tuple[FailureCategory, str] | None:
        if response.answer.startswith(("LLM 调用失败", "Agent 执行异常")):
            return FailureCategory.AGENT_FAILED, response.answer[:1000]
        if response.answer.startswith("Agent 执行超时"):
            return FailureCategory.TIMEOUT, response.answer[:1000]
        if "已达到最多 3 轮自动修复" in response.answer:
            return FailureCategory.REPAIR_LIMIT_EXCEEDED, response.answer[:1000]
        if response.answer.startswith("已达到最大步骤数"):
            return FailureCategory.PATCH_FAILURE, response.answer[:1000]
        return None

    @staticmethod
    def _classify_public_failure(
        response: AnalyzeResponse | None,
        changed_files: list[str],
    ) -> tuple[FailureCategory, str]:
        if response and "已达到最多 3 轮自动修复" in response.answer:
            return (
                FailureCategory.REPAIR_LIMIT_EXCEEDED,
                "修复轮数耗尽且公开 Maven test 仍失败",
            )
        if response is not None and response.repair_plan is None:
            return (
                FailureCategory.PLANNING_FAILURE,
                "Agent 未形成有效 Repair Plan",
            )
        if response is not None and not response.modified_files:
            return (
                FailureCategory.LOCALIZATION_FAILURE,
                "Agent 未修改任何文件",
            )
        if not changed_files:
            return (
                FailureCategory.PATCH_FAILURE,
                "Agent 声称修改但 Git 未发现实际变更",
            )
        return (
            FailureCategory.PUBLIC_TEST_FAILURE,
            "最终公开 Maven test 失败",
        )

    async def run_task(self, task: BenchmarkTask, manager: WorkspaceManager) -> BenchmarkTaskResult:
        started = time.monotonic()
        workspace = None
        initial_test = None
        final_test = None
        hidden_test = None
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
                    category, reason = agent_failure
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
                        classified, classified_reason = self._classify_public_failure(
                            response,
                            self._changed_files(repository_path),
                        )
                        category = category or classified
                        reason = reason or classified_reason

            try:
                final_diff = git.diff(repository_path)
            except git.GitError:
                logger.warning("Failed to get final diff for %s", task.task_id)
            changed_files = self._changed_files(repository_path)

            if task.hidden_tests and category is not FailureCategory.TIMEOUT:
                try:
                    hidden_test = self._run_hidden_tests(task, repository_path)
                except Exception as exc:
                    hidden_test = TestRunResult(
                        tool=BuildTool.MAVEN,
                        command=["mvn", "test"],
                        exit_code=None,
                        success=False,
                        error=f"Hidden test 验证异常: {exc}",
                        duration_seconds=0,
                    )
                if hidden_test is not None:
                    if hidden_test.timed_out or (
                        hidden_test.error and hidden_test.exit_code is None
                    ):
                        category = category or FailureCategory.INFRASTRUCTURE_ERROR
                        reason = reason or f"Hidden test 基础设施错误: {hidden_test.error}"
                    elif not hidden_test.success and category is None:
                        category = FailureCategory.HIDDEN_TEST_FAILURE
                        reason = "隐藏验收 Maven test 失败"

            return self._result(
                task=task,
                workspace_id=workspace.workspace_id,
                started=started,
                initial_test=initial_test,
                final_test=final_test,
                hidden_test=hidden_test,
                response=response,
                usage=usage,
                category=category,
                reason=reason,
                final_diff=final_diff,
                changed_files=changed_files,
            )
        except Exception as exc:
            logger.exception("Benchmark task infrastructure failure: %s", task.task_id)
            return self._result(
                task=task,
                workspace_id=workspace.workspace_id if workspace else None,
                started=started,
                initial_test=initial_test,
                final_test=final_test,
                hidden_test=hidden_test,
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
        hidden_results = [
            result
            for result in results
            if result.hidden_test_result is not None
        ]
        public_passed = sum(1 for result in results if result.public_tests_passed)
        hidden_passed = sum(
            1 for result in hidden_results if result.hidden_tests_passed
        )
        success_by_difficulty: dict[str, float] = {}
        for difficulty in sorted({result.difficulty.value for result in results}):
            group = [
                result
                for result in results
                if result.difficulty.value == difficulty
            ]
            success_by_difficulty[difficulty] = (
                sum(1 for result in group if result.success) / len(group)
                if group
                else 0.0
            )
        success_by_tag: dict[str, float] = {}
        for tag in sorted({tag for result in results for tag in result.tags}):
            group = [result for result in results if tag in result.tags]
            success_by_tag[tag] = (
                sum(1 for result in group if result.success) / len(group)
                if group
                else 0.0
            )
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
            average_plan_revisions=(
                sum(len(result.plan_revisions) for result in results) / total
                if total
                else 0.0
            ),
            average_changed_files=(
                sum(result.changed_file_count for result in results) / total
                if total
                else 0.0
            ),
            average_diff_size=(
                sum(result.diff_size for result in results) / total
                if total
                else 0.0
            ),
            total_llm_calls=sum(result.llm_calls for result in results),
            total_tokens=sum(token_values) if token_values else None,
            estimated_cost_usd=sum(cost_values) if cost_values else None,
            public_test_success_rate=public_passed / total if total else 0.0,
            hidden_test_success_rate=(
                hidden_passed / len(hidden_results) if hidden_results else 0.0
            ),
            success_by_difficulty=success_by_difficulty,
            success_by_tag=success_by_tag,
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
        print(f"Average plan revisions: {summary.average_plan_revisions:.1f}")
        print(f"Average changed files: {summary.average_changed_files:.1f}")
        print(f"Average diff size: {summary.average_diff_size:.1f}")
        print(f"Public test success rate: {summary.public_test_success_rate * 100:.1f}%")
        print(f"Hidden test success rate: {summary.hidden_test_success_rate * 100:.1f}%")
        for difficulty, rate in summary.success_by_difficulty.items():
            print(f"Success rate [{difficulty}]: {rate * 100:.1f}%")
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
