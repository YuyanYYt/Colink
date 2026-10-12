"""Unified source reads preserve exact text, selection and whole-response budgets."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.read_code import execute_read_code
from code_context.read_context import ReadContexts
from code_context.read_requests import READ_REQUEST_ADAPTER
from code_context.server import build_mcp
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def reader(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text(
        "def target():\n    return 1\n"
        "def repeated():\n    return 2\n"
        "def repeated():\n    return 3\n",
        encoding="utf-8",
    )
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = LiveIndexService(tmp_path / "index")
    yield root, backend
    backend.close()


def read(backend, request, snapshot=None, **options):
    return execute_read_code(
        backend,
        "sample",
        request,
        snapshot,
        display_name="独立合成项目",
        **options,
    )


def json_size(value):
    return len(json.dumps(value, ensure_ascii=False))


def test_file_pages_reassemble_crlf_unicode_and_long_escaped_lines(reader):
    root, backend = reader
    content = '"\\喻\u2028' * 600 + "\r\nnext line\r\nfinal line"
    (root / "long.txt").write_bytes(content.encode("utf-8"))
    snapshot, request, chunks = None, {"mode": "file", "path": "long.txt"}, []
    while True:
        result = read(backend, request, snapshot, max_chars=1000)
        assert json_size(result) <= 1000
        assert result["display_name"] == "独立合成项目"
        assert result["source_is_untrusted"] is True
        assert result["read_status"] == ("partial" if result["has_more"] else "ready")
        chunks.append(result["content"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            break
        request = {
            "mode": "file",
            "path": "long.txt",
            "start_line": result["next_start_line"],
            "char_offset": result["next_char_offset"],
        }
    assert "".join(chunks) == content
    assert len(chunks) > 5


def test_symbol_selector_reads_exact_body_and_ambiguous_names_need_selection(reader):
    _, backend = reader
    selected = read(backend, {"mode": "symbol", "path": "main.py", "qualname": "target"})
    assert selected["content"] == "def target():\n    return 1\n"
    assert selected["symbol"]["qualname"] == "target"
    assert selected["sha256"] and selected["read_status"] == "ready"
    ambiguous = read(
        backend,
        {"mode": "symbol", "path": "main.py", "qualname": "repeated"},
        selected["snapshot"],
    )
    assert ambiguous["read_status"] == "ambiguous"
    assert len(ambiguous["candidates"]) == 2
    assert "content" not in ambiguous
    for candidate in ambiguous["candidates"]:
        body = read(
            backend,
            {"mode": "symbol", "symbol_id": candidate["symbol_id"]},
            selected["snapshot"],
        )
        assert body["symbol"] == candidate
        assert body["content"].startswith("def repeated():")


def test_symbol_pages_continue_inside_a_long_source_line(reader):
    root, backend = reader
    content = 'def text():\n    return "' + "\\\\" * 2500 + '喻"\n'
    (root / "long.py").write_text(content, encoding="utf-8", newline="")
    request = {"mode": "symbol", "path": "long.py", "qualname": "text"}
    snapshot, chunks = None, []
    while True:
        page = read(backend, request, snapshot, max_chars=1600)
        assert json_size(page) <= 1600
        chunks.append(page["content"])
        snapshot = page["snapshot"]
        if not page["has_more"]:
            break
        request = {
            "mode": "symbol",
            "symbol_id": page["symbol"]["symbol_id"],
            "line_offset": page["next_line_offset"],
            "char_offset": page["next_char_offset"],
        }
    assert "".join(chunks) == content


def test_batch_shares_json_budget_and_reassembles_only_explicit_ranges(reader):
    root, backend = reader
    contents = ['喻\\"' * 350 + "\n", "second\n" * 50, "third\n" * 40]
    items = []
    for index, content in enumerate(contents):
        path = f"batch-{index}.txt"
        (root / path).write_text(content + "outside range\n", encoding="utf-8")
        items.append({"path": path, "end_line": content.count("\n")})
    request = {"mode": "batch", "items": items}
    snapshot, chunks, pages = None, {i: [] for i in range(len(items))}, 0
    while True:
        result = read(backend, request, snapshot, max_chars=1400)
        pages += 1
        assert json_size(result) <= 1400
        for item in result["items"]:
            chunks[item["item_index"]].append(item["content"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            break
        request = {"mode": "batch", "items": items, "cursor": result["next_cursor"]}
        assert pages < 100
    assert pages > 3
    assert ["".join(chunks[i]) for i in range(len(items))] == contents


@pytest.mark.parametrize(
    "input_request",
    [
        {"mode": "file", "path": "main.py", "start_line": True},
        {"mode": "file", "path": "main.py", "start_line": "1"},
        {"mode": "file", "path": "main.py", "start_line": 1.0},
        {"mode": "file", "path": "../outside.py"},
        {"mode": "file", "path": "/outside.py"},
        {"mode": "file", "path": "main.py", "end_line": 1001},
        {"mode": "file", "path": "main.py", "snapshot_handle": "private-input"},
        {"mode": "symbol", "path": "main.py"},
        {"mode": "symbol", "symbol_id": "sym_" + "a" * 32, "path": "main.py"},
        {"mode": "search", "kind": "text", "query": "target", "limit": "1"},
        {"mode": "search", "kind": "symbol", "query": "target", "exact": 1},
        {"mode": "search", "kind": "text", "query": "target", "language": "python"},
        {"mode": "batch", "items": [{"path": "main.py"}] * 6},
        {"mode": "batch", "items": [{"path": "main.py"}], "cursor": {"item_index": 1}},
    ],
)
def test_modes_reject_unknown_fields_unsafe_paths_and_coercion(reader, input_request):
    _, backend = reader
    with pytest.raises(SourceError, match="INVALID_READ_REQUEST") as error:
        read(backend, input_request)
    assert "private-input" not in str(error.value)


def test_failed_reads_and_budget_errors_release_only_new_contexts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "ok.txt").write_text("one\ntwo\n")
    backend = LiveQueries({"sample": SourceAccess(root)}, contexts=ReadContexts(max_contexts=1))
    try:
        for _ in range(3):
            with pytest.raises(SourceError):
                read(backend, {"mode": "file", "path": "missing.txt"})
        with pytest.raises(SourceError, match="READ_METADATA_EXCEEDS_BUDGET"):
            execute_read_code(
                backend,
                "sample",
                {"mode": "file", "path": "ok.txt"},
                max_chars=1000,
                display_name="x" * 1000,
            )
        result = read(backend, {"mode": "file", "path": "ok.txt"})
        with pytest.raises(SourceError):
            read(backend, {"mode": "file", "path": "missing.txt"}, result["snapshot"])
        assert (
            read(backend, {"mode": "file", "path": "ok.txt"}, result["snapshot"])["content"]
            == "one\ntwo\n"
        )
    finally:
        backend.close()


def test_batch_resume_requires_the_returned_context(reader):
    _, backend = reader
    with pytest.raises(SourceError, match="READ_CURSOR_CONTEXT_REQUIRED"):
        read(
            backend,
            {
                "mode": "batch",
                "items": [{"path": "main.py"}],
                "cursor": {"item_index": 0, "start_line": 1, "char_offset": 1},
            },
        )


def test_preconstructed_model_cannot_bypass_strict_validation(reader):
    _, backend = reader
    request = READ_REQUEST_ADAPTER.validate_python({"mode": "file", "path": "main.py"})
    changed = request.model_copy(update={"start_line": "1"})
    with pytest.raises(SourceError, match="INVALID_READ_REQUEST"):
        read(backend, changed)


def test_public_schema_and_strict_mcp_types_preserve_budget(reader):
    _, backend = reader
    mcp = build_mcp(backend)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert "read_code" in tools
    schema = tools["read_code"].input_schema
    assert schema["properties"]["request"]["discriminator"]["propertyName"] == "mode"
    assert (
        READ_REQUEST_ADAPTER.json_schema()["$defs"]["FileReadRequest"]["additionalProperties"]
        is False
    )
    result = asyncio.run(
        mcp.call_tool(
            "read_code",
            {
                "project_id": "sample",
                "request": {"mode": "file", "path": "main.py"},
                "max_chars": 1000,
            },
        )
    ).structured_content
    assert json_size(result) <= 1000
    for extra in ({"max_chars": "1000"}, {"snapshot_handle": "private-input"}):
        with pytest.raises(ToolError) as error:
            asyncio.run(
                mcp.call_tool(
                    "read_code",
                    {
                        "project_id": "sample",
                        "request": {"mode": "file", "path": "main.py"},
                        **extra,
                    },
                )
            )
        assert "private-input" not in str(error.value)


def test_symbol_search_cursor_covers_all_candidates_without_skipping(reader):
    root, backend = reader
    (root / "many.py").write_text("\n".join(f"def named_{i}(): return {i}" for i in range(35)))
    request = {
        "mode": "search",
        "kind": "symbol",
        "query": "named_",
        "limit": 20,
        "context_lines": 2,
    }
    snapshot, ids = None, []
    while True:
        result = read(backend, request, snapshot, max_chars=2200)
        assert json_size(result) <= 2200
        ids.extend(symbol["symbol_id"] for symbol in result["symbols"])
        snapshot = result["snapshot"]
        if not result["has_more"]:
            break
        request = {**request, "cursor": result["next_cursor"]}
    assert len(ids) == len(set(ids)) == 35
