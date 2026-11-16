"""Minimal LangGraph agent loop.

Pipeline::

    START -> agent -> [has tool calls?]
              ^            |
              |      yes   v
              +------- tools (execute, feed results back to the LLM)
              |            |
              +---- no ----+--> END (final answer)

The agent calls the LLM with repository inspection, atomic patch, and
controlled test tools bound; tool results are appended to ``messages`` and
the loop repeats until the LLM answers without tool calls, an error occurs,
the repair limit is reached, or the maximum step count is reached.

Only hand-written nodes and edges are used — no high-level agent APIs.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from app.agent.capabilities import (
    DEFAULT_AGENT_PROFILE,
    FULL_CAPABILITIES,
    AgentCapabilities,
    AgentProfile,
    capabilities_for,
    resolve_profile,
)
from app.agent.budget import (
    BudgetExtension,
    BudgetProgress,
    StepBudgetPolicy,
    StepBudgetPolicyName,
    StepBudgetTelemetry,
    create_step_budget_policy,
    production_step_budget_name,
)
from app.agent.context_lifecycle import compact_model_messages
from app.agent.commitment import (
    BROAD_EXPLORATION_TOOLS,
    CommitmentTelemetry,
    DependencyProbeEvent,
    EXPLORATION_TOOLS,
    EvidenceCommitmentController,
    PlanStatusEvent,
)
from app.agent.state import AgentState
from app.agent.telemetry import PatchErrorCategory, ToolEvent
from app.code_intel.tools import create_code_intel_tools
from app.core.config import settings
from app.llm.client import build_chat_model
from app.repair.models import (
    CreateEvidenceRepairPlanRequest,
    CreateQualityEvidenceRepairPlanRequest,
    CreateRepairPlanRequest,
    EvidenceRepairPlan,
    PlanExecutionStatus,
    PlanRevision,
    RepairPlan,
    QualityEvidenceRepairPlan,
)
from app.repair.tools import create_repair_plan_tools
from app.schemas.agent import AnalyzeResponse
from app.tools.build_tools import create_build_tools
from app.tools.git_tools import create_git_tools
from app.tools.patch_tools import create_patch_tools
from app.tools.registry import create_repo_tools
from app.workspace import git
from app.workspace.models import TestRunResult
from app.workspace.patch import MAX_PATCH_DIFF_CHARS

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 20
MAX_REPAIR_ROUNDS = 3
#: Safety cap for a whole agent run (single LLM calls have their own timeout).
DEFAULT_RUN_TIMEOUT_SECONDS = 300.0

PROMPT_COMMON_START = (
    "You are a Java coding assistant working inside one isolated workspace. "
)
PROMPT_CODE_INTELLIGENCE = (
    "Prefer Java symbol tools (list_symbols, find_symbol, find_references) to "
    "locate declarations and usages, and call analyze_impact before changing a "
    "method when its blast radius matters. Use find_callers/find_callees for call "
    "relationships, then read_file or search_code only as needed. "
)
PROMPT_REPAIR_PLANNER = (
    "Before the first apply_patch, create a structured plan with create_repair_plan. "
    "After every failed Maven test, revise the plan with create_repair_plan and a "
    "revision_reason before patching again. For a code change, follow this order: "
    "locate the problem, read relevant code, create the plan, use apply_patch to "
    "edit existing UTF-8 files. "
)
PROMPT_REPAIR_LOOP = (
    "A successful patch automatically runs the fixed "
    "Maven test. Inspect failures, read code again, revise the plan, and patch again. "
)
PROMPT_COMMON_END = (
    "apply_patch requires exact old_text/new_text replacements and is atomic. "
    "Never modify files outside apply_patch, never run shell commands, and never "
    "invent Maven arguments. At most three repair patches are allowed after the "
    "first failed test. When tests pass, summarize the change and mention the "
    "test result and diff. Answer in the same language as the user's task."
)


def build_system_prompt(capabilities: AgentCapabilities) -> str:
    parts = [PROMPT_COMMON_START]
    if capabilities.code_intelligence:
        parts.append(PROMPT_CODE_INTELLIGENCE)
    if capabilities.repair_planner:
        parts.append(PROMPT_REPAIR_PLANNER)
    if capabilities.repair_loop:
        parts.append(PROMPT_REPAIR_LOOP)
    parts.append(PROMPT_COMMON_END)
    return "".join(parts)


SYSTEM_PROMPT = build_system_prompt(FULL_CAPABILITIES)


def _test_failure_summary(result: TestRunResult) -> str:
    parts = [
        result.error or "",
        (result.stderr or "")[-1000:],
        (result.stdout or "")[-1000:],
    ]
    summary = "\n".join(part for part in parts if part).strip()
    return summary[:2000]


def _argument_fingerprint(arguments: dict) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _target_file(tool_name: str, arguments: dict) -> str | None:
    if tool_name == "apply_patch":
        paths = list(
            dict.fromkeys(
                str(file_entry.get("path"))
                for file_entry in arguments.get("files", [])
                if isinstance(file_entry, dict) and file_entry.get("path")
            )
        )
        return ", ".join(paths[:5]) or None
    for key in ("path", "file_path"):
        if arguments.get(key):
            return str(arguments[key])
    return None


def _tool_query(tool_name: str, arguments: dict) -> str | None:
    for key in (
        "query",
        "symbol",
        "name",
        "file",
        "path",
        "class_name",
        "method",
    ):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _result_files(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates: list[dict] = []
    for key in (
        "symbols",
        "references",
        "calls",
        "targets",
        "direct_callers",
        "test_references",
        "related_methods",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(
                item for item in value if isinstance(item, dict)
            )
    paths = list(
        dict.fromkeys(
            str(item["file"])
            for item in candidates
            if item.get("file")
        )
    )
    return ", ".join(paths[:20]) or None


def _classify_patch_error(content: str) -> PatchErrorCategory:
    try:
        body = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict) and body.get("error_category"):
        try:
            return PatchErrorCategory(body["error_category"])
        except ValueError:
            pass
    if (
        "同一路径不能在一次 patch 中重复" in content
        or "同一路径只能出现一次" in content
        or "ApplyPatchRequest" in content
        or "validation error" in content.lower()
    ):
        return PatchErrorCategory.INVALID_PATCH_FORMAT
    if "出现 0 次" in content:
        return PatchErrorCategory.OLD_TEXT_NOT_FOUND
    if "出现" in content and "次" in content:
        return PatchErrorCategory.OLD_TEXT_MULTIPLE_MATCHES
    if "目标不是已存在" in content or "文件不存在" in content:
        return PatchErrorCategory.TARGET_FILE_NOT_FOUND
    if any(
        marker in content
        for marker in ("路径非法", "越出", "符号链接", "硬链接", "禁止修改 .git")
    ):
        return PatchErrorCategory.PATH_VALIDATION_FAILED
    if any(marker in content for marker in ("UTF-8", "二进制", "换行")):
        return PatchErrorCategory.ENCODING_ERROR
    if any(marker in content.lower() for marker in ("permission", "权限", "workspace")):
        return PatchErrorCategory.PERMISSION_OR_WORKSPACE_ERROR
    if any(marker in content.lower() for marker in ("超时", "timeout", "timed out")):
        return PatchErrorCategory.TIMEOUT
    return PatchErrorCategory.OTHER


def _error_summary(content: str, limit: int = 500) -> str:
    normalized = " ".join(content.split())
    return normalized[:limit]


def _tool_event(
    *,
    index: int,
    step: int,
    repair_round: int,
    tool_name: str,
    call_id: str | None,
    arguments: dict,
    content: str,
    success: bool,
    invoked: bool,
    patch_applied: bool | None = None,
    auto_generated: bool = False,
    elapsed_seconds: float | None = None,
) -> ToolEvent:
    category = None
    summary = None
    if not success:
        category = (
            _classify_patch_error(content)
            if tool_name == "apply_patch"
            else PatchErrorCategory.OTHER
        )
        summary = _error_summary(content)
    files = arguments.get("files", [])
    files_requested = len(files) if isinstance(files, list) else 0
    replacements_requested = sum(
        len(file_entry.get("replacements", []))
        for file_entry in files
        if isinstance(file_entry, dict)
    )
    successful_replacements = 0
    failed_replacement_index = None
    result_count = 0
    truncated = False
    duplicate_results_removed = 0
    try:
        body = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, dict):
        successful_replacements = int(body.get("successful_replacements") or 0)
        index_value = body.get("replacement_index")
        failed_replacement_index = int(index_value) if index_value else None
        returned = body.get("returned")
        if isinstance(returned, dict):
            result_count = sum(int(value) for value in returned.values())
        elif returned is not None:
            result_count = int(returned)
        elif body.get("result_count") is not None:
            result_count = int(body["result_count"])
        else:
            result_count = len(body.get("affected_files", []))
        truncated = bool(body.get("truncated"))
        duplicate_results_removed = int(
            body.get("duplicates_removed")
            or body.get("duplicate_results_removed")
            or 0
        )
        if not success and body.get("message"):
            summary = str(body["message"])[:500]
    target_file = _target_file(tool_name, arguments)
    if target_file is None and isinstance(body, dict):
        target_file = _result_files(body)
    start_line = arguments.get("start_line")
    end_line = arguments.get("end_line")
    return ToolEvent(
        tool_call_index=index,
        step=step,
        repair_round=repair_round,
        tool_name=tool_name,
        call_id=call_id,
        target_file=target_file,
        query=_tool_query(tool_name, arguments),
        success=success,
        invoked=invoked,
        error_category=category,
        error_summary=summary,
        patch_applied=patch_applied,
        output_chars=len(content),
        argument_fingerprint=_argument_fingerprint(arguments),
        auto_generated=auto_generated,
        files_requested=files_requested,
        replacements_requested=replacements_requested,
        successful_replacements=successful_replacements,
        failed_replacement_index=failed_replacement_index,
        result_count=result_count,
        truncated=truncated,
        duplicate_results_removed=duplicate_results_removed,
        read_start_line=int(start_line) if start_line else None,
        read_end_line=int(end_line) if end_line else None,
        elapsed_seconds=elapsed_seconds,
    )


def _execution_metrics(
    messages: list,
) -> tuple[int, str, dict[str, int], dict[str, int], int, int]:
    """Derive tool-call telemetry from the completed message history."""
    calls_by_id: dict[str, tuple[str, dict]] = {}
    tool_calls = 0
    last_action = ""
    tool_usage: dict[str, int] = {}
    tool_failure_counts: dict[str, int] = {}
    empty_patch_calls = 0
    auto_maven_test_runs = 0
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_id = str(call.get("id") or "")
                if call_id:
                    calls_by_id[call_id] = (
                        str(call.get("name") or "unknown"),
                        call.get("args") or {},
                    )
        elif isinstance(message, ToolMessage):
            tool_calls += 1
            action = calls_by_id.get(str(message.tool_call_id))
            if action is not None:
                name, args = action
                tool_usage[name] = tool_usage.get(name, 0) + 1
                content = str(message.content)
                structured_failure = False
                if content.lstrip().startswith("{"):
                    try:
                        structured_body = json.loads(content)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        structured_body = {}
                    structured_failure = structured_body.get("success") is False
                if content.startswith(("[tool error]", "[planner required]")) or structured_failure:
                    tool_failure_counts[name] = tool_failure_counts.get(name, 0) + 1
                if name == "apply_patch" and content.lstrip().startswith("{"):
                    try:
                        body = json.loads(content)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        body = {}
                    if body.get("success") is True and not str(body.get("diff") or "").strip():
                        empty_patch_calls += 1
                if name == "run_tests" and str(message.tool_call_id).startswith(
                    "auto_test_"
                ):
                    auto_maven_test_runs += 1
                last_action = (
                    f"{name} {json.dumps(args, ensure_ascii=False, sort_keys=True)}"
                )[:2000]
    return (
        tool_calls,
        last_action,
        dict(sorted(tool_usage.items())),
        dict(sorted(tool_failure_counts.items())),
        empty_patch_calls,
        auto_maven_test_runs,
    )


def _patch_telemetry(events: list[ToolEvent]) -> dict:
    patch_events = [
        event for event in events if event.tool_name == "apply_patch"
    ]
    recovered = 0
    for index, event in enumerate(patch_events):
        if event.success:
            continue
        failed_targets = {
            path.strip()
            for path in (event.target_file or "").split(",")
            if path.strip()
        }
        if any(
            later.success
            and failed_targets
            & {
                path.strip()
                for path in (later.target_file or "").split(",")
                if path.strip()
            }
            for later in patch_events[index + 1 :]
        ):
            recovered += 1
    return {
        "apply_patch_invocations": sum(
            1 for event in patch_events if event.invoked
        ),
        "apply_patch_failures": sum(
            1 for event in patch_events if not event.success
        ),
        "patch_files_requested": sum(
            event.files_requested for event in patch_events
        ),
        "patch_replacements_requested": sum(
            event.replacements_requested for event in patch_events
        ),
        "successful_patch_replacements": sum(
            event.successful_replacements for event in patch_events
        ),
        "failed_patch_replacement_indices": [
            event.failed_replacement_index
            for event in patch_events
            if not event.success and event.failed_replacement_index is not None
        ],
        "recovered_patch_failures": recovered,
    }


CI_TOOL_NAMES = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
}


def _ci_telemetry(events: list[ToolEvent]) -> dict:
    per_tool: dict[str, dict] = {}
    known_ci_files: set[str] = set()
    follow_up_source_reads = 0
    read_file_calls = 0
    for event in events:
        if event.tool_name == "read_file":
            read_file_calls += 1
            if event.target_file in known_ci_files:
                follow_up_source_reads += 1
        if event.tool_name not in CI_TOOL_NAMES:
            continue
        for path in (event.target_file or "").split(","):
            if path.strip():
                known_ci_files.add(path.strip())
        stats = per_tool.setdefault(
            event.tool_name,
            {
                "invocation_count": 0,
                "output_chars": 0,
                "result_count": 0,
                "truncated_count": 0,
                "duplicate_results_removed": 0,
                "max_output_chars": 0,
            },
        )
        stats["invocation_count"] += 1
        stats["output_chars"] += event.output_chars
        stats["result_count"] += event.result_count
        stats["truncated_count"] += int(event.truncated)
        stats["duplicate_results_removed"] += event.duplicate_results_removed
        stats["max_output_chars"] = max(
            stats["max_output_chars"],
            event.output_chars,
        )
    for stats in per_tool.values():
        stats["avg_output_chars"] = (
            stats["output_chars"] / stats["invocation_count"]
            if stats["invocation_count"]
            else 0.0
        )
    return {
        "per_tool": per_tool,
        "total_ci_output_chars": sum(
            stats["output_chars"] for stats in per_tool.values()
        ),
        "read_file_calls": read_file_calls,
        "follow_up_source_reads": follow_up_source_reads,
    }


def _budget_progress(state: AgentState) -> BudgetProgress:
    events = list(state.get("tool_events", []))
    current_step = state["step_count"]
    recent_threshold = current_step - 4

    def latest_step(predicate) -> int | None:
        return max(
            (
                event.step
                for event in events
                if predicate(event)
            ),
            default=None,
        )

    plan_steps = [
        event.step
        for event in events
        if event.tool_name == "create_repair_plan" and event.success
    ]
    patch_steps = [
        event.step
        for event in events
        if event.tool_name == "apply_patch"
        and event.success
        and event.patch_applied is not False
    ]
    last_test_step = latest_step(
        lambda event: event.tool_name == "run_tests" and event.invoked
    )
    test_result = state.get("test_result")
    recent_execution_events = []
    for event in events:
        if event.step < recent_threshold:
            continue
        if event.tool_name == "create_repair_plan" and event.success:
            recent_execution_events.append(
                f"step {event.step}: create_repair_plan"
            )
        elif (
            event.tool_name == "apply_patch"
            and event.success
            and event.patch_applied is not False
        ):
            recent_execution_events.append(f"step {event.step}: apply_patch")
        elif event.tool_name == "run_tests" and event.invoked:
            recent_execution_events.append(f"step {event.step}: run_tests")
    recent_execution_events = list(dict.fromkeys(recent_execution_events))
    return BudgetProgress(
        step_count=current_step,
        current_step_limit=state.get(
            "effective_step_limit",
            state["step_count"],
        ),
        final_test_passed=bool(test_result and test_result.success),
        plan_active=state.get("repair_plan") is not None,
        plan_stale=bool(state.get("plan_stale", True)),
        last_plan_step=max(plan_steps, default=None),
        last_successful_patch_step=max(patch_steps, default=None),
        last_test_step=last_test_step,
        last_test_failed=bool(test_result and not test_result.success),
        has_seen_test_failure=bool(
            state.get("has_seen_test_failure", False)
        ),
        repair_rounds=int(state.get("repair_rounds", 0)),
        recent_execution_events=recent_execution_events,
    )


def _budget_termination_reason(
    policy: StepBudgetPolicy,
    state: AgentState,
    decision_reason: str,
) -> str:
    effective_limit = state.get(
        "effective_step_limit",
        policy.base_step_limit,
    )
    if effective_limit >= policy.hard_step_limit:
        if policy.name == StepBudgetPolicyName.ADAPTIVE_20_30.value:
            return "max_30"
        if policy.hard_step_limit == 30:
            return "max_30"
        return "base_step_limit_no_progress"
    if state.get("budget_extensions"):
        return "adaptive_extended_then_limit"
    if decision_reason == "exploration_only":
        return "base_step_limit_no_progress"
    return "base_step_limit_no_progress"


def _step_budget_telemetry(
    policy: StepBudgetPolicy,
    state: AgentState,
) -> StepBudgetTelemetry:
    extensions = list(state.get("budget_extensions", []))
    termination_reason = state.get("termination_reason")
    if termination_reason is None:
        test_result = state.get("test_result")
        if test_result is not None and test_result.success:
            termination_reason = "success"
        else:
            termination_reason = "agent_end"
    return StepBudgetTelemetry(
        policy=policy.name,
        base_step_limit=policy.base_step_limit,
        hard_step_limit=policy.hard_step_limit,
        effective_step_limit=state.get(
            "effective_step_limit",
            policy.base_step_limit,
        ),
        extensions_granted=len(extensions),
        extension_steps_granted=sum(
            extension.steps_granted for extension in extensions
        ),
        extension_steps=extensions,
        extension_reason=extensions[-1].reason if extensions else None,
        step_at_extension=(
            extensions[0].step_at_extension if extensions else None
        ),
        last_progress_event=state.get("last_progress_event"),
        termination_reason=termination_reason,
    )


def build_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    max_steps: int = DEFAULT_MAX_STEPS,
    capabilities: AgentCapabilities = FULL_CAPABILITIES,
    profile: AgentProfile = DEFAULT_AGENT_PROFILE,
    context_lifecycle_compaction: bool = False,
    step_budget_policy: StepBudgetPolicy | None = None,
):
    """Build the compiled agent graph: LLM <-> tools until a final answer."""
    tools_by_name = {tool.name: tool for tool in tools}
    budget_policy = step_budget_policy or create_step_budget_policy(
        production_step_budget_name(max_steps),
        legacy_max_steps=max_steps,
    )

    def agent_node(state: AgentState) -> dict:
        """Call the LLM with bound tools; update step counter / final answer."""
        effective_limit = state.get(
            "effective_step_limit",
            budget_policy.base_step_limit,
        )
        budget_update: dict = {}
        if state["step_count"] >= effective_limit:
            progress = _budget_progress(state)
            decision = budget_policy.should_extend(progress)
            if decision.extend and effective_limit < budget_policy.hard_step_limit:
                new_limit = min(
                    effective_limit + decision.extra_steps,
                    budget_policy.hard_step_limit,
                )
                extension = BudgetExtension(
                    step_at_extension=state["step_count"],
                    previous_limit=effective_limit,
                    new_limit=new_limit,
                    steps_granted=new_limit - effective_limit,
                    reason=decision.reason,
                    last_progress_event=decision.last_progress_event,
                )
                logger.info(
                    "Step budget extended %d -> %d (%s)",
                    effective_limit,
                    new_limit,
                    decision.reason,
                )
                budget_update = {
                    "effective_step_limit": new_limit,
                    "budget_extensions": [extension],
                    "last_progress_event": (
                        decision.last_progress_event
                        or state.get("last_progress_event")
                    ),
                }
            else:
                termination_reason = _budget_termination_reason(
                    budget_policy,
                    state,
                    decision.reason,
                )
                logger.warning(
                    "Step budget stopped at %d (%s)",
                    effective_limit,
                    decision.reason,
                )
                return {
                    "current_step": "finished",
                    "final_answer": (
                        f"已达到最大步骤数（{effective_limit}），"
                        f"Budget Policy 停止原因：{decision.reason}。"
                        "如需继续分析，请缩小任务范围后重试。"
                    ),
                    "termination_reason": termination_reason,
                    "last_progress_event": (
                        decision.last_progress_event
                        or state.get("last_progress_event")
                    ),
                }
        try:
            llm = model.bind_tools(tools) if tools else model
            set_context_metadata = getattr(
                llm,
                "set_context_metadata",
                None,
            )
            if callable(set_context_metadata):
                set_context_metadata(
                    repair_round=state.get("repair_rounds", 0),
                    profile=profile.value,
                    raw_messages=state["messages"],
                )
            model_messages = (
                compact_model_messages(state["messages"])
                if context_lifecycle_compaction
                else state["messages"]
            )
            response = llm.invoke(model_messages)
        except Exception as exc:
            logger.exception("LLM call failed (step %d)", state["step_count"] + 1)
            return {
                **budget_update,
                "current_step": "error",
                "step_count": state["step_count"] + 1,
                "final_answer": f"LLM 调用失败（可能为超时或网络错误），Agent 已停止：{exc}",
                "termination_reason": "error",
            }
        content = response.content if isinstance(response.content, str) else ""
        update: dict = {
            **budget_update,
            "messages": [response],
            "current_step": "thinking",
            "step_count": state["step_count"] + 1,
        }
        if not getattr(response, "tool_calls", None):
            update["current_step"] = "finished"
            update["final_answer"] = content or "（模型未返回内容）"
            test_result = state.get("test_result")
            update["termination_reason"] = (
                "success"
                if test_result is not None and test_result.success
                else "agent_end"
            )
        return update

    def tools_node(state: AgentState) -> dict:
        """Execute the pending tool calls; errors become tool messages."""
        last = state["messages"][-1]
        tool_messages: list = []
        modified_files: list[str] = []
        latest_test: TestRunResult | None = state.get("test_result")
        has_seen_test_failure = state.get("has_seen_test_failure", False)
        repair_rounds = state.get("repair_rounds", 0)
        repair_plan = state.get("repair_plan")
        plan_revisions: list[PlanRevision] = []
        plan_stale = state.get("plan_stale", True)
        latest_patch_diff = state.get("latest_patch_diff", "")
        last_failure_summary = state.get("last_failure_summary")
        last_progress_event = state.get("last_progress_event")
        plan_status_events: list[PlanStatusEvent] = []
        validation_errors: list[str] = []
        evidence_sufficient_step = state.get("evidence_sufficient_step")
        dependency_probes: list[DependencyProbeEvent] = []
        dependency_probe_fingerprints = list(
            state.get("dependency_probe_fingerprints", [])
        )
        targeted_exploration_calls = state.get(
            "targeted_exploration_calls",
            0,
        )
        broad_exploration_calls = state.get(
            "broad_exploration_calls",
            0,
        )
        last_quality_plan_step = state.get("last_quality_plan_step")
        tool_events: list[ToolEvent] = []
        calls = getattr(last, "tool_calls", None) or []
        patch_requested = any(call.get("name") == "apply_patch" for call in calls)
        patch_succeeded = False
        explicit_test_passed = False
        for call in calls:
            name = call.get("name")
            args = call.get("args") or {}
            call_id = call.get("id")
            repair_round_before_call = repair_rounds
            tool = tools_by_name.get(name)
            invocation_succeeded = False
            if tool is None:
                content = f"[tool error] 未知工具：{name}（可用工具：{', '.join(sorted(tools_by_name))}）"
            elif (
                capabilities.hypothesis_quality
                and name == "apply_patch"
                and isinstance(repair_plan, QualityEvidenceRepairPlan)
                and not plan_stale
                and EvidenceCommitmentController.hypothesis_readiness(
                    repair_plan
                ).requires_targeted_exploration
            ):
                validation_errors.append(
                    "unresolved_dependency_blocks_patch"
                )
                content = (
                    "[hypothesis quality] 当前 Hypothesis 仍有 unresolved "
                    "dependency 或 alternative explanation。先完成定向证据检查并"
                    "修订 Hypothesis，再 apply_patch。"
                )
            elif (
                capabilities.evidence_commitment
                and name == "apply_patch"
                and repair_plan is not None
                and not plan_stale
                and not EvidenceCommitmentController.patch_matches_plan(
                    args,
                    repair_plan,
                )
            ):
                validation_errors.append("patch_target_mismatch")
                content = (
                    "[commitment required] apply_patch 目标与当前 Repair Plan "
                    "不一致。请修订 Plan，或只修改计划中的文件。"
                )
            elif (
                capabilities.evidence_commitment
                and not capabilities.hypothesis_quality
                and name in EXPLORATION_TOOLS
                and isinstance(repair_plan, EvidenceRepairPlan)
                and not plan_stale
                and not repair_plan.hypothesis.unresolved_questions
                and not EvidenceCommitmentController.exploration_is_directed(
                    str(name),
                    args,
                    repair_plan,
                )
            ):
                validation_errors.append("broad_exploration_blocked")
                content = (
                    "[commitment required] 当前 Plan 没有 unresolved question。"
                    "禁止继续大范围探索；请执行计划中的 Patch，"
                    "或提供具体 unresolved_question 后修订 Plan。"
                )
            elif (
                capabilities.hypothesis_quality
                and name in EXPLORATION_TOOLS
                and isinstance(repair_plan, QualityEvidenceRepairPlan)
                and not plan_stale
                and EvidenceCommitmentController.hypothesis_readiness(
                    repair_plan
                ).requires_targeted_exploration
                and (
                    _argument_fingerprint(args)
                    in dependency_probe_fingerprints
                    or not EvidenceCommitmentController.dependency_probe_allowed(
                        str(name),
                        args,
                        repair_plan,
                        existing_fingerprints=set(
                            dependency_probe_fingerprints
                        ),
                        max_probes=4,
                    )
                )
            ):
                validation_errors.append("dependency_probe_blocked")
                content = (
                    "[hypothesis quality] 本次探索未对应新的 unresolved "
                    "dependency candidate，或已超过 4 次定向检查。"
                    "请修订 Hypothesis/Plan，不得重复同一个检查。"
                )
            elif (
                capabilities.hypothesis_quality
                and name in EXPLORATION_TOOLS
                and isinstance(repair_plan, QualityEvidenceRepairPlan)
                and not plan_stale
                and not EvidenceCommitmentController.hypothesis_readiness(
                    repair_plan
                ).requires_targeted_exploration
                and not EvidenceCommitmentController.exploration_is_directed(
                    str(name),
                    args,
                    repair_plan,
                )
            ):
                validation_errors.append("broad_exploration_blocked")
                content = (
                    "[hypothesis quality] Hypothesis 已 ready；"
                    "禁止继续大范围探索，请执行 Plan 中的 Patch。"
                )
            elif (
                capabilities.repair_loop
                and
                name == "apply_patch"
                and has_seen_test_failure
                and repair_rounds >= MAX_REPAIR_ROUNDS
            ):
                content = (
                    f"[tool error] 已达到最多 {MAX_REPAIR_ROUNDS} 轮自动修复上限，"
                    "禁止继续 apply_patch。请基于现有 diff 和测试结果给出最终说明。"
                )
            elif (
                capabilities.repair_planner
                and name == "apply_patch"
                and (repair_plan is None or plan_stale)
            ):
                content = (
                    "[planner required] apply_patch 前必须先调用 create_repair_plan；"
                    "Maven 失败后必须提交带 revision_reason 的修订计划。"
                )
            elif (
                capabilities.repair_planner
                and
                name == "create_repair_plan"
                and repair_plan is not None
                and not args.get("revision_reason")
            ):
                content = (
                    "[planner required] 更新已有 Repair Plan 时必须提供 "
                    "revision_reason，说明上一次失败和本轮调整。"
                )
            elif name == "run_tests" and patch_requested:
                content = (
                    "[auto test] 本批包含 apply_patch，显式 run_tests 已跳过；"
                    "Patch 成功后会自动运行 Maven test。"
                )
            else:
                try:
                    content = str(tool.invoke(args))
                    invocation_succeeded = True
                except Exception as exc:
                    logger.exception("Tool %s failed", name)
                    content = f"[tool error] 工具 {name} 执行失败：{exc}"

            if invocation_succeeded and name == "apply_patch":
                try:
                    patch_body = json.loads(content)
                    if patch_body.get("success") is True:
                        affected = patch_body.get("affected_files") or []
                        modified_files.extend(str(path) for path in affected)
                        patch_succeeded = True
                        latest_patch_diff = str(
                            patch_body.get("diff") or latest_patch_diff
                        )
                        if has_seen_test_failure:
                            repair_rounds += 1
                        plan_status_events.append(
                            PlanStatusEvent(
                                step=max(state["step_count"], 1),
                                status=PlanExecutionStatus.PATCH_APPLIED,
                                reason="successful apply_patch",
                            )
                        )
                        last_progress_event = (
                            f"step {max(state['step_count'], 1)}: apply_patch"
                        )
                    else:
                        invocation_succeeded = False
                except (TypeError, ValueError, json.JSONDecodeError):
                    invocation_succeeded = False
                    logger.warning("Could not parse apply_patch result: %s", content[:500])
            elif invocation_succeeded and name == "create_repair_plan":
                try:
                    request_class = (
                        CreateQualityEvidenceRepairPlanRequest
                        if capabilities.hypothesis_quality
                        else CreateEvidenceRepairPlanRequest
                        if capabilities.evidence_commitment
                        else CreateRepairPlanRequest
                    )
                    request = request_class.model_validate_json(content)
                    if isinstance(
                        request,
                        (
                            CreateEvidenceRepairPlanRequest,
                            CreateQualityEvidenceRepairPlanRequest,
                        ),
                    ):
                        plan_class = (
                            QualityEvidenceRepairPlan
                            if isinstance(
                                request,
                                CreateQualityEvidenceRepairPlanRequest,
                            )
                            else EvidenceRepairPlan
                        )
                        plan = plan_class.model_validate(
                            request.model_dump(exclude={"revision_reason"})
                        )
                        all_evidence_events = [
                            *state.get("tool_events", []),
                            *tool_events,
                        ]
                        needs_fresh_evidence = (
                            isinstance(repair_plan, QualityEvidenceRepairPlan)
                            and EvidenceCommitmentController.hypothesis_readiness(
                                repair_plan
                            ).requires_targeted_exploration
                            and last_quality_plan_step is not None
                        )
                        has_fresh_evidence = bool(
                            last_quality_plan_step is not None
                            and any(
                                event.step > last_quality_plan_step
                                and (
                                    event.tool_name == "read_file"
                                    or event.tool_name == "run_tests"
                                    or event.tool_name in {
                                        "find_symbol",
                                        "find_references",
                                        "find_callers",
                                        "find_callees",
                                        "analyze_impact",
                                    }
                                )
                                for event in all_evidence_events
                            )
                        )
                        if needs_fresh_evidence and not has_fresh_evidence:
                            validation_errors.append(
                                "quality revision lacks new observation"
                            )
                            invocation_succeeded = False
                            content = (
                                "[hypothesis quality] 修订 unresolved "
                                "Hypothesis 前必须获得新的源码、引用或测试观察。"
                            )
                            raise ValueError("missing fresh evidence")
                        evidence_errors = (
                            EvidenceCommitmentController.validate_quality_plan(
                                request,
                                all_evidence_events,
                                has_seen_test_failure=has_seen_test_failure,
                            )
                            if isinstance(
                                request,
                                CreateQualityEvidenceRepairPlanRequest,
                            )
                            else EvidenceCommitmentController.validate_evidence_plan(
                                request,
                                [
                                    *state.get("tool_events", []),
                                    *tool_events,
                                ],
                                has_seen_test_failure=has_seen_test_failure,
                            )
                        )
                        if evidence_errors:
                            validation_errors.extend(evidence_errors)
                            invocation_succeeded = False
                            content = (
                                "[commitment required] RepairHypothesis "
                                "validation failed: "
                                + "; ".join(evidence_errors[:5])
                            )
                            raise ValueError("evidence validation failed")
                    else:
                        plan = RepairPlan.model_validate(
                            request.model_dump(
                                exclude={"revision_reason"}
                            )
                        )
                    reason = request.revision_reason or "initial plan"
                    if repair_plan is not None:
                        plan_status_events.append(
                            PlanStatusEvent(
                                step=max(state["step_count"], 1),
                                status=PlanExecutionStatus.REVISED,
                                reason=reason,
                            )
                        )
                    else:
                        plan_status_events.append(
                            PlanStatusEvent(
                                step=max(state["step_count"], 1),
                                status=PlanExecutionStatus.PROPOSED,
                                reason=reason,
                            )
                        )
                    revision_number = (
                        len(state.get("plan_revisions", []))
                        + len(plan_revisions)
                        + 1
                    )
                    plan_revisions.append(
                        PlanRevision(
                            revision=revision_number,
                            reason=reason,
                            plan=plan,
                            modified_files=list(
                                dict.fromkeys(
                                    [
                                        *state.get("modified_files", []),
                                        *modified_files,
                                    ]
                                )
                            ),
                            failure_summary=(
                                last_failure_summary
                                if last_failure_summary
                                and (repair_plan is not None or plan_stale)
                                else None
                            ),
                            diff=str(state.get("latest_patch_diff", "")),
                        )
                    )
                    repair_plan = plan
                    plan_stale = False
                    if isinstance(plan, QualityEvidenceRepairPlan):
                        dependency_probe_fingerprints.clear()
                        last_quality_plan_step = max(
                            state["step_count"],
                            1,
                        )
                    plan_status_events.append(
                        PlanStatusEvent(
                            step=max(state["step_count"], 1),
                            status=PlanExecutionStatus.COMMITTED,
                            reason=reason,
                        )
                    )
                    last_progress_event = (
                        f"step {max(state['step_count'], 1)}: create_repair_plan"
                    )
                except ValueError:
                    logger.warning(
                        "Could not parse create_repair_plan result: %s",
                        content[:500],
                    )
            elif invocation_succeeded and name == "run_tests":
                try:
                    latest_test = TestRunResult.model_validate_json(content)
                    if repair_plan is not None:
                        plan_status_events.append(
                            PlanStatusEvent(
                                step=max(state["step_count"], 1),
                                status=PlanExecutionStatus.VERIFYING,
                                reason="explicit test run",
                            )
                        )
                    last_progress_event = (
                        f"step {max(state['step_count'], 1)}: run_tests"
                    )
                    if not latest_test.success:
                        has_seen_test_failure = True
                        plan_stale = True
                        last_failure_summary = _test_failure_summary(latest_test)
                        if repair_plan is not None:
                            plan_status_events.append(
                                PlanStatusEvent(
                                    step=max(state["step_count"], 1),
                                    status=PlanExecutionStatus.REJECTED,
                                    reason="explicit test failed",
                                )
                            )
                    else:
                        explicit_test_passed = True
                        if repair_plan is not None:
                            plan_status_events.append(
                                PlanStatusEvent(
                                    step=max(state["step_count"], 1),
                                    status=PlanExecutionStatus.COMPLETED,
                                    reason="explicit test passed",
                                )
                            )
                except ValueError:
                    logger.warning("Could not parse run_tests result: %s", content[:500])
            if invocation_succeeded and name in EXPLORATION_TOOLS:
                readiness = (
                    EvidenceCommitmentController.hypothesis_readiness(
                        repair_plan
                    )
                    if isinstance(repair_plan, QualityEvidenceRepairPlan)
                    else None
                )
                if readiness and readiness.requires_targeted_exploration:
                    targeted_exploration_calls += 1
                    dependency_probe_fingerprints.append(
                        _argument_fingerprint(args)
                    )
                    dependency_probes.append(
                        DependencyProbeEvent(
                            step=max(state["step_count"], 1),
                            question=(
                                readiness.unresolved_dependencies[0]
                                if readiness.unresolved_dependencies
                                else readiness.alternatives[0]
                            ),
                            tool_name=str(name),
                            target=_target_file(str(name), args),
                            query=_tool_query(str(name), args),
                        )
                    )
                elif name in BROAD_EXPLORATION_TOOLS:
                    broad_exploration_calls += 1
            tool_invoked = (
                name == "apply_patch"
                and bool(content)
                and not content.startswith(
                    ("[tool error]", "[planner required]", "[auto test]")
                )
            ) or (name != "apply_patch" and invocation_succeeded)
            tool_messages.append(ToolMessage(content=content, tool_call_id=call_id))
            tool_events.append(
                _tool_event(
                    index=(
                        len(state.get("tool_events", []))
                        + len(tool_events)
                        + 1
                    ),
                    step=max(state["step_count"], 1),
                    repair_round=repair_round_before_call,
                    tool_name=str(name or "unknown"),
                    call_id=str(call_id) if call_id else None,
                    arguments=args,
                    content=content,
                    success=invocation_succeeded,
                    invoked=tool_invoked,
                    patch_applied=(
                        True
                        if name == "apply_patch" and invocation_succeeded
                        else (
                            False
                            if name == "apply_patch" and tool_invoked
                            else None
                        )
                    ),
                    elapsed_seconds=(
                        time.monotonic()
                        - state.get("started_monotonic", time.monotonic())
                    ),
                )
            )

        final_answer = state.get("final_answer", "")
        current_step = "tools"
        run_tests_tool = tools_by_name.get("run_tests")
        if (
            capabilities.repair_loop
            and patch_succeeded
            and run_tests_tool is not None
        ):
            auto_call_id = f"auto_test_{uuid.uuid4().hex[:12]}"
            auto_call = {
                "name": "run_tests",
                "args": {"tool": "maven"},
                "id": auto_call_id,
            }
            tool_messages.append(
                AIMessage(content="", tool_calls=[auto_call])
            )
            plan_status_events.append(
                PlanStatusEvent(
                    step=max(state["step_count"], 1),
                    status=PlanExecutionStatus.VERIFYING,
                    reason="automatic test after patch",
                )
            )
            try:
                auto_content = str(run_tests_tool.invoke({"tool": "maven"}))
                latest_test = TestRunResult.model_validate_json(auto_content)
                last_progress_event = (
                    f"step {state['step_count']}: run_tests"
                )
                if not latest_test.success:
                    has_seen_test_failure = True
                    plan_stale = True
                    last_failure_summary = _test_failure_summary(latest_test)
                    plan_status_events.append(
                        PlanStatusEvent(
                            step=state["step_count"],
                            status=PlanExecutionStatus.REJECTED,
                            reason="automatic test failed",
                        )
                    )
                else:
                    plan_status_events.append(
                        PlanStatusEvent(
                            step=state["step_count"],
                            status=PlanExecutionStatus.COMPLETED,
                            reason="automatic test passed",
                        )
                    )
            except Exception as exc:
                logger.exception("Automatic Maven test failed to execute")
                auto_content = f"[tool error] 自动 Maven 测试执行失败：{exc}"
            tool_messages.append(
                ToolMessage(content=auto_content, tool_call_id=auto_call_id)
            )
            tool_events.append(
                _tool_event(
                    index=(
                        len(state.get("tool_events", []))
                        + len(tool_events)
                        + 1
                    ),
                    step=max(state["step_count"], 1),
                    repair_round=repair_rounds,
                    tool_name="run_tests",
                    call_id=auto_call_id,
                    arguments={"tool": "maven"},
                    content=auto_content,
                    success=bool(
                        latest_test is not None and latest_test.success
                    ),
                    invoked=True,
                    auto_generated=True,
                    elapsed_seconds=(
                        time.monotonic()
                        - state.get("started_monotonic", time.monotonic())
                    ),
                )
            )

            if latest_test is not None and latest_test.success:
                current_step = "finished"
                final_answer = "Maven 测试通过，自动修复闭环已结束。"
                termination_reason = "success"
            elif repair_rounds >= MAX_REPAIR_ROUNDS:
                current_step = "finished"
                final_answer = (
                    f"已达到最多 {MAX_REPAIR_ROUNDS} 轮自动修复，"
                    "Maven 测试仍未通过。"
                )
                termination_reason = "agent_end"
            else:
                termination_reason = None
        elif explicit_test_passed:
            current_step = "finished"
            final_answer = "Maven 测试通过，自动修复闭环已结束。"
            termination_reason = "success"
        else:
            termination_reason = None

        evidence_prompt_sent = state.get("evidence_prompt_sent", False)
        if (
            capabilities.evidence_commitment
            and not evidence_prompt_sent
            and repair_plan is None
        ):
            all_events = [
                *state.get("tool_events", []),
                *tool_events,
            ]
            readiness = EvidenceCommitmentController.readiness(
                all_events,
                has_seen_test_failure=has_seen_test_failure,
                current_step=state["step_count"],
            )
            if readiness.sufficient:
                prompt_content = (
                    EvidenceCommitmentController.evidence_prompt(readiness)
                )
                if capabilities.hypothesis_quality:
                    prompt_content += (
                        " Explain the causal mechanism and a testable prediction. "
                        "If callers, implementations, inheritance, cross-file "
                        "contracts, or competing explanations remain uncertain, "
                        "record structured unresolved dependencies instead of "
                        "pretending the hypothesis is complete."
                    )
                tool_messages.append(
                    HumanMessage(
                        content=prompt_content
                    )
                )
                evidence_prompt_sent = True
                evidence_sufficient_step = state["step_count"]

        logger.info(
            "Executed %d message(s) (step %d, auto_test=%s)",
            len(tool_messages),
            state["step_count"],
            patch_succeeded,
        )
        return {
            "messages": tool_messages,
            "current_step": current_step,
            "modified_files": modified_files,
            "test_result": latest_test,
            "has_seen_test_failure": has_seen_test_failure,
            "repair_rounds": repair_rounds,
            "final_answer": final_answer,
            "repair_plan": repair_plan,
            "plan_revisions": plan_revisions,
            "plan_stale": plan_stale,
            "latest_patch_diff": latest_patch_diff,
            "last_failure_summary": last_failure_summary,
            "tool_events": tool_events,
            "last_progress_event": last_progress_event,
            "termination_reason": termination_reason,
            "plan_status_events": plan_status_events,
            "evidence_prompt_sent": evidence_prompt_sent,
            "evidence_sufficient_step": evidence_sufficient_step,
            "commitment_validation_errors": validation_errors,
            "dependency_probes": dependency_probes,
            "dependency_probe_fingerprints": dependency_probe_fingerprints,
            "last_quality_plan_step": last_quality_plan_step,
            "targeted_exploration_calls": targeted_exploration_calls,
            "broad_exploration_calls": broad_exploration_calls,
        }

    def route_after_agent(state: AgentState) -> Literal["tools", "end"]:
        if state.get("current_step") in ("error", "finished"):
            return "end"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return "end"

    def route_after_tools(state: AgentState) -> Literal["agent", "end"]:
        return "end" if state.get("current_step") == "finished" else "agent"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        {"agent": "agent", "end": END},
    )
    return builder.compile()


async def run_agent(
    task: str,
    repository_path: str | Path,
    model: BaseChatModel | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    run_timeout_seconds: float | None = DEFAULT_RUN_TIMEOUT_SECONDS,
    workspace_id: str | None = None,
    workspace_manager=None,
    profile: AgentProfile | str = DEFAULT_AGENT_PROFILE,
    context_lifecycle_compaction: bool = False,
    test_runner: Callable | None = None,
    step_budget_policy: str | StepBudgetPolicy | None = None,
    evidence_commitment: bool = False,
    hypothesis_quality: bool = False,
) -> AnalyzeResponse:
    """Run the agent loop for a task against a local repository.

    In workspace mode the Agent can make atomic patches and execute the fixed
    Maven/Gradle test lifecycle. The LLM is injected (tests pass a fake); when
    omitted it is built from settings, which requires LLM_* in .env.
    """
    resolved_profile = resolve_profile(profile)
    capabilities = replace(
        capabilities_for(resolved_profile),
        evidence_commitment=evidence_commitment or hypothesis_quality,
        hypothesis_quality=hypothesis_quality,
    )
    budget_policy = (
        step_budget_policy
        if isinstance(step_budget_policy, StepBudgetPolicy)
        else create_step_budget_policy(
            step_budget_policy or production_step_budget_name(max_steps),
            legacy_max_steps=max_steps,
        )
    )
    model = model or build_chat_model()
    tools = create_repo_tools(repository_path)
    if workspace_id:
        # All mutating and execution tools are bound to this workspace only.
        if capabilities.code_intelligence:
            tools += create_code_intel_tools(
                workspace_id,
                repository_path,
                manager=workspace_manager,
            )
        tools += create_git_tools(workspace_id, repository_path, manager=workspace_manager)
        if capabilities.repair_planner:
            tools += create_repair_plan_tools(
                workspace_id,
                repository_path,
                manager=workspace_manager,
                require_evidence=capabilities.evidence_commitment,
                hypothesis_quality=capabilities.hypothesis_quality,
            )
        tools += create_patch_tools(
            workspace_id,
            repository_path,
            manager=workspace_manager,
        )
        tools += create_build_tools(
            workspace_id,
            repository_path,
            manager=workspace_manager,
            timeout_seconds=settings.BUILD_TEST_TIMEOUT_SECONDS,
            test_runner=test_runner,
        )
    system_prompt = build_system_prompt(capabilities)
    graph = build_agent_graph(
        model,
        tools,
        max_steps=max_steps,
        capabilities=capabilities,
        profile=resolved_profile,
        context_lifecycle_compaction=context_lifecycle_compaction,
        step_budget_policy=budget_policy,
    )

    initial_state: AgentState = {
        "task": task,
        "messages": [SystemMessage(content=system_prompt), HumanMessage(content=task)],
        "current_step": "init",
        "step_count": 0,
        "final_answer": "",
        "modified_files": [],
        "test_result": None,
        "has_seen_test_failure": False,
        "repair_rounds": 0,
        "repair_plan": None,
        "plan_revisions": [],
        "plan_stale": True,
        "latest_patch_diff": "",
        "last_failure_summary": None,
        "tool_events": [],
        "started_monotonic": time.monotonic(),
        "effective_step_limit": budget_policy.base_step_limit,
        "budget_extensions": [],
        "termination_reason": None,
        "last_progress_event": None,
        "plan_status_events": [],
        "evidence_prompt_sent": False,
        "evidence_sufficient_step": None,
        "commitment_validation_errors": [],
        "dependency_probes": [],
        "dependency_probe_fingerprints": [],
        "last_quality_plan_step": None,
        "targeted_exploration_calls": 0,
        "broad_exploration_calls": 0,
    }

    logger.info(
        "Agent run started (repo=%s, step_budget=%s, profile=%s)",
        repository_path,
        budget_policy.name,
        resolved_profile.value,
    )
    try:
        if run_timeout_seconds is None:
            result = await graph.ainvoke(initial_state)
        else:
            result = await asyncio.wait_for(graph.ainvoke(initial_state), timeout=run_timeout_seconds)
    except TimeoutError:
        logger.error("Agent run timed out after %ss", run_timeout_seconds)
        return AnalyzeResponse(answer=f"Agent 执行超时（>{run_timeout_seconds:.0f}s），已安全停止。", steps=0)
    except Exception as exc:
        logger.exception("Agent run crashed")
        return AnalyzeResponse(answer=f"Agent 执行异常：{exc}", steps=0)

    modified_files = list(dict.fromkeys(result.get("modified_files", [])))
    (
        tool_calls,
        last_agent_action,
        tool_usage,
        tool_failure_counts,
        empty_patch_calls,
        auto_maven_test_runs,
    ) = _execution_metrics(result.get("messages", []))
    tool_events = list(result.get("tool_events", []))
    patch_metrics = _patch_telemetry(tool_events)
    ci_metrics = _ci_telemetry(tool_events)
    latest_test = result.get("test_result")
    repair_plan = result.get("repair_plan")
    plan_revisions = result.get("plan_revisions", [])
    budget_telemetry = _step_budget_telemetry(budget_policy, result)
    commitment_telemetry = EvidenceCommitmentController.build_telemetry(
        evidence_commitment=capabilities.evidence_commitment,
        hypothesis_quality=capabilities.hypothesis_quality,
        tool_events=tool_events,
        plan_revisions=plan_revisions,
        plan_status_history=list(result.get("plan_status_events", [])),
        evidence_sufficient_step=result.get("evidence_sufficient_step"),
        final_test_passed=bool(latest_test and latest_test.success),
        has_seen_test_failure=bool(
            result.get("has_seen_test_failure", False)
        ),
        validation_errors=list(
            result.get("commitment_validation_errors", [])
        ),
        targeted_exploration_calls=result.get(
            "targeted_exploration_calls",
            0,
        ),
        broad_exploration_calls=result.get(
            "broad_exploration_calls",
            0,
        ),
        dependency_probes=list(result.get("dependency_probes", [])),
        evidence_provenance_valid=not any(
            "not been observed" in error
            or "source changed after observation" in error
            or "validation failed" in error
            for error in result.get("commitment_validation_errors", [])
        ),
    )
    final_diff = ""
    if workspace_id and modified_files:
        try:
            final_diff = git.diff(Path(repository_path))
        except git.GitError as exc:
            final_diff = f"[无法获取 git diff: {exc}]"
        if len(final_diff) > MAX_PATCH_DIFF_CHARS:
            omitted = len(final_diff) - MAX_PATCH_DIFF_CHARS
            final_diff = (
                final_diff[:MAX_PATCH_DIFF_CHARS]
                + f"\n...[git diff 已截断，省略 {omitted} 个字符]"
            )

    final_answer = result["final_answer"]
    if workspace_id and (modified_files or latest_test or repair_plan):
        report: list[str] = [final_answer, "", "### 修改摘要"]
        report.extend(f"- {path}" for path in modified_files)
        if not modified_files:
            report.append("- 无文件修改")
        report.extend(["", "### 测试结果"])
        if latest_test is None:
            report.append("- 未运行测试")
        else:
            report.append("- 状态：" + ("通过" if latest_test.success else "失败"))
            report.append(f"- 退出码：{latest_test.exit_code}")
            report.append(f"- 耗时：{latest_test.duration_seconds}s")
            if latest_test.error:
                report.append(f"- 错误：{latest_test.error}")
        if final_diff:
            report.extend(["", "### 最终 Diff", "```diff", final_diff, "```"])
        if repair_plan is not None:
            report.extend(["", "### Repair Plan", repair_plan.problem_summary])
            report.extend(
                f"- {change.file_path}: {change.change_description}"
                for change in repair_plan.planned_changes
            )
            report.append(
                "- 验证：" + "; ".join(repair_plan.validation_plan.focus)
            )
        if plan_revisions:
            report.extend(["", "### Plan Revisions"])
            report.extend(
                f"- Revision {revision.revision}: {revision.reason}"
                for revision in plan_revisions
            )
        final_answer = "\n".join(report)

    logger.info(
        "Agent run finished (steps=%d, step=%s, modified=%d, repairs=%d)",
        result["step_count"],
        result["current_step"],
        len(modified_files),
        result.get("repair_rounds", 0),
    )
    return AnalyzeResponse(
        answer=final_answer,
        steps=result["step_count"],
        tool_calls=tool_calls,
        last_agent_action=last_agent_action,
        tool_usage=tool_usage,
        tool_failure_counts=tool_failure_counts,
        empty_patch_calls=empty_patch_calls,
        auto_maven_test_runs=auto_maven_test_runs,
        tool_events=tool_events,
        tool_output_chars=sum(event.output_chars for event in tool_events),
        **patch_metrics,
        ci_telemetry=ci_metrics["per_tool"],
        total_ci_output_chars=ci_metrics["total_ci_output_chars"],
        read_file_calls=ci_metrics["read_file_calls"],
        follow_up_source_reads=ci_metrics["follow_up_source_reads"],
        modified_files=modified_files,
        repair_rounds=result.get("repair_rounds", 0),
        test_result=latest_test,
        diff=final_diff,
        repair_plan=repair_plan,
        plan_revisions=plan_revisions,
        budget_telemetry=budget_telemetry,
        commitment_telemetry=commitment_telemetry,
    )
