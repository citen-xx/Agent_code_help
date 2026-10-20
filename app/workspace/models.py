"""Workspace domain models."""

from enum import StrEnum

from pydantic import BaseModel, Field


class WorkspaceStatus(StrEnum):
    """Lifecycle state of a local workspace."""

    CREATING = "creating"
    READY = "ready"
    ERROR = "error"


class BuildTool(StrEnum):
    """Supported Java build tools."""

    MAVEN = "maven"
    GRADLE = "gradle"


class BuildProjectInfo(BaseModel):
    """Detected build configuration at a repository root."""

    tool: BuildTool = Field(description="Detected build tool.")
    build_files: list[str] = Field(description="Root build files that identify the project.")
    command: list[str] = Field(description="Fixed test command that will be executed.")
    uses_wrapper: bool = Field(description="Whether the repository's wrapper is used.")


class TestRunResult(BaseModel):
    """Structured result of a controlled Maven/Gradle test run."""

    tool: BuildTool | None = Field(default=None, description="Build tool used, when known.")
    command: list[str] = Field(default_factory=list, description="Executed command.")
    exit_code: int | None = Field(default=None, description="Process exit code.")
    stdout: str = Field(default="", description="Captured standard output.")
    stderr: str = Field(default="", description="Captured standard error.")
    success: bool = Field(default=False, description="Whether tests completed successfully.")
    timed_out: bool = Field(default=False, description="Whether the timeout was reached.")
    truncated: bool = Field(default=False, description="Whether stdout or stderr was truncated.")
    error: str | None = Field(default=None, description="Execution error, if any.")
    duration_seconds: float = Field(ge=0, description="Wall-clock execution time.")


class WorkspaceInfo(BaseModel):
    """Basic status of a workspace (create / get response body)."""

    workspace_id: str = Field(description="Unique workspace id.")
    repository_path: str = Field(description="Local path of the cloned repository.")
    repository_url: str = Field(description="Source Git repository URL.")
    branch: str | None = Field(
        default=None,
        description="Current branch ('detached' when a commit is checked out).",
    )
    commit: str | None = Field(default=None, description="Current HEAD commit hash.")
    status: WorkspaceStatus = Field(description="Workspace lifecycle status.")
