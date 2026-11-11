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
    LOCALIZATION_FAILURE = "localization_failure"
    PLANNING_FAILURE = "planning_failure"
    PATCH_FAILURE = "patch_failure"
    PUBLIC_TEST_FAILURE = "public_test_failure"
    HIDDEN_TEST_FAILURE = "hidden_test_failure"
    REPAIR_LIMIT_EXCEEDED = "repair_limit_exceeded"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


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
    suite: str = "v1"
    difficulty: Difficulty = Difficulty.EASY
    hidden_tests: str | None = None


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
    suite: str = "v1"
    difficulty: Difficulty = Difficulty.EASY
    tags: list[str] = Field(default_factory=list)
    success: bool
    public_tests_passed: bool = False
    hidden_tests_passed: bool | None = None
    hidden_failure_summary: str | None = None
    final_test_passed: bool
    modified_files: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    tool_calls: int = Field(default=0, ge=0)
    last_agent_action: str = ""
    tool_usage: dict[str, int] = Field(default_factory=dict)
    tool_failure_counts: dict[str, int] = Field(default_factory=dict)
    empty_patch_calls: int = Field(default=0, ge=0)
    auto_maven_test_runs: int = Field(default=0, ge=0)
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
    hidden_test_result: TestRunResult | None = None
    changed_file_count: int = Field(default=0, ge=0)
    diff_lines_added: int = Field(default=0, ge=0)
    diff_lines_removed: int = Field(default=0, ge=0)
    diff_size: int = Field(default=0, ge=0)
    modified_unrelated_files: bool = False
    modified_test_files: bool = False
    modified_build_files: bool = False
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
    average_llm_calls: float = 0.0
    average_tokens: float | None = None
    average_repair_rounds: float
    average_plan_revisions: float
    average_changed_files: float
    average_diff_size: float
    total_llm_calls: int
    total_tokens: int | None
    estimated_cost_usd: float | None
    public_test_success_rate: float
    hidden_test_pass_rate: float = 0.0
    medium_success_rate: float = 0.0
    hard_success_rate: float = 0.0
    hidden_tests_evaluated: int = Field(default=0, ge=0)
    hidden_test_success_rate: float
    success_by_difficulty: dict[str, float] = Field(default_factory=dict)
    success_by_tag: dict[str, float] = Field(default_factory=dict)


class BenchmarkReport(BaseModel):
    run_id: str
    started_at: str
    finished_at: str
    model: str = "unknown"
    benchmark: str = "unknown"
    variant: str = "full"
    capabilities: dict[str, bool] = Field(default_factory=dict)
    git_commit: str | None = None
    git_dirty: bool | None = None
    tasks: list[BenchmarkTask]
    results: list[BenchmarkTaskResult]
    summary: BenchmarkSummary
