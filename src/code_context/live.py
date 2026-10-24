"""Query backend for saved local files, without permanent source-text snapshots."""

from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock

from code_context.fingerprint_cache import FingerprintCache
from code_context.models import validate_project
from code_context.read_context import ContextCapacityError, ContextError, ReadContexts
from code_context.source_access import SourceAccess, SourceError
from code_context.source_page import source_page


class LiveQueries:
    source_mode = "live"

    def __init__(
        self, sources: dict[str, SourceAccess] | None = None, contexts=None, registry=None
    ):
        sources = sources or (registry.authorized_sources() if registry is not None else {})
        for project_id in sources:
            validate_project(project_id)
        self.sources = dict(sources)
        self._source_lock = RLock()
        self._closed = False
        self._source_ids = {project: source.source_id for project, source in sources.items()}
        self.fingerprint_cache = FingerprintCache()
        self.fingerprint_cache.retain_sources(self._source_ids.values())
        for source in self.sources.values():
            source.attach_fingerprint_cache(self.fingerprint_cache)
        self.registry = registry
        self.contexts = contexts or ReadContexts()
        self.index_service = None
        self.write_coordinator = None
        self.watcher = None

    def source(self, project_id):
        try:
            validate_project(project_id)
        except ValueError:
            raise SourceError("INVALID_PROJECT: choose an authorized project") from None
        with self._source_lock:
            if self._closed:
                raise SourceError("LIVE_CLOSED: restart the live backend")
            source = self.sources.get(project_id)
        try:
            if self.registry is not None:
                source = self.registry.source(project_id)
            elif source is None:
                raise SourceError("PROJECT_NOT_AUTHORIZED: choose from list_projects")
            else:
                source.ensure_available()
        except SourceError:
            with self._source_lock:
                self._source_ids.pop(project_id, None)
                self.fingerprint_cache.retain_sources(self._source_ids.values())
            self.contexts.invalidate_project(project_id)
            raise
        with self._source_lock:
            if self._closed:
                raise SourceError("LIVE_CLOSED: restart the live backend")
            self.sources[project_id] = source
            if self._source_ids.get(project_id) != source.source_id:
                self._source_ids[project_id] = source.source_id
                self.fingerprint_cache.retain_sources(self._source_ids.values())
        # Attach outside backend/cache locks: a source can hold its I/O lock.
        source.attach_fingerprint_cache(self.fingerprint_cache)
        return source

    def list_projects(self):
        if self.registry is not None:
            result = self.registry.list_projects()
            for project in result["projects"]:
                project.pop("relative_root", None)
                project.update(source_mode="live", file_count=None)
            return result
        projects = []
        for project_id in sorted(self.sources):
            try:
                self.sources[project_id].ensure_available()
                status = "available"
            except SourceError:
                status = "unavailable"
            projects.append(
                {
                    "project_id": project_id,
                    "revision": 0,
                    "file_count": None,
                    "source_mode": "live",
                    "status": status,
                }
            )
        return {"projects": projects}

    def project_name(self, project_id):
        if self.registry is not None:
            return self.registry.names().get(project_id, project_id)
        return self.sources[project_id].root.name if project_id in self.sources else project_id

    def refresh_sources(self):
        if self.registry is not None:
            sources = self.registry.authorized_sources()
            with self._source_lock:
                if self._closed:
                    raise SourceError("LIVE_CLOSED: restart the live backend")
                previous = set(self.sources)
                self.sources = sources
                self._source_ids = {p: source.source_id for p, source in sources.items()}
                self.fingerprint_cache.retain_sources(self._source_ids.values())
            for source in sources.values():
                source.attach_fingerprint_cache(self.fingerprint_cache)
            self.contexts.clear()
            if self.index_service is not None:
                for project in previous | self.sources.keys():
                    self.index_service.invalidate(project)
            if self.watcher is not None:
                self.watcher.reconfigure(self.sources)

    def resolve_snapshot(self, project_id, snapshot=None):
        handle, _source_id = self._resolve_context(project_id, snapshot)
        return handle, handle

    def _resolve_context(self, project_id, snapshot=None):
        source = self.source(project_id)
        self.guard_read(project_id)
        created = snapshot is None or snapshot == "current"
        handle = None
        completed = False
        try:
            if created:
                handle = self.contexts.create(project_id, source.source_id)
            else:
                handle = snapshot
                self.contexts.get(project_id, source.source_id, handle)
            with self.fingerprint_batch(project_id, source):
                self.contexts.validate(project_id, source.source_id, handle, source.fingerprint)
            self.guard_read(project_id)
            completed = True
        except ContextCapacityError:
            raise SourceError(
                "LIVE_CONTEXT_CAPACITY: active read metadata is full; reuse a valid context "
                "or retry after unused contexts expire"
            ) from None
        except ContextError:
            raise SourceError(
                "LIVE_CONTEXT_INVALID: restart analysis from repo_overview; "
                "historical snapshots are not available in direct mode"
            ) from None
        finally:
            if created and handle is not None and not completed:
                self.contexts.discard(project_id, source.source_id, handle)
        return handle, source.source_id

    @contextmanager
    def context_request(self, project_id, snapshot=None):
        """Own new metadata until the request succeeds; preserve supplied contexts."""
        handle, source_id = self._resolve_context(project_id, snapshot)
        try:
            yield handle, handle
        except BaseException:
            if snapshot is None or snapshot == "current":
                self.contexts.discard(project_id, source_id, handle)
            raise

    def guard_read(self, project_id):
        """Publication guard; never acquire the coordinator lock under Source locks."""
        if self.write_coordinator is not None:
            self.write_coordinator.guard_read(project_id)

    @contextmanager
    def fingerprint_batch(self, project_id, source=None):
        """Pin one source's metadata pass and recheck backend authorization/binding."""
        source = self.source(project_id) if source is None else source
        try:
            with source.fingerprint_batch():
                if self.source(project_id) is not source:
                    raise SourceError("SOURCE_REPLACED: source changed before validation")
                yield source
                if self.source(project_id) is not source:
                    raise SourceError("SOURCE_REPLACED: source changed during validation")
        except SourceError:
            self.contexts.invalidate_project(project_id)
            raise

    def _observe(self, project_id, handle, document):
        source = self.source(project_id)
        try:
            self.contexts.observe(
                project_id, source.source_id, handle, document.path, document.sha256
            )
        except ContextCapacityError:
            raise SourceError(
                "LIVE_CONTEXT_CAPACITY: active read metadata is full; reuse observed files "
                "or retry after unused contexts expire"
            ) from None
        except ContextError:
            raise SourceError("LIVE_CONTEXT_CHANGED: restart analysis from repo_overview") from None

    def repo_overview(self, project_id, selection, offset=0, limit=200):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise SourceError("INVALID_PAGE: use nonnegative offset and 1-1000 files")
        with self.context_request(project_id, selection) as (handle, _):
            source = self.source(project_id)
            metadata = source.manifest()
            files = metadata["files"]
            page = [{"path": f["path"], "size": f["size"]} for f in files[offset : offset + limit]]
            self.resolve_snapshot(project_id, handle)
            return {
                "project_id": project_id,
                "snapshot": handle,
                "source_mode": "live",
                "created_at": datetime.now(UTC).isoformat(),
                "files": page,
                "file_count": len(files),
                "offset": offset,
                "has_more": offset + len(page) < len(files),
                "discovery_partial": metadata["partial"],
                "hashes_available": False,
                "history_available": False,
                "code_intelligence": {"status": "on_demand"},
            }

    def read_file(
        self,
        project_id,
        path,
        selection=None,
        start_line=1,
        end_line=None,
        max_chars=20000,
        char_offset=0,
    ):
        if not 1000 <= max_chars <= 50000:
            raise SourceError("max_chars must be between 1000 and 50000")
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise SourceError("invalid line range")
        end_line = start_line + 199 if end_line is None else end_line
        if end_line - start_line + 1 > 1000:
            raise SourceError("read at most 1000 lines at a time")
        with self.context_request(project_id, selection) as (handle, _):
            document = self.source(project_id).read(path)
            self._observe(project_id, handle, document)
            try:
                page = source_page(
                    document.content, start_line, end_line, max_chars, char_offset, physical=True
                )
            except ValueError as exc:
                raise SourceError(str(exc)) from None
            self.resolve_snapshot(project_id, handle)
            return {
                "project_id": project_id,
                "snapshot": handle,
                "source_mode": "live",
                "path": path,
                "sha256": document.sha256,
                "size": document.size,
                "source_is_untrusted": True,
                **page,
            }

    def search_code(self, project_id, query, selection=None, limit=50, **parameters):
        from code_context.read_search import search_source

        with self.context_request(project_id, selection) as (handle, _):
            result = search_source(
                self,
                project_id,
                self.source(project_id),
                handle,
                query,
                limit=limit,
                **parameters,
            )
            self.resolve_snapshot(project_id, handle)
            return result

    def get_recent_diff(self, project_id, snapshot=None, path=None, **parameters):
        with self.context_request(project_id, snapshot) as (handle, _):
            if self.write_coordinator is not None:
                result = self.write_coordinator.get_diff(project_id, path=path, **parameters)
            else:
                from code_context.write_diff import TaskDiff

                options = {
                    "baseline": "previous",
                    "detail": "summary",
                    "offset": 0,
                    "limit": 50,
                    "max_chars": 20_000,
                    **parameters,
                }
                TaskDiff._parameters(path, **options)
                if options["baseline"] == "empty":
                    result = TaskDiff._empty(
                        project_id,
                        self.source(project_id),
                        path,
                        options["detail"],
                        options["offset"],
                        options["limit"],
                        options["max_chars"],
                    )
                else:
                    result = {
                        "project_id": project_id,
                        "source_mode": "live",
                        "changes_available": False,
                        "baseline": None,
                        "reason": "NO_TASK_BASELINE",
                        "changes": [],
                        "has_more": False,
                        "next_offset": None,
                        "truncated": False,
                    }
            self.resolve_snapshot(project_id, handle)
            return {**result, "snapshot": handle}

    def code_query(self, project_id, snapshot, operation, **parameters):
        with self.context_request(project_id, snapshot) as (handle, _):
            if self.index_service is None:
                raise SourceError("INDEX_NOT_READY: live structural indexing is not attached")
            result = self.index_service.query(self, project_id, handle, operation, **parameters)
            # The index service validates the entire context and inventory before
            # and after its query. Recheck authorization/publication here without
            # repeating a fifth complete fingerprint pass over a large project.
            self.source(project_id)
            self.guard_read(project_id)
            return result

    def mcp_status(self):
        runtime = getattr(self, "workspace_runtime", None)
        # The workspace owns both write and execution grants, including revocation
        # when its local control dies. Do not report execution as closed merely
        # because this source-query backend omitted the runtime's status fields.
        status = (
            runtime.status()
            if runtime is not None
            else self.write_coordinator.status()
            if self.write_coordinator is not None
            else {}
        )
        return {
            **status,
            "state": status.get("state", "live_read"),
            "source_mode": "live",
            "history_available": False,
            "last_seen": datetime.now(UTC).isoformat(),
            "last_sync_at": None,
            "watcher": self.watcher.status() if self.watcher is not None else None,
            "write_enabled": status.get("write_enabled", False),
        }

    def clear(self):
        """Clear this backend's contexts and charged metadata, without source I/O."""
        self.contexts.clear()
        self.fingerprint_cache.clear()

    def close(self):
        with self._source_lock:
            self._closed = True
            self._source_ids.clear()
            # Revoke puts from an in-flight accessor before waiting for index work.
            self.fingerprint_cache.retain_sources(())
        self.clear()
        if self.index_service is not None:
            self.index_service.close()
