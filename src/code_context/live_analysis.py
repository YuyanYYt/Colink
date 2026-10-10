"""Request-scoped read adapters for advanced analysis of validated live facts.

Each SELECT is detached under the existing project's lock. Python traversal and
AST work then run without holding the producer lock. A request can only consume
one unchanged publication; source guards still run before and after the request.
"""

from __future__ import annotations

import sqlite3
import time

from code_context.source_access import SourceError


class _Rows:
    def __init__(self, rows):
        self.rows = iter(rows)

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return self.rows


class AnalysisFacts:
    """SELECT-only view of one live publication, with a per-query CPU deadline."""

    source_mode = "live"

    def __init__(self, service, project_id, source_id, manifest, epoch, max_ms):
        if type(max_ms) is not int or not 100 <= max_ms <= 10_000:
            raise ValueError("max_ms must be between 100 and 10000")
        self.service = service
        self.project_id = project_id
        self.source_id = source_id
        self.manifest = manifest
        self.epoch = epoch
        self.deadline = time.monotonic() + max_ms / 1000

    def validate(self):
        """Called with service lock held; this check has no analysis deadline."""
        service = self.service
        if service._closed:
            raise SourceError("INDEX_CLOSED: live index coordinator is closed")
        row = service._cached(self.project_id)
        state = service._states.get(self.project_id, {})
        if (
            row is None
            or row["source_id"] != self.source_id
            or row["manifest"] != self.manifest
            or row["index_version"] != service._index_version
            or state.get("epoch", 0) != self.epoch
            or state.get("status") in {"dirty", "building", "index_not_ready"}
        ):
            raise SourceError("LIVE_INDEX_CHANGED: analysis publication changed")

    def _budget(self):
        if time.monotonic() >= self.deadline:
            from code_context.intelligence_analysis import AnalysisBudgetExceeded

            raise AnalysisBudgetExceeded

    def execute(self, sql, parameters=()):
        # SQL comes from fixed query implementations, never from an MCP input.
        if not sql.lstrip().upper().startswith(("SELECT ", "WITH ", "EXPLAIN ")):
            raise ValueError("analysis facts only support SELECT statements")
        self._budget()
        service = self.service
        with service._lock:
            self.validate()
            self._budget()
            service._db.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)
            try:
                rows = service._db.execute(sql, parameters).fetchall()
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
                    from code_context.intelligence_analysis import AnalysisBudgetExceeded

                    raise AnalysisBudgetExceeded from None
                raise
            finally:
                service._db.set_progress_handler(None, 0)
        self._budget()
        return _Rows(rows)

    def getlimit(self, category):
        with self.service._lock:
            self.validate()
            return self.service._db.getlimit(category)
