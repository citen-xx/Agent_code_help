"""LangGraph state definition for the Java code agent."""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage

from app.agent.budget import BudgetExtension
from app.agent.commitment import DependencyProbeEvent, PlanStatusEvent
from app.repair.models import PlanRevision, RepairPlan
from app.agent.telemetry import ToolEvent
from app.workspace.models import TestRunResult


class AgentState(TypedDict):
    """State shared across agent graph nodes.

    Attributes:
        task: The coding/analysis task given by the user.
        messages: Chat history (LLM + tool messages, appended via reducer).
        current_step: Label of the current stage ("init" | "thinking" |
            "tools" | "finished" | "error").
        step_count: Number of LLM calls executed so far.
        final_answer: The agent's final response.
        modified_files: Files successfully changed by apply_patch.
        test_result: Latest Maven/Gradle test result.
        has_seen_test_failure: Whether a test run has failed.
        repair_rounds: Number of apply_patch calls made after test failures.
        repair_plan: Current structured repair plan.
        plan_revisions: Historical plan revisions.
        plan_stale: Whether a test failure requires a revised plan.
        latest_patch_diff: Latest diff returned by apply_patch.
        last_failure_summary: Most recent test failure summary.
        tool_events: Passive telemetry for every tool execution.
        started_monotonic: Agent-run start timestamp for relative telemetry.
        effective_step_limit: Current policy-enforced LLM-call limit.
        budget_extensions: Deterministic adaptive-budget decisions.
        termination_reason: Stable reason why the Agent loop ended.
        last_progress_event: Most recent plan/patch/test progress marker.
        plan_status_events: Structured plan execution state transitions.
        evidence_prompt_sent: Whether sufficient-evidence guidance was emitted.
        evidence_sufficient_step: First step that met runtime evidence readiness.
        commitment_validation_errors: Rejected evidence/plan violations.
        dependency_probes: Targeted exploration tied to unresolved questions.
        dependency_probe_fingerprints: Deterministic probe deduplication keys.
    """

    task: str
    messages: Annotated[list[BaseMessage], operator.add]
    current_step: str
    step_count: int
    final_answer: str
    modified_files: Annotated[list[str], operator.add]
    test_result: TestRunResult | None
    has_seen_test_failure: bool
    repair_rounds: int
    repair_plan: RepairPlan | None
    plan_revisions: Annotated[list[PlanRevision], operator.add]
    plan_stale: bool
    latest_patch_diff: str
    last_failure_summary: str | None
    tool_events: Annotated[list[ToolEvent], operator.add]
    started_monotonic: float
    effective_step_limit: int
    budget_extensions: Annotated[list[BudgetExtension], operator.add]
    termination_reason: str | None
    last_progress_event: str | None
    plan_status_events: Annotated[list[PlanStatusEvent], operator.add]
    evidence_prompt_sent: bool
    evidence_sufficient_step: int | None
    commitment_validation_errors: Annotated[list[str], operator.add]
    dependency_probes: Annotated[list[DependencyProbeEvent], operator.add]
    dependency_probe_fingerprints: list[str]
    last_quality_plan_step: int | None
    targeted_exploration_calls: int
    broad_exploration_calls: int
