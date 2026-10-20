"""Git read-only tool tests — bound to local workspaces, no network."""

from pathlib import Path

import pytest

from app.tools.git_tools import create_git_tools
from app.tools.repo import ToolError
from app.workspace.manager import WorkspaceNotFoundError
from tests.git_helpers import git_run


@pytest.fixture
def ws(manager, origin_repo):
    return manager.create(origin_repo.as_uri())


@pytest.fixture
def git_tools(ws):
    return {tool.name: tool for tool in create_git_tools(ws.workspace_id, ws.repository_path)}


def test_git_status_clean(ws, git_tools):
    out = git_tools["git_status"].invoke({})
    assert "branch: main" in out
    assert "working tree: clean" in out


def test_git_status_dirty(ws, git_tools):
    repo = Path(ws.repository_path)
    (repo / "Main.java").write_text("public class Main { int x; }\n", encoding="utf-8")
    (repo / "New.java").write_text("class New {}\n", encoding="utf-8")
    out = git_tools["git_status"].invoke({})
    assert "branch: main" in out
    assert "1 modified" in out
    assert "1 untracked" in out
    assert "Main.java" in out
    assert "New.java" in out


def test_git_diff(ws, git_tools):
    repo = Path(ws.repository_path)
    (repo / "Main.java").write_text("public class Main { int x; }\n", encoding="utf-8")
    out = git_tools["git_diff"].invoke({})
    assert "int x" in out


def test_git_diff_clean(ws, git_tools):
    out = git_tools["git_diff"].invoke({})
    assert "无修改" in out


def test_git_diff_truncated(ws, git_tools):
    repo = Path(ws.repository_path)
    (repo / "Main.java").write_text(
        "public class Main {\n" + "//" + "x" * 30_000 + "\n}\n", encoding="utf-8"
    )
    out = git_tools["git_diff"].invoke({})
    assert "truncated" in out
    assert "chars omitted" in out


def test_git_log(ws, git_tools):
    out = git_tools["git_log"].invoke({"limit": 10})
    assert "add order service" in out
    assert "initial commit" in out
    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    assert lines[0].endswith("add order service")  # newest first


def test_git_log_limit(ws, git_tools):
    out = git_tools["git_log"].invoke({"limit": 1})
    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(lines) == 1
    assert "add order service" in out
    assert "initial commit" not in out


def test_git_show(ws, git_tools):
    first = git_run(["rev-parse", "HEAD~1"], cwd=Path(ws.repository_path)).strip()
    out = git_tools["git_show"].invoke({"commit": first})
    assert "initial commit" in out
    assert "Main.java" in out


def test_git_show_truncated(ws, git_tools):
    repo = Path(ws.repository_path)
    (repo / "Big.java").write_text("//" + "x" * 30_000 + "\n", encoding="utf-8")
    git_run(["add", "-A"], cwd=repo)
    git_run(
        ["-c", "user.name=Tester", "-c", "user.email=test@example.com",
         "commit", "-qm", "large commit"],
        cwd=repo,
    )
    sha = git_run(["rev-parse", "HEAD"], cwd=repo).strip()
    out = git_tools["git_show"].invoke({"commit": sha})
    assert "truncated" in out
    assert "chars omitted" in out


def test_git_show_rejects_non_commit_argument(ws, git_tools):
    with pytest.raises(ToolError):
        git_tools["git_show"].invoke({"commit": "--help"})


def test_git_tools_require_matching_workspace(ws, manager):
    with pytest.raises(WorkspaceNotFoundError):
        create_git_tools("abcdef012345", ws.repository_path, manager=manager)


def test_git_tools_reject_arbitrary_repository_path(tmp_path, ws):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolError):
        create_git_tools(ws.workspace_id, outside)
