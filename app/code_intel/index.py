"""Workspace-scoped in-memory Java symbol index."""

import logging
import os
import threading
from pathlib import Path
from typing import TypeVar

from app.code_intel.models import (
    CallQueryResult,
    ImpactAnalysisResult,
    ImpactedCaller,
    JavaCallEdge,
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

    def _all_calls(self) -> list[JavaCallEdge]:
        return [
            call for file_index in self._files.values() for call in file_index.calls
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

    @staticmethod
    def _class_matches(qualifier: str, containing_class: str) -> bool:
        return (
            containing_class == qualifier
            or containing_class.endswith("." + qualifier)
            or containing_class.split(".")[-1] == qualifier.split(".")[-1]
        )

    def _resolve_call(
        self,
        call: JavaCallEdge,
        methods_by_name: dict[str, list[JavaSymbol]],
    ) -> JavaCallEdge:
        all_candidates = methods_by_name.get(call.callee_name, [])
        qualifier = call.callee_qualifier
        candidates = all_candidates
        if qualifier in {None, "this", "super"} and call.containing_class:
            local = [
                method
                for method in all_candidates
                if method.containing_class == call.containing_class
            ]
            if local:
                candidates = local
        elif qualifier and qualifier not in {"this", "super"}:
            class_matches = [
                method
                for method in all_candidates
                if method.containing_class is not None
                and self._class_matches(qualifier, method.containing_class)
            ]
            if class_matches:
                candidates = class_matches

        candidates = sorted(
            candidates,
            key=lambda method: (
                method.file_path,
                method.line_start,
                method.signature,
            ),
        )
        return call.model_copy(
            update={
                "candidates": candidates,
                "ambiguous": len(candidates) > 1,
            }
        )

    @staticmethod
    def _target_matches(
        symbol: JavaSymbol,
        *,
        class_name: str | None = None,
    ) -> bool:
        if class_name is None:
            return True
        if symbol.containing_class is None:
            return False
        return (
            symbol.containing_class == class_name
            or symbol.containing_class.endswith("." + class_name)
        )

    def _method_targets(
        self,
        name: str,
        *,
        class_name: str | None = None,
        file_path: str | None = None,
    ) -> list[JavaSymbol]:
        normalized_file = self._normalize_file_path(file_path) if file_path else None
        methods = [
            symbol
            for symbol in self._all_symbols()
            if symbol.name == name
            and symbol.symbol_type == JavaSymbolType.METHOD
            and self._target_matches(symbol, class_name=class_name)
            and (normalized_file is None or symbol.file_path == normalized_file)
        ]
        methods.sort(
            key=lambda symbol: (
                symbol.file_path,
                symbol.line_start,
                symbol.signature,
            )
        )
        return methods

    @staticmethod
    def _target_key(symbol: JavaSymbol) -> tuple[str, int, str]:
        return (symbol.file_path, symbol.line_start, symbol.signature)

    @staticmethod
    def _candidate_key(symbol: JavaSymbol) -> tuple[str, int, str]:
        return (symbol.file_path, symbol.line_start, symbol.signature)

    def find_callers(
        self,
        method_name: str,
        *,
        class_name: str | None = None,
        file_path: str | None = None,
        limit: int = 200,
    ) -> CallQueryResult:
        if not self._built:
            self.build()
        with self._lock:
            targets = self._method_targets(
                method_name,
                class_name=class_name,
                file_path=file_path,
            )
            if not targets:
                return CallQueryResult(
                    targets=[],
                    calls=[],
                    ambiguous=False,
                    diagnostics=self._all_diagnostics(),
                    total=0,
                    truncated=False,
                )
            target_keys = {self._target_key(target) for target in targets}
            methods_by_name: dict[str, list[JavaSymbol]] = {}
            for symbol in self._all_symbols():
                if symbol.symbol_type == JavaSymbolType.METHOD:
                    methods_by_name.setdefault(symbol.name, []).append(symbol)
            callers: list[JavaCallEdge] = []
            for call in self._all_calls():
                if call.callee_name != method_name or not call.containing_method:
                    continue
                edge = self._resolve_call(call, methods_by_name)
                candidate_keys = {
                    self._candidate_key(candidate)
                    for candidate in edge.candidates
                }
                if candidate_keys & target_keys:
                    callers.append(edge)
            callers.sort(
                key=lambda call: (call.file_path, call.line, call.column)
            )
            selected, total, truncated = self._limit(callers, limit)
            return CallQueryResult(
                targets=targets,
                calls=selected,
                ambiguous=len(targets) > 1 or any(call.ambiguous for call in callers),
                diagnostics=self._all_diagnostics(),
                total=total,
                truncated=truncated,
            )

    def find_callees(
        self,
        method_name: str,
        *,
        class_name: str | None = None,
        file_path: str | None = None,
        limit: int = 200,
    ) -> CallQueryResult:
        if not self._built:
            self.build()
        with self._lock:
            targets = self._method_targets(
                method_name,
                class_name=class_name,
                file_path=file_path,
            )
            methods_by_name: dict[str, list[JavaSymbol]] = {}
            for symbol in self._all_symbols():
                if symbol.symbol_type == JavaSymbolType.METHOD:
                    methods_by_name.setdefault(symbol.name, []).append(symbol)
            calls: list[JavaCallEdge] = []
            for target in targets:
                for call in self._all_calls():
                    if (
                        call.containing_method == method_name
                        and call.file_path == target.file_path
                        and target.line_start <= call.caller_line_start <= target.line_end
                        and call.containing_class == target.containing_class
                    ):
                        calls.append(self._resolve_call(call, methods_by_name))
            calls.sort(
                key=lambda call: (call.file_path, call.line, call.column)
            )
            selected, total, truncated = self._limit(calls, limit)
            return CallQueryResult(
                targets=targets,
                calls=selected,
                ambiguous=len(targets) > 1 or any(call.ambiguous for call in calls),
                diagnostics=self._all_diagnostics(),
                total=total,
                truncated=truncated,
            )

    @staticmethod
    def _is_test_path(file_path: str) -> bool:
        normalized = file_path.replace("\\", "/")
        return "src/test/java/" in normalized

    def analyze_impact(
        self,
        name: str,
        *,
        depth: int = 1,
        limit: int = 200,
    ) -> ImpactAnalysisResult:
        if not self._built:
            self.build()
        depth = min(max(depth, 1), 2)
        with self._lock:
            type_names = {
                JavaSymbolType.CLASS,
                JavaSymbolType.INTERFACE,
                JavaSymbolType.ENUM,
                JavaSymbolType.RECORD,
            }
            type_targets = [
                symbol
                for symbol in self._all_symbols()
                if symbol.name == name and symbol.symbol_type in type_names
            ]
            method_targets = self._method_targets(name)
            mode = "class" if type_targets else "method"
            targets = type_targets if type_targets else method_targets
            if not targets:
                return ImpactAnalysisResult(
                    query=name,
                    mode="unknown",
                    depth=depth,
                    targets=[],
                    ambiguous=False,
                    impacted_callers=[],
                    test_references=[],
                    related_methods=[],
                    diagnostics=self._all_diagnostics(),
                    total=0,
                    truncated=False,
                )

            if mode == "class":
                initial_methods = [
                    symbol
                    for symbol in self._all_symbols()
                    if symbol.symbol_type == JavaSymbolType.METHOD
                    and symbol.containing_class is not None
                    and any(
                        symbol.containing_class == target.name
                        or symbol.containing_class.endswith("." + target.name)
                        for target in type_targets
                    )
                ]
            else:
                initial_methods = method_targets

            queue = list(initial_methods)
            visited_methods: set[tuple[str, int, str]] = set()
            impacted: list[ImpactedCaller] = []
            seen_calls: set[tuple[str, int, int, str]] = set()
            current_depth = 1
            while queue and current_depth <= depth:
                next_methods: list[JavaSymbol] = []
                for method in queue:
                    method_key = self._target_key(method)
                    if method_key in visited_methods:
                        continue
                    visited_methods.add(method_key)
                    callers = self.find_callers(
                        method.name,
                        class_name=method.containing_class,
                        file_path=method.file_path,
                        limit=limit,
                    )
                    for call in callers.calls:
                        key = (
                            call.file_path,
                            call.line,
                            call.column,
                            call.callee_name,
                        )
                        if key in seen_calls:
                            continue
                        seen_calls.add(key)
                        impacted.append(
                            ImpactedCaller(
                                depth=current_depth,
                                in_test=self._is_test_path(call.file_path),
                                call=call,
                            )
                        )
                        if current_depth < depth:
                            for candidate in self._all_symbols():
                                if (
                                    candidate.symbol_type == JavaSymbolType.METHOD
                                    and candidate.name == call.containing_method
                                    and candidate.containing_class == call.containing_class
                                    and candidate.file_path == call.file_path
                                    and candidate.line_start <= call.caller_line_start
                                    <= candidate.line_end
                                ):
                                    next_methods.append(candidate)
                queue = next_methods
                current_depth += 1

            reference_names = {name}
            for target in targets:
                if target.symbol_type == JavaSymbolType.METHOD and target.containing_class:
                    reference_names.add(target.containing_class.split(".")[-1])
            test_refs: list[JavaReference] = []
            seen_refs: set[tuple[str, int, int, str]] = set()
            for reference_name in reference_names:
                result = self.find_references(
                    reference_name,
                    include_definitions=False,
                    limit=limit,
                )
                for reference in result.references:
                    if not self._is_test_path(reference.file_path):
                        continue
                    key = (
                        reference.file_path,
                        reference.line_start,
                        reference.column_start,
                        reference.name,
                    )
                    if key not in seen_refs:
                        seen_refs.add(key)
                        test_refs.append(reference)

            if mode == "method":
                classes = {
                    target.containing_class
                    for target in method_targets
                    if target.containing_class
                }
                target_signatures = {self._target_key(target) for target in method_targets}
                related = [
                    symbol
                    for symbol in self._all_symbols()
                    if symbol.symbol_type == JavaSymbolType.METHOD
                    and symbol.containing_class in classes
                    and self._target_key(symbol) not in target_signatures
                ]
            else:
                related = initial_methods
            related.sort(
                key=lambda symbol: (
                    symbol.file_path,
                    symbol.line_start,
                    symbol.signature,
                )
            )

            impacted.sort(
                key=lambda item: (
                    item.depth,
                    item.call.file_path,
                    item.call.line,
                    item.call.column,
                )
            )
            test_refs.sort(
                key=lambda reference: (
                    reference.file_path,
                    reference.line_start,
                    reference.column_start,
                )
            )
            total = len(impacted) + len(test_refs)
            truncated = (
                len(impacted) > limit
                or len(test_refs) > limit
                or len(related) > limit
            )
            return ImpactAnalysisResult(
                query=name,
                mode=mode,
                depth=depth,
                targets=targets,
                ambiguous=len(targets) > 1
                or any(item.call.ambiguous for item in impacted),
                impacted_callers=impacted[:limit],
                test_references=test_refs[:limit],
                related_methods=related[:limit],
                diagnostics=self._all_diagnostics(),
                total=total,
                truncated=truncated,
            )
