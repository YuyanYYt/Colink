"""Small, source-free facts shared by the Python/Java extractors and resolver.

No module is imported or executed from an indexed project. Names and locations
are evidence, not an assertion that a dynamic runtime target has been proven.
"""

import hashlib
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

PARSER_VERSION = f"facts-v3-py{sys.version_info.major}.{sys.version_info.minor}-java023"
MAX_FILE_SYMBOLS = 5_000
MAX_FILE_REFERENCES = 20_000
MAX_FILE_NODES = 250_000
MAX_PARSE_BYTES = 4 * 1024 * 1024
CLASS_KINDS = frozenset({"class", "interface", "enum", "record", "annotation"})
CALLABLE_KINDS = frozenset({"function", "method", "constructor"})
RELATION_KINDS = frozenset(
    {
        "IMPORT",
        "REFERENCE",
        "CALL",
        "TYPE_REFERENCE",
        "INHERITANCE",
        "INSTANTIATION",
        "DECORATOR",
        "CONTAINS",
        "IMPLEMENTS",
    }
)


def symbol_id(path: str, qualname: str, line: int) -> str:
    key = f"{path}\0{qualname}\0{line}".encode()
    return "sym_" + hashlib.sha256(key).hexdigest()[:32]


def module_id(path: str) -> str:
    return symbol_id(path, "", 0)


@dataclass
class Symbol:
    path: str
    name: str
    qualname: str
    kind: str
    start_line: int
    end_line: int
    scope: str = ""
    column: int = 1
    signature: str = ""
    parameters: list[str] = field(default_factory=list)
    return_type: str = ""

    @property
    def id(self) -> str:
        return symbol_id(self.path, self.qualname, self.start_line)


@dataclass
class Scope:
    qualname: str
    kind: str
    parent: str = ""
    globals: list[str] = field(default_factory=list)
    nonlocals: list[str] = field(default_factory=list)


@dataclass
class Binding:
    name: str
    scope: str
    kind: str
    line: int
    annotation: str = ""
    value: str = ""


@dataclass
class ImportBinding:
    module: str
    imported_name: str
    local_name: str
    line: int
    scope: str = ""
    level: int = 0
    type_only: bool = False
    conditional: bool = False
    is_static: bool = False


@dataclass
class Reference:
    name: str
    kind: str
    line: int
    scope: str = ""
    source_qualname: str = ""
    column: int = 1
    type_only: bool = False
    conditional: bool = False
    arity: int | None = None


@dataclass
class ParsedFile:
    path: str
    language: str = "python"
    status: str = "ready"
    module: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    scopes: list[Scope] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)
    imports: list[ImportBinding] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "ParsedFile":
        return cls(
            path=value["path"],
            language=value["language"],
            status=value["status"],
            module=value.get("module", ""),
            symbols=[Symbol(**s) for s in value.get("symbols", [])],
            scopes=[Scope(**s) for s in value.get("scopes", [])],
            bindings=[Binding(**b) for b in value.get("bindings", [])],
            imports=[ImportBinding(**i) for i in value.get("imports", [])],
            references=[Reference(**r) for r in value.get("references", [])],
            diagnostics=value.get("diagnostics", []),
        )


@dataclass
class Relation:
    source_path: str
    kind: str
    name: str
    line: int
    source_symbol_id: str | None = None
    source_qualname: str = ""
    column: int = 1
    target_path: str | None = None
    target_symbol_id: str | None = None
    target_qualname: str | None = None
    target_module: str | None = None
    resolution: str = "unresolved"
    evidence: str = "unknown"
    type_only: bool = False
    conditional: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
