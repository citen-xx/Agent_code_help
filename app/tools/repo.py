"""Read-only repository inspection tools.

Every tool resolves paths against a fixed repository root and refuses to
touch anything outside of it (path traversal protection). Tools never
modify or delete files, and never execute shell commands.
"""

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_LIST_ENTRIES = 200
MAX_READ_CHARS = 100_000
MAX_SEARCH_RESULTS = 50
IGNORED_DIRS = {
    ".git", ".svn", ".hg",
    ".idea", ".vscode", ".venv", "venv", "node_modules", "__pycache__",
    "target", "build", "out", "dist",
}


class ToolError(RuntimeError):
    """Raised when a tool cannot complete; the message is fed back to the LLM."""


def resolve_inside(repository_path: str | Path, rel_path: str = ".") -> Path:
    """Resolve ``rel_path`` against ``repository_path`` and guarantee the
    result stays inside the repository. Raises ToolError otherwise."""
    repo = Path(repository_path).expanduser().resolve()
    if not repo.is_dir():
        raise ToolError(f"repository_path 不是有效目录: {repository_path}")
    candidate = (repo / rel_path).resolve()
    try:
        candidate.relative_to(repo)
    except ValueError:
        raise ToolError(f"路径越界，禁止访问仓库外部路径: {rel_path!r}") from None
    return candidate


def list_files(repository_path: str | Path, path: str = ".") -> str:
    """List entries (directories first, then files) at a repository-relative path."""
    repo = Path(repository_path).expanduser().resolve()
    target = resolve_inside(repo, path)
    if not target.exists():
        raise ToolError(f"路径不存在: {path}")
    if target.is_file():
        return f"- {target.relative_to(repo).as_posix()}"
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = []
    for entry in entries:
        if entry.is_dir() and entry.name in IGNORED_DIRS:
            continue
        lines.append(f"- {entry.relative_to(repo).as_posix()}{'/' if entry.is_dir() else ''}")
        if len(lines) >= MAX_LIST_ENTRIES:
            lines.append("...[list truncated]")
            break
    if not lines:
        rel = target.relative_to(repo).as_posix() or "."
        return f"（空目录）{rel}"
    return "\n".join(lines)


def read_file(repository_path: str | Path, path: str) -> str:
    """Read a file inside the repository (truncated if very large)."""
    target = resolve_inside(repository_path, path)
    if not target.exists():
        raise ToolError(f"文件不存在: {path}")
    if not target.is_file():
        raise ToolError(f"不是文件: {path}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ToolError(f"读取文件失败 {path}: {exc}") from exc
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n...[file truncated]"
    return text


def search_code(repository_path: str | Path, query: str, file_pattern: str = "*") -> str:
    """Case-insensitive substring search over repository file contents."""
    if not query.strip():
        raise ToolError("query 不能为空")
    repo = Path(repository_path).expanduser().resolve()
    if not repo.is_dir():
        raise ToolError(f"repository_path 不是有效目录: {repository_path}")
    matches = []
    needle = query.lower()
    for file in _iter_files(repo):
        rel = file.relative_to(repo).as_posix()
        if not fnmatch.fnmatch(rel, file_pattern):
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                matches.append((rel, lineno, line.strip()[:200]))
                if len(matches) >= MAX_SEARCH_RESULTS:
                    break
        if len(matches) >= MAX_SEARCH_RESULTS:
            break
    if not matches:
        return f'未找到匹配 "{query}" 的内容（pattern: {file_pattern}）'
    noun = "matches" if len(matches) > 1 else "match"
    lines = [f'Found {len(matches)} {noun} for "{query}":']
    lines += [f"- {rel}:{lineno}: {line}" for rel, lineno, line in matches]
    if len(matches) >= MAX_SEARCH_RESULTS:
        lines.append("...[results truncated]")
    return "\n".join(lines)


def _iter_files(repo: Path):
    """Yield all files under the repository, skipping VCS/build directories."""
    for path in repo.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.relative_to(repo).parts):
            continue
        if path.is_file():
            yield path
