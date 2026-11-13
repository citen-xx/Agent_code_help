"""Agent loop tests — LLM is always faked, no real API access."""

import time

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.graph import run_agent
from tests.fakes import FakeChatModel, tool_call


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    return tmp_path


async def test_tool_roundtrip_and_final_answer(repo):
    model = FakeChatModel(script=[
        tool_call("list_files", {"path": "."}, "call_1"),
        AIMessage(content="已找到 Main.java，项目结构很简单。"),
    ])
    result = await run_agent("分析这个 Java 项目", repo, model=model)
    assert result.answer == "已找到 Main.java，项目结构很简单。"
    assert result.steps == 2
    assert result.budget_telemetry is not None
    assert result.budget_telemetry.policy == "adaptive_20_30"
    # the tool result was fed back into messages for the second LLM call
    tool_messages = [m for m in model.history[1] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert "Main.java" in tool_messages[0].content


async def test_unknown_tool_is_fed_back(repo):
    model = FakeChatModel(script=[
        tool_call("delete_file", {"path": "Main.java"}, "call_1"),
        AIMessage(content="该工具不存在，我将基于已有信息回答。"),
    ])
    result = await run_agent("分析项目", repo, model=model)
    assert result.steps == 2
    tool_msg = [m for m in model.history[1] if isinstance(m, ToolMessage)][0]
    assert "[tool error]" in tool_msg.content
    assert "delete_file" in tool_msg.content


async def test_tool_exception_is_fed_back(repo):
    model = FakeChatModel(script=[
        tool_call("read_file", {"path": "Missing.java"}, "call_1"),
        AIMessage(content="文件不存在，无法继续分析。"),
    ])
    result = await run_agent("分析项目", repo, model=model)
    assert result.steps == 2
    tool_msg = [m for m in model.history[1] if isinstance(m, ToolMessage)][0]
    assert "[tool error]" in tool_msg.content
    assert "Missing.java" in tool_msg.content


async def test_legacy_repository_mode_has_no_patch_tool(repo):
    original = (repo / "Main.java").read_text(encoding="utf-8")
    model = FakeChatModel(script=[
        tool_call(
            "apply_patch",
            {
                "files": [
                    {
                        "path": "Main.java",
                        "replacements": [
                            {
                                "old_text": "public class Main {}",
                                "new_text": "public class Main { int changed; }",
                            }
                        ],
                    }
                ]
            },
            "call_patch",
        ),
        AIMessage(content="仓库路径模式不允许修改。"),
    ])
    result = await run_agent("修改项目", repo, model=model)
    assert result.steps == 2
    tool_msg = [m for m in model.history[1] if isinstance(m, ToolMessage)][0]
    assert "未知工具" in tool_msg.content
    assert (repo / "Main.java").read_text(encoding="utf-8") == original


async def test_max_steps_terminates_safely(repo):
    model = FakeChatModel(script=[tool_call("list_files", {"path": "."}, f"call_{i}") for i in range(20)])
    result = await run_agent("分析项目", repo, model=model, max_steps=3)
    assert result.steps == 3
    assert "最大步骤" in result.answer
    assert model.calls == 3  # never exceeded the limit


async def test_adaptive_budget_does_not_extend_pure_exploration(repo):
    model = FakeChatModel(
        script=[
            tool_call("list_files", {"path": "."}, f"call_{i}")
            for i in range(25)
        ]
    )
    result = await run_agent(
        "分析项目",
        repo,
        model=model,
        step_budget_policy="adaptive_20_30",
    )
    assert result.steps == 20
    assert model.calls == 20
    assert result.budget_telemetry is not None
    assert result.budget_telemetry.extensions_granted == 0
    assert (
        result.budget_telemetry.termination_reason
        == "base_step_limit_no_progress"
    )


async def test_fixed_30_budget_uses_same_graph(repo):
    model = FakeChatModel(
        script=[
            tool_call("list_files", {"path": "."}, f"call_{i}")
            for i in range(31)
        ]
    )
    result = await run_agent(
        "分析项目",
        repo,
        model=model,
        step_budget_policy="fixed_30",
    )
    assert result.steps == 30
    assert model.calls == 30
    assert result.budget_telemetry is not None
    assert result.budget_telemetry.policy == "fixed_30"
    assert result.budget_telemetry.termination_reason == "max_30"


async def test_llm_exception_terminates_safely(repo):
    model = FakeChatModel(script=[], error=RuntimeError("connection reset"))
    result = await run_agent("分析项目", repo, model=model)
    assert result.steps == 1
    assert "LLM 调用失败" in result.answer


async def test_llm_timeout_terminates_safely(repo):
    model = FakeChatModel(script=[], error=TimeoutError("request timed out"))
    result = await run_agent("分析项目", repo, model=model)
    assert result.steps == 1
    assert "LLM 调用失败" in result.answer


class SlowChatModel(FakeChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(0.2)
        return super()._generate(messages, stop, run_manager, **kwargs)


async def test_run_timeout_terminates_safely(repo):
    model = SlowChatModel(script=[AIMessage(content="answer")])
    result = await run_agent("分析项目", repo, model=model, run_timeout_seconds=0.05)
    assert "超时" in result.answer
    assert result.steps == 0
