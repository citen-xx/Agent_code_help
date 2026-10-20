"""Workspace-bound apply_patch tool tests."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.tools.patch_tools import create_patch_tools
from app.tools.repo import ToolError
from app.workspace.manager import WorkspaceNotFoundError


@pytest.fixture
def workspace(manager, origin_repo):
    return manager.create(origin_repo.as_uri())


def _tool(workspace, manager):
    return {
        tool.name: tool
        for tool in create_patch_tools(
            workspace.workspace_id,
            workspace.repository_path,
            manager=manager,
        )
    }["apply_patch"]


def test_apply_patch_tool_is_bound_and_returns_diff(workspace, manager):
    tool = _tool(workspace, manager)
    body = json.loads(
        tool.invoke(
            {
                "changes": [
                    {
                        "path": "Main.java",
                        "old_text": "public class Main {}",
                        "new_text": "public class Main { int patched; }",
                    }
                ]
            }
        )
    )
    assert body["success"] is True
    assert body["affected_files"] == ["Main.java"]
    assert "diff --git" in body["diff"]


def test_apply_patch_tool_rejects_arbitrary_arguments(workspace, manager):
    tool = _tool(workspace, manager)
    with pytest.raises(ValidationError):
        tool.invoke(
            {
                "changes": [
                    {
                        "path": "Main.java",
                        "old_text": "public class Main {}",
                        "new_text": "public class Main { int patched; }",
                        "absolute": True,
                    }
                ]
            }
        )


def test_apply_patch_tool_requires_matching_workspace(workspace, manager):
    with pytest.raises(WorkspaceNotFoundError):
        create_patch_tools(
            "abcdef012345",
            workspace.repository_path,
            manager=manager,
        )


def test_apply_patch_tool_rejects_arbitrary_repository_path(tmp_path, workspace):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolError):
        create_patch_tools(workspace.workspace_id, outside)


def test_patch_is_isolated_to_one_workspace(manager, origin_repo):
    first = manager.create(origin_repo.as_uri())
    second = manager.create(origin_repo.as_uri())
    first_tool = _tool(first, manager)
    first_tool.invoke(
        {
            "changes": [
                {
                    "path": "Main.java",
                    "old_text": "public class Main {}",
                    "new_text": "public class Main { int onlyFirst; }",
                }
            ]
        }
    )
    assert "onlyFirst" in (Path(first.repository_path) / "Main.java").read_text(
        encoding="utf-8"
    )
    assert "onlyFirst" not in (Path(second.repository_path) / "Main.java").read_text(
        encoding="utf-8"
    )
