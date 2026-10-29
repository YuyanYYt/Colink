"""One live reader for exact text, exact symbols, search and explicit small batches."""

import time

from code_context.intelligence_coverage import bound_index_coverage
from code_context.read_requests import (
    BatchReadRequest,
    FileReadRequest,
    SymbolReadRequest,
    SymbolSearchRequest,
    TextSearchRequest,
    normalize_read_budgets,
    validate_read_request,
)
from code_context.read_search import (
    decode_search_cursor,
    encode_search_cursor,
    json_chars,
    read_base,
    search_binding,
    search_source,
)
from code_context.source_access import SourceError
from code_context.source_page import physical_lines, source_page


def _budget_error():
    return SourceError(
        "READ_METADATA_EXCEEDS_BUDGET: increase max_chars or narrow the explicit request"
    )


def _read_document(backend, project_id, handle, path):
    document = backend.source(project_id).read(path)
    backend._observe(project_id, handle, document)
    return document


def _document_page(document, first, end, char_offset, budget, symbol=None):
    lines = physical_lines(document.content, keepends=True)
    if first > len(lines) and not (first == 1 and not lines):
        raise SourceError("INVALID_READ_RANGE: starting line is outside the saved file")
    try:
        page = source_page(document.content, first, end, budget, char_offset, physical=True)
    except ValueError:
        raise SourceError(
            "INVALID_READ_RANGE: character offset is outside the starting line"
        ) from None
    result = {
        "path": document.path,
        "sha256": document.sha256,
        "size": document.size,
        **page,
    }
    if symbol is not None:
        result["symbol"] = symbol
        more = page["content_truncated"] or page["end_line"] < symbol["end_line"]
        following = page["next_start_line"] if more else None
        result.update(
            has_more=more,
            next_start_line=following,
            next_char_offset=page["next_char_offset"] if more else None,
            next_line_offset=following - max(1, symbol["start_line"]) if following else None,
        )
    return result


def _fit_page(document, first, end, char_offset, max_chars, container, symbol=None):
    """Recompute both source and cursor when shrinking, accounting for JSON escapes."""

    def candidate(chars):
        return container(_document_page(document, first, end, char_offset, chars, symbol))

    result = candidate(max_chars)
    if json_chars(result) <= max_chars:
        return result
    low, high, best = 1, max_chars, None
    while low <= high:
        middle = (low + high) // 2
        result = candidate(middle)
        if json_chars(result) <= max_chars:
            best = result
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        raise _budget_error()
    return best


def _bound_candidates(result, max_chars):
    candidates = result["candidates"]
    while json_chars(result) > max_chars and candidates:
        candidates.pop()
        result["truncated"] = True
        result["has_more"] = result["offset"] + len(candidates) < result["total"]
        result["next_offset"] = result["offset"] + len(candidates) if result["has_more"] else None
    if json_chars(result) > max_chars or not candidates and result["total"] > result["offset"]:
        raise _budget_error()
    return result


def _read_symbol(backend, project_id, handle, request, max_chars, max_ms, base):
    selector = (
        {"symbol_id": request.symbol_id}
        if request.symbol_id is not None
        else {"path": request.path, "qualname": request.qualname}
    )
    selected = backend.code_query(
        project_id,
        handle,
        "analyze_code",
        mode="symbol_context",
        selector=selector,
        include=[],
        max_depth=1,
        max_nodes=200,
        max_edges=1,
        max_chars=50_000,
        max_ms=max_ms,
        offset=request.candidate_offset,
        limit=request.candidate_limit,
    )
    if selected.get("analysis_status") == "ambiguous":
        result = {
            **selected,
            **base,
            "read_status": "ambiguous",
            "selection_required": "choose one candidate symbol_id before reading source",
        }
        return _bound_candidates(result, max_chars)
    if selected.get("analysis_status") == "selection_not_found":
        result = {**selected, **base, "read_status": "selection_not_found"}
        # No analysis was computed: the lookup reason already states the
        # indexed-facts boundary, so avoid duplicating structural graph prose.
        result.pop("coverage", None)
        result.pop("analysis_status", None)
        if "index_coverage" in result:
            result["index_coverage"] = bound_index_coverage(
                result["index_coverage"], max_chars, compact=True
            )
        if json_chars(result) > max_chars:
            raise _budget_error()
        return result
    if selected.get("index_not_ready") or "symbol" not in selected:
        result = {**selected, **base, "read_status": "index_not_ready"}
        if json_chars(result) > max_chars:
            raise _budget_error()
        return result
    symbol = selected["symbol"]
    first = max(1, symbol["start_line"]) + request.line_offset
    if first > max(1, symbol["end_line"]):
        raise SourceError("INVALID_READ_RANGE: line_offset is outside the selected symbol")
    end = min(symbol["end_line"], first + request.max_lines - 1)
    document = _read_document(backend, project_id, handle, symbol["path"])
    metadata = {
        **base,
        "index_partial": selected.get("index_partial", False),
    }
    if "index_coverage" in selected:
        metadata["index_coverage"] = bound_index_coverage(
            selected["index_coverage"], max_chars, compact=True
        )
    return _fit_page(
        document,
        first,
        end,
        request.char_offset,
        max_chars,
        lambda page: {
            **metadata,
            **page,
            "read_status": "partial" if page["has_more"] else "ready",
        },
        symbol,
    )


