"""Workspace management: one isolated directory per analysis task.

Every workspace lives under a single workspace root (``.workspaces/`` by
default) as ``<root>/<workspace_id>/repository``. The manager clones the
requested Git repository there; workspace ids are strictly validated (12
hex chars) so no path traversal is possible.
"""

import json
import logging
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import settings
from app.workspace import git
from app.workspace.models import WorkspaceInfo

logger = logging.getLogger(__name__)

WORKSPACE_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~-]*$")
_ALLOWED_SCHEMES = {"https", "file"}

CLONE_TIMEOUT_SECONDS = 300.0


class WorkspaceError(RuntimeError):
    """Base error for workspace operations."""


class WorkspaceValidationError(WorkspaceError):
    """Invalid URL / branch / commit parameters."""


class WorkspaceNotFoundError(WorkspaceError):
    """Unknown workspace id (or invalid id format)."""


def validate_repository_url(url: str, allow_file_urls: bool = False) -> str:
    """Validate a repository URL.

    HTTPS is the production default. ``file://`` is opt-in for the offline
    test suite only; the application's shared manager keeps it disabled.
    """
    if not url or not url.strip():
        raise WorkspaceValidationError("repository_url 不能为空")
    url = url.strip()
    if any(c.isspace() for c in url):
        raise WorkspaceValidationError("repository_url 包含非法字符")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise WorkspaceValidationError(f"repository_url 格式非法: {exc}") from exc
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise WorkspaceValidationError(
            f"repository_url 仅支持 https://（本地测试可用 file://），收到 scheme: {parsed.scheme!r}"
        )
    if parsed.scheme == "file" and not allow_file_urls:
        raise WorkspaceValidationError("repository_url 仅支持 https://；file:// 仅供测试框架显式启用")
    if parsed.username or parsed.password:
        raise WorkspaceValidationError("repository_url 不能包含用户名或密码")
    if parsed.scheme == "https" and (not parsed.hostname or not parsed.path.strip("/")):
        raise WorkspaceValidationError(
            "repository_url 必须包含主机名和仓库路径，例如 https://github.com/org/repo.git"
        )
    if parsed.scheme == "file" and not parsed.path.strip("/"):
        raise WorkspaceValidationError("file:// URL 缺少路径")
    return url


def validate_git_ref(value: str, label: str) -> str:
    """Reject anything that looks like a flag or a revision range."""
    if not _GIT_REF_RE.match(value) or ".." in value:
        raise WorkspaceValidationError(
            f"{label} 非法（仅允许字母/数字/_ . / ~ -，且不能以 - 开头）: {value!r}"
        )
    return value


def _rmtree_force(path: Path) -> None:
    """Remove a directory tree, clearing read-only flags first.

    Git pack files (.idx/.pack) are read-only, which breaks plain
    ``shutil.rmtree`` on Windows.
    """

    def _onerror(func, target, _exc_info):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_onerror)


