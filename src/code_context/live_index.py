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
from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import TYPE_CHECKING

from code_context.intelligence_index import (
    INDEX_VERSION,
    MAX_SNAPSHOT_FACTS,
    MAX_SNAPSHOT_INDEX_BYTES,
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
_INDEX_VERSION = f"live-facts-v1-{INDEX_VERSION}"
_CONFIG_PATHS = frozenset({"pyproject.toml", ".gitignore", ".codecontextignore"})
_TABLES = ("files", "li_parse_cache", "ci_files", "ci_symbols", "ci_relations")
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
    "CREATE INDEX ci_relation_source ON ci_relations(project_id, revision, source_symbol_id)",
    "CREATE INDEX ci_relation_target ON ci_relations(project_id, revision, target_symbol_id)",
    "CREATE INDEX ci_relation_file ON ci_relations(project_id, revision, source_path)",
)


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
        max_bytes: int = 64 * 1024 * 1024,
        max_projects: int = 4,
        wait_seconds: float = 10,
        *,
        max_peak_bytes: int | None = None,
        max_source_bytes: int = MAX_SNAPSHOT_PARSE_BYTES,
        max_parse_bytes: int = MAX_SNAPSHOT_PARSE_BYTES,
        max_index_bytes: int = MAX_SNAPSHOT_INDEX_BYTES,
        max_facts: int = MAX_SNAPSHOT_FACTS,
        max_files: int = MAX_FILES,
        max_pending_projects: int | None = None,
        max_context_bindings: int = 128,
        manifest_seconds: float = 5,
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
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="colink-live-index")

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
        if _signature(self._manifest(source)) != manifest:
            raise SourceError("LIVE_INDEX_CHANGED: project files changed; restart analysis")
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
                "limits": self._limits(),
            }

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
            self._context(backend, source, project_id, handle)
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
        except (ValueError, TypeError, AttributeError):
            raise SourceError(
                "INVALID_STRUCTURE_QUERY: check bounds, path and symbol identifier"
            ) from None
        except sqlite3.Error:
            self.invalidate(project_id)
            raise SourceError("INDEX_NOT_READY: fact cache could not serve the query") from None

    @staticmethod
    def _not_ready(project_id, handle, reason):
        return {
            "project_id": project_id,
            "snapshot": handle,
            "source_mode": "live",
            "index_partial": True,
            "index_not_ready": True,
            "index_status": "index_not_ready",
            "reason": reason,
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
                        "WHERE project_id=? AND parser_version=?",
                        (project_id, PARSER_VERSION),
                    )
                }
            )
        files, parsed_files, parses = [], {}, []
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
        configuration = read_root_configuration(None)
        partial = bool(metadata["partial"] or metadata.get("skipped"))
        for item in metadata["files"]:
            with self._lock:
                if self._closed:
                    raise SourceError("INDEX_CLOSED: build cancelled")
            path, sha, parsed = item["path"], None, None
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
                                parsed = ParsedFile.from_dict(json.loads(cached[1]))
                                reused_count += 1
                            else:
                                try:
                                    parsed = parse_file(path, document.content)
                                except Exception:
                                    parsed = _limited(path, "PARSER_FAILED")
                                parsed_count += 1
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
                data = encode(parsed.to_dict())
                cost = len(data.encode("utf-8"))
                facts = sum(
                    len(getattr(parsed, name))
                    for name in ("symbols", "scopes", "bindings", "imports", "references")
                )
                if (
                    parse_bytes + cost > self.max_parse_bytes
                    or parse_facts + facts > self.max_facts
                ):
                    parsed = _limited(path, "PROJECT_PARSE_BUDGET_EXCEEDED")
                    data = encode(parsed.to_dict())
                    cost, facts = len(data.encode("utf-8")), 0
                    partial = True
                if parse_bytes + cost <= self.max_parse_bytes:
                    parse_bytes += cost
                    parse_facts += facts
                    if sha is not None and not any(
                        d.get("code", "").startswith("PROJECT_") for d in parsed.diagnostics
                    ):
                        parses.append(
                            (project_id, source.source_id, path, sha, PARSER_VERSION, data)
                        )
                parsed_files[path] = parsed
                partial |= parsed.status != "ready"
        del prior
        resolver = Resolver(parsed_files, configuration)
        indexed, symbols, relations = [], [], []
        index_bytes, fact_count = file_bytes, 0
        languages, statuses = {}, {}
        for path, parsed in parsed_files.items():
            symbol_rows = [
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
                    encode({**asdict(symbol), "symbol_id": symbol.id, "language": parsed.language}),
                )
                for symbol in parsed.symbols
            ]
            relation_rows = []
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
                        encode(relation.to_dict()),
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
            cost = len(encode(indexed_row).encode("utf-8")) + sum(
                len(encode(row).encode("utf-8")) for row in [*symbol_rows, *relation_rows]
            )
            count = len(symbol_rows) + len(relation_rows)
            status, diagnostics = parsed.status, parsed.diagnostics
            accepted = (
                index_bytes + cost <= self.max_index_bytes and fact_count + count <= self.max_facts
            )
            if not accepted:
                status, diagnostics, partial = (
                    "resource_limited",
                    [{"code": "PROJECT_INDEX_BUDGET_EXCEEDED"}],
                    True,
                )
            else:
                symbols.extend(symbol_rows)
                relations.extend(relation_rows)
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
            "resolved_files": len(parsed_files),
            "reused_relation_files": 0,
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
        )

    def _publish(self, build):
        # Retry an atomic replacement with successively more LRU victims. SQLITE_FULL
        # rolls back the entire transaction, including attempted evictions.
        db = self._db
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
            try:
                db.execute("BEGIN IMMEDIATE")
                for project_id in [build.project_id, *victims[:count]]:
                    db.execute("DELETE FROM li_projects WHERE project_id=?", (project_id,))
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
                for table, rows in zip(
                    _TABLES,
                    (build.files, build.parses, build.indexed, build.symbols, build.relations),
                    strict=True,
                ):
                    if rows:
                        db.executemany(
                            f"INSERT INTO {table} VALUES({','.join('?' for _ in rows[0])})", rows
                        )
                db.commit()
            except sqlite3.OperationalError as exc:
                db.rollback()
                if exc.sqlite_errorcode != sqlite3.SQLITE_FULL:
                    raise
            else:
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
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._db.close()
            self._bindings.clear()
            fcntl.flock(self._ownership_fd, fcntl.LOCK_UN)
            os.close(self._ownership_fd)
