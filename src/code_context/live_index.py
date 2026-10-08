"""On-demand, producer-owned facts for authorized live Python/Java sources.

There are no source blobs or historical revisions here. ``revision=1`` in the
fact tables is only an adapter for the existing SELECT-only query functions.
Every publication replaces a project's current facts, bound to its source_id.
The single worker owns parsing, rebinding, publication and derived-cache LRU.

SQLite uses FULL auto-vacuum, a physical page ceiling, DELETE journaling and
memory-only sort/temp storage. Reserve three times the page ceiling for a main
file plus rollback journal/header headroom; no WAL or second on-disk build is
created. Logical parse/index/source budgets are independent of that disk cap.
Guards detect observed changes; they do not provide a filesystem atomic snapshot.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import time
import zlib
from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import TYPE_CHECKING

from code_context.binding_dependencies import declaration_summary, summary_file
from code_context.fact_payloads import FactCacheError, decode_fact
from code_context.intelligence_index import (
    INDEX_VERSION,
    MAX_SNAPSHOT_PARSE_BYTES,
    encode,
    parse_file,
)
from code_context.intelligence_models import PARSER_VERSION, ParsedFile
from code_context.intelligence_queries import QUERIES, _symbol, bound_result
from code_context.intelligence_resolver import Resolver
from code_context.intelligence_roots import PythonSourceRoots, read_root_configuration
from code_context.models import validate_project
from code_context.policy import MAX_FILES
from code_context.read_context import ContextError
from code_context.source_access import SourceAccess, SourceError
from code_context.source_page import source_page

if TYPE_CHECKING:
    from code_context.live import LiveQueries

_APPLICATION_ID = 0x434C4958
_SCHEMA_VERSION = 1
_PAGE_SIZE = 4096
_INDEX_VERSION = f"live-facts-v5-local-{INDEX_VERSION}"
_PARSE_CACHE_VERSION = f"live-packed-v1-{PARSER_VERSION}"
_BINDING_CACHE_VERSION = "binding-reads-v1"
_MAX_BINDING_BYTES = 16 * 1024 * 1024
_CONFIG_PATHS = frozenset({"pyproject.toml", ".gitignore", ".codecontextignore"})
# Disk space is allocated on demand; this is not a process memory ceiling.
MAX_LIVE_DATABASE_BYTES = 500 * 1024 * 1024
# Compressed payload budgets remain separate from physical pages.
MAX_LIVE_INDEX_BYTES = 48 * 1024 * 1024
MAX_LIVE_FACTS = 200_000
_TABLES = ("files", "li_parse_cache", "ci_files", "ci_symbols", "ci_relations", "li_binding_cache")
_BINDING_SCHEMA = """CREATE TABLE IF NOT EXISTS li_binding_cache (
    project_id TEXT NOT NULL, source_id TEXT NOT NULL, path TEXT NOT NULL,
    data BLOB NOT NULL, PRIMARY KEY(project_id, path),
    FOREIGN KEY(project_id, source_id) REFERENCES li_projects(project_id, source_id)
    ON DELETE CASCADE)"""
_SCHEMA = (
    """CREATE TABLE li_projects (
        project_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
        manifest TEXT NOT NULL, index_version TEXT NOT NULL,
        stats TEXT NOT NULL, last_used REAL NOT NULL,
        UNIQUE(project_id, source_id))""",
    """CREATE TABLE files (
        project_id TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision=1), path TEXT NOT NULL,
        sha256 TEXT, size INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        PRIMARY KEY(project_id, revision, path),
        FOREIGN KEY(project_id, source_id) REFERENCES li_projects(project_id, source_id)
        ON DELETE CASCADE)""",
    """CREATE TABLE li_parse_cache (
        project_id TEXT NOT NULL, source_id TEXT NOT NULL, path TEXT NOT NULL,
        sha256 TEXT NOT NULL, parser_version TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(project_id, path),
        FOREIGN KEY(project_id, source_id) REFERENCES li_projects(project_id, source_id)
        ON DELETE CASCADE)""",
    """CREATE TABLE ci_files (
        project_id TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision=1), path TEXT NOT NULL,
        language TEXT NOT NULL, status TEXT NOT NULL, module TEXT NOT NULL,
        diagnostics TEXT NOT NULL, PRIMARY KEY(project_id, revision, path),
        FOREIGN KEY(project_id, source_id) REFERENCES li_projects(project_id, source_id)
        ON DELETE CASCADE)""",
    """CREATE TABLE ci_symbols (
        project_id TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision=1), symbol_id TEXT NOT NULL,
        path TEXT NOT NULL, name TEXT NOT NULL, qualname TEXT NOT NULL, kind TEXT NOT NULL,
        start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(project_id, revision, symbol_id),
        FOREIGN KEY(project_id, source_id) REFERENCES li_projects(project_id, source_id)
        ON DELETE CASCADE)""",
    "CREATE INDEX ci_symbol_name ON ci_symbols(project_id, revision, name)",
    "CREATE INDEX ci_symbol_path ON ci_symbols(project_id, revision, path)",
    """CREATE TABLE ci_relations (
        project_id TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision=1), source_path TEXT NOT NULL,
        source_symbol_id TEXT, target_path TEXT, target_symbol_id TEXT,
        kind TEXT NOT NULL, resolution TEXT NOT NULL, line INTEGER NOT NULL, data TEXT NOT NULL,
        FOREIGN KEY(project_id, source_id) REFERENCES li_projects(project_id, source_id)
        ON DELETE CASCADE)""",
    "CREATE INDEX ci_relation_source ON ci_relations(project_id, revision, source_symbol_id) "
    "WHERE source_symbol_id IS NOT NULL",
    "CREATE INDEX ci_relation_target ON ci_relations(project_id, revision, target_symbol_id) "
    "WHERE target_symbol_id IS NOT NULL",
    "CREATE INDEX ci_relation_file ON ci_relations(project_id, revision, source_path)",
    _BINDING_SCHEMA,
)


_COMPRESSED_RELATION_PREFIX = b"CL1:"
_COMPRESSED_PARSE_PREFIX = b"CP1:"


def _parse_cache_payload(data: str) -> str | bytes:
    """Persist large parsed facts compactly; retain small legacy JSON unchanged."""
    raw = data.encode("utf-8")
    compact = _COMPRESSED_PARSE_PREFIX + zlib.compress(raw, level=1)
    return compact if len(compact) < len(raw) else data


def _parse_cached_file(
    payload: str | bytes, *, max_bytes: int = MAX_SNAPSHOT_PARSE_BYTES
) -> ParsedFile:
    value = decode_fact(payload, prefix=_COMPRESSED_PARSE_PREFIX, max_bytes=max_bytes)
    try:
        return ParsedFile.from_dict(value)
    except (KeyError, TypeError, AttributeError):
        raise FactCacheError("invalid derived parse") from None


def _relation_payload(value: dict) -> str | bytes:
    """Compact live relations without changing the legacy mirror schema."""
    raw = encode(value).encode("utf-8")
    if len(raw) > 65_536:
        return raw.decode("utf-8")
    return _COMPRESSED_RELATION_PREFIX + zlib.compress(raw, level=1)


def _row_budget(row: tuple) -> int:
    """Bound compressed SQLite fact rows without treating bytes as JSON text."""
    if isinstance(row[-1], bytes):
        return len(encode(row[:-1]).encode("utf-8")) + len(row[-1]) + 5
    return len(encode(row).encode("utf-8"))


def _payload_digest(payload):
    if payload is None:
        return None
    if not isinstance(payload, (str, bytes)):
        raise FactCacheError("invalid derived payload type")
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _binding_payload(value):
    return b"BC1:" + zlib.compress(encode(value).encode("utf-8"), level=1)


def _bindings_digest(rows):
    digest = hashlib.sha256()
    for path, payload in sorted(rows):
        digest.update(encode(path).encode("utf-8"))
        digest.update(_payload_digest(payload).encode("ascii"))
    return digest.hexdigest()


def _binding_cached(payload, path, language, cached):
    value = decode_fact(payload, prefix=b"BC1:", max_bytes=MAX_SNAPSHOT_PARSE_BYTES)
    if (
        value.get("version") != _BINDING_CACHE_VERSION
        or value.get("sha256") != cached[0]
        or value.get("parse_digest") != _payload_digest(cached[1])
        or not isinstance(value.get("reads"), dict)
        or any(
            not isinstance(key, str) or not isinstance(digest, str) or len(digest) != 64
            for key, digest in value["reads"].items()
        )
        or any(
            type(value.get(key)) is not int or not 0 <= value[key] <= MAX_LIVE_DATABASE_BYTES
            for key in ("parse_bytes", "parse_facts", "index_bytes", "index_facts")
        )
    ):
        raise FactCacheError("invalid binding cache")
    try:
        parsed = summary_file(value["summary"], path, language)
        # Keep only small accounting values. The raw summary duplicates every
        # declaration, and read sets are needed only if topology changes. Load
        # those one file at a time below, rather than retaining all decoded JSON.
        return {
            key: value[key] for key in ("parse_bytes", "parse_facts", "index_bytes", "index_facts")
        }, parsed
    except (KeyError, TypeError, ValueError, AttributeError):
        raise FactCacheError("invalid binding summary") from None


def _language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return "python" if suffix in {".py", ".pyi"} else "java" if suffix == ".java" else "unsupported"


def _signature(metadata: dict) -> str:
    # Include unsupported file metadata too: file_dependencies accepts their paths.
    value = {
        "files": [
            (item["path"], item["size"], tuple(item["fingerprint"])) for item in metadata["files"]
        ],
        "partial": metadata["partial"],
        "skipped": metadata.get("skipped", {}),
    }
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _limited(path: str, code: str) -> ParsedFile:
    return ParsedFile(
        path, language=_language(path), status="resource_limited", diagnostics=[{"code": code}]
    )


@dataclass
class _Build:
    project_id: str
    source_id: str
    manifest: str
    files: list[tuple]
    parses: list[tuple]
    indexed: list[tuple]
    symbols: list[tuple]
    relations: list[tuple]
    stats: dict
    bindings: list[tuple] = field(default_factory=list)
    base_manifest: str | None = None
    local: bool = False
    file_updates: set[str] = field(default_factory=set)
    parse_updates: set[str] = field(default_factory=set)
    fact_updates: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)


class LiveIndexService:
    """Attach to ``LiveQueries.index_service``; text/overview never call this service.

    ``max_bytes`` bounds only this new database's physical pages. The default
    temporary peak reservation is 3 * max_bytes (rollback journal included).
    A smaller max_peak_bytes conservatively lowers the usable page ceiling.
    Existing mirrors, source trees and recovery data are never opened or evicted.
    Status is metadata only; cached facts require live validation before use.
    """

    def __init__(
        self,
        data_dir: Path,
        max_bytes: int = MAX_LIVE_DATABASE_BYTES,
        max_projects: int = 4,
        wait_seconds: float = 10,
        *,
        max_peak_bytes: int | None = None,
        max_source_bytes: int = MAX_SNAPSHOT_PARSE_BYTES,
        max_parse_bytes: int = MAX_SNAPSHOT_PARSE_BYTES,
        max_index_bytes: int = MAX_LIVE_INDEX_BYTES,
        max_facts: int = MAX_LIVE_FACTS,
        max_files: int = MAX_FILES,
        max_pending_projects: int | None = None,
        max_context_bindings: int = 128,
        manifest_seconds: float = 5,
        executor: ThreadPoolExecutor | None = None,
    ):
        budgets = (
            max_bytes,
            max_projects,
            max_source_bytes,
            max_parse_bytes,
            max_index_bytes,
            max_facts,
            max_files,
            max_context_bindings,
        )
        if any(type(value) is not int or value < 1 for value in budgets):
            raise ValueError("index budgets must be positive integers")
        for value in (wait_seconds, manifest_seconds):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("index time budgets must be finite numbers")
        if wait_seconds < 0 or manifest_seconds <= 0 or max_files > MAX_FILES:
            raise ValueError("invalid index time or file budget")
        max_peak_bytes = 3 * max_bytes if max_peak_bytes is None else max_peak_bytes
        max_pending_projects = (
            max_projects if max_pending_projects is None else max_pending_projects
        )
        if type(max_peak_bytes) is not int or max_peak_bytes < 1:
            raise ValueError("max_peak_bytes must be a positive integer")
        if type(max_pending_projects) is not int or max_pending_projects < 1:
            raise ValueError("max_pending_projects must be a positive integer")
        self.max_bytes, self.max_peak_bytes = max_bytes, max_peak_bytes
        self.max_projects, self.wait_seconds = max_projects, wait_seconds
        self.max_source_bytes, self.max_parse_bytes = max_source_bytes, max_parse_bytes
        self.max_index_bytes, self.max_facts = max_index_bytes, max_facts
        self.max_files, self.manifest_seconds = max_files, manifest_seconds
        self.max_pending_projects, self.max_context_bindings = (
            max_pending_projects,
            max_context_bindings,
        )
        self._index_version = (
            _INDEX_VERSION
            + "-"
            + hashlib.sha256(
                repr(
                    (max_source_bytes, max_parse_bytes, max_index_bytes, max_facts, max_files)
                ).encode()
            ).hexdigest()[:16]
        )
        self._page_limit = min(max_bytes, max_peak_bytes // 3) // _PAGE_SIZE
        self.path = Path(data_dir) / "live-index.sqlite3"
        self._lock = RLock()
        self._closed = False
        self._tasks: dict[str, Future] = {}
        self._states: dict[str, dict] = {}
        self._bindings: dict[tuple[str, str], tuple[object, str, str]] = {}
        self._recent: OrderedDict[str, str] = OrderedDict()
        self._db: sqlite3.Connection | None = None
        self._ownership_fd: int | None = None
        self._storage_error: str | None = None
        self._last_publication: dict = {}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._ownership_fd = os.open(
                self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(self._ownership_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._open_database()
        except (OSError, sqlite3.Error, SourceError):
            if self._db is not None:
                self._db.close()
            if self._ownership_fd is not None:
                os.close(self._ownership_fd)
            raise SourceError(
                "INDEX_STORAGE_UNAVAILABLE: cannot own the dedicated fact cache"
            ) from None
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="colink-live-index"
        )

    def _open_database(self):
        if self.path.is_symlink():
            raise SourceError("INDEX_STORAGE_UNAVAILABLE")
        existing = self.path.exists() and self.path.stat().st_size > 0
        self._db = db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        db.row_factory = sqlite3.Row
        if existing and (
            db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
            or db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION
        ):
            raise SourceError("INDEX_STORAGE_UNAVAILABLE")
        db.execute("PRAGMA page_size=4096")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='li_projects'").fetchone():
            db.execute("PRAGMA auto_vacuum=FULL")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA cache_size=-2048")
        db.execute("PRAGMA foreign_keys=ON")
        if self._page_limit < 1 or db.execute("PRAGMA page_count").fetchone()[0] > self._page_limit:
            self._storage_error = "GLOBAL_STORAGE_BUDGET_EXCEEDED"
            return
        db.execute(f"PRAGMA max_page_count={self._page_limit}")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='li_projects'").fetchone():
            db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            try:
                db.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    db.execute(statement)
                db.commit()
            except sqlite3.OperationalError as exc:
                db.rollback()
                if exc.sqlite_errorcode == sqlite3.SQLITE_FULL:
                    self._storage_error = "GLOBAL_STORAGE_BUDGET_EXCEEDED"
                    return
                raise
        # A failed schema creation leaves an owned, small empty cache on disk.
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='li_projects'").fetchone():
            self._storage_error = "GLOBAL_STORAGE_BUDGET_EXCEEDED"
            return
        # Additive metadata only. Build 19 can still open this cache and safely
        # rebuild its own facts; its parent deletion also removes these rows.
        db.execute(_BINDING_SCHEMA)
        db.commit()
        # NULL endpoints cannot match the symbol equality queries. Keep their
        # evidence rows, but omit those keys from the search indexes. This also
        # upgrades existing derived caches without changing the table schema.
        replacements = []
        for name, column in (
            ("ci_relation_source", "source_symbol_id"),
            ("ci_relation_target", "target_symbol_id"),
        ):
            definition = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            if definition is None or "WHERE" not in definition[0].upper():
                replacements.append((name, column, definition is not None))
        if replacements:
            try:
                db.execute("BEGIN IMMEDIATE")
                for name, column, exists in replacements:
                    if exists:
                        db.execute(f"DROP INDEX {name}")
                    db.execute(
                        f"CREATE INDEX {name} ON ci_relations(project_id, revision, {column}) "
                        f"WHERE {column} IS NOT NULL"
                    )
                db.commit()
            except sqlite3.Error:
                db.rollback()
                raise
        for row in db.execute(
            "SELECT project_id FROM li_projects ORDER BY last_used DESC"
        ).fetchall()[self.max_projects :]:
            db.execute("DELETE FROM li_projects WHERE project_id=?", (row[0],))
        db.commit()

    def _source(self, backend: LiveQueries, project_id: str) -> SourceAccess:
        try:
            validate_project(project_id)
        except (TypeError, ValueError):
            raise SourceError("INVALID_PROJECT: choose an authorized project") from None
        if callable(getattr(backend, "source", None)):
            source = backend.source(project_id)
        else:
            try:
                source = backend.sources[project_id]
            except KeyError:
                raise SourceError("PROJECT_NOT_AUTHORIZED: choose from list_projects") from None
            source.ensure_available()
        return source

    def _manifest(self, source):
        return source.manifest(max_files=self.max_files, max_seconds=self.manifest_seconds)

    def _context(self, backend, source, project_id, handle, *, validate=True):
        try:
            backend.contexts.get(project_id, source.source_id, handle)
            if validate:
                with backend.fingerprint_batch(project_id, source):
                    backend.contexts.validate(
                        project_id, source.source_id, handle, source.fingerprint
                    )
        except ContextError:
            raise SourceError("LIVE_CONTEXT_INVALID: restart analysis from repo_overview") from None

    @staticmethod
    def _observe(backend, source, project_id, handle, path, sha):
        try:
            backend.contexts.observe(project_id, source.source_id, handle, path, sha)
        except ContextError:
            raise SourceError("LIVE_CONTEXT_CHANGED: restart analysis from repo_overview") from None

    def _guard(self, backend, source, project_id, handle, manifest):
        if self._source(backend, project_id).source_id != source.source_id:
            raise SourceError("SOURCE_REPLACED: reauthorize and restart analysis")
        # Validate all observed hashes, then the full inventory. This guard runs
        # before publication and on both sides of every fact query; another
        # inventory walk before the same hash pass adds no freshness guarantee.
        self._context(backend, source, project_id, handle)
        if _signature(self._manifest(source)) != manifest:
            raise SourceError("LIVE_INDEX_CHANGED: project files changed during validation")
        if self._source(backend, project_id).source_id != source.source_id:
            raise SourceError("SOURCE_REPLACED: source changed during validation")

    def _bind_context(self, backend, source, project_id, handle, manifest):
        with self._lock:
            # Remove expired backend handles without substituting a new one for an old one.
            for key, (contexts, source_id, _) in list(self._bindings.items()):
                try:
                    contexts.get(key[0], source_id, key[1])
                except ContextError:
                    del self._bindings[key]
            key, binding = (project_id, handle), (backend.contexts, source.source_id, manifest)
            if key in self._bindings and self._bindings[key] != binding:
                raise SourceError(
                    "LIVE_CONTEXT_CHANGED: structural file set changed; restart analysis"
                )
            if key not in self._bindings and len(self._bindings) >= self.max_context_bindings:
                raise SourceError("INDEX_NOT_READY: structural context metadata capacity exceeded")
            self._bindings[key] = binding

    def _cached(self, project_id):
        if self._storage_error:
            return None
        return self._db.execute(
            "SELECT * FROM li_projects WHERE project_id=?", (project_id,)
        ).fetchone()

    def _remember(self, project_id, reason):
        self._recent[project_id] = reason
        self._recent.move_to_end(project_id)
        while len(self._recent) > self.max_projects:
            self._recent.popitem(last=False)

    def invalidate(self, project_id: str):
        """Watcher/coordinator hint only; no reads, builds or context substitution."""
        with self._lock:
            state = self._states.get(project_id)
            if state is None and not self._closed and self._cached(project_id) is not None:
                state = self._states[project_id] = {"epoch": 0}
            if state is not None:
                state["epoch"] = state.get("epoch", 0) + 1
                state.update(status="dirty", reason="INVALIDATED")

    def status(self, project_id: str) -> dict:
        """Metadata only. A restarted cache is unverified until a structural request."""
        with self._lock:
            row = None if self._closed else self._cached(project_id)
            state = self._states.get(project_id, {})
            stats = json.loads(row["stats"]) if row is not None else {}
            storage_bytes = self.path.stat().st_size if self.path.exists() else 0
            recent_status = (
                "dirty"
                if self._recent.get(project_id) == "SOURCE_OR_CONTEXT_CHANGED"
                else "index_not_ready"
                if self._recent.get(project_id)
                in {"INDEX_BUILD_FAILED", "GLOBAL_STORAGE_BUDGET_EXCEEDED"}
                else "not_requested"
            )
            return {
                "project_id": project_id,
                "source_mode": "live",
                "status": "closed"
                if self._closed
                else state.get("status", "cached" if row else recent_status),
                "index_partial": stats.get("partial", True),
                "requires_validation": True,
                "reason": self._storage_error
                or state.get("reason")
                or self._recent.get(project_id),
                "stats": stats,
                "storage_bytes": storage_bytes,
                "storage": self._storage_details(project_id),
                "limits": self._limits(),
            }

    def _storage_details(self, project_id):
        # Page limits belong to this live connection; a separate read-only
        # diagnostic connection reports its own default max_page_count instead.
        details = {"page_ceiling": self._page_limit, "page_size": _PAGE_SIZE}
        if not self._closed:
            details.update(
                page_count=self._db.execute("PRAGMA page_count").fetchone()[0],
                free_pages=self._db.execute("PRAGMA freelist_count").fetchone()[0],
            )
        if self._last_publication.get("project_id") == project_id:
            details["last_publication"] = dict(self._last_publication)
        return details

    def _limits(self):
        return {
            "database_bytes": self.max_bytes,
            "database_page_ceiling_bytes": self._page_limit * _PAGE_SIZE,
            "temporary_peak_bytes": self.max_peak_bytes,
            "peak_strategy": "DELETE journal; reserve 3x page ceiling; memory temp; no WAL",
            "cached_projects": self.max_projects,
            "pending_projects": self.max_pending_projects,
            "source_bytes_per_project": self.max_source_bytes,
            "parse_bytes_per_project": self.max_parse_bytes,
            "index_bytes_per_project": self.max_index_bytes,
            "facts_per_project": self.max_facts,
            "files_per_project": self.max_files,
            "retained_states": 1,
            "binding_metadata_bytes_per_project": _MAX_BINDING_BYTES,
        }

    def _request(self, backend, source, project_id, handle, metadata, manifest):
        with self._lock:
            if self._closed:
                raise SourceError("INDEX_CLOSED: live index coordinator is closed")
            if self._storage_error:
                return None
            row = self._cached(project_id)
            state = self._states.get(project_id, {})
            matches = (
                row is not None
                and row["source_id"] == source.source_id
                and row["manifest"] == manifest
                and row["index_version"] == self._index_version
            )
            if matches and state.get("status") not in {"dirty", "building", "index_not_ready"}:
                return None
            if project_id in self._tasks:
                # A different inventory/source cannot consume a build already in progress.
                if state.get("manifest") != manifest or state.get("source_id") != source.source_id:
                    self.invalidate(project_id)
                return self._tasks[project_id]
            if len(self._tasks) >= self.max_pending_projects:
                return False
            state = self._states.setdefault(project_id, {"epoch": 0})
            state.update(
                status="building", reason=None, manifest=manifest, source_id=source.source_id
            )
            future = self._executor.submit(
                self._produce,
                backend,
                source,
                project_id,
                handle,
                metadata,
                manifest,
                state["epoch"],
            )
            self._tasks[project_id] = future
            future.add_done_callback(lambda done: self._finished(project_id, done))
            return future

    def _finished(self, project_id, future):
        with self._lock:
            if self._tasks.get(project_id) is future:
                del self._tasks[project_id]
            # Failed/oversized requests must not create an unbounded status cache.
            if not self._closed and self._cached(project_id) is None:
                state = self._states.pop(project_id, {})
                self._remember(project_id, state.get("reason") or "BUILD_CANCELLED")

    def query(
        self,
        backend: LiveQueries,
        project_id: str,
        handle: str,
        operation: str,
        max_chars: int = 20_000,
        **parameters,
    ) -> dict:
        with self._lock:
            if self._closed:
                raise SourceError("INDEX_CLOSED: live index coordinator is closed")
        aliases = {
            "get_file_dependencies": "file_dependencies",
            "get_project_architecture": "project_architecture",
            "get_external_dependencies": "external_dependencies",
            "get_impact_analysis": "impact_analysis",
        }
        if operation in {"get_call_graph", "get_class_graph"}:
            parameters["graph"] = "call" if operation == "get_call_graph" else "class"
            parameters.setdefault(
                "direction", "outgoing" if operation == "get_call_graph" else "both"
            )
            operation = "symbol_graph"
        operation = aliases.get(operation, operation)
        if operation not in QUERIES:
            raise SourceError("INVALID_INDEX_OPERATION: unknown structure query")
        if type(max_chars) is not int or not 1000 <= max_chars <= 50_000:
            raise SourceError("INVALID_RESPONSE_BUDGET: max_chars must be between 1000 and 50000")
        try:
            source = self._source(backend, project_id)
            # LiveQueries already validates the entry context. Direct callers
            # still require a live handle, and complete hash/inventory guards
            # remain before publication and on both sides of the fact query.
            self._context(backend, source, project_id, handle, validate=False)
            metadata = self._manifest(source)
            manifest = _signature(metadata)
            self._bind_context(backend, source, project_id, handle, manifest)
            future = self._request(backend, source, project_id, handle, metadata, manifest)
            if future is False:
                return self._not_ready(project_id, handle, "BUILD_QUEUE_LIMIT")
            if future is not None:
                try:
                    future.result(timeout=self.wait_seconds)
                except TimeoutError:
                    return self._not_ready(project_id, handle, "BUILD_PENDING")
                except CancelledError:
                    raise SourceError("INDEX_CLOSED: structure build cancelled") from None
                except SourceError as exc:
                    if str(exc).startswith("INDEX_NOT_READY:"):
                        return self._not_ready(project_id, handle, "INDEX_BUILD_FAILED")
                    raise
            with self._lock:
                if self._closed:
                    raise SourceError("INDEX_CLOSED: live index coordinator is closed")
                row = self._cached(project_id)
                state = self._states.get(project_id, {})
                if (
                    row is None
                    or row["source_id"] != source.source_id
                    or row["manifest"] != manifest
                    or state.get("status") in {"dirty", "building", "index_not_ready"}
                ):
                    return self._not_ready(
                        project_id,
                        handle,
                        self._storage_error
                        or state.get("reason")
                        or self._recent.get(project_id)
                        or "CACHE_NOT_READY",
                    )
                participants = self._db.execute(
                    "SELECT path, sha256 FROM files WHERE project_id=? AND sha256 IS NOT NULL",
                    (project_id,),
                ).fetchall()
                stats = json.loads(row["stats"])
            for item in participants:
                self._observe(backend, source, project_id, handle, item["path"], item["sha256"])
            self._guard(backend, source, project_id, handle, manifest)
            with self._lock:
                if self._closed:
                    raise SourceError("INDEX_CLOSED: live index coordinator is closed")
                row = self._cached(project_id)
                if (
                    self._closed
                    or row is None
                    or row["source_id"] != source.source_id
                    or row["manifest"] != manifest
                    or self._states.get(project_id, {}).get("status") == "dirty"
                ):
                    return self._not_ready(project_id, handle, "CACHE_INVALIDATED")
                base = {
                    "project_id": project_id,
                    "snapshot": handle,
                    "source_mode": "live",
                    "index_partial": stats["partial"],
                }
                if operation == "read_symbol":
                    result = self._read_symbol(
                        backend, source, project_id, handle, base, max_chars, **parameters
                    )
                else:
                    result = bound_result(
                        {**QUERIES[operation](self._db, project_id, 1, **parameters), **base},
                        max_chars,
                    )
            self._guard(backend, source, project_id, handle, manifest)
            with self._lock:
                if self._closed or self._states.get(project_id, {}).get("status") == "dirty":
                    raise SourceError("LIVE_INDEX_CHANGED: index invalidated during query")
                self._db.execute(
                    "UPDATE li_projects SET last_used=? WHERE project_id=?",
                    (time.time(), project_id),
                )
                self._db.commit()
                self._states.setdefault(project_id, {"epoch": 0}).update(
                    status="ready", reason=None
                )
            return result
        except SourceError:
            self.invalidate(project_id)
            raise
        except FactCacheError:
            with self._lock:
                self.invalidate(project_id)
                try:
                    # Preserve valid parses but prevent reuse of damaged relations,
                    # including after restart. Only this derived project's facts
                    # are rebuilt on the next request.
                    self._db.execute(
                        "UPDATE li_projects SET index_version='' WHERE project_id=?",
                        (project_id,),
                    )
                    self._db.commit()
                except sqlite3.Error:
                    raise SourceError("INDEX_NOT_READY: fact cache needs rebuilding") from None
            return self._not_ready(project_id, handle, "INDEX_CACHE_INVALID")
        except (ValueError, TypeError, AttributeError):
            raise SourceError(
                "INVALID_STRUCTURE_QUERY: check bounds, path and symbol identifier"
            ) from None
        except sqlite3.Error:
            self.invalidate(project_id)
            raise SourceError("INDEX_NOT_READY: fact cache could not serve the query") from None

    def _not_ready(self, project_id, handle, reason):
        with self._lock:
            storage = self._storage_details(project_id)
        return {
            "project_id": project_id,
            "snapshot": handle,
            "source_mode": "live",
            "index_partial": True,
            "index_not_ready": True,
            "index_status": "index_not_ready",
            "reason": reason,
            "storage": storage,
        }

    def _read_symbol(
        self,
        backend,
        source,
        project_id,
        handle,
        base,
        max_chars,
        *,
        symbol_id,
        line_offset=0,
        max_lines=200,
        char_offset=0,
    ):
        if (
            type(line_offset) is not int
            or line_offset < 0
            or type(max_lines) is not int
            or not 1 <= max_lines <= 1000
            or type(char_offset) is not int
            or char_offset < 0
        ):
            raise ValueError("invalid symbol page")
        symbol = _symbol(self._db, project_id, 1, symbol_id)
        indexed = self._db.execute(
            "SELECT sha256, fingerprint FROM files WHERE project_id=? AND path=?",
            (project_id, symbol["path"]),
        ).fetchone()
        document = source.read(symbol["path"])
        if (
            indexed is None
            or document.sha256 != indexed["sha256"]
            or tuple(document.version) != tuple(json.loads(indexed["fingerprint"]))
        ):
            raise SourceError("LIVE_INDEX_CHANGED: symbol source changed; restart analysis")
        self._observe(backend, source, project_id, handle, document.path, document.sha256)
        first = max(1, symbol["start_line"]) + line_offset
        end = min(symbol["end_line"], first + max_lines - 1)
        if first > max(1, symbol["end_line"]):
            raise ValueError("line_offset outside symbol")

        def page(budget):
            result = source_page(document.content, first, end, budget, char_offset, physical=True)
            more = result["content_truncated"] or result["end_line"] < symbol["end_line"]
            following = result["next_start_line"] if more else None
            return {
                **base,
                "symbol": symbol,
                "path": document.path,
                "sha256": document.sha256,
                **result,
                "has_more": more,
                "next_start_line": following,
                "next_char_offset": result["next_char_offset"] if more else None,
                "next_line_offset": following - max(1, symbol["start_line"]) if following else None,
                "source_is_untrusted": True,
            }

        result = page(max_chars)
        if len(json.dumps(result, ensure_ascii=False)) <= max_chars:
            return result
        low, high = 0, len(result["content"])
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(page(middle), ensure_ascii=False)) <= max_chars:
                low = middle
            else:
                high = middle - 1
        if not low:
            raise ValueError("symbol metadata exceeds response budget")
        return page(low)

    def _produce(self, backend, source, project_id, handle, metadata, manifest, epoch):
        try:
            build = self._extract(backend, source, project_id, handle, metadata, manifest)
            self._guard(backend, source, project_id, handle, manifest)
            with self._lock:
                if self._closed or self._states[project_id]["epoch"] != epoch:
                    raise SourceError("LIVE_INDEX_CHANGED: build invalidated before publication")
                if self._source(backend, project_id).source_id != source.source_id:
                    raise SourceError("SOURCE_REPLACED: source changed during build")
                self._publish(build)
        except SourceError:
            with self._lock:
                self._states[project_id].update(status="dirty", reason="SOURCE_OR_CONTEXT_CHANGED")
            raise
        except Exception:
            with self._lock:
                self._states[project_id].update(
                    status="index_not_ready", reason="INDEX_BUILD_FAILED"
                )
            raise SourceError("INDEX_NOT_READY: structure build failed safely") from None

    def _extract(self, backend, source, project_id, handle, metadata, manifest):
        started = time.perf_counter()
        with self._lock:
            old = self._cached(project_id)
            prior = (
                {}
                if old is None or old["source_id"] != source.source_id
                else {
                    row["path"]: (row["sha256"], row["data"])
                    for row in self._db.execute(
                        "SELECT path, sha256, data FROM li_parse_cache "
                        "WHERE project_id=? AND parser_version IN (?, ?)",
                        (project_id, _PARSE_CACHE_VERSION, PARSER_VERSION),
                    )
                }
            )
            old_stats = (
                json.loads(old["stats"])
                if old is not None
                and old["source_id"] == source.source_id
                and old["index_version"] == self._index_version
                else {}
            )
            old_topology_hash = (
                old_stats.get("topology_hash")
                if old_stats.get("relation_reuse_safe", False)
                else None
            )
            prior_bindings = (
                {
                    row["path"]: row["data"]
                    for row in self._db.execute(
                        "SELECT path, data FROM li_binding_cache WHERE project_id=?",
                        (project_id,),
                    )
                }
                if old_stats.get("local_bindings_complete")
                else {}
            )
            try:
                valid_bindings = _bindings_digest(prior_bindings.items()) == old_stats.get(
                    "binding_metadata_digest"
                )
            except FactCacheError:
                valid_bindings = False
            if not valid_bindings:
                prior_bindings = {}
            old_files = (
                {
                    row["path"]: tuple(row)
                    for row in self._db.execute(
                        "SELECT * FROM files WHERE project_id=?", (project_id,)
                    )
                }
                if old_stats
                else {}
            )
        files, parsed_files, parses = [], {}, []
        billing, parse_rows, binding_data = {}, {}, {}
        full_parses = {}
        reused_paths = set()
        # Batch the reusable parse hashes before the extraction loop acquires any
        # index lock. The usual manifest/context guards still run before publish;
        # no Source -> Index / Index -> Source inverse lock is introduced.
        reusable = {
            item["path"] for item in metadata["files"] if _language(item["path"]) != "unsupported"
        }
        with backend.fingerprint_batch(project_id, source):
            for path in list(prior):
                if path not in reusable or source.fingerprint(path) != prior[path][0]:
                    del prior[path]
        source_bytes = parse_bytes = parse_facts = parsed_count = reused_count = file_bytes = 0
        repaired_parse_files = 0
        configuration = read_root_configuration(None)
        partial = bool(metadata["partial"] or metadata.get("skipped"))
        for item in metadata["files"]:
            with self._lock:
                if self._closed:
                    raise SourceError("INDEX_CLOSED: build cancelled")
            path, sha, parsed = item["path"], None, None
            cached = cached_binding = None
            # Metadata consumes the index budget as well. Omitted paths have no facts.
            file_cost = len(
                encode(
                    (
                        project_id,
                        source.source_id,
                        path,
                        item["size"],
                        item["fingerprint"],
                        "0" * 64,
                    )
                ).encode("utf-8")
            )
            if file_bytes + file_cost > self.max_index_bytes:
                partial = True
                if any(
                    entry["path"] == "pyproject.toml" for entry in metadata["files"]
                ) and not any(row[3] == "pyproject.toml" for row in files):
                    configuration = PythonSourceRoots(
                        (), "invalid", "PYTHON_SOURCE_ROOT_CONFIG_UNAVAILABLE"
                    )
                break
            file_bytes += file_cost
            supported = _language(path) != "unsupported"
            if supported or path in _CONFIG_PATHS:
                if source_bytes + item["size"] > self.max_source_bytes:
                    parsed = _limited(path, "PROJECT_SOURCE_BUDGET_EXCEEDED") if supported else None
                    partial = True
                    if path == "pyproject.toml":
                        configuration = PythonSourceRoots(
                            (), "invalid", "PYTHON_SOURCE_ROOT_CONFIG_UNAVAILABLE"
                        )
                else:
                    cached = prior.pop(path, None) if supported else None
                    cached_binding = None
                    if cached is not None:
                        try:
                            if path in prior_bindings:
                                try:
                                    cached_binding, parsed = _binding_cached(
                                        prior_bindings[path], path, _language(path), cached
                                    )
                                except FactCacheError:
                                    parsed = _parse_cached_file(
                                        cached[1], max_bytes=self.max_parse_bytes
                                    )
                            else:
                                parsed = _parse_cached_file(
                                    cached[1], max_bytes=self.max_parse_bytes
                                )
                            if parsed.path != path or parsed.language != _language(path):
                                raise FactCacheError("invalid derived parse identity")
                        except FactCacheError:
                            cached, parsed = None, None
                            repaired_parse_files += 1
                    try:
                        # Every retained prior parse passed the batch's double-stat
                        # and hash validation; changed files still need complete reads.
                        unchanged = cached is not None
                        document = None if unchanged else source.read(path)
                    except SourceError as exc:
                        if not str(exc).startswith("FILE_EXCLUDED:"):
                            raise
                        partial = True
                        parsed = _limited(path, "SOURCE_TEXT_EXCLUDED") if supported else None
                        if path == "pyproject.toml":
                            configuration = PythonSourceRoots(
                                (), "invalid", "PYTHON_SOURCE_ROOT_CONFIG_UNAVAILABLE"
                            )
                    else:
                        if document is not None and (
                            tuple(document.version) != tuple(item["fingerprint"])
                            or document.size != item["size"]
                        ):
                            raise SourceError("LIVE_INDEX_CHANGED: source changed during build")
                        sha = cached[0] if unchanged else document.sha256
                        source_bytes += item["size"]
                        self._observe(backend, source, project_id, handle, path, sha)
                        if path == "pyproject.toml":
                            configuration = read_root_configuration(document.content)
                        if supported:
                            if unchanged:
                                reused_count += 1
                                reused_paths.add(path)
                            else:
                                try:
                                    parsed = parse_file(path, document.content)
                                except Exception:
                                    parsed = _limited(path, "PARSER_FAILED")
                                parsed_count += 1
                                full_parses[path] = parsed
                        del document
            files.append(
                (
                    project_id,
                    source.source_id,
                    1,
                    path,
                    sha,
                    item["size"],
                    encode(item["fingerprint"]),
                )
            )
            if parsed is not None:
                if supported and cached_binding is not None:
                    cost, facts = cached_binding["parse_bytes"], cached_binding["parse_facts"]
                    data = None
                    binding_data[path] = cached_binding
                else:
                    data = encode(parsed.to_dict())
                    cost = len(data.encode("utf-8"))
                    facts = sum(
                        len(getattr(parsed, name))
                        for name in ("symbols", "scopes", "bindings", "imports", "references")
                    )
                    full_parses[path] = parsed
                if (
                    parse_bytes + cost > self.max_parse_bytes
                    or parse_facts + facts > self.max_facts
                ):
                    parsed = _limited(path, "PROJECT_PARSE_BUDGET_EXCEEDED")
                    data = encode(parsed.to_dict())
                    cost, facts = len(data.encode("utf-8")), 0
                    partial = True
                    binding_data.pop(path, None)
                    full_parses[path] = parsed
                billing[path] = (cost, facts)
                if parse_bytes + cost <= self.max_parse_bytes:
                    parse_bytes += cost
                    parse_facts += facts
                    if sha is not None and not any(
                        d.get("code", "").startswith("PROJECT_") for d in parsed.diagnostics
                    ):
                        parse_row = (
                            project_id,
                            source.source_id,
                            path,
                            sha,
                            # Old builds only understand plain JSON with
                            # PARSER_VERSION. A distinct storage marker lets
                            # them skip compressed rows and rebuild safely.
                            _PARSE_CACHE_VERSION,
                            cached[1] if data is None else _parse_cache_payload(data),
                        )
                        parses.append(parse_row)
                        parse_rows[path] = parse_row
                parsed_files[path] = parsed
                partial |= parsed.status != "ready"
        del prior
        resolver = Resolver(parsed_files, configuration, track_dependencies=True)
        excluded_paths = {
            path
            for path, parsed in parsed_files.items()
            if parsed.status == "resource_limited"
            and parsed.diagnostics == [{"code": "SOURCE_TEXT_EXCLUDED"}]
        }
        exclusion_versions = tuple(
            (item["path"], item["fingerprint"])
            for item in metadata["files"]
            if item["path"] in excluded_paths
        )
        topology_hash = hashlib.sha256(
            repr((resolver.topology(), exclusion_versions)).encode("utf-8")
        ).hexdigest()
        reuse_safe = (
            not metadata["partial"]
            and not metadata.get("skipped")
            and not resolver.python_roots.diagnostic
            and all(
                parsed.status == "ready" or path in excluded_paths
                for path, parsed in parsed_files.items()
            )
        )
        reuse_relations = old_topology_hash == topology_hash and reuse_safe
        file_updates = {row[3] for row in files if old_files.get(row[3]) != row}
        removed = set(old_files) - {row[3] for row in files}
        local_candidate = (
            reuse_safe
            and old_stats.get("relation_reuse_safe", False)
            and old_stats.get("local_bindings_complete", False)
            and all(path in binding_data for path in reused_paths)
            and list(resolver.python_source_roots) == old_stats.get("python_source_roots")
            and not (_CONFIG_PATHS & (file_updates | removed))
            and encode(exclusion_versions) == encode(old_stats.get("excluded_versions", []))
        )
        reused_relation_files = 0
        indexed, symbols, relations = [], [], []
        bindings, retained_facts = [], set()
        fact_updates, parse_updates = set(), set()
        binding_bytes = 0
        index_bytes, fact_count = file_bytes, 0
        languages, statuses = {}, {}
        for path, parsed in parsed_files.items():
            cached_info = binding_data.get(path)
            reuse_file = reuse_relations and path in reused_paths and cached_info is not None
            if local_candidate and path in reused_paths and not reuse_relations:
                try:
                    previous_reads = decode_fact(
                        prior_bindings[path], prefix=b"BC1:", max_bytes=MAX_SNAPSHOT_PARSE_BYTES
                    )["reads"]
                    reuse_file = resolver.binding_reads.unchanged(previous_reads)
                    del previous_reads
                except (ValueError, TypeError, KeyError):
                    local_candidate, reuse_file = False, False
            retain = local_candidate and reuse_file
            symbol_rows = (
                []
                if retain
                else [
                    (
                        project_id,
                        source.source_id,
                        1,
                        symbol.id,
                        path,
                        symbol.name,
                        symbol.qualname,
                        symbol.kind,
                        symbol.start_line,
                        symbol.end_line,
                        encode(
                            {**asdict(symbol), "symbol_id": symbol.id, "language": parsed.language}
                        ),
                    )
                    for symbol in parsed.symbols
                ]
            )
            relation_rows = []
            if reuse_file:
                # Immutable file and unchanged project topology retain valid bindings.
                # Load a single file at a time; never hold a second project-wide graph.
                if not retain:
                    with self._lock:
                        old_rows = self._db.execute(
                            "SELECT source_symbol_id, target_path, target_symbol_id, "
                            "kind, resolution, line, data FROM ci_relations "
                            "WHERE project_id=? AND source_path=? ORDER BY rowid",
                            (project_id, path),
                        ).fetchall()
                    relation_rows = [
                        (project_id, source.source_id, 1, path, *tuple(row)) for row in old_rows
                    ]
                reused_relation_files += 1
            else:
                # Unchanged sources use declaration-only inputs until their reads
                # actually changed. Reload just that file's validated parse body.
                if path not in full_parses and path in parse_rows:
                    parsed = _parse_cached_file(
                        parse_rows[path][-1], max_bytes=self.max_parse_bytes
                    )
                    resolver.files[path] = parsed
                    full_parses[path] = parsed
                fact_updates.add(path)
                for relation in resolver.resolve_file(path):
                    relation_rows.append(
                        (
                            project_id,
                            source.source_id,
                            1,
                            path,
                            relation.source_symbol_id,
                            relation.target_path,
                            relation.target_symbol_id,
                            relation.kind,
                            relation.resolution,
                            relation.line,
                            _relation_payload(relation.to_dict()),
                        )
                    )
            indexed_row = (
                project_id,
                source.source_id,
                1,
                path,
                parsed.language,
                parsed.status,
                parsed.module,
                encode({"items": parsed.diagnostics[:20]}),
            )
            cost = (
                cached_info["index_bytes"]
                if retain
                else len(encode(indexed_row).encode("utf-8"))
                + sum(_row_budget(row) for row in [*symbol_rows, *relation_rows])
            )
            count = cached_info["index_facts"] if retain else len(symbol_rows) + len(relation_rows)
            status, diagnostics = parsed.status, parsed.diagnostics
            accepted = (
                index_bytes + cost <= self.max_index_bytes and fact_count + count <= self.max_facts
            )
            if not accepted:
                reuse_safe = False
                status, diagnostics, partial = (
                    "resource_limited",
                    [{"code": "PROJECT_INDEX_BUDGET_EXCEEDED"}],
                    True,
                )
            else:
                symbols.extend(symbol_rows)
                relations.extend(relation_rows)
                if retain:
                    retained_facts.add(path)
                index_bytes += cost
                fact_count += count
            languages[parsed.language] = languages.get(parsed.language, 0) + 1
            statuses[status] = statuses.get(status, 0) + 1
            indexed_row = (
                project_id,
                source.source_id,
                1,
                path,
                parsed.language,
                status,
                parsed.module,
                encode({"items": diagnostics[:20]}),
            )
            metadata_cost = len(encode(indexed_row).encode("utf-8"))
            if accepted:
                indexed.append(indexed_row)  # Already charged along with this file's facts.
            elif index_bytes + metadata_cost <= self.max_index_bytes:
                index_bytes += metadata_cost
                indexed.append(indexed_row)
            # Incomplete/resource-limited builds keep their original full-build
            # semantics. Auxiliary metadata is capped independently inside the
            # same physical database; exhausting it disables local optimization.
            parse_row = parse_rows.get(path)
            if reuse_safe:
                if reuse_file and cached_info:
                    payload = prior_bindings[path]
                else:
                    payload = _binding_payload(
                        {
                            "version": _BINDING_CACHE_VERSION,
                            "summary": declaration_summary(parsed),
                            "reads": resolver.binding_reads.files.pop(path, {}),
                            "sha256": parse_row[3] if parse_row else None,
                            "parse_digest": _payload_digest(parse_row[-1]) if parse_row else None,
                            "parse_bytes": billing[path][0],
                            "parse_facts": billing[path][1],
                            "index_bytes": cost,
                            "index_facts": count,
                        }
                    )
                binding_bytes += len(payload)
                bindings.append((project_id, source.source_id, path, payload))
            if path not in reused_paths:
                parse_updates.add(path)
        local_complete = (
            reuse_safe
            and len(bindings) == len(parsed_files)
            and binding_bytes <= _MAX_BINDING_BYTES
        )
        local = bool(local_candidate and local_complete)
        if not local_complete:
            bindings = []
        if not local:
            # Full fallback still needs the untouched facts that a local build
            # retained in-place. Read them only on this uncommon fallback path.
            with self._lock:
                for path in sorted(retained_facts):
                    symbols.extend(
                        tuple(row)
                        for row in self._db.execute(
                            "SELECT * FROM ci_symbols WHERE project_id=? AND path=? ORDER BY rowid",
                            (project_id, path),
                        )
                    )
                    relations.extend(
                        tuple(row)
                        for row in self._db.execute(
                            "SELECT * FROM ci_relations WHERE project_id=? AND source_path=? "
                            "ORDER BY rowid",
                            (project_id, path),
                        )
                    )
        stats = {
            "partial": partial or bool(resolver.python_roots.diagnostic),
            "supported_languages": ["python", "java"],
            "files_by_language": languages,
            "files_by_status": statuses,
            "python_source_roots": list(resolver.python_source_roots),
            "python_source_roots_mode": resolver.python_roots.mode,
            "python_source_roots_diagnostics": [{"code": resolver.python_roots.diagnostic}]
            if resolver.python_roots.diagnostic
            else [],
            "parsed_files": parsed_count,
            "reused_parse_files": reused_count,
            "repaired_parse_files": repaired_parse_files,
            "resolved_files": len(parsed_files),
            "reused_relation_files": reused_relation_files,
            "rebound_files": len(parsed_files) - reused_relation_files,
            "relation_reuse_safe": reuse_safe,
            "topology_hash": topology_hash,
            "excluded_versions": exclusion_versions,
            "local_bindings_complete": local_complete,
            "binding_metadata_digest": _bindings_digest((row[2], row[3]) for row in bindings)
            if local_complete
            else None,
            "binding_metadata_bytes": binding_bytes if local_complete else 0,
            "publication_mode": "local" if local else "full",
            "published_fact_files": len(fact_updates) if local else len(parsed_files),
            "published_metadata_files": len(file_updates) if local else len(files),
            "source_bytes": source_bytes,
            "parse_payload_bytes": parse_bytes,
            "index_payload_bytes": index_bytes,
            "parse_fact_count": parse_facts,
            "fact_count": fact_count,
            "file_count": len(files),
            "metadata_payload_bytes": file_bytes,
            "discovery_partial": metadata["partial"],
            "build_ms": round((time.perf_counter() - started) * 1000, 3),
            "analysis": "static; unresolved targets are not runtime facts",
        }
        return _Build(
            project_id,
            source.source_id,
            manifest,
            files,
            parses,
            indexed,
            symbols,
            relations,
            stats,
            bindings,
            old["manifest"] if old else None,
            local,
            file_updates,
            parse_updates,
            fact_updates,
            removed,
        )

    def _publish(self, build):
        # Retry an atomic replacement with successively more LRU victims. SQLITE_FULL
        # rolls back the entire transaction, including attempted evictions.
        db = self._db
        if build.local:
            base = self._cached(build.project_id)
            if (
                base is None
                or base["manifest"] != build.base_manifest
                or base["source_id"] != build.source_id
                or base["index_version"] != self._index_version
            ):
                raise SourceError("LIVE_INDEX_CHANGED: local publication base changed")
        victims = [
            row[0]
            for row in db.execute(
                "SELECT project_id FROM li_projects WHERE project_id<>? "
                "ORDER BY last_used, project_id",
                (build.project_id,),
            )
        ]
        minimum = max(0, len(victims) + 1 - self.max_projects)
        for count in range(minimum, len(victims) + 1):
            self._last_publication = {
                "project_id": build.project_id,
                "phase": "li_projects",
                "complete": False,
                "completed_table_peak_pages": db.execute("PRAGMA page_count").fetchone()[0],
                "project_id_bytes": len(build.project_id.encode("utf-8")),
                "fact_count": build.stats["fact_count"],
                "index_payload_bytes": build.stats["index_payload_bytes"],
                "parse_payload_bytes": build.stats["parse_payload_bytes"],
                "mode": "local" if build.local else "full",
            }
            try:
                db.execute("BEGIN IMMEDIATE")
                for project_id in victims[:count]:
                    db.execute("DELETE FROM li_projects WHERE project_id=?", (project_id,))
                if build.local:
                    db.execute(
                        "UPDATE li_projects SET manifest=?, index_version=?, stats=?, last_used=? "
                        "WHERE project_id=? AND source_id=?",
                        (
                            build.manifest,
                            self._index_version,
                            encode(build.stats),
                            time.time(),
                            build.project_id,
                            build.source_id,
                        ),
                    )
                else:
                    db.execute("DELETE FROM li_projects WHERE project_id=?", (build.project_id,))
                    db.execute(
                        "INSERT INTO li_projects VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            build.project_id,
                            build.source_id,
                            build.manifest,
                            self._index_version,
                            encode(build.stats),
                            time.time(),
                        ),
                    )
                self._last_publication["inserted_rows"] = {}
                local_paths = (
                    build.file_updates,
                    build.parse_updates,
                    build.parse_updates,
                    build.parse_updates,
                    build.fact_updates,
                    build.fact_updates,
                )
                path_columns = ("path", "path", "path", "path", "source_path", "path")
                path_indexes = (3, 2, 3, 4, 3, 2)
                for table, rows in zip(
                    _TABLES,
                    (
                        build.files,
                        build.parses,
                        build.indexed,
                        build.symbols,
                        build.relations,
                        build.bindings,
                    ),
                    strict=True,
                ):
                    self._last_publication["phase"] = table
                    if build.local:
                        position = _TABLES.index(table)
                        paths = local_paths[position]
                        db.executemany(
                            f"DELETE FROM {table} WHERE project_id=? "
                            f"AND {path_columns[position]}=?",
                            ((build.project_id, path) for path in sorted(paths | build.removed)),
                        )
                        rows = [row for row in rows if row[path_indexes[position]] in paths]
                    self._last_publication["inserted_rows"][table] = len(rows)
                    if rows:
                        db.executemany(
                            f"INSERT INTO {table} VALUES({','.join('?' for _ in rows[0])})", rows
                        )
                    self._last_publication["completed_table_peak_pages"] = max(
                        self._last_publication["completed_table_peak_pages"],
                        db.execute("PRAGMA page_count").fetchone()[0],
                    )
                db.commit()
            except sqlite3.OperationalError as exc:
                db.rollback()
                if exc.sqlite_errorcode != sqlite3.SQLITE_FULL:
                    raise
            except Exception:
                db.rollback()
                raise
            else:
                self._last_publication.update(phase="committed", complete=True)
                for project_id in victims[:count]:
                    if project_id not in self._tasks:
                        self._states.pop(project_id, None)
                    self._remember(project_id, "LRU_EVICTED")
                self._states[build.project_id].update(status="ready", reason=None)
                return
        self._states[build.project_id].update(
            status="index_not_ready", reason="GLOBAL_STORAGE_BUDGET_EXCEEDED"
        )

    def close(self):
        """Stop production and close the owned cache; never delete artifacts."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = list(self._tasks.values())
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        else:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    task.result()
                except Exception:
                    pass  # The build's existing structured failure is retained.
        with self._lock:
            self._db.close()
            self._bindings.clear()
            fcntl.flock(self._ownership_fd, fcntl.LOCK_UN)
            os.close(self._ownership_fd)
