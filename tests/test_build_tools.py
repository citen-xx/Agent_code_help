"""LangChain build tool binding and security tests."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.tools.build_tools import create_build_tools
from app.tools.repo import ToolError
from app.workspace.manager import WorkspaceNotFoundError
from tests.build_helpers import patch_popen, patch_popen_sequence


@pytest.fixture
def workspace(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    (Path(info.repository_path) / "pom.xml").write_text(
        "<project/>", encoding="utf-8"
    )
    return info


def test_detect_tool_returns_build_project(workspace, manager):
    tools = {
        tool.name: tool
        for tool in create_build_tools(
            workspace.workspace_id, workspace.repository_path, manager=manager
        )
    }
    body = json.loads(tools["detect_build_tools"].invoke({}))
    assert body[0]["tool"] == "maven"
    assert body[0]["build_files"] == ["pom.xml"]


def test_run_tests_tool_returns_structured_output(workspace, manager, monkeypatch):
    patch_popen(
        monkeypatch,
        stdout="Tests run: 2",
        stderr="warning",
        returncode=0,
    )
    tools = {
        tool.name: tool
        for tool in create_build_tools(
            workspace.workspace_id, workspace.repository_path, manager=manager
        )
    }

    body = json.loads(tools["run_tests"].invoke({"tool": "maven"}))

    assert body["tool"] == "maven"
    assert body["exit_code"] == 0
    assert body["stdout"] == "Tests run: 2"
    assert body["stderr"] == "warning"
    assert body["success"] is True


def test_run_tests_rejects_arbitrary_fields(workspace, manager):
    tools = {
        tool.name: tool
        for tool in create_build_tools(
            workspace.workspace_id, workspace.repository_path, manager=manager
        )
    }
    with pytest.raises(ValidationError):
        tools["run_tests"].invoke({"tool": "maven", "path": "C:/"})
    # Detection has no command argument; unknown fields cannot alter the
    # fixed test command.
    body = json.loads(
        tools["detect_build_tools"].invoke({"command": "mvn test"})
    )
    assert body[0]["command"][-1] == "test"


def test_build_tools_require_matching_workspace(workspace, manager):
    with pytest.raises(WorkspaceNotFoundError):
        create_build_tools("abcdef012345", workspace.repository_path, manager=manager)


def test_build_tools_reject_arbitrary_path(tmp_path, workspace):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolError):
        create_build_tools(workspace.workspace_id, outside)


def test_build_tools_are_bound_to_each_workspace(manager, origin_repo, monkeypatch):
    first = manager.create(origin_repo.as_uri())
    second = manager.create(origin_repo.as_uri())
    for info in (first, second):
        (Path(info.repository_path) / "pom.xml").write_text(
            "<project/>", encoding="utf-8"
        )
    first_tools = {
        tool.name: tool
        for tool in create_build_tools(
            first.workspace_id, first.repository_path, manager=manager
        )
    }
    second_tools = {
        tool.name: tool
        for tool in create_build_tools(
            second.workspace_id, second.repository_path, manager=manager
        )
    }
    captured = patch_popen_sequence(monkeypatch, [{"returncode": 0}])

    first_tools["run_tests"].invoke({"tool": "maven"})
    second_tools["run_tests"].invoke({"tool": "maven"})

    mounts = [
        call["command"][call["command"].index("--mount") + 1]
        for call in captured
    ]
    assert f"source={Path(first.repository_path)}" in mounts[0]
    assert f"source={Path(second.repository_path)}" in mounts[1]
