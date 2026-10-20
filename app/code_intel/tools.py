"""Workspace-bound read-only Java code intelligence tools."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.code_intel.models import JavaSymbolType
from app.code_intel.registry import get_or_build_index
from app.tools.repo import ToolError

if TYPE_CHECKING:
    from app.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


class _ListSymbolsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str | None = Field(
        default=None,
        description="Optional repository-relative Java file path.",
    )
    class_name: str | None = Field(
        default=None,
        description="Optional class/interface/enum/record name.",
    )
    symbol_type: JavaSymbolType | None = Field(
        default=None,
        description="Optional symbol type filter.",
    )
    limit: int = Field(default=200, ge=1, le=500)


class _FindSymbolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Exact symbol name.")
    symbol_type: JavaSymbolType | None = None
    file_path: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class _FindReferencesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Exact identifier name.")
    file_path: str | None = None
    include_definitions: bool = False
    limit: int = Field(default=200, ge=1, le=500)


def create_code_intel_tools(
    workspace_id: str,
    repository_path: str | Path,
    manager: "WorkspaceManager | None" = None,
) -> list[BaseTool]:
    if manager is not None:
        repo = manager.assert_repository_binding(workspace_id, repository_path)
    else:
        repo = Path(repository_path).expanduser().resolve()
        if repo.name != "repository" or repo.parent.name != workspace_id:
            raise ToolError(
                f"Code Intelligence Tool 只能绑定 workspace {workspace_id}"
            )
    if not repo.is_dir():
        raise ToolError(f"workspace repository 不存在: {repo}")

    def _index():
        return get_or_build_index(workspace_id, repo)

    def _list_symbols(
        file_path: str | None = None,
        class_name: str | None = None,
        symbol_type: JavaSymbolType | None = None,
        limit: int = 200,
    ) -> str:
        try:
            result = _index().list_symbols(
                file_path=file_path,
                class_name=class_name,
                symbol_type=symbol_type,
                limit=limit,
            )
        except Exception as exc:
            raise ToolError(f"Java 符号索引失败: {exc}") from exc
        return result.model_dump_json(indent=2)

    def _find_symbol(
        name: str,
        symbol_type: JavaSymbolType | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> str:
        try:
            result = _index().find_symbol(
                name,
                symbol_type=symbol_type,
                file_path=file_path,
                limit=limit,
            )
        except Exception as exc:
            raise ToolError(f"Java 符号查询失败: {exc}") from exc
        return result.model_dump_json(indent=2)

    def _find_references(
        name: str,
        file_path: str | None = None,
        include_definitions: bool = False,
        limit: int = 200,
    ) -> str:
        try:
            result = _index().find_references(
                name,
                file_path=file_path,
                include_definitions=include_definitions,
                limit=limit,
            )
        except Exception as exc:
            raise ToolError(f"Java 引用查询失败: {exc}") from exc
        return result.model_dump_json(indent=2)

    logger.debug("Binding Java code intelligence to workspace %s", workspace_id)
    return [
        StructuredTool.from_function(
            name="list_symbols",
            description=(
                "List Java symbols in the bound workspace, optionally filtered "
                "by file, class, or symbol type. Read-only Tree-sitter index."
            ),
            func=_list_symbols,
            args_schema=_ListSymbolsArgs,
        ),
        StructuredTool.from_function(
            name="find_symbol",
            description=(
                "Find exact Java symbol definitions by name and return every "
                "matching overload/nested/duplicate result with file and lines."
            ),
            func=_find_symbol,
            args_schema=_FindSymbolArgs,
        ),
        StructuredTool.from_function(
            name="find_references",
            description=(
                "Find identifier references to a Java symbol name in the bound "
                "workspace. Definition hits can be included explicitly."
            ),
            func=_find_references,
            args_schema=_FindReferencesArgs,
        ),
    ]
