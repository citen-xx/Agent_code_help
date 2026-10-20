"""Workspace domain models."""

from enum import StrEnum

from pydantic import BaseModel, Field


class WorkspaceStatus(StrEnum):
    """Lifecycle state of a local workspace."""

    CREATING = "creating"
    READY = "ready"
    ERROR = "error"


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
