"""Selective, bounded in-memory AST reuse for guarded Live Python dataflow.

Only the AST is retained. Source text, symbols, method summaries, request graphs,
call contexts and responses are never cached here. Consumers must read/validate
current source before calling ``parse`` and treat the returned AST as read-only.
Parsing and footprint accounting happen outside the cache lock; concurrent
misses may parse independently rather than introducing a wait or background job.
"""

from __future__ import annotations

import ast
import hashlib
import math
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from code_context.intelligence_models import MAX_FILE_NODES, MAX_PARSE_BYTES

_FILENAME = "<CoLink Python data flow>"
_VERSION = ("python-dataflow-ast-v1", sys.implementation.cache_tag, sys.version_info[:3])


@dataclass(frozen=True)
class _Entry:
    tree: ast.Module
    bytes: int


def _footprint(tree, key, maximum, check):
    """Conservatively count the complete retained Python object graph once.

    Shared interned/operator objects are counted per entry as well. This makes
    the retention budget conservative; it is not a claim about process RSS.
    No object traversal state survives admission.
    """
    pending, seen = [tree, key], set()
    size, nodes, visited = sys.getsizeof(_Entry(tree, 0)), 0, 0
    while pending:
        value = pending.pop()
        if isinstance(value, ast.AST):
            # Count AST occurrences before object deduplication: ast.parse
            # shares operator/context instances, whereas the existing request
            # node budget counts every occurrence in ast.walk.
            nodes += 1
            if nodes > MAX_FILE_NODES:
                return None
        identifier = id(value)
        if identifier in seen:
            continue
        seen.add(identifier)
        visited += 1
        if visited % 256 == 0:
            check()
        size += sys.getsizeof(value)
        if size > maximum:
            return None
        if isinstance(value, ast.AST):
            pending.append(vars(value))
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    check()
    # OrderedDict entries and their scalar/accounting fields need a small,
    # conservative reserve independent of the shared container's baseline.
    size += 512
    return size if size <= maximum else None


class DataflowAstCache:
    """One optional workspace cache, capped across all project/source identities.

    Admission needs both a >=32 KiB source and measured parsing cost >=5 ms by
    default. Small/cheap or oversized trees remain request-local. Hits retain
    every caller's normal source/publication and analysis-budget validation.
    ``clear``/``evict_project`` advance a generation so an in-flight parse cannot
    repopulate a cleared cache. ``close`` also disables future admission.
    """

    def __init__(
        self,
        *,
        max_entries=4,
        max_bytes=32 * 1024 * 1024,
        min_parse_ms=5,
        min_source_bytes=32 * 1024,
    ):
        if (
            type(max_entries) is not int
            or not 1 <= max_entries <= 64
            or type(max_bytes) is not int
            or max_bytes < 1
            or type(min_source_bytes) is not int
            or not 0 <= min_source_bytes <= MAX_PARSE_BYTES
            or isinstance(min_parse_ms, bool)
            or not isinstance(min_parse_ms, (int, float))
            or not math.isfinite(min_parse_ms)
            or min_parse_ms < 0
        ):
            raise ValueError("invalid dataflow AST retention budget")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.min_parse_ms, self.min_source_bytes = min_parse_ms, min_source_bytes
        self._lock = RLock()
        self._entries = OrderedDict()
        self._bytes = 0
        self._generation = 0
        self._closed = False
        self._hits = self._misses = self._admitted = 0

    def parse(
        self,
        *,
        project_id: str,
        source_id: str,
        source_identity: str,
        path: str,
        text: str,
        check: Callable[[], None],
    ) -> ast.Module:
        """Parse exact validated source; cache acceptance never authorizes reads."""
        check()
        raw = text.encode("utf-8")
        if len(raw) > MAX_PARSE_BYTES:
            raise ValueError("dataflow AST source exceeds existing parse budget")
        identity = (project_id, source_id, source_identity, path)
        key = (*identity, hashlib.sha256(raw).hexdigest(), _VERSION)
        source_bytes = len(raw)
        del raw
        with self._lock:
            generation = self._generation
            entry = self._entries.get(key) if not self._closed else None
            if entry is not None:
                self._hits += 1
                self._entries.move_to_end(key)
                tree = entry.tree
            else:
                self._misses += 1
                # Keep only one content version of the same saved source file.
                # Exact hashes isolate concurrently read versions; all versions
                # remain subject to the request's final current-source guard.
                for old_key in list(self._entries):
                    if old_key[:4] == identity:
                        self._bytes -= self._entries.pop(old_key).bytes
                tree = None
        check()
        if tree is not None:
            return tree
        started = time.perf_counter()
        tree = ast.parse(text, filename=_FILENAME)
        parse_ms = (time.perf_counter() - started) * 1000
        check()
        if source_bytes < self.min_source_bytes or parse_ms < self.min_parse_ms:
            return tree
        with self._lock:
            eligible = not self._closed and self._generation == generation
        if not eligible:
            return tree
        try:
            size = _footprint(tree, key, self.max_bytes, check)
        except (MemoryError, RecursionError):
            # An optional retention attempt must not turn valid parsed source
            # into a parse failure. Its request-local tree remains usable.
            return tree
        if size is None:
            return tree
        with self._lock:
            if not self._closed and self._generation == generation:
                existing = self._entries.get(key)
                if existing is None:
                    for old_key in list(self._entries):
                        if old_key[:4] == identity:
                            self._bytes -= self._entries.pop(old_key).bytes
                    while self._entries and (
                        len(self._entries) >= self.max_entries
                        or self._bytes + size > self.max_bytes
                    ):
                        _, oldest = self._entries.popitem(last=False)
                        self._bytes -= oldest.bytes
                    self._entries[key] = _Entry(tree, size)
                    self._bytes += size
                    self._admitted += 1
                else:
                    self._entries.move_to_end(key)
        return tree

    def clear(self):
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._bytes = 0

    def evict_project(self, project_id):
        with self._lock:
            self._generation += 1
            for key in list(self._entries):
                if key[0] == project_id:
                    self._bytes -= self._entries.pop(key).bytes

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self._entries.clear()
            self._bytes = 0

    def snapshot(self):
        """Content-free accounting for tests and local performance diagnostics."""
        with self._lock:
            return {
                "entries": len(self._entries),
                "retained_bytes": self._bytes,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "min_parse_ms": self.min_parse_ms,
                "min_source_bytes": self.min_source_bytes,
                "closed": self._closed,
                "hits": self._hits,
                "misses": self._misses,
                "admitted": self._admitted,
            }
