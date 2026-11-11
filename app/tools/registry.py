"""LangChain tool wrappers for the repository tools.

The repository path is closed over at creation time, so the LLM can only
inspect the one repository the request named — it cannot pass a different
path.
"""

from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.tools.repo import list_files, read_file, search_code


class _ListFilesArgs(BaseModel):
    """Arguments for list_files."""

    path: str = Field(default=".", description="Repository-relative path to list. Defaults to the repository root.")


class _ReadFileArgs(BaseModel):
    """Arguments for read_file."""

    path: str = Field(description="Repository-relative path of the file to read.")
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="Optional 1-based first line for a precise source read.",
    )
    end_line: int | None = Field(
        default=None,
        ge=1,
        description="Optional inclusive last line; at most 200 lines are returned.",
    )


class _SearchCodeArgs(BaseModel):
    """Arguments for search_code."""

    query: str = Field(min_length=1, description="Text to search for (case-insensitive substring match).")
    file_pattern: str = Field(default="*", description="Optional glob filter for file names, e.g. '*.java'.")


def create_repo_tools(repository_path: str | Path) -> list[BaseTool]:
    """Create the three read-only repo tools, bound to ``repository_path``."""

    def _list_files(path: str = ".") -> str:
        return list_files(repository_path, path)

    def _read_file(
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        return read_file(
            repository_path,
            path,
            start_line=start_line,
            end_line=end_line,
        )

    def _search_code(query: str, file_pattern: str = "*") -> str:
        return search_code(repository_path, query, file_pattern)

    return [
        StructuredTool.from_function(
            name="list_files",
            description=(
                "List files and directories at a path inside the repository. "
                "Read-only. Use it to explore the project structure."
            ),
            func=_list_files,
            args_schema=_ListFilesArgs,
        ),
        StructuredTool.from_function(
            name="read_file",
            description=(
                "Read the content of a single file inside the repository. "
                "Read-only. For a known symbol location, provide start_line and "
                "end_line to fetch only the relevant source range."
            ),
            func=_read_file,
            args_schema=_ReadFileArgs,
        ),
        StructuredTool.from_function(
            name="search_code",
            description=(
                "Search file contents inside the repository (case-insensitive "
                "substring). Read-only. Use it to locate code by keyword; "
                "narrow results with a glob file_pattern like '*.java'."
            ),
            func=_search_code,
            args_schema=_SearchCodeArgs,
        ),
    ]