class WorkspaceManager:
    """Creates and manages isolated per-task workspaces under one root."""

    def __init__(
        self,
        root: str | Path,
        clone_timeout_seconds: float = CLONE_TIMEOUT_SECONDS,
        allow_file_urls: bool = False,
    ):
        self.root = Path(root).expanduser().resolve()
        self.clone_timeout_seconds = clone_timeout_seconds
        self.allow_file_urls = allow_file_urls

    # ---- creation ----

    def create(self, repository_url: str, branch: str | None = None, commit: str | None = None) -> WorkspaceInfo:
        """Clone ``repository_url`` into a fresh workspace under the root."""
        url = validate_repository_url(repository_url, allow_file_urls=self.allow_file_urls)
        if branch:
            branch = validate_git_ref(branch, "branch")
        if commit:
            commit = validate_git_ref(commit, "commit")

        workspace_id, workspace_dir = self._reserve_workspace_dir()
        repo_dir = workspace_dir / "repository"

        logger.info("Creating workspace %s (url=%s, branch=%s, commit=%s)", workspace_id, url, branch, commit)
        try:
            git.clone_repository(url, repo_dir, timeout=self.clone_timeout_seconds)
            if branch:
                git.checkout_branch(repo_dir, branch)
            if commit:
                git.checkout_commit(repo_dir, commit)
            self._write_meta(workspace_dir, repository_url=url, branch=branch, commit=commit)
            info = WorkspaceInfo(
                workspace_id=workspace_id,
                repository_path=str(repo_dir),
                repository_url=url,
                branch=git.current_branch(repo_dir),
                commit=git.head_commit(repo_dir),
            )
        except Exception:
            # clean up the partial workspace, then re-raise
            # (safe: the directory is root/<validated-id> only)
            try:
                _rmtree_force(workspace_dir)
            except OSError:
                logger.warning("Failed to clean up partial workspace %s", workspace_dir)
            raise

        logger.info("Workspace %s ready (branch=%s, commit=%s)", workspace_id, info.branch, info.commit[:12])
        return info

    # ---- lookup / deletion ----

    def get(self, workspace_id: str) -> WorkspaceInfo:
        """Return the current status of an existing workspace."""
        workspace_dir = self._workspace_dir(workspace_id)
        repo_dir = workspace_dir / "repository"
        if not repo_dir.is_dir():
            raise WorkspaceNotFoundError(f"workspace 不存在: {workspace_id}")
        meta = self._read_meta(workspace_dir)
        return WorkspaceInfo(
            workspace_id=workspace_id,
            repository_path=str(repo_dir),
            repository_url=meta.get("repository_url", ""),
            branch=git.current_branch(repo_dir),
            commit=git.head_commit(repo_dir),
        )

    def resolve_repository_path(self, workspace_id: str) -> Path:
        """Resolve a workspace id to its local repository path."""
        return Path(self.get(workspace_id).repository_path)

    def assert_repository_binding(self, workspace_id: str, repository_path: str | Path) -> Path:
        """Ensure ``repository_path`` is exactly this workspace's repository.

        This is used when creating Git tools so a mismatched id/path pair
        cannot escape the workspace root.
        """
        expected = self.resolve_repository_path(workspace_id)
        actual = Path(repository_path).expanduser().resolve()
        if actual != expected:
            raise WorkspaceNotFoundError(
                f"workspace 绑定不匹配: {workspace_id} -> {repository_path}"
            )
        return expected

    def delete(self, workspace_id: str) -> None:
        """Delete a workspace (only its own directory under the root)."""
        workspace_dir = self._workspace_dir(workspace_id)
        if not workspace_dir.is_dir():
            raise WorkspaceNotFoundError(f"workspace 不存在: {workspace_id}")
        logger.info("Deleting workspace %s", workspace_id)
        _rmtree_force(workspace_dir)

    # ---- internals ----

    def _workspace_dir(self, workspace_id: str) -> Path:
        """Map a workspace id to its directory.

        Strict format validation (12 lowercase hex chars) makes path
        traversal impossible; the parent check is belt and suspenders.
        """
        if not WORKSPACE_ID_RE.match(workspace_id or ""):
            raise WorkspaceNotFoundError(f"workspace 不存在: {workspace_id}")
        candidate = (self.root / workspace_id).resolve()
        if candidate.parent != self.root:
            raise WorkspaceNotFoundError(f"workspace 不存在: {workspace_id}")
        return candidate

    def _reserve_workspace_dir(self) -> tuple[str, Path]:
        """Atomically reserve a fresh workspace directory under the root."""
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            workspace_id = uuid.uuid4().hex[:12]
            candidate = self.root / workspace_id
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            resolved = candidate.resolve()
            if resolved.parent != self.root:
                raise WorkspaceError("workspace 目录解析后越出 workspace 根目录")
            return workspace_id, resolved
        raise WorkspaceError("无法生成唯一的 workspace_id")

    def _write_meta(self, workspace_dir: Path, **fields: str | None) -> None:
        (workspace_dir / "meta.json").write_text(
            json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _read_meta(workspace_dir: Path) -> dict:
        try:
            return json.loads((workspace_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


#: Process-wide default manager (root comes from settings.WORKSPACE_ROOT).
workspace_manager = WorkspaceManager(
    Path(settings.WORKSPACE_ROOT),
    clone_timeout_seconds=settings.WORKSPACE_CLONE_TIMEOUT_SECONDS,
)
