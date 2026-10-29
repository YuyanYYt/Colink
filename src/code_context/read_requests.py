"""Strict, mode-specific requests for the unified live source reader."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from code_context.policy import validate_path
from code_context.source_access import SourceError

MAX_READ_CHARS = 50_000
MAX_REQUESTED_READ_CHARS = (1 << 53) - 1


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class FileRange(ReadModel):
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    char_offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_range(self):
        validate_path(self.path)
        end = self.end_line if self.end_line is not None else self.start_line + 199
        if end < self.start_line or end - self.start_line >= 1000:
            raise ValueError("read a forward range of at most 1000 lines")
        return self


class FileReadRequest(FileRange):
    mode: Literal["file"]


class SymbolReadRequest(ReadModel):
    mode: Literal["symbol"]
    symbol_id: str | None = Field(default=None, pattern=r"^sym_[0-9a-f]{32}$")
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    qualname: str | None = Field(default=None, min_length=1, max_length=500)
    line_offset: int = Field(default=0, ge=0)
    max_lines: int = Field(default=200, ge=1, le=1000)
    char_offset: int = Field(default=0, ge=0)
    candidate_offset: int = Field(default=0, ge=0, le=1_000_000)
    candidate_limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def valid_selector(self):
        if self.symbol_id is not None:
            if self.path is not None or self.qualname is not None:
                raise ValueError("symbol_id cannot be combined with path or qualname")
        elif self.path is None or self.qualname is None:
            raise ValueError("select symbol_id or exact path + qualname")
        if self.path is not None:
            validate_path(self.path)
        if self.qualname is not None and any(ord(c) < 32 or ord(c) == 127 for c in self.qualname):
            raise ValueError("qualname must be printable")
        return self


class TextSearchRequest(ReadModel):
    mode: Literal["search"]
    kind: Literal["text"]
    query: str = Field(min_length=1, max_length=200)
    path_prefix: str | None = Field(default=None, min_length=1, max_length=1024)
    limit: int = Field(default=50, ge=1, le=200)
    context_lines: int = Field(default=0, ge=0, le=3)
    cursor: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def valid_search(self):
        if "\n" in self.query or "\r" in self.query or "\x00" in self.query:
            raise ValueError("text search requires a single-line query")
        if self.path_prefix is not None:
            validate_path(self.path_prefix.rstrip("/"))
        return self


class SymbolSearchRequest(ReadModel):
    mode: Literal["search"]
    kind: Literal["symbol"]
    query: str = Field(min_length=1, max_length=200)
    exact: bool = False
    symbol_kind: str | None = Field(default=None, min_length=1, max_length=100)
    path_prefix: str | None = Field(default=None, min_length=1, max_length=1024)
    language: Literal["python", "java", "typescript", "javascript"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    context_lines: int = Field(default=0, ge=0, le=3)
    cursor: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def valid_search(self):
        if any(ord(c) < 32 or ord(c) == 127 for c in self.query):
            raise ValueError("symbol query must be printable")
        if self.path_prefix is not None:
            validate_path(self.path_prefix.rstrip("/"))
        return self


SearchReadRequest = Annotated[TextSearchRequest | SymbolSearchRequest, Field(discriminator="kind")]


class BatchPosition(ReadModel):
    item_index: int = Field(ge=0, le=4)
    start_line: int = Field(ge=1)
    char_offset: int = Field(default=0, ge=0)


class BatchReadRequest(ReadModel):
    mode: Literal["batch"]
    items: list[FileRange] = Field(min_length=1, max_length=5)
    cursor: BatchPosition | None = None

    @model_validator(mode="after")
    def valid_cursor(self):
        if self.cursor is not None:
            if self.cursor.item_index >= len(self.items):
                raise ValueError("batch cursor is outside the explicit items")
            item = self.items[self.cursor.item_index]
            end = item.end_line if item.end_line is not None else item.start_line + 199
            if not item.start_line <= self.cursor.start_line <= end:
                raise ValueError("batch cursor is outside the requested range")
            if (
                self.cursor.start_line == item.start_line
                and self.cursor.char_offset < item.char_offset
            ):
                raise ValueError("batch cursor precedes the requested character offset")
        return self


ReadRequest = Annotated[
    FileReadRequest | SymbolReadRequest | SearchReadRequest | BatchReadRequest,
    Field(discriminator="mode"),
]
READ_REQUEST_ADAPTER = TypeAdapter(ReadRequest)


def validate_read_request(request):
    try:
        return READ_REQUEST_ADAPTER.validate_python(request, strict=True)
    except (ValidationError, ValueError, TypeError):
        raise SourceError(
            "INVALID_READ_REQUEST: use a strict file, symbol, text/symbol search or batch request"
        ) from None


def read_budgets(max_chars, max_ms):
    if type(max_chars) is not int or not 1000 <= max_chars <= MAX_READ_CHARS:
        raise SourceError("INVALID_READ_BUDGET: max_chars must be between 1000 and 50000")
    if type(max_ms) is not int or not 100 <= max_ms <= 10_000:
        raise SourceError("INVALID_READ_BUDGET: max_ms must be between 100 and 10000")


def normalize_read_budgets(max_chars, max_ms):
    """Accept an oversized request without expanding the actual response limit."""
    if type(max_chars) is not int or not 1000 <= max_chars <= MAX_REQUESTED_READ_CHARS:
        raise SourceError(
            "INVALID_READ_BUDGET: max_chars must be a JSON-safe integer of at least 1000"
        )
    effective = min(max_chars, MAX_READ_CHARS)
    read_budgets(effective, max_ms)
    adjustment = (
        {"requested_max_chars": max_chars, "effective_max_chars": effective, "adjusted": True}
        if effective != max_chars
        else None
    )
    return effective, adjustment
