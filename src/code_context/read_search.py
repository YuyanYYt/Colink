"""Budgeted live text search with authenticated, context-bound continuation."""

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import weakref
from threading import RLock

from code_context.policy import MAX_TOTAL_BYTES
from code_context.read_requests import TextSearchRequest, read_budgets
from code_context.source_access import SourceError
from code_context.source_page import physical_lines, source_page

_CURSOR = struct.Struct(">BIId32s")
_CURSOR_CHARS = 5 + ((_CURSOR.size + 32) * 8 + 5) // 6
_CURSOR_LOCK = RLock()
_CURSOR_KEYS = weakref.WeakKeyDictionary()


def json_chars(value):
    return len(json.dumps(value, ensure_ascii=False))


def read_base(project_id, handle, mode, display_name=None):
    result = {
        "project_id": project_id,
        "snapshot": handle,
        "source_mode": "live",
        "mode": mode,
        "source_is_untrusted": True,
    }
    if display_name is not None:
        result["display_name"] = display_name
    return result


def search_binding(project_id, source_id, handle, options):
    return json.dumps(
        [project_id, source_id, handle, options], ensure_ascii=False, sort_keys=True
    ).encode("utf-8")


def _cursor_key(backend):
    with _CURSOR_LOCK:
        key = _CURSOR_KEYS.get(backend)
        if key is None:
            key = secrets.token_bytes(32)
            _CURSOR_KEYS[backend] = key
        return key


def encode_search_cursor(backend, binding, first, second=0, inventory=None):
    payload = _CURSOR.pack(1, first, second, time.monotonic(), inventory or bytes(32))
    signature = hmac.digest(_cursor_key(backend), payload + binding, "sha256")
    return "read_" + base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def decode_search_cursor(backend, binding, cursor):
    try:
        if not isinstance(cursor, str) or not cursor.startswith("read_") or len(cursor) != 113:
            raise ValueError
        raw = base64.b64decode(cursor[5:] + "==", altchars=b"-_", validate=True)
        if len(raw) != _CURSOR.size + 32:
            raise ValueError
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(
            signature, hmac.digest(_cursor_key(backend), payload + binding, "sha256")
        ):
            raise ValueError
        version, first, second, created, inventory = _CURSOR.unpack(payload)
        now = time.monotonic()
        ttl = min(900, getattr(backend.contexts, "_ttl_seconds", 900))
        if version != 1 or not 0 <= now - created < ttl:
            raise ValueError
        return first, second, inventory
    except (ValueError, TypeError, struct.error):
        raise SourceError(
            "INVALID_READ_CURSOR: reuse the returned snapshot and unchanged search request; "
            "expired, modified or cross-project cursors require a fresh search"
        ) from None


def _inventory_signature(metadata):
    inventory = [
        [item["path"], item["size"], list(item.get("fingerprint", ()))]
        for item in metadata["files"]
    ]
    return hashlib.sha256(
        json.dumps([inventory, metadata["partial"]], ensure_ascii=False).encode("utf-8")
    ).digest()


