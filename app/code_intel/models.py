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
