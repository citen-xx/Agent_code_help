"""Workspace-scoped in-memory Java symbol index."""

import logging
import os
import threading
from pathlib import Path
from typing import TypeVar

from app.code_intel.models import (
    JavaParseDiagnostic,
    JavaReference,
    JavaSymbol,
    JavaSymbolType,
    ReferenceQueryResult,
    SymbolQueryResult,
)
from app.code_intel.parser import ParsedJavaFile, parse_java_file

logger = logging.getLogger(__name__)

IGNORED_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    ".venv",
    "node_modules",
    "target",
    "build",
    "out",
    "dist",
    "__pycache__",
}

T = TypeVar("T")


class CodeIntelError(RuntimeError):
    """Raised for invalid index/query operations."""


class CodeIntelligenceIndex:
    """In-memory symbol/reference index for one workspace repository."""

    def __init__(self, workspace_id: str, repository_path: str | Path):
        self.workspace_id = workspace_id
        self.repository_path = Path(repository_path).expanduser().resolve()
        self._files: dict[str, ParsedJavaFile] = {}
        self._built = False
        self._lock = threading.RLock()

    @property
    def built(self) -> bool:
        return self._built

    def _normalize_file_path(self, raw_path: str) -> str:
        path = Path(raw_path)
        if path.is_absolute() or path.drive:
            raise CodeIntelError(f"只允许 repository 相对路径: {raw_path!r}")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise CodeIntelError(f"非法文件路径: {raw_path!r}")
        candidate = (self.repository_path / path).resolve()
        try:
            candidate.relative_to(self.repository_path)
        except ValueError:
            raise CodeIntelError(f"文件路径越出 repository: {raw_path!r}") from None
        return candidate.relative_to(self.repository_path).as_posix()

    def _java_files(self) -> list[str]:
        if not self.repository_path.is_dir():
            raise CodeIntelError(f"workspace repository 不存在: {self.repository_path}")
        found: list[str] = []
        for root, dirs, files in os.walk(self.repository_path, followlinks=False):
            current = Path(root)
            dirs[:] = sorted(
                directory
                for directory in dirs
                if directory not in IGNORED_DIRS
                and not (current / directory).is_symlink()
            )
            for filename in sorted(files):
                if not filename.endswith(".java"):
                    continue
                path = current / filename
                if path.is_symlink():
                    continue
                found.append(path.relative_to(self.repository_path).as_posix())
        return found

    def _replace_file(self, relative_path: str) -> None:
        try:
            self._files[relative_path] = parse_java_file(
                self.repository_path,
                relative_path,
            )
        except (OSError, ValueError) as exc:
            self._files[relative_path] = ParsedJavaFile(
                diagnostics=[
                    JavaParseDiagnostic(
                        file_path=relative_path,
                        line=1,
                        column=1,
                        message=f"Java 文件索引失败: {exc}",
                    )
                ]
            )

    def build(self) -> None:
        with self._lock:
            self._files = {}
            for relative_path in self._java_files():
                self._replace_file(relative_path)
            self._built = True
            logger.info(
                "Built Java index for workspace %s (%d files)",
                self.workspace_id,
                len(self._files),
            )

    def refresh_files(self, paths: list[str]) -> int:
        """Refresh only the given repository-relative Java files."""
        with self._lock:
            if not self._built:
                return 0
            refreshed = 0
            for raw_path in paths:
                relative_path = self._normalize_file_path(raw_path)
                if not relative_path.endswith(".java"):
                    continue
                target = self.repository_path / relative_path
                if not target.exists() or target.is_symlink():
                    self._files.pop(relative_path, None)
                else:
                    self._replace_file(relative_path)
                refreshed += 1
            return refreshed

    def _all_symbols(self) -> list[JavaSymbol]:
        return [
            symbol
            for file_index in self._files.values()
            for symbol in file_index.symbols
        ]

    def _all_references(self) -> list[JavaReference]:
        return [
            reference
            for file_index in self._files.values()
            for reference in file_index.references
        ]

    def _all_diagnostics(self) -> list[JavaParseDiagnostic]:
        return [
            diagnostic
            for file_index in self._files.values()
            for diagnostic in file_index.diagnostics
        ]

    @staticmethod
    def _limit(items: list[T], limit: int) -> tuple[list[T], int, bool]:
        return items[:limit], len(items), len(items) > limit

    def list_symbols(
        self,
        *,
        file_path: str | None = None,
        class_name: str | None = None,
        symbol_type: JavaSymbolType | None = None,
        limit: int = 200,
    ) -> SymbolQueryResult:
        if not self._built:
            self.build()
        with self._lock:
            normalized_file = (
                self._normalize_file_path(file_path) if file_path else None
            )
            symbols = self._all_symbols()
            if normalized_file:
                symbols = [
                    symbol
                    for symbol in symbols
                    if symbol.file_path == normalized_file
                ]
            if class_name:
                type_names = {
                    JavaSymbolType.CLASS,
                    JavaSymbolType.INTERFACE,
                    JavaSymbolType.ENUM,
                    JavaSymbolType.RECORD,
                }
                symbols = [
                    symbol
                    for symbol in symbols
                    if symbol.containing_class == class_name
                    or (symbol.name == class_name and symbol.symbol_type in type_names)
                    or (
                        symbol.containing_class is not None
                        and symbol.containing_class.startswith(class_name + ".")
                    )
                ]
            if symbol_type:
                symbols = [
                    symbol for symbol in symbols if symbol.symbol_type == symbol_type
                ]
            symbols.sort(
                key=lambda symbol: (
                    symbol.file_path,
                    symbol.line_start,
                    symbol.symbol_type.value,
                    symbol.name,
                )
            )
            selected, total, truncated = self._limit(symbols, limit)
            diagnostics = self._all_diagnostics()
            if normalized_file:
                diagnostics = [
                    diagnostic
                    for diagnostic in diagnostics
                    if diagnostic.file_path == normalized_file
                ]
            return SymbolQueryResult(
                symbols=selected,
                diagnostics=diagnostics,
                total=total,
                truncated=truncated,
            )

    def find_symbol(
        self,
        name: str,
        *,
        symbol_type: JavaSymbolType | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> SymbolQueryResult:
        if not self._built:
            self.build()
        with self._lock:
            normalized_file = (
                self._normalize_file_path(file_path) if file_path else None
            )
            symbols = [symbol for symbol in self._all_symbols() if symbol.name == name]
            if symbol_type:
                symbols = [
                    symbol for symbol in symbols if symbol.symbol_type == symbol_type
                ]
            if normalized_file:
                symbols = [
                    symbol
                    for symbol in symbols
                    if symbol.file_path == normalized_file
                ]
            symbols.sort(
                key=lambda symbol: (
                    symbol.file_path,
                    symbol.line_start,
                    symbol.symbol_type.value,
                )
            )
            selected, total, truncated = self._limit(symbols, limit)
            return SymbolQueryResult(
                symbols=selected,
                diagnostics=self._all_diagnostics(),
                total=total,
                truncated=truncated,
            )

    def find_references(
        self,
        name: str,
        *,
        file_path: str | None = None,
        include_definitions: bool = False,
        limit: int = 200,
    ) -> ReferenceQueryResult:
        if not self._built:
            self.build()
        with self._lock:
            normalized_file = (
                self._normalize_file_path(file_path) if file_path else None
            )
            references = [
                reference
                for reference in self._all_references()
                if reference.name == name
                and (include_definitions or not reference.is_definition)
            ]
            if normalized_file:
                references = [
                    reference
                    for reference in references
                    if reference.file_path == normalized_file
                ]
            references.sort(
                key=lambda reference: (
                    reference.file_path,
                    reference.line_start,
                    reference.column_start,
                )
            )
            selected, total, truncated = self._limit(references, limit)
            return ReferenceQueryResult(
                references=selected,
                diagnostics=self._all_diagnostics(),
                total=total,
                truncated=truncated,
            )
