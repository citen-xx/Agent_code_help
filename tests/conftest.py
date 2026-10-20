"""Shared fixtures: offline local Git repositories and workspace managers."""

import pytest

from app.workspace.manager import WorkspaceManager
from tests.git_helpers import git_run


@pytest.fixture
def workspace_root(tmp_path):
    """A per-test .workspaces root directory."""
    return tmp_path / ".workspaces"


@pytest.fixture
def manager(workspace_root):
    """A WorkspaceManager rooted at a temporary directory."""
    return WorkspaceManager(workspace_root, clone_timeout_seconds=60, allow_file_urls=True)


@pytest.fixture
def origin_repo(tmp_path):
    """A local git repository (main + develop branches) usable as clone source."""
    repo = tmp_path / "origin"
    repo.mkdir()
    git_run(["init", "-q", "--initial-branch=main"], cwd=repo)
    git_run(["config", "user.email", "test@example.com"], cwd=repo)
    git_run(["config", "user.name", "Tester"], cwd=repo)
    (repo / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    git_run(["add", "-A"], cwd=repo)
    git_run(["commit", "-qm", "initial commit"], cwd=repo)
    first_sha = git_run(["rev-parse", "HEAD"], cwd=repo).strip()
    (repo / "OrderService.java").write_text("public class OrderService {}\n", encoding="utf-8")
    git_run(["add", "-A"], cwd=repo)
    git_run(["commit", "-qm", "add order service"], cwd=repo)
    git_run(["checkout", "-q", "-b", "develop", first_sha], cwd=repo)
    (repo / "Develop.java").write_text("public class Develop {}\n", encoding="utf-8")
    git_run(["add", "-A"], cwd=repo)
    git_run(["commit", "-qm", "develop branch work"], cwd=repo)
    git_run(["checkout", "-q", "main"], cwd=repo)
    return repo