def _search_symbols(backend, project_id, source, handle, request, max_chars, max_ms, base):
    deadline = time.monotonic() + max_ms / 1000
    options = request.model_dump(exclude={"cursor", "limit"})
    binding = search_binding(project_id, source.source_id, handle, options)
    offset = 0
    if request.cursor is not None:
        offset, second, inventory = decode_search_cursor(backend, binding, request.cursor)
        if second or inventory != bytes(32) or offset > 1_000_000:
            raise SourceError("INVALID_READ_CURSOR: symbol search position is invalid")
    found = backend.code_query(
        project_id,
        handle,
        "symbol_search",
        query=request.query,
        exact=request.exact,
        kind=request.symbol_kind,
        path_prefix=request.path_prefix,
        language=request.language,
        offset=offset,
        limit=request.limit,
        max_chars=50_000,
        max_ms=max_ms,
    )
    if "index_coverage" in found:
        found["index_coverage"] = bound_index_coverage(
            found["index_coverage"], max_chars, compact=True
        )
    if found.get("index_not_ready"):
        result = {**found, **base, "kind": "symbol", "read_status": "index_not_ready"}
        if json_chars(result) > max_chars:
            raise _budget_error()
        return result
    symbols = [dict(symbol) for symbol in found["symbols"]]
    reasons = ["max_ms"] if time.monotonic() >= deadline else []
    if request.context_lines:
        for symbol in symbols:
            if time.monotonic() >= deadline:
                symbol["context_omitted"] = True
                if "max_ms" not in reasons:
                    reasons.append("max_ms")
                continue
            document = _read_document(backend, project_id, handle, symbol["path"])
            symbol["sha256"] = document.sha256
            symbol["size"] = document.size
            symbol["context"] = source_page(
                document.content,
                max(1, symbol["start_line"] - request.context_lines),
                symbol["start_line"] + request.context_lines,
                1000,
                physical=True,
            )

    def response():
        more = offset + len(symbols) < found["total"]
        return {
            **found,
            **base,
            "kind": "symbol",
            "query": request.query,
            "symbols": symbols,
            "has_more": more,
            "next_offset": None,
            "next_cursor": encode_search_cursor(backend, binding, offset + len(symbols))
            if more
            else None,
            "read_status": "partial" if more or found.get("index_partial") or reasons else "ready",
            "truncated": bool(reasons) or found.get("truncated", False),
            "truncated_reason": list(reasons),
        }

    for symbol in reversed(symbols):
        if json_chars(response()) <= max_chars:
            break
        if "context" in symbol:
            symbol.pop("context")
            symbol["context_omitted"] = True
            if "max_chars" not in reasons:
                reasons.append("max_chars")
    while symbols and json_chars(response()) > max_chars:
        symbols.pop()
        if "max_chars" not in reasons:
            reasons.append("max_chars")
    result = response()
    if json_chars(result) > max_chars or not symbols and offset < found["total"]:
        raise _budget_error()
    return result


