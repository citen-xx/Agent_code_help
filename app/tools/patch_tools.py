"""Workspace-bound agent tool for atomic source patches."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool

from app.code_intel.registry import refresh_workspace_files
from app.tools.repo import ToolError
from app.workspace.models import ApplyPatchRequest
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
        changes: list,
    ) -> str:
        try:
            result = apply_workspace_patch(repo, changes)
        except PatchError as exc:
            raise ToolError(str(exc)) from exc
        refresh_workspace_files(workspace_id, repo, result.affected_files)
        return result.model_dump_json(indent=2)

    return [
        StructuredTool.from_function(
            name="apply_patch",
            description=(
                "Atomically replace exact text in one or more existing UTF-8 "
                "files inside the bound workspace repository. No new files, "
                "deletions, binary files, paths, shell commands, or arbitrary "
                "arguments are accepted. Each old_text must occur exactly once. "
                "Returns affected_files and the current git diff."
            ),
            func=_apply_patch,
            args_schema=ApplyPatchRequest,
        )
    ]
