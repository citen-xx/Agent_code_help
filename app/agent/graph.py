"""Minimal LangGraph agent loop.

Pipeline::

    START -> agent -> [has tool calls?]
              ^            |
              |      yes   v
              +------- tools (execute, feed results back to the LLM)
              |            |
              +---- no ----+--> END (final answer)

The agent calls the LLM (with the read-only repo tools bound); tool results
are appended to ``messages`` and the loop repeats until the LLM answers
without tool calls, an error occurs, or the maximum step count is reached.

Only hand-written nodes and edges are used — no high-level agent APIs.
"""

import asyncio
import logging
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

from app.agent.state import AgentState
from app.llm.client import build_chat_model
from app.schemas.agent import AnalyzeResponse
from app.tools.git_tools import create_git_tools
from app.tools.registry import create_repo_tools

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 10
#: Safety cap for a whole agent run (single LLM calls have their own timeout).
DEFAULT_RUN_TIMEOUT_SECONDS = 300.0

SYSTEM_PROMPT = (
    "You are a Java code analysis assistant. You can inspect a Java repository "
    "with read-only tools: list_files, read_file, search_code, and (for a "
    "workspace) git_status, git_diff, git_log, git_show. "
    "Never modify or delete files, and never run shell commands. "
    "Explore step by step, then give a concise final answer once you have "
    "enough information. Answer in the same language as the user's task."
)


def build_agent_graph(model: BaseChatModel, tools: list[BaseTool], max_steps: int = DEFAULT_MAX_STEPS):
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
            response = llm.invoke(state["messages"])
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
        tool_messages: list[ToolMessage] = []
        for call in getattr(last, "tool_calls", None) or []:
            name = call.get("name")
            args = call.get("args") or {}
            call_id = call.get("id")
            tool = tools_by_name.get(name)
            if tool is None:
                content = f"[tool error] 未知工具：{name}（可用工具：{', '.join(sorted(tools_by_name))}）"
            else:
                try:
                    content = str(tool.invoke(args))
                except Exception as exc:
                    logger.exception("Tool %s failed", name)
                    content = f"[tool error] 工具 {name} 执行失败：{exc}"
            tool_messages.append(ToolMessage(content=content, tool_call_id=call_id))
        logger.info("Executed %d tool call(s) (step %d)", len(tool_messages), state["step_count"])
        return {"messages": tool_messages, "current_step": "tools"}

    def route_after_agent(state: AgentState) -> Literal["tools", "end"]:
        if state.get("current_step") in ("error", "finished"):
            return "end"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return "end"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
    builder.add_edge("tools", "agent")
    return builder.compile()


async def run_agent(
    task: str,
    repository_path: str | Path,
    model: BaseChatModel | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    run_timeout_seconds: float | None = DEFAULT_RUN_TIMEOUT_SECONDS,
    workspace_id: str | None = None,
    workspace_manager=None,
) -> AnalyzeResponse:
    """Run the agent loop for a task against a local repository (read-only).

    The LLM is injected (tests pass a fake); when omitted it is built from
    settings, which requires LLM_* to be configured in .env.
    """
    model = model or build_chat_model()
    tools = create_repo_tools(repository_path)
    if workspace_id:
        # read-only Git tools, bound to this workspace's repository only
        tools += create_git_tools(workspace_id, repository_path, manager=workspace_manager)
    graph = build_agent_graph(model, tools, max_steps=max_steps)

    initial_state: AgentState = {
        "task": task,
        "messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=task)],
        "current_step": "init",
        "step_count": 0,
        "final_answer": "",
    }

    logger.info("Agent run started (repo=%s, max_steps=%d)", repository_path, max_steps)
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

    logger.info("Agent run finished (steps=%d, step=%s)", result["step_count"], result["current_step"])
    return AnalyzeResponse(answer=result["final_answer"], steps=result["step_count"])
