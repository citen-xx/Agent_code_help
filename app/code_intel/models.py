"""Models for Java symbol and reference queries."""

from enum import StrEnum

from pydantic import BaseModel, Field


class JavaSymbolType(StrEnum):
    PACKAGE = "package"
    IMPORT = "import"
    CLASS = "class"
    INTERFACE = "interface"
    ENUM = "enum"
    RECORD = "record"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    FIELD = "field"


class JavaSymbol(BaseModel):
    name: str
    symbol_type: JavaSymbolType
    file_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    containing_class: str | None = None
    signature: str


class JavaReference(BaseModel):
    name: str
    file_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    column_start: int = Field(ge=1)
    column_end: int = Field(ge=1)
    context: str
    is_definition: bool = False


class JavaCallEdge(BaseModel):
    caller_symbol: str
    callee_name: str
    file_path: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    containing_class: str | None = None
    containing_method: str | None = None
    callee_qualifier: str | None = None
    caller_signature: str = ""
    caller_line_start: int = Field(default=1, ge=1)
    caller_line_end: int = Field(default=1, ge=1)
    ambiguous: bool = False
    candidates: list[JavaSymbol] = Field(default_factory=list)


class JavaParseDiagnostic(BaseModel):
    file_path: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    message: str


class SymbolQueryResult(BaseModel):
    symbols: list[JavaSymbol]
    diagnostics: list[JavaParseDiagnostic]
    total: int
    truncated: bool


class ReferenceQueryResult(BaseModel):
    references: list[JavaReference]
    diagnostics: list[JavaParseDiagnostic]
    total: int
    truncated: bool


class CallQueryResult(BaseModel):
    targets: list[JavaSymbol]
    calls: list[JavaCallEdge]
    ambiguous: bool
    diagnostics: list[JavaParseDiagnostic]
    total: int
    truncated: bool


class ImpactedCaller(BaseModel):
    depth: int = Field(ge=1, le=2)
    in_test: bool
    call: JavaCallEdge


class ImpactAnalysisResult(BaseModel):
    query: str
    mode: str
    depth: int
    targets: list[JavaSymbol]
    ambiguous: bool
    impacted_callers: list[ImpactedCaller]
    test_references: list[JavaReference]
    related_methods: list[JavaSymbol]
    diagnostics: list[JavaParseDiagnostic]
    total: int
    truncated: bool
