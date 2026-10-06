"""Bounded, in-memory metadata for live reads, never historical source snapshots."""

import math
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from code_context.policy import validate_path


class ContextError(RuntimeError):
    """A live read context cannot safely support the requested operation."""


@dataclass(frozen=True, slots=True)
class ReadContext:
    project_id: str
    source_id: str
    files: dict[str, str]
    created_at: float


class ReadContexts:
    """Bind opaque handles to observed hashes, with global, non-sliding limits.

    Full capacity rejects new metadata instead of replacing active contexts. Hash
    readers run outside the lock; concurrent additions require another validation.
    Source access and creation for current/omitted selectors belong to the caller.
    """

    def __init__(
        self,
        max_contexts: int = 128,
        max_files: int = 50_000,
        ttl_seconds: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_contexts) is not int or max_contexts < 1:
            raise ValueError("max_contexts must be a positive integer")
        if type(max_files) is not int or max_files < 1:
            raise ValueError("max_files must be a positive integer")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be finite and positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._max_contexts = max_contexts
        self._max_files = max_files
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._contexts: dict[str, ReadContext] = {}
        self._file_count = 0
        self._lock = RLock()

    def create(self, project_id: str, source_id: str) -> str:
        if not isinstance(project_id, str) or not project_id:
            raise ContextError("a project binding is required")
        if not isinstance(source_id, str) or not source_id:
            raise ContextError("a source binding is required")
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            if len(self._contexts) >= self._max_contexts:
                raise ContextError("read context capacity exceeded")
            handle = "live_" + secrets.token_urlsafe(32)
            while handle in self._contexts:
                handle = "live_" + secrets.token_urlsafe(32)
            self._contexts[handle] = ReadContext(project_id, source_id, {}, now)
            return handle

    def get(self, project_id: str, source_id: str, handle: str | None) -> ReadContext:
        with self._lock:
            context = self._get_locked(project_id, source_id, handle)
            return ReadContext(
                context.project_id, context.source_id, context.files.copy(), context.created_at
            )

    def observe(
        self, project_id: str, source_id: str, handle: str | None, path: str, sha256: str
    ) -> None:
        with self._lock:
            context = self._get_locked(project_id, source_id, handle)
            if not isinstance(path, str):
                raise ContextError("invalid file metadata path")
            try:
                validate_path(path)
            except ValueError:
                raise ContextError("invalid file metadata path") from None
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ContextError("invalid SHA-256 metadata")
            previous = context.files.get(path)
            if previous is not None:
                if previous != sha256:
                    self._discard_locked(handle)
                    raise ContextError("read context file changed; create a new context")
                return
            if self._file_count >= self._max_files:
                raise ContextError("read context file capacity exceeded")
            context.files[path] = sha256
            self._file_count += 1

    def validate(
        self,
        project_id: str,
        source_id: str,
        handle: str | None,
        reader: Callable[[str], str | None],
    ) -> None:
        with self._lock:
            context = self._get_locked(project_id, source_id, handle)
            files = context.files.copy()
        if not callable(reader):
            raise ContextError("validation requires a hash reader")
        for path, expected in files.items():
            try:
                actual = reader(path)
                unchanged = isinstance(actual, str) and actual == expected
            except Exception:
                unchanged = False
            # Raise outside the exception handler so reader errors are not retained
            # as an exception context or displayed with possibly private details.
            if not unchanged:
                with self._lock:
                    if self._contexts.get(handle) is context:
                        self._discard_locked(handle)
                raise ContextError(
                    "read context validation failed: a file changed or is unavailable; "
                    "create a new context"
                ) from None
        with self._lock:
            current = self._get_locked(project_id, source_id, handle)
            if current is not context or current.files != files:
                raise ContextError("read context changed during validation; validate again")

    def invalidate_project(self, project_id: str) -> None:
        with self._lock:
            for handle, context in list(self._contexts.items()):
                if context.project_id == project_id:
                    self._discard_locked(handle)

    def clear(self) -> None:
        with self._lock:
            self._contexts.clear()
            self._file_count = 0

    def _get_locked(self, project_id: str, source_id: str, handle: str | None) -> ReadContext:
        if handle is None or handle == "current":
            raise ContextError("create a live read context before reading")
        if not isinstance(handle, str) or not handle.startswith("live_"):
            raise ContextError(
                "historical or unsupported read contexts are unavailable in live mode"
            )
        context = self._contexts.get(handle)
        now = self._clock()
        expired = context is not None and now - context.created_at >= self._ttl_seconds
        self._expire_locked(now)
        if expired:
            raise ContextError("read context has expired; create a new context")
        if context is None:
            raise ContextError(
                "read context is unknown, expired or invalidated; create a new context"
            )
        if context.project_id != project_id or context.source_id != source_id:
            raise ContextError("read context project or source binding does not match")
        return context

    def _expire_locked(self, now: float) -> None:
        for handle, context in list(self._contexts.items()):
            if now - context.created_at >= self._ttl_seconds:
                self._discard_locked(handle)

    def _discard_locked(self, handle: str | None) -> None:
        context = self._contexts.pop(handle, None)
        if context is not None:
            self._file_count -= len(context.files)
