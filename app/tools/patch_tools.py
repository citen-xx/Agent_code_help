"""Workspace-bound agent tool for atomic source patches."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool

from app.code_intel.registry import refresh_workspace_files
from app.tools.repo import ToolError
from app.workspace.models import (
    ApplyPatchRequest,
    PatchFailureResult,
    PatchFile,
)
from app.workspace.patch import PatchError, apply_workspace_patch

if TYPE_CHECKING:
    from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


def create_patch_tools(
    workspace_id: str,
    repository_path: str | Path,
    manager: "WorkspaceManager | None" = None,
) -> list[BaseTool]:
    """Create the apply_patch tool bound to one workspace repository."""
    if manager is not None:
        repo = manager.assert_repository_binding(workspace_id, repository_path)
    else:
        repo = Path(repository_path).expanduser().resolve()
        if repo.name != "repository" or repo.parent.name != workspace_id:
            raise ToolError(
                f"apply_patch 只能绑定 workspace {workspace_id} 的 repository 目录"
            )
    if not repo.is_dir():
        raise ToolError(f"workspace repository 不存在: {repo}")

    def _apply_patch(
        files: list,
    ) -> str:
        file_entries = [PatchFile.model_validate(item) for item in files]
        replacement_count = sum(
            len(file_entry.replacements) for file_entry in file_entries
        )
        try:
            result = apply_workspace_patch(repo, file_entries)
        except PatchError as exc:
            return PatchFailureResult(
                error_category=exc.category,
                file=exc.file,
                replacement_index=exc.replacement_index,
                message=str(exc),
                files_requested=len(file_entries),
                replacements_requested=replacement_count,
                successful_replacements=0,
            ).model_dump_json(indent=2)
        refresh_workspace_files(workspace_id, repo, result.affected_files)
        return result.model_dump_json(indent=2)

    return [
        StructuredTool.from_function(
            name="apply_patch",
            description=(
                "Transactionally patch one or more existing UTF-8 files. Group "
                "all edits for a path under exactly one files[] entry. Each file "
                "contains an ordered replacements[] list; replacements execute "
                "sequentially against the updated in-memory content. Every "
                "old_text must match exactly once. If any replacement or file "
                "fails, no file is written. Returns a structured error with file "
                "and replacement_index, or affected_files and the git diff."
            ),
            func=_apply_patch,
            args_schema=ApplyPatchRequest,
        )
    ]
