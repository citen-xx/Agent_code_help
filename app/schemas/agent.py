"""Schemas for the /agent endpoints."""

from pydantic import BaseModel, Field, model_validator


class AnalyzeRequest(BaseModel):
    """Request body of ``POST /agent/analyze``.

    Either ``workspace_id`` (recommended) or ``repository_path`` (legacy)
    must be provided — never both.
    """

    task: str = Field(min_length=1, description="The natural-language task to perform on the repository.")
    workspace_id: str | None = Field(default=None, description="Workspace to analyze (recommended).")
    repository_path: str | None = Field(
        default=None, min_length=1, description="Local repository path to analyze (legacy)."
    )

    @model_validator(mode="after")
    def _validate_target(self) -> "AnalyzeRequest":
        has_workspace = bool(self.workspace_id)
        has_path = bool(self.repository_path)
        if has_workspace == has_path:
            raise ValueError("必须且只能提供 workspace_id 或 repository_path 其中之一")
        return self


class AnalyzeResponse(BaseModel):
    """Response body of ``POST /agent/analyze`` (also the agent run result)."""

    answer: str = Field(description="The agent's final answer.")
    steps: int = Field(ge=0, description="Number of LLM iterations executed.")
