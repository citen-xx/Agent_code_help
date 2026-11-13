"""Deterministic step-budget policies for the Agent loop."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StepBudgetPolicyName(StrEnum):
    FIXED_20 = "fixed_20"
    FIXED_30 = "fixed_30"
    ADAPTIVE_20_30 = "adaptive_20_30"


DEFAULT_PRODUCTION_BUDGET = StepBudgetPolicyName.ADAPTIVE_20_30


class BudgetProgress(BaseModel):
    """Runtime-visible progress only; evaluator ground truth is excluded."""

    model_config = ConfigDict(extra="forbid")

    step_count: int = Field(ge=0)
    current_step_limit: int = Field(ge=1)
    final_test_passed: bool = False
    plan_active: bool = False
    plan_stale: bool = False
    last_plan_step: int | None = Field(default=None, ge=1)
    last_successful_patch_step: int | None = Field(default=None, ge=1)
    last_test_step: int | None = Field(default=None, ge=1)
    last_test_failed: bool = False
    has_seen_test_failure: bool = False
    repair_rounds: int = Field(default=0, ge=0)
    recent_execution_events: list[str] = Field(default_factory=list)


class BudgetDecision(BaseModel):
    extend: bool
    extra_steps: int = Field(default=0, ge=0)
    reason: str
    last_progress_event: str | None = None


class BudgetExtension(BaseModel):
    step_at_extension: int = Field(ge=1)
    previous_limit: int = Field(ge=1)
    new_limit: int = Field(ge=1)
    steps_granted: int = Field(gt=0)
    reason: str
    last_progress_event: str | None = None


class StepBudgetTelemetry(BaseModel):
    policy: str
    base_step_limit: int = Field(ge=1)
    hard_step_limit: int = Field(ge=1)
    effective_step_limit: int = Field(ge=1)
    extensions_granted: int = Field(default=0, ge=0)
    extension_steps_granted: int = Field(default=0, ge=0)
    extension_steps: list[BudgetExtension] = Field(default_factory=list)
    extension_reason: str | None = None
    step_at_extension: int | None = Field(default=None, ge=1)
    last_progress_event: str | None = None
    termination_reason: str


class StepBudgetPolicy:
    """Base interface shared by fixed and adaptive policies."""

    name: str
    base_step_limit: int
    hard_step_limit: int
    extension_increment: int = 0

    def should_extend(self, progress: BudgetProgress) -> BudgetDecision:
        return BudgetDecision(
            extend=False,
            reason="fixed_budget_exhausted",
        )


class FixedStepBudgetPolicy(StepBudgetPolicy):
    def __init__(
        self,
        *,
        limit: int,
        name: str,
    ) -> None:
        self.name = name
        self.base_step_limit = limit
        self.hard_step_limit = limit
        self.extension_increment = 0


class AdaptiveStepBudgetPolicy(StepBudgetPolicy):
    """Conservative 20 -> 25 -> 30 policy."""

    def __init__(self) -> None:
        self.name = StepBudgetPolicyName.ADAPTIVE_20_30
        self.base_step_limit = 20
        self.hard_step_limit = 30
        self.extension_increment = 5
        self.recent_window = 5
        self.plan_recency_window = 4

    def should_extend(self, progress: BudgetProgress) -> BudgetDecision:
        if progress.final_test_passed:
            return BudgetDecision(
                extend=False,
                reason="success",
                last_progress_event="final_test_passed",
            )
        if progress.step_count >= self.hard_step_limit:
            return BudgetDecision(
                extend=False,
                reason="hard_cap_reached",
                last_progress_event=self._last_progress(progress),
            )
        if not progress.recent_execution_events:
            return BudgetDecision(
                extend=False,
                reason="exploration_only",
                last_progress_event=self._last_progress(progress),
            )

        last_progress = progress.recent_execution_events[-1]
        patch_recent = (
            progress.last_successful_patch_step is not None
            and progress.step_count - progress.last_successful_patch_step
            < self.recent_window
        )
        patch_unverified = (
            patch_recent
            and (
                progress.last_test_step is None
                or progress.last_test_step
                < progress.last_successful_patch_step
                or progress.last_test_failed
            )
        )
        if patch_unverified:
            return BudgetDecision(
                extend=True,
                extra_steps=self.extension_increment,
                reason="patch_attempted_and_unverified",
                last_progress_event=last_progress,
            )

        recent_plan = (
            progress.last_plan_step is not None
            and progress.step_count - progress.last_plan_step
            <= self.plan_recency_window
        )
        plan_pending = (
            progress.plan_active
            and (
                progress.last_successful_patch_step is None
                or progress.last_plan_step is not None
                and progress.last_plan_step
                > progress.last_successful_patch_step
            )
        )
        if recent_plan and plan_pending:
            return BudgetDecision(
                extend=True,
                extra_steps=self.extension_increment,
                reason="recent_repair_plan_pending",
                last_progress_event=last_progress,
            )

        repair_cycle = (
            progress.has_seen_test_failure
            and progress.last_test_step is not None
            and progress.step_count - progress.last_test_step
            < self.recent_window
            and (
                progress.repair_rounds > 0
                or progress.plan_stale
            )
            and progress.last_test_failed
        )
        if repair_cycle:
            return BudgetDecision(
                extend=True,
                extra_steps=self.extension_increment,
                reason="repair_cycle_active",
                last_progress_event=last_progress,
            )

        return BudgetDecision(
            extend=False,
            reason="no_qualifying_execution_progress",
            last_progress_event=last_progress,
        )

    @staticmethod
    def _last_progress(progress: BudgetProgress) -> str | None:
        if progress.recent_execution_events:
            return progress.recent_execution_events[-1]
        if progress.last_successful_patch_step is not None:
            return f"step {progress.last_successful_patch_step}: patch"
        if progress.last_plan_step is not None:
            return f"step {progress.last_plan_step}: repair_plan"
        if progress.last_test_step is not None:
            return f"step {progress.last_test_step}: test"
        return None


def create_step_budget_policy(
    name: str | StepBudgetPolicyName,
    *,
    legacy_max_steps: int | None = None,
) -> StepBudgetPolicy:
    if isinstance(name, StepBudgetPolicyName):
        resolved = name
    else:
        try:
            resolved = StepBudgetPolicyName(name)
        except ValueError:
            if legacy_max_steps is None:
                raise ValueError(f"未知 Step Budget Policy: {name}") from None
            return FixedStepBudgetPolicy(
                limit=legacy_max_steps,
                name=f"fixed_{legacy_max_steps}",
            )
    if resolved is StepBudgetPolicyName.FIXED_20:
        return FixedStepBudgetPolicy(
            limit=20,
            name=StepBudgetPolicyName.FIXED_20,
        )
    if resolved is StepBudgetPolicyName.FIXED_30:
        return FixedStepBudgetPolicy(
            limit=30,
            name=StepBudgetPolicyName.FIXED_30,
        )
    return AdaptiveStepBudgetPolicy()


def production_step_budget_name(max_steps: int) -> str:
    """Use Adaptive for the production default and fixed custom limits in tests."""
    if max_steps == 20:
        return DEFAULT_PRODUCTION_BUDGET.value
    return f"fixed_{max_steps}"
