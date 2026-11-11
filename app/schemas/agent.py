"""Schemas for the /agent endpoints."""

from pydantic import BaseModel, Field, model_validator

from app.agent.capabilities import DEFAULT_AGENT_PROFILE, AgentProfile
from app.agent.telemetry import ToolEvent
from app.repair.models import PlanRevision, RepairPlan
from app.workspace.models import TestRunResult


class AnalyzeRequest(BaseModel):
    """Request body of ``POST /agent/analyze``.

    Either ``workspace_id`` (recommended) or ``repository_path`` (legacy)
    must be provided — never both.
    """

    task: str = Field(min_length=1, description="The natural-language task to perform on the repository.")
    profile: AgentProfile = Field(
        default=DEFAULT_AGENT_PROFILE,
        description="Agent capability profile; defaults to the full production Agent.",
    )
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
    tool_calls: int = Field(
        default=0,
        ge=0,
        description="Number of tool executions performed during the run.",
    )
    last_agent_action: str = Field(
        default="",
        description="Most recent tool action, used for benchmark failure analysis.",
    )
    tool_usage: dict[str, int] = Field(
        default_factory=dict,
        description="Executed tool names and call counts.",
    )
    tool_failure_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Tool execution errors grouped by tool name.",
    )
    empty_patch_calls: int = Field(
        default=0,
        ge=0,
        description="Successful apply_patch calls that produced no diff.",
    )
    auto_maven_test_runs: int = Field(
        default=0,
        ge=0,
        description="Maven tests triggered automatically after a patch.",
    )
    tool_events: list[ToolEvent] = Field(
        default_factory=list,
        description="Sanitized passive telemetry for each executed tool call.",
    )
    tool_output_chars: int = Field(default=0, ge=0)
    modified_files: list[str] = Field(
        default_factory=list,
        description="Files changed by the Agent in this run.",
    )
    repair_rounds: int = Field(
        default=0,
        ge=0,
        description="Patch attempts made after a failed test.",
    )
    test_result: TestRunResult | None = Field(
        default=None,
        description="Latest controlled Maven/Gradle test result.",
    )
    diff: str = Field(
        default="",
        description="Current uncommitted diff after the Agent run.",
    )
    repair_plan: RepairPlan | None = Field(
        default=None,
        description="Latest structured repair plan.",
    )
    plan_revisions: list[PlanRevision] = Field(
        default_factory=list,
        description="Chronological repair-plan revision history.",
    )
