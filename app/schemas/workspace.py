"""Schemas for the /workspaces endpoints."""

from pydantic import BaseModel, Field


class WorkspaceCreateRequest(BaseModel):
    """Request body of ``POST /workspaces``."""

    repository_url: str = Field(min_length=1, description="HTTPS Git repository URL to clone.")
    branch: str | None = Field(default=None, description="Optional branch to check out after cloning.")
    commit: str | None = Field(default=None, description="Optional commit to check out (applied after branch).")