def _match(document, lines, number, query, context_lines, text_chars=1000):
    line = lines[number - 1]
    column = line.index(query)
    before = min(200, max(0, text_chars - len(query)) // 2)
    begin = max(0, column - before)
    end = min(len(line), begin + text_chars)
    result = {
        "path": document.path,
        "line": number,
        "column": column + 1,
        "text": line[begin:end],
        "text_start_column": begin + 1,
        "truncated": begin != 0 or end < len(line),
    }
    if context_lines:
        result["sha256"] = document.sha256
        result["size"] = document.size
        result["context"] = source_page(
            document.content,
            max(1, number - context_lines),
            min(len(lines), number + context_lines),
            min(1000, max(1, text_chars)),
            physical=True,
        )
    return result


def search_source(
    backend,
    project_id,
    source,
    handle,
    query,
    *,
    limit=50,
    path_prefix=None,
    cursor=None,
    context_lines=0,
    max_chars=20_000,
    max_ms=3000,
    display_name=None,
):
    """Scan only authorized saved text; the caller owns context creation/final validation.

    Each page scans at most MAX_TOTAL_BYTES and cooperatively checks its deadline.
    Inventory and source checks may require extra time; no source is imported or run.
    A cursor authenticates project, source, snapshot, query, prefix and context width,
    plus the discovered inventory. Changing page size or budget remains supported.
    """
    read_budgets(max_chars, max_ms)
    try:
        request = TextSearchRequest.model_validate(
            {
                "mode": "search",
                "kind": "text",
                "query": query,
                "limit": limit,
                "path_prefix": path_prefix,
                "cursor": cursor,
                "context_lines": context_lines,
            },
            strict=True,
        )
    except ValueError:
        raise SourceError(
            "INVALID_TEXT_SEARCH: check query, prefix, limit and context width"
        ) from None
    deadline = time.monotonic() + max_ms / 1000
    binding = search_binding(
        project_id,
        source.source_id,
        handle,
        {
            "kind": "text",
            "query": query,
            "path_prefix": path_prefix,
            "context_lines": context_lines,
        },
    )
    position, expected = (0, 0), None
    if cursor is not None:
        first, second, expected = decode_search_cursor(backend, binding, cursor)
        position = first, second
    metadata = source.manifest(max_seconds=max(0.001, deadline - time.monotonic()))
    inventory = _inventory_signature(metadata)
    if expected is not None and not hmac.compare_digest(inventory, expected):
        raise SourceError(
            "READ_SEARCH_CHANGED: discovered files changed or discovery coverage changed; "
            "start a fresh search"
        )
    files = [
        item
        for item in metadata["files"]
        if request.path_prefix is None or item["path"].startswith(request.path_prefix)
    ]
    if position[0] > len(files) or position[0] == len(files) and position[1]:
        raise SourceError("INVALID_READ_CURSOR: search position is outside discovered files")
    matches, scanned_bytes, excluded = [], 0, 0
    match_chars = 0
    base = {**read_base(project_id, handle, "search", display_name), "kind": "text", "query": query}

    def response(pos, reasons, *, measure=False):
        unfinished = pos[0] < len(files)
        partial = metadata["partial"] or any(
            reason in {"max_ms", "max_bytes"} for reason in reasons
        )
        return {
            **base,
            "matches": [] if measure else matches,
            "has_more": unfinished,
            "next_cursor": (
                "x" * _CURSOR_CHARS
                if measure
                else encode_search_cursor(backend, binding, *pos, inventory)
            )
            if unfinished
            else None,
            "search_complete": not unfinished and not metadata["partial"],
            "search_partial": partial,
            "discovery_partial": metadata["partial"],
            "read_status": "partial" if unfinished or partial else "ready",
            "scanned_bytes": scanned_bytes,
            "excluded_files": excluded,
            "truncated": bool(reasons),
            "truncated_reason": list(reasons),
        }

    metadata_key, metadata_chars = None, 0

    def response_chars(pos, reasons, values_chars):
        nonlocal metadata_key, metadata_chars
        # Position is carried inside a fixed-width, unescaped cursor. Only these
        # counters/flags can change the remaining JSON cost while scanning.
        key = pos[0] < len(files), tuple(reasons), scanned_bytes, excluded
        if key != metadata_key:
            metadata_key = key
            metadata_chars = json_chars(response(pos, reasons, measure=True))
        # The metadata already includes []; account for each complete encoded
        # match and the default JSON list separators without re-encoding old hits.
        return metadata_chars + values_chars + 2 * max(0, len(matches) - 1)

    def finish(pos, reasons):
        result = response(pos, reasons)
        if json_chars(result) > max_chars:
            raise SourceError(
                "READ_METADATA_EXCEEDS_BUDGET: increase max_chars or narrow the request"
            )
        return result

    for file_index in range(position[0], len(files)):
        position = file_index, position[1] if file_index == position[0] else 0
        if time.monotonic() >= deadline:
            return finish(position, ["max_ms"])
        item = files[file_index]
        if scanned_bytes + item["size"] > MAX_TOTAL_BYTES:
            return finish(position, ["max_bytes"])
        try:
            document = source.read(item["path"])
        except SourceError as exc:
            if str(exc).startswith("FILE_EXCLUDED:"):
                excluded += 1
                position = file_index + 1, 0
                continue
            raise
        if document.size != item["size"] or tuple(document.version) != tuple(item["fingerprint"]):
            raise SourceError(
                "READ_SEARCH_CHANGED: saved text changed after discovery; start a fresh search"
            )
        scanned_bytes += document.size
        backend._observe(project_id, handle, document)
        lines = physical_lines(document.content)
        first_line = position[1]
        if first_line > len(lines):
            raise SourceError("INVALID_READ_CURSOR: search line is outside the saved file")
        for line_index in range(first_line, len(lines)):
            position = file_index, line_index
            if time.monotonic() >= deadline:
                return finish(position, ["max_ms"])
            following = (
                (file_index, line_index + 1) if line_index + 1 < len(lines) else (file_index + 1, 0)
            )
            if query not in lines[line_index]:
                position = following
                continue
            candidate = _match(document, lines, line_index + 1, query, context_lines)
            previous_chars = match_chars
            match_chars += json_chars(candidate)
            matches.append(candidate)
            if response_chars(following, ["max_chars"], match_chars) > max_chars:
                low, high = len(query), min(1000, len(lines[line_index]))
                best, best_chars = None, 0
                while low <= high:
                    middle = (low + high) // 2
                    narrowed = _match(document, lines, line_index + 1, query, 0, middle)
                    if context_lines:
                        narrowed["context_omitted"] = True
                    narrowed_chars = json_chars(narrowed)
                    if (
                        response_chars(following, ["max_chars"], previous_chars + narrowed_chars)
                        <= max_chars
                    ):
                        best = narrowed
                        best_chars = narrowed_chars
                        low = middle + 1
                    else:
                        high = middle - 1
                if best is None:
                    matches.pop()
                    match_chars = previous_chars
                    if matches:
                        return finish(position, ["max_chars"])
                    raise SourceError(
                        "READ_ITEM_EXCEEDS_BUDGET: increase max_chars to fit one search match"
                    )
                matches[-1] = best
                match_chars = previous_chars + best_chars
                return finish(following, ["max_chars"])
            position = following
            if len(matches) >= limit:
                return finish(position, ["limit"] if position[0] < len(files) else [])
        position = file_index + 1, 0
    return finish(position, [])


def search_text(backend, project_id, query, snapshot=None, **options):
    if options.get("cursor") is not None and snapshot in {None, "current"}:
        raise SourceError("READ_CURSOR_CONTEXT_REQUIRED: pass the snapshot returned by search")
    with backend.context_request(project_id, snapshot) as (_, handle):
        result = search_source(
            backend, project_id, backend.source(project_id), handle, query, **options
        )
        backend.resolve_snapshot(project_id, handle)
        return result
