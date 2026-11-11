"""Passive per-tool-call telemetry for evaluation and failure analysis."""

from enum import StrEnum

from pydantic import BaseModel, Field


class PatchErrorCategory(StrEnum):
    OLD_TEXT_NOT_FOUND = "old_text_not_found"
    OLD_TEXT_MULTIPLE_MATCHES = "old_text_multiple_matches"
    STALE_CONTEXT = "stale_context"
    INVALID_PATCH_FORMAT = "invalid_patch_format"
    TARGET_FILE_NOT_FOUND = "target_file_not_found"
    PATH_VALIDATION_FAILED = "path_validation_failed"
    ENCODING_OR_NEWLINE_MISMATCH = "encoding_or_newline_mismatch"
    PERMISSION_OR_WORKSPACE_ERROR = "permission_or_workspace_error"
    TIMEOUT = "timeout"
    OTHER = "other"


class ToolEvent(BaseModel):
    tool_call_index: int = Field(ge=1)
    step: int = Field(ge=1)
    repair_round: int = Field(ge=0)
    tool_name: str
    call_id: str | None = None
    target_file: str | None = None
    success: bool
    error_category: PatchErrorCategory | None = None
    error_summary: str | None = None
    patch_applied: bool | None = None
    output_chars: int = Field(default=0, ge=0)
    argument_fingerprint: str
    auto_generated: bool = False
