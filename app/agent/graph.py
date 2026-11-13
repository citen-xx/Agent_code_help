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
from app.agent.context_lifecycle import compact_model_messages
from app.agent.state import AgentState
from app.agent.telemetry import PatchErrorCategory, ToolEvent
from app.code_intel.tools import create_code_intel_tools
from app.core.config import settings
from app.llm.client import build_chat_model
from app.repair.models import CreateRepairPlanRequest, PlanRevision, RepairPlan
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


def build_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    max_steps: int = DEFAULT_MAX_STEPS,
    capabilities: AgentCapabilities = FULL_CAPABILITIES,
    profile: AgentProfile = DEFAULT_AGENT_PROFILE,
    context_lifecycle_compaction: bool = False,
):
    """Build the compiled agent graph: LLM <-> tools until a final answer."""
    tools_by_name = {tool.name: tool for tool in tools}

    def agent_node(state: AgentState) -> dict:
        """Call the LLM with bound tools; update step counter / final answer."""
        if state["step_count"] >= max_steps:
            logger.warning("Max steps (%d) reached — terminating safely", max_steps)
            return {
                "current_step": "finished",
                "final_answer": (
                    f"已达到最大步骤数（{max_steps}），Agent 已安全停止。"
                    "如需继续分析，请缩小任务范围后重试。"
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
                "current_step": "error",
                "step_count": state["step_count"] + 1,
                "final_answer": f"LLM 调用失败（可能为超时或网络错误），Agent 已停止：{exc}",
            }
        content = response.content if isinstance(response.content, str) else ""
        update: dict = {
            "messages": [response],
            "current_step": "thinking",
            "step_count": state["step_count"] + 1,
        }
        if not getattr(response, "tool_calls", None):
            update["current_step"] = "finished"
            update["final_answer"] = content or "（模型未返回内容）"
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
                    else:
                        invocation_succeeded = False
                except (TypeError, ValueError, json.JSONDecodeError):
                    invocation_succeeded = False
                    logger.warning("Could not parse apply_patch result: %s", content[:500])
            elif invocation_succeeded and name == "create_repair_plan":
                try:
                    request = CreateRepairPlanRequest.model_validate_json(content)
                    plan = RepairPlan.model_validate(
                        request.model_dump(exclude={"revision_reason"})
                    )
                    reason = request.revision_reason or "initial plan"
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
                except ValueError:
                    logger.warning(
                        "Could not parse create_repair_plan result: %s",
                        content[:500],
                    )
            elif invocation_succeeded and name == "run_tests":
                try:
                    latest_test = TestRunResult.model_validate_json(content)
                    if not latest_test.success:
                        has_seen_test_failure = True
                        plan_stale = True
                        last_failure_summary = _test_failure_summary(latest_test)
                    else:
                        explicit_test_passed = True
                except ValueError:
                    logger.warning("Could not parse run_tests result: %s", content[:500])
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
            try:
                auto_content = str(run_tests_tool.invoke({"tool": "maven"}))
                latest_test = TestRunResult.model_validate_json(auto_content)
                if not latest_test.success:
                    has_seen_test_failure = True
                    plan_stale = True
                    last_failure_summary = _test_failure_summary(latest_test)
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
            elif repair_rounds >= MAX_REPAIR_ROUNDS:
                current_step = "finished"
                final_answer = (
                    f"已达到最多 {MAX_REPAIR_ROUNDS} 轮自动修复，"
                    "Maven 测试仍未通过。"
                )
        elif explicit_test_passed:
            current_step = "finished"
            final_answer = "Maven 测试通过，自动修复闭环已结束。"

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
) -> AnalyzeResponse:
    """Run the agent loop for a task against a local repository.

    In workspace mode the Agent can make atomic patches and execute the fixed
    Maven/Gradle test lifecycle. The LLM is injected (tests pass a fake); when
    omitted it is built from settings, which requires LLM_* in .env.
    """
    resolved_profile = resolve_profile(profile)
    capabilities = capabilities_for(resolved_profile)
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
    }

    logger.info(
        "Agent run started (repo=%s, max_steps=%d, profile=%s)",
        repository_path,
        max_steps,
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
    )
