"""Workspace domain models."""

from pydantic import BaseModel, Field


class WorkspaceInfo(BaseModel):
    """Basic status of a workspace (create / get response body)."""

    workspace_id: str = Field(description="Unique workspace id.")
    repository_path: str = Field(description="Local path of the cloned repository.")
    repository_url: str = Field(description="Source Git repository URL.")
    branch: str = Field(description="Current branch ('detached' when a commit is checked out).")
    commit: str = Field(description="Current HEAD commit hash.")
