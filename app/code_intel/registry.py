"""Process-wide registry of on-demand workspace Java indexes."""

import logging
import threading
from pathlib import Path

from app.code_intel.index import CodeIntelligenceIndex

logger = logging.getLogger(__name__)

_indexes: dict[str, CodeIntelligenceIndex] = {}
_lock = threading.RLock()


def get_or_build_index(
    workspace_id: str,
    repository_path: str | Path,
) -> CodeIntelligenceIndex:
    repo = Path(repository_path).expanduser().resolve()
    with _lock:
        index = _indexes.get(workspace_id)
        if index is None or index.repository_path != repo:
            index = CodeIntelligenceIndex(workspace_id, repo)
            _indexes[workspace_id] = index
        if not index.built:
            index.build()
        return index


def get_index(workspace_id: str) -> CodeIntelligenceIndex | None:
    with _lock:
        return _indexes.get(workspace_id)


def refresh_workspace_files(
    workspace_id: str,
    repository_path: str | Path,
    paths: list[str],
) -> int:
    index = get_index(workspace_id)
    if index is None or index.repository_path != Path(repository_path).resolve():
        return 0
    try:
        return index.refresh_files(paths)
    except Exception:
        logger.exception("Failed to refresh Java index for workspace %s", workspace_id)
        remove_index(workspace_id)
        return 0


def remove_index(workspace_id: str) -> None:
    with _lock:
        _indexes.pop(workspace_id, None)


def clear_all_indexes() -> None:
    with _lock:
        _indexes.clear()
