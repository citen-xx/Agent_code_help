"""Agent tools for detecting and executing Maven/Gradle tests.

The tools expose no filesystem path, shell command, or arbitrary build
arguments. They are bound to one workspace repository when created.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.tools.repo import ToolError
from app.workspace.build import (
    BuildError,
    DEFAULT_TEST_TIMEOUT_SECONDS,
    detect_build_projects,
    run_workspace_tests,
)

if TYPE_CHECKING:
    from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


class _DetectBuildArgs(BaseModel):
    """No arguments accepted."""

    model_config = ConfigDict(extra="forbid")


class _RunTestsArgs(BaseModel):
    """Only the build tool selector is accepted."""

    model_config = ConfigDict(extra="forbid")

    tool: Literal["auto", "maven", "gradle"] = Field(
        default="auto",
        description="Build tool to use. auto fails when both Maven and Gradle exist.",
    )


def create_build_tools(
    workspace_id: str,
    repository_path: str | Path,
    manager: "WorkspaceManager | None" = None,
    timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS,
    test_runner: Callable | None = None,
) -> list[BaseTool]:
    """Create build detection and test execution tools for one workspace."""
    if manager is not None:
        repo = manager.assert_repository_binding(workspace_id, repository_path)
    else:
        repo = Path(repository_path).expanduser().resolve()
        if repo.name != "repository" or repo.parent.name != workspace_id:
            raise ToolError(
                f"Build Tool 只能绑定 workspace {workspace_id} 的 repository 目录"
            )
    if not repo.is_dir():
        raise ToolError(f"workspace repository 不存在: {repo}")

    def _detect() -> str:
        try:
            projects = detect_build_projects(repo)
        except BuildError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps(
            [project.model_dump(mode="json") for project in projects],
            ensure_ascii=False,
            indent=2,
        )

    def _run_tests(tool: Literal["auto", "maven", "gradle"] = "auto") -> str:
        runner = test_runner or run_workspace_tests
        result = runner(
            repo,
            tool=tool,
            timeout_seconds=timeout_seconds,
        )
        return result.model_dump_json(indent=2)

    logger.debug("Binding build tools to workspace %s (repo=%s)", workspace_id, repo)
    return [
        StructuredTool.from_function(
            name="detect_build_tools",
            description=(
                "Detect whether the workspace root is a Maven or Gradle project. "
                "Read-only."
            ),
            func=_detect,
            args_schema=_DetectBuildArgs,
        ),
        StructuredTool.from_function(
            name="run_tests",
            description=(
                "Run the fixed Maven or Gradle 'test' lifecycle in the bound "
                "workspace and return exit_code, stdout, stderr, timeout and "
                "truncation details. Tests execute repository code and may take "
                "time. Read-only with respect to Agent file editing."
            ),
            func=_run_tests,
            args_schema=_RunTestsArgs,
        ),
    ]
