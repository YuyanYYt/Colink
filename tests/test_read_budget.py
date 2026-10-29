"""Oversized read requests stay bounded and retain exact continuation semantics."""

import asyncio
import hashlib
import json

import pytest
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.read_code import execute_read_code
from code_context.server import build_mcp
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def reader(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "small.py").write_text("def target():\n    return 1\n")
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = LiveIndexService(tmp_path / "index")
    yield root, backend
    backend.close()


def read(backend, request, snapshot=None, **options):
    return execute_read_code(backend, "sample", request, snapshot, **options)


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def bounded(page, requested):
    assert size(page) <= 50_000
    assert page["response_budget"] == {
        "requested_max_chars": requested,
        "effective_max_chars": 50_000,
        "adjusted": True,
    }


@pytest.mark.parametrize("requested", [50_001, 100_000, (1 << 53) - 1])
def test_oversized_file_budget_pages_exact_escaped_unicode_source(reader, requested):
    root, backend = reader
    content = ('"\\喻\u2028' * 22_000) + "\r\nlast line\r\n"
    path = root / "long.txt"
    path.write_bytes(content.encode())
    original = hashlib.sha256(path.read_bytes()).digest()
    request = {"mode": "file", "path": "long.txt"}
    snapshot, chunks = None, []
    while True:
        page = read(backend, request, snapshot, max_chars=requested)
        bounded(page, requested)
        chunks.append(page["content"])
        snapshot = page["snapshot"]
        if not page["has_more"]:
            break
        assert page["content"]
        request = {
            "mode": "file",
            "path": "long.txt",
            "start_line": page["next_start_line"],
            "char_offset": page["next_char_offset"],
        }
        assert len(chunks) < 20
    assert len(chunks) > 1
    assert "".join(chunks) == content
    assert hashlib.sha256(path.read_bytes()).digest() == original


def test_oversized_batch_keeps_one_total_budget_and_reassembles_each_item(reader):
    root, backend = reader
    contents = ['喻\\"' * 20_000 + "\r\n", "second\n" * 200, "third\n" * 100]
    items = []
    for index, content in enumerate(contents):
        name = f"batch-{index}.txt"
        (root / name).write_bytes((content + "outside range\n").encode())
        items.append({"path": name, "end_line": content.count("\n")})
    request = {"mode": "batch", "items": items}
    snapshot, chunks, pages = None, {i: [] for i in range(len(items))}, 0
    while True:
        result = read(backend, request, snapshot, max_chars=100_000)
        bounded(result, 100_000)
        pages += 1
        for item in result["items"]:
            chunks[item["item_index"]].append(item["content"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            break
        request = {"mode": "batch", "items": items, "cursor": result["next_cursor"]}
        assert pages < 20
    assert pages > 1
    assert ["".join(chunks[i]) for i in range(len(items))] == contents


def test_oversized_symbol_budget_keeps_source_and_symbol_paging(reader):
    root, backend = reader
    content = 'def long_value():\n    return "' + "\\\\" * 25_000 + '喻"\n'
    (root / "long.py").write_bytes(content.encode())
    request = {"mode": "symbol", "path": "long.py", "qualname": "long_value"}
    snapshot, chunks = None, []
    while True:
        result = read(backend, request, snapshot, max_chars=100_000)
        bounded(result, 100_000)
        assert result["symbol"]["qualname"] == "long_value"
        chunks.append(result["content"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            break
        request = {
            "mode": "symbol",
            "symbol_id": result["symbol"]["symbol_id"],
            "line_offset": result["next_line_offset"],
            "char_offset": result["next_char_offset"],
        }
        assert len(chunks) < 20
    assert len(chunks) > 1
    assert "".join(chunks) == content


def test_oversized_text_search_budget_paginates_every_match(reader):
    root, backend = reader
    (root / "hits.txt").write_text(
        "".join(f"needle {number} " + '\\"' * 500 + "\n" for number in range(180))
    )
    request = {
        "mode": "search",
        "kind": "text",
        "query": "needle",
        "path_prefix": "hits.txt",
        "limit": 200,
    }
    snapshot, positions, pages = None, [], 0
    while True:
        result = read(backend, request, snapshot, max_chars=100_000)
        bounded(result, 100_000)
        pages += 1
        positions.extend(match["line"] for match in result["matches"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            assert result["search_complete"]
            break
        request = {**request, "cursor": result["next_cursor"]}
        assert pages < 20
    assert pages > 1
    assert positions == list(range(1, 181))


def test_oversized_symbol_search_budget_paginates_every_candidate(reader):
    root, backend = reader
    (root / "many.py").write_text(
        "".join(f"def candidate_{i:03d}():\n    return {i}\n" for i in range(150))
    )
    request = {
        "mode": "search",
        "kind": "symbol",
        "query": "candidate_",
        "path_prefix": "many.py",
        "limit": 100,
        "context_lines": 3,
    }
    snapshot, names, pages = None, [], 0
    while True:
        result = read(backend, request, snapshot, max_chars=100_000, max_ms=10_000)
        bounded(result, 100_000)
        pages += 1
        names.extend(symbol["qualname"] for symbol in result["symbols"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            break
        request = {**request, "cursor": result["next_cursor"]}
        assert pages < 20
    assert pages > 1
    assert sorted(names) == [f"candidate_{i:03d}" for i in range(150)]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("budget", [1000, 20_000, 50_000])
def test_in_range_budget_keeps_unchanged_response_envelope(reader, budget):
    _, backend = reader
    result = read(backend, {"mode": "file", "path": "small.py"}, max_chars=budget)
    assert size(result) <= budget
    assert "response_budget" not in result


@pytest.mark.parametrize(
    "budget", [-1, 0, 999, True, 100_000.0, "100000", None, 1 << 53, float("inf"), float("nan")]
)
def test_budget_tolerance_does_not_accept_invalid_types_or_unbounded_integers(reader, budget):
    _, backend = reader
    with pytest.raises(SourceError, match="INVALID_READ_BUDGET"):
        read(backend, {"mode": "file", "path": "small.py"}, max_chars=budget)


@pytest.mark.parametrize("max_ms", [99, 10_001, True, "3000"])
def test_oversized_char_request_does_not_expand_time_limit(reader, max_ms):
    _, backend = reader
    with pytest.raises(SourceError, match="INVALID_READ_BUDGET"):
        read(
            backend,
            {"mode": "file", "path": "small.py"},
            max_chars=100_000,
            max_ms=max_ms,
        )


def test_public_schema_sdk_and_backend_accept_the_same_oversized_integer(reader):
    _, backend = reader
    mcp = build_mcp(backend)
    schema = mcp._tool_manager.get_tool("read_code").parameters
    arguments = {
        "project_id": "sample",
        "request": {"mode": "file", "path": "small.py"},
        "max_chars": 100_000,
    }
    Draft202012Validator(schema).validate(arguments)
    reply = asyncio.run(mcp.call_tool("read_code", arguments))
    assert not reply.is_error
    bounded(reply.structured_content, 100_000)
    assert reply.structured_content["content"] == "def target():\n    return 1\n"
    for value in ("100000", True, 100_000.0):
        with pytest.raises(ToolError):
            asyncio.run(mcp.call_tool("read_code", {**arguments, "max_chars": value}))