def _read_batch(backend, project_id, handle, request, max_chars, max_ms, base):
    deadline = time.monotonic() + max_ms / 1000
    items = []
    first_index = request.cursor.item_index if request.cursor is not None else 0
    position = request.cursor.model_dump() if request.cursor is not None else None

    def response(cursor, reasons):
        return {
            **base,
            "items": items,
            "has_more": cursor is not None,
            "next_cursor": cursor,
            "read_status": "partial" if cursor is not None else "ready",
            "truncated": bool(reasons),
            "truncated_reason": list(reasons),
        }

    for index in range(first_index, len(request.items)):
        item = request.items[index]
        first = (
            position["start_line"]
            if index == first_index and position is not None
            else item.start_line
        )
        char_offset = (
            position["char_offset"]
            if index == first_index and position is not None
            else item.char_offset
        )
        end = item.end_line if item.end_line is not None else item.start_line + 199
        current = {"item_index": index, "start_line": first, "char_offset": char_offset}
        if time.monotonic() >= deadline:
            result = response(current, ["max_ms"])
            if json_chars(result) > max_chars:
                raise _budget_error()
            return result
        document = _read_document(backend, project_id, handle, item.path)

        def container(page, index=index, prior_items=items):
            continuation = None
            if page["content_truncated"]:
                continuation = {
                    "item_index": index,
                    "start_line": page["next_start_line"],
                    "char_offset": page["next_char_offset"],
                }
            elif index + 1 < len(request.items):
                following = request.items[index + 1]
                continuation = {
                    "item_index": index + 1,
                    "start_line": following.start_line,
                    "char_offset": following.char_offset,
                }
            more_in_file = page["has_more"]
            range_more = page["content_truncated"]
            result = response(continuation, ["max_chars"] if range_more else [])
            result["items"] = [
                *prior_items,
                {
                    **page,
                    "item_index": index,
                    "has_more": range_more,
                    "file_has_more": more_in_file,
                },
            ]
            return result

        try:
            result = _fit_page(document, first, end, char_offset, max_chars, container)
        except SourceError as exc:
            if items and str(exc).startswith("READ_METADATA_EXCEEDS_BUDGET:"):
                result = response(current, ["max_chars"])
                if json_chars(result) > max_chars:
                    raise _budget_error() from None
                return result
            raise
        items = result["items"]
        if items[-1]["content_truncated"]:
            return result
    result = response(None, [])
    if json_chars(result) > max_chars:
        raise _budget_error()
    return result


def execute_read_code(
    backend,
    project_id,
    request,
    snapshot=None,
    *,
    max_chars=20_000,
    max_ms=3000,
    display_name=None,
):
    """Read saved source under one live context and one complete JSON response budget."""
    request = validate_read_request(request)
    response_limit, adjustment = normalize_read_budgets(max_chars, max_ms)
    # Every result is a nonempty JSON object. Adding one field costs exactly
    # this standalone object's encoded size, including its comma separator.
    # Reserve it before paging so cursors always describe the source returned.
    max_chars = response_limit - (
        json_chars({"response_budget": adjustment}) if adjustment is not None else 0
    )
    if snapshot is not None and not isinstance(snapshot, str):
        raise SourceError("INVALID_READ_CONTEXT: snapshot must be a returned string handle")
    if display_name is not None and not isinstance(display_name, str):
        raise SourceError("INVALID_READ_METADATA: display_name must be text")
    if (
        isinstance(request, (TextSearchRequest, SymbolSearchRequest, BatchReadRequest))
        and request.cursor is not None
    ):
        if snapshot in {None, "current"}:
            raise SourceError("READ_CURSOR_CONTEXT_REQUIRED: pass the snapshot returned by search")
    with backend.context_request(project_id, snapshot) as (_, handle):
        base = read_base(project_id, handle, request.mode, display_name)
        if isinstance(request, FileReadRequest):
            document = _read_document(backend, project_id, handle, request.path)
            end = request.end_line if request.end_line is not None else request.start_line + 199
            result = _fit_page(
                document,
                request.start_line,
                end,
                request.char_offset,
                max_chars,
                lambda page: {
                    **base,
                    **page,
                    "read_status": "partial" if page["has_more"] else "ready",
                },
            )
        elif isinstance(request, SymbolReadRequest):
            result = _read_symbol(backend, project_id, handle, request, max_chars, max_ms, base)
        elif isinstance(request, TextSearchRequest):
            result = search_source(
                backend,
                project_id,
                backend.source(project_id),
                handle,
                request.query,
                limit=request.limit,
                path_prefix=request.path_prefix,
                cursor=request.cursor,
                context_lines=request.context_lines,
                max_chars=max_chars,
                max_ms=max_ms,
                display_name=display_name,
            )
        elif isinstance(request, SymbolSearchRequest):
            result = _search_symbols(
                backend,
                project_id,
                backend.source(project_id),
                handle,
                request,
                max_chars,
                max_ms,
                base,
            )
        elif isinstance(request, BatchReadRequest):
            result = _read_batch(backend, project_id, handle, request, max_chars, max_ms, base)
        else:
            raise SourceError("INVALID_READ_REQUEST: select a supported reading mode")
        if adjustment is not None:
            result["response_budget"] = adjustment
        if json_chars(result) > response_limit:
            raise _budget_error()
        backend.resolve_snapshot(project_id, handle)
        return result
