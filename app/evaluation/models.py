"""Benchmark task, result, and report models."""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.repair.models import PlanRevision
from app.schemas.agent import AnalyzeResponse
from app.workspace.models import TestRunResult


class FailureCategory(StrEnum):
    WORKSPACE_SETUP_FAILED = "workspace_setup_failed"
    INITIAL_TEST_UNEXPECTEDLY_PASSED = "initial_test_unexpectedly_passed"
    AGENT_FAILED = "agent_failed"
    TIMEOUT = "timeout"
    FINAL_TEST_FAILED = "final_test_failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class BenchmarkTask(BaseModel):
    task_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    base_commit: str = Field(min_length=1)
    problem_statement: str = Field(min_length=1)
    expected_test_command: str = Field(default="mvn test")
    timeout: float = Field(default=300.0, gt=0)
    tags: list[str] = Field(default_factory=list)


class LLMUsage(BaseModel):
    llm_calls: int = Field(default=0, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class AgentRunOutput(BaseModel):
    response: AnalyzeResponse
    usage: LLMUsage = Field(default_factory=LLMUsage)


class BenchmarkTaskResult(BaseModel):
    task_id: str
    success: bool
    final_test_passed: bool
    modified_files: list[str] = Field(default_factory=list)
    repair_rounds: int = Field(default=0, ge=0)
    plan_revisions: list[PlanRevision] = Field(default_factory=list)
    duration_seconds: float = Field(ge=0)
    final_diff: str = ""
    failure_reason: str | None = None
    failure_category: FailureCategory | None = None
    agent_answer: str = ""
    workspace_id: str | None = None
    initial_test_result: TestRunResult | None = None
    final_test_result: TestRunResult | None = None
    llm_calls: int = Field(default=0, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class BenchmarkSummary(BaseModel):
    total_tasks: int
    passed_tasks: int
    failed_tasks: int
    success_rate: float
    average_duration: float
    average_repair_rounds: float
    total_llm_calls: int
    total_tokens: int | None
    estimated_cost_usd: float | None


class BenchmarkReport(BaseModel):
    run_id: str
    started_at: str
    finished_at: str
    tasks: list[BenchmarkTask]
    results: list[BenchmarkTaskResult]
    summary: BenchmarkSummary
