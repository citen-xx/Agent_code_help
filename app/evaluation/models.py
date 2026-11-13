"""Benchmark task, result, and report models."""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.agent.telemetry import ToolEvent
from app.repair.models import PlanRevision
from app.schemas.agent import AnalyzeResponse
from app.workspace.models import TestRunResult


class FailureCategory(StrEnum):
    REPRODUCTION_FAILURE = "reproduction_failure"
    COMPILE_FAILURE = "compile_failure"
    WORKSPACE_SETUP_FAILED = "workspace_setup_failed"
    INITIAL_TEST_UNEXPECTEDLY_PASSED = "initial_test_unexpectedly_passed"
    AGENT_FAILED = "agent_failed"
    TIMEOUT = "timeout"
    FINAL_TEST_FAILED = "final_test_failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    LOCALIZATION_FAILURE = "localization_failure"
    ROOT_CAUSE_FAILURE = "root_cause_failure"
    INCORRECT_PATCH = "incorrect_patch"
    INCOMPLETE_PATCH = "incomplete_patch"
    REGRESSION = "regression"
    TEST_OVERFITTING = "test_overfitting"
    STEP_LIMIT = "step_limit"
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
    source: str = "fixture"
    external_difficulty: str | None = None
    project: str | None = None
    bug_id: int | None = Field(default=None, ge=1)
    buggy_revision: str | None = None
    fixed_revision: str | None = None
    triggering_tests: list[str] = Field(default_factory=list)
    relevant_tests: list[str] = Field(default_factory=list)
    ground_truth_files: list[str] = Field(default_factory=list)
    jdk: str | None = None
    test_patch: str | None = None


class LLMUsage(BaseModel):
    llm_calls: int = Field(default=0, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class ContextCallTelemetry(BaseModel):
    """Per-LLM-call provider usage and exact message-history chars."""

    call_index: int = Field(ge=1)
    task_id: str
    profile: str
    repair_round: int = Field(default=0, ge=0)
    message_count: int = Field(default=0, ge=0)
    provider_prompt_tokens: int | None = Field(default=None, ge=0)
    provider_completion_tokens: int | None = Field(default=None, ge=0)
    provider_total_tokens: int | None = Field(default=None, ge=0)
    system_message_chars: int = Field(default=0, ge=0)
    user_message_chars: int = Field(default=0, ge=0)
    assistant_message_chars: int = Field(default=0, ge=0)
    tool_message_chars: int = Field(default=0, ge=0)
    tool_call_chars: int = Field(default=0, ge=0)
    ci_tool_chars: int = Field(default=0, ge=0)
    read_file_chars: int = Field(default=0, ge=0)
    test_output_chars: int = Field(default=0, ge=0)
    patch_output_chars: int = Field(default=0, ge=0)
    planner_chars: int = Field(default=0, ge=0)
    other_chars: int = Field(default=0, ge=0)
    tool_schema_chars: int = Field(default=0, ge=0)
    total_history_chars: int = Field(default=0, ge=0)


class MessageLifecycleTelemetry(BaseModel):
    """Raw chars and cumulative exposure for one history message."""

    message_key: str
    role: str
    source: str
    raw_chars: int = Field(ge=0)
    introduced_call_index: int = Field(ge=1)
    retained_call_count: int = Field(default=0, ge=0)
    context_exposure_chars: int = Field(default=0, ge=0)


class RawToolOutputTelemetry(BaseModel):
    """Raw test output retained in the evaluation trace, not model context."""

    call_index: int = Field(ge=1)
    tool_name: str
    content: str


class ContextTelemetry(BaseModel):
    calls: list[ContextCallTelemetry] = Field(default_factory=list)
    messages: list[MessageLifecycleTelemetry] = Field(default_factory=list)
    raw_test_outputs: list[RawToolOutputTelemetry] = Field(default_factory=list)


class AgentRunOutput(BaseModel):
    response: AnalyzeResponse
    usage: LLMUsage = Field(default_factory=LLMUsage)
    context_telemetry: ContextTelemetry = Field(default_factory=ContextTelemetry)


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
    agent_tool_calls: int = Field(default=0, ge=0)
    harness_action_count: int = Field(default=0, ge=0)
    manual_test_runs: int = Field(default=0, ge=0)
    apply_patch_attempts: int = Field(default=0, ge=0)
    apply_patch_failures: int = Field(default=0, ge=0)
    apply_patch_failure_rate: float | None = Field(default=None, ge=0, le=1)
    tool_output_chars: int = Field(default=0, ge=0)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    apply_patch_invocations: int = Field(default=0, ge=0)
    patch_files_requested: int = Field(default=0, ge=0)
    patch_replacements_requested: int = Field(default=0, ge=0)
    successful_patch_replacements: int = Field(default=0, ge=0)
    failed_patch_replacement_indices: list[int] = Field(default_factory=list)
    recovered_patch_failures: int = Field(default=0, ge=0)
    ci_telemetry: dict = Field(default_factory=dict)
    total_ci_output_chars: int = Field(default=0, ge=0)
    read_file_calls: int = Field(default=0, ge=0)
    follow_up_source_reads: int = Field(default=0, ge=0)
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
    context_telemetry: ContextTelemetry = Field(default_factory=ContextTelemetry)
    benchmark_metrics: dict = Field(default_factory=dict)
    secondary_observations: list[str] = Field(default_factory=list)


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
