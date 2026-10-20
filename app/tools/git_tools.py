"""Read-only Git tools for the agent, bound to one workspace.

The tools take NO filesystem path and NO free-form git subcommand — they
only inspect the workspace's own repository, fixed at tool creation time.
"""

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.tools.repo import ToolError
from app.workspace import git

if TYPE_CHECKING:
    from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 20_000
MAX_STATUS_ENTRIES = 100
MAX_LOG_ENTRIES = 50

_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~-]*$")


class _NoArgs(BaseModel):
    """No arguments."""


class _GitLogArgs(BaseModel):
    """Arguments for git_log."""

    limit: int = Field(default=10, ge=1, le=MAX_LOG_ENTRIES, description="Maximum number of commits to return.")


class _GitShowArgs(BaseModel):
    """Arguments for git_show."""

    commit: str = Field(min_length=1, description="Commit hash (full or short) to inspect.")


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n...[git {label} truncated: {omitted} chars omitted]"


def _git_status(repository_path: Path) -> str:
    out = git.status_porcelain(repository_path)
    lines = out.splitlines()
    if not lines:
        return "branch: unknown\nworking tree: clean"
    header = lines[0][3:] if lines[0].startswith("## ") else lines[0]
    branch = header
    tracking = ""
    if header.startswith("HEAD"):  # detached HEAD
        branch = "detached"
        tracking = header
    elif header.startswith("Initial commit on "):
        branch = header.removeprefix("Initial commit on ")
    elif "..." in header:
        branch, tracking = header.split("...", 1)
        tracking = tracking.strip()
    entries = lines[1:]
    modified = [e for e in entries if not e.startswith("??")]
    untracked = [e for e in entries if e.startswith("??")]
    parts = [f"branch: {branch}" + (f" [{tracking}]" if tracking else "")]
    if entries:
        summary = []
        if modified:
            summary.append(f"{len(modified)} modified")
        if untracked:
            summary.append(f"{len(untracked)} untracked")
        parts.append("working tree: " + ", ".join(summary))
    else:
        parts.append("working tree: clean")
    if entries:
        parts.append("\n".join(f"- {e}" for e in entries[:MAX_STATUS_ENTRIES]))
        if len(entries) > MAX_STATUS_ENTRIES:
            parts.append(f"...[git status truncated: {len(entries) - MAX_STATUS_ENTRIES} entries omitted]")
    return "\n".join(parts)


def _git_diff(repository_path: Path) -> str:
    out = git.diff(repository_path)
    if not out.strip():
        return "（无修改）working tree 与 HEAD 一致"
    return _truncate(out, MAX_DIFF_CHARS, "diff")


def _git_log(repository_path: Path, limit: int = 10) -> str:
    out = git.log(repository_path, limit)
    lines = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        commit_hash, author, date, subject = parts
        lines.append(f"- {commit_hash[:12]} {author} {date} {subject}")
    return "\n".join(lines) if lines else "（暂无提交记录）"


def _git_show(repository_path: Path, commit: str) -> str:
    if not _GIT_REF_RE.match(commit) or ".." in commit:
        raise ToolError(f"非法的 commit 参数: {commit!r}")
    out = git.show(repository_path, commit)
    return _truncate(out, MAX_DIFF_CHARS, "show")


def create_git_tools(
    workspace_id: str,
    repository_path: str | Path,
    manager: "WorkspaceManager | None" = None,
) -> list[BaseTool]:
    """Create the four read-only Git tools, bound to one workspace."""

    if manager is not None:
        repo = manager.assert_repository_binding(workspace_id, repository_path)
    else:
        repo = Path(repository_path).expanduser().resolve()
        if repo.name != "repository" or repo.parent.name != workspace_id:
            raise ToolError(
                f"Git Tool 只能绑定 workspace {workspace_id} 的 repository 目录"
            )
    if not repo.is_dir():
        raise ToolError(f"workspace repository 不存在: {repo}")
    logger.debug("Binding git tools to workspace %s (repo=%s)", workspace_id, repo)

    def _status() -> str:
        return _git_status(repo)

    def _diff() -> str:
        return _git_diff(repo)

    def _log(limit: int = 10) -> str:
        return _git_log(repo, limit)

    def _show(commit: str) -> str:
        return _git_show(repo, commit)

    return [
        StructuredTool.from_function(
            name="git_status",
            description=(
                "Show the workspace repository's current branch and working "
                "tree status (modified / untracked files). Read-only."
            ),
            func=_status,
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            name="git_diff",
            description=(
                "Show the current uncommitted diff (working tree vs HEAD) of "
                "the workspace repository. Read-only."
            ),
            func=_diff,
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            name="git_log",
            description=(
                "Show the recent commit history (hash, author, date, message) "
                "of the workspace repository. Read-only."
            ),
            func=_log,
            args_schema=_GitLogArgs,
        ),
        StructuredTool.from_function(
            name="git_show",
            description=(
                "Show a specific commit's metadata and diff by commit hash. "
                "Read-only."
            ),
            func=_show,
            args_schema=_GitShowArgs,
        ),
    ]
