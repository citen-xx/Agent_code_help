"""Repair Planner enforcement and fallback tests."""

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.graph import run_agent
from app.repair.tools import create_repair_plan_tools
from app.tools.repo import ToolError
from app.workspace.manager import WorkspaceManager, WorkspaceNotFoundError
from tests.fakes import FakeChatModel, tool_call


def _write(repo: Path, relative: str, source: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.fixture
def workspace(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    _write(repo, "pom.xml", "<project/>")
    _write(repo, "Main.java", "public class Main { // step0 }\n")
    _write(
        repo,
        "src/test/java/MainTest.java",
        "public class MainTest {}\n",
    )
    return info


def _plan_args(
    impacted_file: str = "Main.java",
    impacted_test: str = "src/test/java/MainTest.java",
    revision_reason: str | None = None,
) -> dict:
    return {
        "problem_summary": "修复 Main 缺陷",
        "target_symbols": ["Main"],
        "impacted_files": [impacted_file],
        "impacted_tests": [impacted_test],
        "planned_changes": [
            {
                "file_path": impacted_file,
                "target_symbol": "Main",
                "change_description": "修正 Main 的核心逻辑",
                "reason": "恢复预期行为",
            }
        ],
        "risk_notes": ["只修改 Main.java"],
        "validation_plan": {
            "run_maven_test": True,
            "focus": ["MainTest", "Main 的调用方"],
            "notes": ["确认 Docker Maven test 通过"],
        },
        "revision_reason": revision_reason,
    }


def _patch_call() -> AIMessage:
    return tool_call(
        "apply_patch",
        {
            "changes": [
                {
                    "path": "Main.java",
                    "old_text": "// step0",
                    "new_text": "// step1",
                }
            ]
        },
        "patch_without_plan",
    )


async def test_first_patch_is_blocked_without_plan(workspace, manager):
    model = FakeChatModel(
        script=[_patch_call(), AIMessage(content="需要先生成修复计划。")]
    )
    result = await run_agent(
        "修改 Main",
        workspace.repository_path,
        model=model,
        workspace_id=workspace.workspace_id,
        workspace_manager=manager,
    )
    assert result.modified_files == []
    assert result.repair_plan is None
    assert result.plan_revisions == []
    text = (Path(workspace.repository_path) / "Main.java").read_text(encoding="utf-8")
    assert "// step0" in text
    feedback = "\n".join(
        message.content
        for message in model.history[1]
        if isinstance(message, ToolMessage)
    )
    assert "planner required" in feedback


def test_plan_tool_is_workspace_scoped(manager, origin_repo):
    first = manager.create(origin_repo.as_uri())
    second = manager.create(origin_repo.as_uri())
    repo_second = Path(second.repository_path)
    _write(repo_second, "OnlySecond.java", "class OnlySecond {}\n")
    first_tools = create_repair_plan_tools(
        first.workspace_id,
        first.repository_path,
        manager=manager,
    )
    second_tools = create_repair_plan_tools(
        second.workspace_id,
        second.repository_path,
        manager=manager,
    )
    with pytest.raises(ToolError, match="不存在|失败"):
        first_tools[0].invoke(
            _plan_args(impacted_file="OnlySecond.java", impacted_test="")
        )
    body = json.loads(second_tools[0].invoke(_plan_args(impacted_file="OnlySecond.java")))
    assert body["impacted_files"] == ["OnlySecond.java"]

    with pytest.raises(WorkspaceNotFoundError):
        create_repair_plan_tools(
            "abcdef012345",
            first.repository_path,
            manager=manager,
        )


async def test_planner_can_fallback_to_search_and_read(workspace, manager):
    model = FakeChatModel(
        script=[
            tool_call("find_symbol", {"name": "Main"}, "find_main"),
            tool_call("analyze_impact", {"name": "Main", "depth": 1}, "impact_main"),
            tool_call("search_code", {"query": "Main", "file_pattern": "*.java"}, "search_main"),
            tool_call("read_file", {"path": "Main.java"}, "read_main"),
            tool_call("create_repair_plan", _plan_args(), "plan_main"),
            AIMessage(content="计划已生成。"),
        ]
    )
    result = await run_agent(
        "分析并规划 Main 修复",
        workspace.repository_path,
        model=model,
        workspace_id=workspace.workspace_id,
        workspace_manager=manager,
    )
    assert result.repair_plan is not None
    assert result.repair_plan.impacted_files == ["Main.java"]
    assert result.repair_plan.impacted_tests == ["src/test/java/MainTest.java"]
    assert result.repair_plan.validation_plan.run_maven_test is True
    assert result.plan_revisions[0].revision == 1
    tool_text = "\n".join(
        message.content
        for history in model.history
        for message in history
        if isinstance(message, ToolMessage)
    )
    assert "public class Main" in tool_text
    assert "MainTest" in tool_text
    assert model.calls == 6


async def test_repository_path_mode_has_no_repair_planner(tmp_path):
    (tmp_path / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    model = FakeChatModel(
        script=[
            tool_call(
                "create_repair_plan",
                {
                    **_plan_args(impacted_test=""),
                    "impacted_files": ["Main.java"],
                },
                "legacy_plan",
            ),
            AIMessage(content="只读模式不启用修复计划。"),
        ]
    )
    result = await run_agent("分析 Main", tmp_path, model=model)
    assert result.repair_plan is None
    assert result.plan_revisions == []
    feedback = "\n".join(
        message.content
        for message in model.history[1]
        if isinstance(message, ToolMessage)
    )
    assert "未知工具" in feedback
