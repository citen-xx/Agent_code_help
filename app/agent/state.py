"""LangGraph state definition for the Java code agent."""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage

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
