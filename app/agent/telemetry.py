"""Passive per-tool-call telemetry for evaluation and failure analysis."""

from pydantic import BaseModel, Field

from app.workspace.models import PatchFailureCategory

PatchErrorCategory = PatchFailureCategory


class ToolEvent(BaseModel):
    tool_call_index: int = Field(ge=1)
    step: int = Field(ge=1)
    repair_round: int = Field(ge=0)
    tool_name: str
    call_id: str | None = None
    target_file: str | None = None
    success: bool
    invoked: bool = False
    error_category: PatchFailureCategory | None = None
    error_summary: str | None = None
    patch_applied: bool | None = None
    output_chars: int = Field(default=0, ge=0)
    argument_fingerprint: str
    auto_generated: bool = False
    files_requested: int = Field(default=0, ge=0)
    replacements_requested: int = Field(default=0, ge=0)
    successful_replacements: int = Field(default=0, ge=0)
    failed_replacement_index: int | None = Field(default=None, ge=1)
