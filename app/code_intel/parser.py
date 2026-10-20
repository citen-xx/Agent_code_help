"""Tree-sitter Java symbol extraction.

The parser is intentionally shallow: it extracts syntactic declarations and
identifier references without javac type resolution or classpath analysis.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from app.code_intel.models import (
    JavaCallEdge,
    JavaParseDiagnostic,
    JavaReference,
    JavaSymbol,
    JavaSymbolType,
)

logger = logging.getLogger(__name__)

_LANGUAGE = Language(tree_sitter_java.language())

_TYPE_DECLARATIONS = {
    "class_declaration": JavaSymbolType.CLASS,
    "interface_declaration": JavaSymbolType.INTERFACE,
    "enum_declaration": JavaSymbolType.ENUM,
    "record_declaration": JavaSymbolType.RECORD,
    "annotation_type_declaration": JavaSymbolType.INTERFACE,
}

_METHOD_DECLARATIONS = {"method_declaration"}
_CONSTRUCTOR_DECLARATIONS = {
    "constructor_declaration",
    "compact_constructor_declaration",
}
_FIELD_DECLARATIONS = {"field_declaration", "constant_declaration"}
_CONTAINER_NODES = {
    "program",
    "class_body",
    "interface_body",
    "enum_body",
    "enum_body_declarations",
}


@dataclass
class ParsedJavaFile:
    symbols: list[JavaSymbol] = field(default_factory=list)
    references: list[JavaReference] = field(default_factory=list)
    calls: list[JavaCallEdge] = field(default_factory=list)
    diagnostics: list[JavaParseDiagnostic] = field(default_factory=list)


def _node_bytes(node: Node, source: bytes) -> bytes:
    return source[node.start_byte : node.end_byte]


def _node_text(node: Node, source: bytes) -> str:
    return _node_bytes(node, source).decode("utf-8", errors="replace")


def _line(node: Node, end: bool = False) -> int:
    point = node.end_point if end else node.start_point
    return point[0] + 1


def _compact(text: str) -> str:
    return " ".join(text.split())


def _signature(node: Node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    end_byte = body.start_byte if body is not None else node.end_byte
    text = source[node.start_byte:end_byte].decode("utf-8", errors="replace")
    return _compact(text.rstrip(";"))


def _first_error(node: Node) -> Node | None:
    if node.type == "ERROR" or node.is_missing:
        return node
    for child in node.named_children:
        error = _first_error(child)
        if error is not None:
            return error
    return None


def _definition_name_node(node: Node) -> Node | None:
    return node.child_by_field_name("name")


def _add_symbol(
    symbols: list[JavaSymbol],
    definition_offsets: set[tuple[int, int]],
    *,
    name_node: Node,
    symbol_type: JavaSymbolType,
    file_path: str,
    signature_node: Node,
    source: bytes,
    containing_class: str | None,
) -> None:
    symbols.append(
        JavaSymbol(
            name=_node_text(name_node, source),
            symbol_type=symbol_type,
            file_path=file_path,
            line_start=_line(signature_node),
            line_end=_line(signature_node, end=True),
            containing_class=containing_class,
            signature=_signature(signature_node, source),
        )
    )
    definition_offsets.add((name_node.start_byte, name_node.end_byte))


def _extract_package_and_imports(
    root: Node,
    source: bytes,
    file_path: str,
    symbols: list[JavaSymbol],
) -> None:
    for child in root.named_children:
        if child.type == "package_declaration":
            name_node = next(
                (
                    node
                    for node in child.named_children
                    if node.type in {"identifier", "scoped_identifier"}
                ),
                None,
            )
            if name_node is not None:
                symbols.append(
                    JavaSymbol(
                        name=_node_text(name_node, source),
                        symbol_type=JavaSymbolType.PACKAGE,
                        file_path=file_path,
                        line_start=_line(child),
                        line_end=_line(child, end=True),
                        signature=_compact(_node_text(child, source)),
                    )
                )
        elif child.type == "import_declaration":
            text = _compact(_node_text(child, source))
            name = text.removeprefix("import").strip().rstrip(";").strip()
            name = name.removeprefix("static").strip()
            symbols.append(
                JavaSymbol(
                    name=name,
                    symbol_type=JavaSymbolType.IMPORT,
                    file_path=file_path,
                    line_start=_line(child),
                    line_end=_line(child, end=True),
                    signature=text,
                )
            )


def _add_field_symbols(
    node: Node,
    source: bytes,
    file_path: str,
    containing_class: str | None,
    symbols: list[JavaSymbol],
    definition_offsets: set[tuple[int, int]],
) -> None:
    declarators = [
        child for child in node.named_children if child.type == "variable_declarator"
    ]
    for declarator in declarators:
        name_node = _definition_name_node(declarator)
        if name_node is not None:
            _add_symbol(
                symbols,
                definition_offsets,
                name_node=name_node,
                symbol_type=JavaSymbolType.FIELD,
                file_path=file_path,
                signature_node=node,
                source=source,
                containing_class=containing_class,
            )


def _add_record_components(
    node: Node,
    source: bytes,
    file_path: str,
    record_name: str,
    symbols: list[JavaSymbol],
    definition_offsets: set[tuple[int, int]],
) -> None:
    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        return
    for parameter in parameters.named_children:
        if parameter.type != "formal_parameter":
            continue
        name_node = _definition_name_node(parameter)
        if name_node is not None:
            _add_symbol(
                symbols,
                definition_offsets,
                name_node=name_node,
                symbol_type=JavaSymbolType.FIELD,
                file_path=file_path,
                signature_node=parameter,
                source=source,
                containing_class=record_name,
            )


def _collect_method_calls(
    node: Node,
    source: bytes,
    file_path: str,
    containing_class: str | None,
    containing_method: str,
    caller_signature: str,
    caller_line_start: int,
    caller_line_end: int,
    calls: list[JavaCallEdge],
) -> None:
    for child in node.named_children:
        if child.type == "method_invocation":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                object_node = child.child_by_field_name("object")
                qualifier = _node_text(object_node, source) if object_node else None
                caller_symbol = (
                    f"{containing_class}.{containing_method}"
                    if containing_class
                    else containing_method
                )
                calls.append(
                    JavaCallEdge(
                        caller_symbol=caller_symbol,
                        callee_name=_node_text(name_node, source),
                        file_path=file_path,
                        line=_line(child),
                        column=child.start_point[1] + 1,
                        containing_class=containing_class,
                        containing_method=containing_method,
                        callee_qualifier=qualifier,
                        caller_signature=caller_signature,
                        caller_line_start=caller_line_start,
                        caller_line_end=caller_line_end,
                    )
                )
        _collect_method_calls(
            child,
            source,
            file_path,
            containing_class,
            containing_method,
            caller_signature,
            caller_line_start,
            caller_line_end,
            calls,
        )


def _walk_declarations(
    node: Node,
    source: bytes,
    file_path: str,
    class_stack: list[str],
    symbols: list[JavaSymbol],
    calls: list[JavaCallEdge],
    definition_offsets: set[tuple[int, int]],
) -> None:
    containing_class = ".".join(class_stack) or None
    for child in node.named_children:
        symbol_type = _TYPE_DECLARATIONS.get(child.type)
        if symbol_type is not None:
            name_node = _definition_name_node(child)
            if name_node is None:
                continue
            name = _node_text(name_node, source)
            _add_symbol(
                symbols,
                definition_offsets,
                name_node=name_node,
                symbol_type=symbol_type,
                file_path=file_path,
                signature_node=child,
                source=source,
                containing_class=containing_class,
            )
            if child.type == "record_declaration":
                _add_record_components(
                    child,
                    source,
                    file_path,
                    ".".join([*class_stack, name]),
                    symbols,
                    definition_offsets,
                )
            body = child.child_by_field_name("body")
            if body is not None:
                _walk_declarations(
                    body,
                    source,
                    file_path,
                    [*class_stack, name],
                    symbols,
                    calls,
                    definition_offsets,
                )
        elif child.type in _METHOD_DECLARATIONS | _CONSTRUCTOR_DECLARATIONS:
            if not class_stack:
                continue
            name_node = _definition_name_node(child)
            if name_node is None:
                continue
            _add_symbol(
                symbols,
                definition_offsets,
                name_node=name_node,
                symbol_type=(
                    JavaSymbolType.METHOD
                    if child.type in _METHOD_DECLARATIONS
                    else JavaSymbolType.CONSTRUCTOR
                ),
                file_path=file_path,
                signature_node=child,
                source=source,
                containing_class=containing_class,
            )
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_method_calls(
                    body,
                    source,
                    file_path,
                    containing_class,
                    _node_text(name_node, source),
                    _signature(child, source),
                    _line(child),
                    _line(child, end=True),
                    calls,
                )
        elif child.type in _FIELD_DECLARATIONS:
            if class_stack:
                _add_field_symbols(
                    child,
                    source,
                    file_path,
                    containing_class,
                    symbols,
                    definition_offsets,
                )
        elif child.type in _CONTAINER_NODES:
            _walk_declarations(
                child,
                source,
                file_path,
                class_stack,
                symbols,
                calls,
                definition_offsets,
            )


def _collect_references(
    root: Node,
    source: bytes,
    source_text: str,
    file_path: str,
    definition_offsets: set[tuple[int, int]],
) -> list[JavaReference]:
    lines = source_text.splitlines()
    references: list[JavaReference] = []

    def visit(node: Node) -> None:
        if node.type in {"identifier", "type_identifier"}:
            name = _node_text(node, source)
            line_index = node.start_point[0]
            context = lines[line_index].strip()[:300] if line_index < len(lines) else ""
            references.append(
                JavaReference(
                    name=name,
                    file_path=file_path,
                    line_start=_line(node),
                    line_end=_line(node, end=True),
                    column_start=node.start_point[1] + 1,
                    column_end=node.end_point[1] + 1,
                    context=context,
                    is_definition=(node.start_byte, node.end_byte)
                    in definition_offsets,
                )
            )
        for child in node.named_children:
            visit(child)

    visit(root)
    return references


def parse_java_source(file_path: str, source: bytes) -> ParsedJavaFile:
    """Parse one Java file and isolate syntax failures to that file."""
    if b"\x00" in source:
        return ParsedJavaFile(
            diagnostics=[
                JavaParseDiagnostic(
                    file_path=file_path,
                    line=1,
                    column=1,
                    message="文件包含 NUL 字节，已跳过 Java 解析",
                )
            ]
        )
    source_text = source.decode("utf-8", errors="replace")
    try:
        tree = Parser(_LANGUAGE).parse(source)
    except Exception as exc:
        return ParsedJavaFile(
            diagnostics=[
                JavaParseDiagnostic(
                    file_path=file_path,
                    line=1,
                    column=1,
                    message=f"Tree-sitter 解析异常: {exc}",
                )
            ]
        )

    root = tree.root_node
    if root.has_error:
        error = _first_error(root) or root
        return ParsedJavaFile(
            diagnostics=[
                JavaParseDiagnostic(
                    file_path=file_path,
                    line=_line(error),
                    column=error.start_point[1] + 1,
                    message=f"Java 语法错误，已跳过文件（节点 {error.type}）",
                )
            ]
        )

    symbols: list[JavaSymbol] = []
    calls: list[JavaCallEdge] = []
    definition_offsets: set[tuple[int, int]] = set()
    _extract_package_and_imports(root, source, file_path, symbols)
    _walk_declarations(
        root,
        source,
        file_path,
        [],
        symbols,
        calls,
        definition_offsets,
    )
    references = _collect_references(
        root,
        source,
        source_text,
        file_path,
        definition_offsets,
    )
    return ParsedJavaFile(symbols=symbols, references=references, calls=calls)


def parse_java_file(repository_path: str | Path, relative_path: str) -> ParsedJavaFile:
    repo = Path(repository_path).resolve()
    path = (repo / relative_path).resolve()
    try:
        path.relative_to(repo)
    except ValueError:
        raise ValueError(f"Java 文件越出 repository: {relative_path}")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"不是有效的 Java 文件: {relative_path}")
    if path.stat().st_size > 2 * 1024 * 1024:
        return ParsedJavaFile(
            diagnostics=[
                JavaParseDiagnostic(
                    file_path=relative_path,
                    line=1,
                    column=1,
                    message="Java 文件超过 2 MiB，已跳过索引",
                )
            ]
        )
    return parse_java_source(relative_path.replace("\\", "/"), path.read_bytes())
