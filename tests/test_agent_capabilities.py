"""Capability profile, prompt, tool exposure, and workflow ablation tests."""

from pathlib import Path

from langchain_core.messages import AIMessage

from app.agent.capabilities import (
    AgentProfile,
    PROFILE_CAPABILITIES,
    capabilities_for,
)
from app.agent.graph import SYSTEM_PROMPT, build_system_prompt, run_agent
from tests.fakes import FakeChatModel, tool_call


CODE_INTELLIGENCE_TOOLS = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
}
BASIC_TOOLS = {
    "list_files",
    "read_file",
    "search_code",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "apply_patch",
    "detect_build_tools",
    "run_tests",
}


def test_profile_capability_ladder():
    assert capabilities_for(AgentProfile.BASIC).as_dict() == {
        "code_intelligence": False,
        "repair_planner": False,
        "repair_loop": False,
        "evidence_commitment": False,
    }
    assert capabilities_for(AgentProfile.CODE_INTELLIGENCE).as_dict() == {
        "code_intelligence": True,
        "repair_planner": False,
        "repair_loop": False,
        "evidence_commitment": False,
    }
    assert capabilities_for(AgentProfile.PLANNER).as_dict() == {
        "code_intelligence": True,
        "repair_planner": True,
        "repair_loop": False,
        "evidence_commitment": False,
    }
    assert capabilities_for(AgentProfile.FULL).as_dict() == {
        "code_intelligence": True,
        "repair_planner": True,
        "repair_loop": True,
        "evidence_commitment": False,
    }
    assert set(PROFILE_CAPABILITIES) == set(AgentProfile)


def test_full_prompt_is_unchanged_and_variants_only_remove_fragments():
    expected_full_prompt = (
        "You are a Java coding assistant working inside one isolated workspace. "
        "Prefer Java symbol tools (list_symbols, find_symbol, find_references) to "
        "locate declarations and usages, and call analyze_impact before changing a "
        "method when its blast radius matters. Use find_callers/find_callees for call "
        "relationships, then read_file or search_code only as needed. "
        "Before the first apply_patch, create a structured plan with create_repair_plan. "
        "After every failed Maven test, revise the plan with create_repair_plan and a "
        "revision_reason before patching again. For a code change, follow this order: "
        "locate the problem, read relevant code, create the plan, use apply_patch to "
        "edit existing UTF-8 files. A successful patch automatically runs the fixed "
        "Maven test. Inspect failures, read code again, revise the plan, and patch again. "
        "apply_patch requires exact old_text/new_text replacements and is atomic. "
        "Never modify files outside apply_patch, never run shell commands, and never "
        "invent Maven arguments. At most three repair patches are allowed after the "
        "first failed test. When tests pass, summarize the change and mention the "
        "test result and diff. Answer in the same language as the user's task."
    )
    assert SYSTEM_PROMPT == expected_full_prompt

    basic = build_system_prompt(capabilities_for(AgentProfile.BASIC))
    code_intelligence = build_system_prompt(
        capabilities_for(AgentProfile.CODE_INTELLIGENCE)
    )
    planner = build_system_prompt(capabilities_for(AgentProfile.PLANNER))
    assert "find_symbol" not in basic
    assert "create_repair_plan" not in basic
    assert "automatically runs" not in basic
    assert "find_symbol" in code_intelligence
    assert "create_repair_plan" not in code_intelligence
    assert "create_repair_plan" in planner
    assert "automatically runs" not in planner
    assert "Never modify files outside apply_patch" in basic


async def _bound_tools_for_profile(
    profile,
    manager,
    origin_repo,
):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    (repo / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    model = FakeChatModel(script=[AIMessage(content="done")])
    await run_agent(
        "inspect",
        repo,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        profile=profile,
    )
    return set(model.bound_tool_names[0])


async def test_capability_profiles_expose_only_their_tools(manager, origin_repo):
    basic = await _bound_tools_for_profile(
        AgentProfile.BASIC, manager, origin_repo
    )
    ci = await _bound_tools_for_profile(
        AgentProfile.CODE_INTELLIGENCE, manager, origin_repo
    )
    planner = await _bound_tools_for_profile(
        AgentProfile.PLANNER, manager, origin_repo
    )
    full = await _bound_tools_for_profile(
        AgentProfile.FULL, manager, origin_repo
    )
    assert basic == BASIC_TOOLS
    assert ci == BASIC_TOOLS | CODE_INTELLIGENCE_TOOLS
    assert planner == BASIC_TOOLS | CODE_INTELLIGENCE_TOOLS | {"create_repair_plan"}
    assert full == planner


async def test_default_profile_is_full(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    (repo / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    model = FakeChatModel(script=[AIMessage(content="done")])
    await run_agent(
        "inspect",
        repo,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
    )
    assert "create_repair_plan" in model.bound_tool_names[0]
    assert CODE_INTELLIGENCE_TOOLS <= set(model.bound_tool_names[0])


async def test_basic_profile_allows_patch_without_plan_or_auto_test(
    manager,
    origin_repo,
):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    (repo / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    model = FakeChatModel(
        script=[
            tool_call(
                "apply_patch",
                {
                    "files": [
                        {
                            "path": "Main.java",
                            "replacements": [
                                {
                                    "old_text": "public class Main {}",
                                    "new_text": "public class Main { int fixed; }",
                                }
                            ],
                        }
                    ]
                },
                "patch_basic",
            ),
            AIMessage(content="fixed"),
        ]
    )
    result = await run_agent(
        "fix",
        repo,
        model=model,
        workspace_id=info.workspace_id,
        workspace_manager=manager,
        profile=AgentProfile.BASIC,
    )
    assert result.modified_files == ["Main.java"]
    assert result.repair_plan is None
    assert result.test_result is None
    assert result.auto_maven_test_runs == 0
    assert result.tool_usage == {"apply_patch": 1}
    assert len(result.tool_events) == 1
    assert result.tool_events[0].tool_name == "apply_patch"
    assert result.tool_events[0].step == 1
    assert result.tool_events[0].repair_round == 0
    assert result.tool_events[0].target_file == "Main.java"
    assert result.tool_events[0].success is True
    assert result.tool_events[0].invoked is True
    assert result.tool_events[0].patch_applied is True
    assert result.tool_events[0].error_category is None
    assert result.tool_events[0].output_chars > 0
    assert result.tool_events[0].files_requested == 1
    assert result.tool_events[0].replacements_requested == 1
    assert result.tool_events[0].successful_replacements == 1
    assert result.apply_patch_invocations == 1
    assert result.patch_files_requested == 1
    assert result.patch_replacements_requested == 1
    assert result.successful_patch_replacements == 1
    assert result.recovered_patch_failures == 0
    assert model.calls == 2


async def test_profile_runs_do_not_share_capability_state(manager, origin_repo):
    basic = await _bound_tools_for_profile(
        AgentProfile.BASIC, manager, origin_repo
    )
    full = await _bound_tools_for_profile(
        AgentProfile.FULL, manager, origin_repo
    )
    assert "find_symbol" not in basic
    assert "find_symbol" in full
    assert "find_symbol" not in BASIC_TOOLS
