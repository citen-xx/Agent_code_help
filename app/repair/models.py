"""Structured repair plan models."""

from pydantic import BaseModel, ConfigDict, Field


class PlannedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(min_length=1)
    target_symbol: str = Field(min_length=1)
    change_description: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RepairValidationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_maven_test: bool
    focus: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_summary: str = Field(min_length=1)
    target_symbols: list[str] = Field(min_length=1)
    impacted_files: list[str] = Field(default_factory=list)
    impacted_tests: list[str] = Field(default_factory=list)
    planned_changes: list[PlannedChange] = Field(min_length=1)
    risk_notes: list[str] = Field(default_factory=list)
    validation_plan: RepairValidationPlan


class CreateRepairPlanRequest(RepairPlan):
    revision_reason: str | None = None


class PlanRevision(BaseModel):
    revision: int = Field(ge=1)
    reason: str
    plan: RepairPlan
    modified_files: list[str] = Field(default_factory=list)
    failure_summary: str | None = None
    diff: str = ""
