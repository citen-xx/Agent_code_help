"""Git CLI helpers for tests — local repos only, never network."""

import subprocess
from pathlib import Path


def git_run(args: list[str], cwd: Path | str) -> str:
    """Run a git command in a local repository and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout
