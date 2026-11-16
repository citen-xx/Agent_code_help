"""Structured repair plan models."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EvidenceType(StrEnum):
    TEST_FAILURE = "test_failure"
    SOURCE_READ = "source_read"
    SYMBOL_QUERY = "symbol_query"
    CALL_GRAPH = "call_graph"
    GIT_CONTEXT = "git_context"


class PlanExecutionStatus(StrEnum):
    PROPOSED = "proposed"
    COMMITTED = "committed"
    EXECUTING = "executing"
    PATCH_APPLIED = "patch_applied"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    REJECTED = "rejected"
    REVISED = "revised"


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_type: EvidenceType
    description: str = Field(min_length=1)
    path: str | None = None
    symbol: str | None = None
    query: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class RepairHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_observation: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(min_length=1)
    suspected_files: list[str] = Field(default_factory=list)
    suspected_symbols: list[str] = Field(default_factory=list)
    root_cause_hypothesis: str = Field(min_length=1)
    proposed_change: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    verification_strategy: list[str] = Field(min_length=1)
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


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


class EvidenceRepairPlan(RepairPlan):
    hypothesis: RepairHypothesis


class CreateEvidenceRepairPlanRequest(EvidenceRepairPlan):
    revision_reason: str | None = None


class PlanRevision(BaseModel):
    revision: int = Field(ge=1)
    reason: str
    plan: RepairPlan | EvidenceRepairPlan
    modified_files: list[str] = Field(default_factory=list)
    failure_summary: str | None = None
    diff: str = ""
