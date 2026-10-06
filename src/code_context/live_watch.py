"""Bounded metadata/event invalidation; notifications are not freshness proof.

One native watcher observes an explicitly pruned directory set non-recursively.
Overflow, unavailable notifications and directory quotas are visible degradation.
Source reads and structural queries still validate their own real inputs.
"""

import hashlib
import os
import threading
import time
from pathlib import Path

from watchfiles import watch

from code_context.policy import MAX_FILES, excluded_path, validate_path
from code_context.source_access import SourceError


class WatchCoordinator:
    def __init__(
        self,
        sources,
        invalidate,
        *,
        max_directories=2048,
        max_projects=64,
        max_events=4096,
        reconcile_seconds=30.0,
        manifest_seconds=0.25,
        watch_function=watch,
    ):
        if (
            min(max_directories, max_projects, max_events) < 1
            or reconcile_seconds <= 0
            or manifest_seconds <= 0
        ):
            raise ValueError("invalid watcher budget")
        self.max_directories = max_directories
        self.max_projects = max_projects
        self.max_events = max_events
        self.reconcile_seconds = reconcile_seconds
        self.manifest_seconds = manifest_seconds
        self.invalidate = invalidate
        self.watch_function = watch_function
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.native_stop = threading.Event()
        self.thread = None
        self.sources = {}
        self.metadata = {}
        self.dirty = set()
        self.refresh = set()
        self.generation = 0
        self.overflow_count = 0
        self.state = "stopped"
        self.failure = None
        self.reconfigure(sources)

    def reconfigure(self, sources):
        if len(sources) > self.max_projects:
            raise SourceError("WATCH_PROJECT_LIMIT: too many authorized projects")
        with self.lock:
            self.sources = dict(sources)
            self.metadata = {}
            self.dirty = set(sources)
            self.refresh = set(sources)
            self.generation += 1
            self.native_stop.set()
        for project in sources:
            self.invalidate(project)

    def notify(self, changes):
        """Coalesce paths into project IDs, never keep an unbounded event queue."""
        affected = set()
        overflow = False
        with self.lock:
            sources = sorted(
                self.sources.items(), key=lambda item: len(item[1].root.parts), reverse=True
            )
        for number, (_, raw_path) in enumerate(changes):
            if number >= self.max_events:
                overflow = True
                affected.update(project for project, _ in sources)
                break
            if not isinstance(raw_path, str) or len(raw_path) > 8192:
                continue
            absolute = Path(os.path.abspath(raw_path))
            for project, source in sources:
                try:
                    relative = absolute.relative_to(source.root).as_posix()
                except ValueError:
                    continue
                if relative != ".":
                    try:
                        validate_path(relative)
                    except ValueError:
                        break
                    if source.scanner._path_problem(relative, None) or excluded_path(relative):
                        break
                affected.add(project)
                break
        with self.lock:
            affected.intersection_update(self.sources)
            self.dirty.update(affected)
            self.refresh.update(affected)
            if overflow:
                self.overflow_count += 1
                self.failure = "event_overflow"
            if affected:
                self.native_stop.set()
        for project in affected:
            self.invalidate(project)
        return affected

    def reconcile(self, projects=None):
        """Bounded metadata compensation, including new directories/missed events."""
        with self.lock:
            sources = dict(self.sources)
            selected = set(sources if projects is None else projects) & sources.keys()
            allowance = max(1, self.max_directories // max(1, len(sources)))
            allocated = set(sorted(sources)[: self.max_directories])
            generation = self.generation
        changed = set()
        for project in sorted(selected):
            if self.closed.is_set():
                break
            source = sources[project]
            try:
                if project not in allocated:
                    raise SourceError("WATCH_DIRECTORY_LIMIT: metadata validation still applies")
                metadata = source.manifest(
                    max_files=MAX_FILES,
                    max_directories=allowance,
                    max_seconds=self.manifest_seconds,
                )
                digest = hashlib.sha256(repr(metadata["files"]).encode()).hexdigest()
                directories = tuple(
                    source.root / relative for relative in metadata["watch_directories"]
                )
                item = {
                    "digest": digest,
                    "directories": directories,
                    "partial": metadata["partial"],
                    "error": None,
                }
            except SourceError:
                item = {
                    "digest": None,
                    "directories": (),
                    "partial": True,
                    "error": "source_unavailable",
                }
            with self.lock:
                if generation != self.generation:
                    return changed
                previous = self.metadata.get(project)
                self.metadata[project] = item
                if previous and previous["digest"] != item["digest"]:
                    self.dirty.add(project)
                    changed.add(project)
                self.refresh.discard(project)
        for project in changed:
            self.invalidate(project)
        return changed

    def status(self):
        with self.lock:
            return {
                "state": self.state,
                "failure": self.failure,
                "directories": sum(len(item["directories"]) for item in self.metadata.values()),
                "directory_limit": self.max_directories,
                "dirty_projects": sorted(self.dirty),
                "partial_projects": sorted(
                    project for project, item in self.metadata.items() if item["partial"]
                ),
                "event_overflows": self.overflow_count,
                "freshness_requires_validation": True,
                "recursive": False,
            }

    def consume(self, project):
        with self.lock:
            self.dirty.discard(project)

    def start(self):
        with self.lock:
            if self.thread is not None or self.closed.is_set():
                return
            self.thread = threading.Thread(
                target=self._run, name="colink-metadata-watch", daemon=True
            )
            self.thread.start()

    def _run(self):
        self.reconcile()
        last_reconcile = time.monotonic()
        while not self.closed.is_set():
            with self.lock:
                pending = set(self.refresh)
            if pending:
                self.reconcile(pending)
            with self.lock:
                paths = tuple(
                    dict.fromkeys(
                        path for item in self.metadata.values() for path in item["directories"]
                    )
                )
                self.native_stop = threading.Event()
                stop = self.native_stop
                self.state = (
                    "degraded"
                    if any(item["partial"] for item in self.metadata.values())
                    else "watching"
                )
            if not paths:
                with self.lock:
                    self.state = "degraded"
                    self.failure = "no_available_watch_paths"
                self.closed.wait(0.5)
                self.reconcile()
                continue
            try:
                for changes in self.watch_function(
                    *paths,
                    recursive=False,
                    watch_filter=None,
                    stop_event=stop,
                    debounce=150,
                    step=30,
                    rust_timeout=500,
                    yield_on_timeout=True,
                    raise_interrupt=False,
                    debug=False,
                ):
                    if self.closed.is_set():
                        break
                    if changes:
                        self.notify(changes)
                        break
                    if time.monotonic() - last_reconcile >= self.reconcile_seconds:
                        self.reconcile()
                        last_reconcile = time.monotonic()
                        break
                    if stop.is_set():
                        break
            except (OSError, RuntimeError):
                with self.lock:
                    self.state = "degraded"
                    self.failure = "notification_unavailable"
                self.closed.wait(min(1.0, self.reconcile_seconds))
                self.reconcile()
        with self.lock:
            self.state = "stopped"

    def close(self):
        self.closed.set()
        with self.lock:
            self.native_stop.set()
            thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)
