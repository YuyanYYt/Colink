"""Bounded, thread-safe LRU of immutable source fingerprints, never source text.

Charged bytes are an accounting estimate, not an RSS hard limit: each entry is
charged 512 bytes of container/scalar headroom plus UTF-8 source ID/path/hash
lengths. An optional authorized-source set is charged separately. No cache lock
calls a loader, source accessor, filesystem operation or other caller callback.
"""

import re
from collections import OrderedDict
from threading import Lock

from code_context.policy import validate_path

MAX_ENTRIES = 50_000
MAX_CHARGED_BYTES = 32 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_OVERHEAD = 512
_BINDING_OVERHEAD = 128


def _source(source_id):
    if type(source_id) is not str or not _HEX.fullmatch(source_id):
        raise ValueError("invalid fingerprint source metadata")
    return source_id


def _key(source_id, path):
    _source(source_id)
    if type(path) is not str:
        raise ValueError("invalid fingerprint path metadata")
    try:
        validate_path(path)
        path.encode("utf-8")
    except (ValueError, UnicodeError):
        raise ValueError("invalid fingerprint path metadata") from None
    return source_id, path


class FingerprintCache:
    def __init__(self, max_entries=MAX_ENTRIES, max_bytes=MAX_CHARGED_BYTES):
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES:
            raise ValueError("invalid fingerprint entry budget")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CHARGED_BYTES:
            raise ValueError("invalid fingerprint byte budget")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._lock = Lock()
        self._entries = OrderedDict()
        self._source_counts = {}
        self._entry_bytes = self._binding_bytes = 0
        self._allowed_sources = None
        self._hits = self._misses = self._evictions = 0

    def get(self, source_id, path):
        key = _key(source_id, path)
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value[0], value[1]  # Tuple of six builtin ints and a hash: immutable.

    def put(self, source_id, path, version, sha256):
        key = _key(source_id, path)
        if (
            type(version) is not tuple
            or len(version) != 6
            or any(type(v) is not int or v.bit_length() > 128 for v in version)
            or type(sha256) is not str
            or not _HEX.fullmatch(sha256)
        ):
            raise ValueError("invalid fingerprint version or hash metadata")
        charge = _ENTRY_OVERHEAD + sum(len(v.encode("utf-8")) for v in (*key, sha256))
        with self._lock:
            if self._allowed_sources is not None and source_id not in self._allowed_sources:
                return False
            if charge + self._binding_bytes > self.max_bytes:
                self._remove_locked(key)
                return False  # An oversized entry must not flush other useful entries.
            self._remove_locked(key)
            while self._entries and (
                len(self._entries) >= self.max_entries
                or self._entry_bytes + self._binding_bytes + charge > self.max_bytes
            ):
                self._remove_locked(next(iter(self._entries)))
                self._evictions += 1
            self._entries[key] = version, sha256, charge
            self._entry_bytes += charge
            self._source_counts[source_id] = self._source_counts.get(source_id, 0) + 1
            return True

    def _remove_locked(self, key):
        value = self._entries.pop(key, None)
        if value is not None:
            self._entry_bytes -= value[2]
            source_id = key[0]
            remaining = self._source_counts[source_id] - 1
            if remaining:
                self._source_counts[source_id] = remaining
            else:
                del self._source_counts[source_id]

    def discard(self, source_id, path):
        key = _key(source_id, path)
        with self._lock:
            self._remove_locked(key)

    def drop_source(self, source_id):
        _source(source_id)
        with self._lock:
            for key in list(self._entries):
                if key[0] == source_id:
                    self._remove_locked(key)

    def retain_sources(self, source_ids):
        """Drop only revoked namespaces; deny later puts by retained stale accessors.

        Consume/validate the iterable outside the lock. The binding set itself is
        bounded and charged, including authorized sources with no cached files.
        """
        allowed = set()
        for source_id in source_ids:
            allowed.add(_source(source_id))
            if len(allowed) > MAX_ENTRIES:
                raise ValueError("fingerprint source binding budget exceeded")
        charge = sum(_BINDING_OVERHEAD + len(v.encode("utf-8")) for v in allowed)
        if charge > self.max_bytes:
            raise ValueError("fingerprint source binding byte budget exceeded")
        with self._lock:
            self._allowed_sources = allowed
            self._binding_bytes = charge
            for key in list(self._entries):
                if key[0] not in allowed:
                    self._remove_locked(key)
            while self._entries and self._entry_bytes + charge > self.max_bytes:
                self._remove_locked(next(iter(self._entries)))
                self._evictions += 1

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._source_counts.clear()
            self._entry_bytes = 0

    def source_size(self, source_id):
        _source(source_id)
        with self._lock:
            return self._source_counts.get(source_id, 0)

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def stats(self):
        with self._lock:
            return {
                "entries": len(self._entries),
                "charged_bytes": self._entry_bytes + self._binding_bytes,
                "entry_charged_bytes": self._entry_bytes,
                "binding_charged_bytes": self._binding_bytes,
                "max_entries": self.max_entries,
                "max_charged_bytes": self.max_bytes,
                "active_sources": (
                    None if self._allowed_sources is None else len(self._allowed_sources)
                ),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "budget_kind": "estimated charged metadata, not an RSS hard cap",
            }


class SourceFingerprints:
    """A source-scoped metadata view; len never counts another source's entries."""

    __slots__ = ("cache", "source_id")

    def __init__(self, cache, source_id):
        self.cache, self.source_id = cache, _source(source_id)

    def get(self, path):
        return self.cache.get(self.source_id, path)

    def put(self, path, version, sha256):
        return self.cache.put(self.source_id, path, version, sha256)

    def discard(self, path):
        self.cache.discard(self.source_id, path)

    def __len__(self):
        return self.cache.source_size(self.source_id)
