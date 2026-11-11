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


class PatchFailureCategory(StrEnum):
    INVALID_PATCH_FORMAT = "invalid_patch_format"
    TARGET_FILE_NOT_FOUND = "target_file_not_found"
    OLD_TEXT_NOT_FOUND = "old_text_not_found"
    OLD_TEXT_MULTIPLE_MATCHES = "old_text_multiple_matches"
    PATH_VALIDATION_FAILED = "path_validation_failed"
    ENCODING_ERROR = "encoding_error"
    WRITE_FAILED = "write_failed"
    STALE_CONTEXT = "stale_context"
    PERMISSION_OR_WORKSPACE_ERROR = "permission_or_workspace_error"
    TIMEOUT = "timeout"
    OTHER = "other"


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


class PatchReplacement(BaseModel):
    """One sequential exact-text replacement inside a file."""

    model_config = ConfigDict(extra="forbid")

    old_text: str = Field(min_length=1, max_length=200_000, description="Existing text to replace.")
    new_text: str = Field(max_length=200_000, description="Replacement text.")


class PatchFile(BaseModel):
    """All replacement operations for one repository-relative file."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    replacements: list[PatchReplacement] = Field(
        min_length=1,
        max_length=100,
        description="Replacements executed in order against the updated file buffer.",
    )


class ApplyPatchRequest(BaseModel):
    """All-or-nothing transactional patch grouped by file."""

    model_config = ConfigDict(extra="forbid")

    files: list[PatchFile] = Field(
        min_length=1,
        max_length=20,
        description="Files and their ordered replacement lists; each path appears once.",
    )


class PatchResult(BaseModel):
    """Result of a successful atomic patch."""

    success: bool = True
    affected_files: list[str] = Field(default_factory=list)
    files_requested: int = Field(default=0, ge=0)
    replacements_requested: int = Field(default=0, ge=0)
    successful_replacements: int = Field(default=0, ge=0)
    diff: str = ""
    diff_truncated: bool = False


class PatchFailureResult(BaseModel):
    """Structured patch error safe to feed back to the Agent."""

    success: bool = False
    error_category: PatchFailureCategory
    file: str | None = None
    replacement_index: int | None = Field(default=None, ge=1)
    message: str
    files_requested: int = Field(default=0, ge=0)
    replacements_requested: int = Field(default=0, ge=0)
    successful_replacements: int = Field(default=0, ge=0)


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
