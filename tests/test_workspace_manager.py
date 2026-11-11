"""WorkspaceManager unit tests — local git repos only, no network."""

import re
from pathlib import Path

import pytest

from app.workspace import git
from app.workspace.manager import (
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspaceNotReadyError,
    WorkspaceValidationError,
)
from app.workspace.models import WorkspaceStatus
from tests.git_helpers import git_run


def test_create_workspace(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    assert re.fullmatch(r"[a-f0-9]{12}", info.workspace_id)
    repo_path = Path(info.repository_path)
    assert repo_path.is_dir()
    # always inside the workspace root: <root>/<id>/repository
    assert repo_path.resolve().parent.parent == manager.root
    assert repo_path.name == "repository"
    assert (repo_path / "Main.java").is_file()
    assert info.branch == "main"
    assert info.commit == git_run(["rev-parse", "HEAD"], cwd=origin_repo).strip()
    assert info.status == WorkspaceStatus.READY


def test_create_workspace_preserves_repository_line_endings(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    raw = (Path(info.repository_path) / "Main.java").read_bytes()
    assert raw == b"public class Main {}\n"


def test_create_workspace_is_isolated(manager, origin_repo):
    first = manager.create(origin_repo.as_uri())
    second = manager.create(origin_repo.as_uri())
    assert first.workspace_id != second.workspace_id
    assert Path(first.repository_path) != Path(second.repository_path)


def test_create_workspace_with_branch(manager, origin_repo):
    info = manager.create(origin_repo.as_uri(), branch="develop")
    assert info.branch == "develop"
    repo = Path(info.repository_path)
    assert (repo / "Develop.java").is_file()
    assert not (repo / "OrderService.java").exists()  # only on main


def test_create_workspace_with_commit(manager, origin_repo):
    first = git_run(["rev-parse", "HEAD~1"], cwd=origin_repo).strip()
    info = manager.create(origin_repo.as_uri(), commit=first)
    assert info.commit == first
    assert info.branch == "detached"
    assert not (Path(info.repository_path) / "OrderService.java").exists()


def test_invalid_urls_rejected(manager):
    for bad in [
        "",
        "not-a-url",
        "http://github.com/org/repo.git",       # plain http
        "ssh://git@github.com/org/repo.git",    # ssh scheme
        "git@github.com:org/repo.git",          # scp-style
        "https://user:secret@github.com/org/repo.git",  # credentials in URL
        "https://github.com",                   # no repo path
    ]:
        with pytest.raises(WorkspaceValidationError):
            manager.create(bad)


def test_file_urls_are_disabled_by_default(workspace_root, origin_repo):
    manager = WorkspaceManager(workspace_root)
    with pytest.raises(WorkspaceValidationError):
        manager.create(origin_repo.as_uri())


def test_invalid_refs_rejected(manager, origin_repo):
    with pytest.raises(WorkspaceValidationError):
        manager.create(origin_repo.as_uri(), branch="--help")
    with pytest.raises(WorkspaceValidationError):
        manager.create(origin_repo.as_uri(), branch="feat/x y")  # space
    with pytest.raises(WorkspaceValidationError):
        manager.create(origin_repo.as_uri(), commit="HEAD..HEAD~1")  # range


def test_clone_failure_raises_and_cleans_up(manager):
    with pytest.raises(git.GitError):
        manager.create("https://127.0.0.1:9/nonexistent.git")
    # no leftover workspace directories
    assert not manager.root.exists() or not any(manager.root.iterdir())


def test_bad_branch_raises_and_cleans_up(manager, origin_repo):
    with pytest.raises(git.GitError):
        manager.create(origin_repo.as_uri(), branch="no-such-branch")
    assert not any(manager.root.iterdir())


def test_workspace_id_path_traversal_blocked(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    for bad in ["../..", "..", "../../../etc", "abc", "a" * 13, "A" * 12, f"{info.workspace_id}/x"]:
        with pytest.raises(WorkspaceNotFoundError):
            manager.get(bad)
        with pytest.raises(WorkspaceNotFoundError):
            manager.delete(bad)
        with pytest.raises(WorkspaceNotFoundError):
            manager.resolve_repository_path(bad)


def test_get_workspace(manager, origin_repo):
    created = manager.create(origin_repo.as_uri())
    fetched = manager.get(created.workspace_id)
    assert fetched.workspace_id == created.workspace_id
    assert fetched.repository_path == created.repository_path
    assert fetched.repository_url == origin_repo.as_uri()
    assert fetched.branch == "main"
    assert fetched.status == WorkspaceStatus.READY


def test_list_workspaces(manager, origin_repo):
    first = manager.create(origin_repo.as_uri())
    second = manager.create(origin_repo.as_uri(), branch="develop")
    (manager.root / "not-a-workspace").mkdir()
    (manager.root / "README.txt").write_text("ignored", encoding="utf-8")

    infos = manager.list()

    assert [info.workspace_id for info in infos] == sorted(
        [first.workspace_id, second.workspace_id]
    )
    assert all(info.status == WorkspaceStatus.READY for info in infos)
    assert {info.branch for info in infos} == {"main", "develop"}


def test_list_workspaces_empty(manager):
    assert manager.list() == []


def test_get_unknown_workspace_raises(manager):
    with pytest.raises(WorkspaceNotFoundError):
        manager.get("abcdef012345")


def test_broken_repository_is_reported_as_error(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repository = Path(info.repository_path)
    repository.rename(repository.with_name("repository.broken"))

    broken = manager.get(info.workspace_id)

    assert broken.status == WorkspaceStatus.ERROR
    with pytest.raises(WorkspaceNotReadyError):
        manager.resolve_repository_path(info.workspace_id)


def test_delete_workspace(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    workspace_dir = Path(info.repository_path).parent
    assert workspace_dir.is_dir()
    manager.delete(info.workspace_id)
    assert not workspace_dir.exists()
    with pytest.raises(WorkspaceNotFoundError):
        manager.delete(info.workspace_id)
    with pytest.raises(WorkspaceNotFoundError):
        manager.get(info.workspace_id)
