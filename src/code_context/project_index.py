"""One current fact database per project, with bounded open connections.

Old shared caches are preserved. A project is rebuilt lazily from authorized
source on first use. Closing an LRU connection does not delete its database.
All projects share one build worker; disk allowance is not a memory limit.
"""

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Condition

from code_context.intelligence_index import MAX_SNAPSHOT_PARSE_BYTES
from code_context.live_index import (
    _APPLICATION_ID,
    _SCHEMA_VERSION,
    MAX_LIVE_DATABASE_BYTES,
    MAX_LIVE_FACTS,
    MAX_LIVE_INDEX_BYTES,
    LiveIndexService,
)
from code_context.local_control import private_directory
from code_context.models import validate_project
from code_context.policy import MAX_FILES
from code_context.source_access import SourceError


class ProjectIndexService:
    def __init__(
        self,
        data_dir: Path,
        *,
        max_open_projects=4,
        max_bytes=MAX_LIVE_DATABASE_BYTES,
        max_peak_bytes=None,
        wait_seconds=10,
    ):
        if type(max_open_projects) is not int or not 1 <= max_open_projects <= 64:
            raise ValueError("invalid open project limit")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("invalid project disk budget")
        peak = 3 * max_bytes if max_peak_bytes is None else max_peak_bytes
        if type(peak) is not int or peak < 1:
            raise ValueError("invalid temporary disk budget")
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or not math.isfinite(wait_seconds)
            or wait_seconds < 0
        ):
            raise ValueError("invalid structure wait budget")
        self.root = private_directory(Path(data_dir)).root
        self.max_open_projects = max_open_projects
        self.wait_seconds = wait_seconds
        self._options = dict(max_bytes=max_bytes, max_peak_bytes=peak, wait_seconds=wait_seconds)
        self._condition = Condition()
        self._entries = OrderedDict()
        self._closed = False
        self._ownership = os.open(
            self.root / "project-index.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(self._ownership)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise OSError("unsafe index owner")
            fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._ownership)
            raise SourceError("INDEX_STORAGE_UNAVAILABLE: cannot own project indexes") from None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="colink-project-index"
        )

    def path_for(self, project_id):
        try:
            validate_project(project_id)
        except (ValueError, TypeError):
            raise SourceError("INVALID_PROJECT: choose an authorized project") from None
        key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return self.root / "projects" / key / "live-index.sqlite3"

    @contextmanager
    def _lease(self, project_id, *, create=False):
        victim = None
        with self._condition:
            if self._closed:
                raise SourceError("INDEX_CLOSED: project index coordinator is closed")
            entry = self._entries.get(project_id)
            if entry is None and create:
                if len(self._entries) >= self.max_open_projects:
                    for key, candidate in self._entries.items():
                        service = candidate["service"]
                        # With no leased callers no new task can be submitted.
                        # Existing callbacks only remove completed tasks. Never
                        # acquire a service/source lock while holding pool locks.
                        if candidate["users"] == 0 and service is not None and not service._tasks:
                            victim = self._entries.pop(key)["service"]
                            break
                    else:
                        entry = None
                if len(self._entries) < self.max_open_projects:
                    entry = self._entries[project_id] = {"service": None, "users": 0}
            if entry is not None and (
                entry["service"] is not None or create and entry["users"] == 0
            ):
                entry["users"] += 1
                self._entries.move_to_end(project_id)
            else:
                entry = None
        if entry is None:
            yield None
            return
        try:
            if victim is not None:
                victim.close()
            if entry["service"] is None:
                directory = private_directory(self.path_for(project_id).parent)
                entry["service"] = LiveIndexService(
                    directory.root,
                    max_projects=1,
                    max_pending_projects=1,
                    executor=self._executor,
                    **self._options,
                )
            yield entry["service"]
        finally:
            with self._condition:
                entry["users"] -= 1
                if entry["service"] is None:
                    self._entries.pop(project_id, None)
                self._condition.notify_all()

    def query(self, backend, project_id, handle, operation, **parameters):
        # Refuse before creating a project cache. The delegated query validates
        # authorization again and retains its complete pre/post source guards.
        backend.source(project_id)
        self.path_for(project_id)
        with self._lease(project_id, create=True) as service:
            if service is None:
                return dict(
                    project_id=project_id,
                    snapshot=handle,
                    source_mode="live",
                    index_partial=True,
                    index_not_ready=True,
                    index_status="index_not_ready",
                    reason="BUILD_QUEUE_LIMIT",
                )
            return service.query(backend, project_id, handle, operation, **parameters)

    def invalidate(self, project_id):
        self.path_for(project_id)
        with self._condition:
            if self._closed:
                return
        with self._lease(project_id) as service:
            if service is not None:
                service.invalidate(project_id)

    def _limits(self):
        main, peak = self._options["max_bytes"], self._options["max_peak_bytes"]
        return dict(
            database_bytes=main,
            database_bytes_per_project=main,
            database_page_ceiling_bytes=min(main, peak // 3) // 4096 * 4096,
            temporary_peak_bytes=peak,
            cached_projects=1,
            pending_projects=1,
            open_project_limit=self.max_open_projects,
            storage_layout="one_database_per_project",
            retained_states=1,
            peak_strategy="DELETE journal; reserve 3x page ceiling; memory temp; no WAL",
            source_bytes_per_project=MAX_SNAPSHOT_PARSE_BYTES,
            parse_bytes_per_project=MAX_SNAPSHOT_PARSE_BYTES,
            index_bytes_per_project=MAX_LIVE_INDEX_BYTES,
            facts_per_project=MAX_LIVE_FACTS,
            files_per_project=MAX_FILES,
        )

    def status(self, project_id):
        path = self.path_for(project_id)
        with self._condition:
            closed = self._closed
        if not closed:
            with self._lease(project_id) as service:
                if service is not None:
                    return {**service.status(project_id), "limits": self._limits()}
        result = dict(
            project_id=project_id,
            source_mode="live",
            status="closed" if closed else "not_requested",
            index_partial=True,
            requires_validation=True,
            reason=None,
            stats={},
            storage_bytes=0,
            limits=self._limits(),
        )
        # Metadata only; never allocate a database or follow a database symlink.
        if not path.exists():
            return result
        if path.is_symlink() or not path.is_file():
            raise SourceError("INDEX_STORAGE_UNAVAILABLE: unsafe project fact cache")
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
                if (
                    db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                    or db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION
                ):
                    raise sqlite3.DatabaseError("unrecognized project cache")
                row = db.execute(
                    "SELECT stats FROM li_projects WHERE project_id=?", (project_id,)
                ).fetchone()
                if row is not None:
                    stats = json.loads(row[0])
                    result.update(
                        status="closed" if closed else "cached",
                        stats=stats,
                        index_partial=stats.get("partial", True),
                    )
                result["storage_bytes"] = path.stat().st_size
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
            raise SourceError(
                "INDEX_STORAGE_UNAVAILABLE: cannot inspect project fact cache"
            ) from None
        return result

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            while any(entry["users"] for entry in self._entries.values()):
                self._condition.wait()
            services = [
                entry["service"] for entry in self._entries.values() if entry["service"] is not None
            ]
            self._entries.clear()
        for service in services:
            service.close()
        self._executor.shutdown(wait=True, cancel_futures=True)
        fcntl.flock(self._ownership, fcntl.LOCK_UN)
        os.close(self._ownership)
