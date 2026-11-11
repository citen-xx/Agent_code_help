"""Controlled Git CLI wrapper.

Only fixed, whitelisted argument lists are ever executed — no shell, no
arbitrary subcommands. Every call runs inside a specific repository
directory and carries a timeout.
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 60.0

#: Never prompt for credentials interactively (server context): missing or
#: rejected auth must fail fast with a clear GitError instead of blocking.
_GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "Never",
    # Read-only inspection must not refresh/write the repository index.
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
}


class GitError(RuntimeError):
    """Raised when a Git operation fails (message includes stderr)."""


def run_git(args: list[str], cwd: Path | str, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    """Run a fixed Git argument list in ``cwd`` and return stdout."""
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_GIT_ENV,
        )
    except FileNotFoundError:
        raise GitError("git 命令不可用：请确认已安装 Git 并加入 PATH") from None
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"Git 操作超时（>{timeout:.0f}s）: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise GitError(f"git {' '.join(cmd)} 失败: {stderr or f'exit code {proc.returncode}'}")
    return proc.stdout


def clone_repository(url: str, target_dir: Path, timeout: float) -> None:
    """Clone ``url`` into ``target_dir`` with repository-native line endings."""
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        [
            "-c",
            "core.autocrlf=false",
            "clone",
            "--quiet",
            url,
            str(target_dir),
        ],
        cwd=target_dir.parent,
        timeout=timeout,
    )


def checkout_branch(repo: Path, branch: str) -> None:
    run_git(["checkout", branch], cwd=repo)


def checkout_commit(repo: Path, commit: str) -> None:
    run_git(["checkout", commit], cwd=repo)


def current_branch(repo: Path) -> str:
    return run_git(["branch", "--show-current"], cwd=repo).strip() or "detached"


def head_commit(repo: Path) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=repo).strip()


def status_porcelain(repo: Path) -> str:
    return run_git(["status", "--porcelain=v1", "--branch"], cwd=repo)


def worktree_is_dirty(repo: Path) -> bool:
    """Return whether porcelain output contains any entry besides its branch header."""
    return any(
        line.strip() and not line.startswith("## ")
        for line in status_porcelain(repo).splitlines()
    )


def diff(repo: Path) -> str:
    return run_git(["diff", "--no-ext-diff", "--no-textconv", "HEAD"], cwd=repo)


def log(repo: Path, limit: int) -> str:
    return run_git(["log", f"-n{limit}", "--format=%H|%an|%ad|%s", "--date=short"], cwd=repo)


def show(repo: Path, commit: str) -> str:
    return run_git(
        ["show", "--format=fuller", "--no-ext-diff", "--no-textconv", commit],
        cwd=repo,
    )
