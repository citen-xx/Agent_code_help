"""Workspace domain models."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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


class PatchChange(BaseModel):
    """One exact text replacement inside a repository file."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512, description="Repository-relative file path.")
    old_text: str = Field(min_length=1, max_length=200_000, description="Existing text to replace.")
    new_text: str = Field(max_length=200_000, description="Replacement text.")


class ApplyPatchRequest(BaseModel):
    """Atomic set of exact text replacements."""

    model_config = ConfigDict(extra="forbid")

    changes: list[PatchChange] = Field(
        min_length=1,
        max_length=20,
        description="Changes applied atomically; each path may appear once.",
    )


class PatchResult(BaseModel):
    """Result of a successful atomic patch."""

    success: bool = True
    affected_files: list[str] = Field(default_factory=list)
    diff: str = ""
    diff_truncated: bool = False


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
