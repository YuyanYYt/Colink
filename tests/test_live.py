import asyncio
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from code_context.live import LiveQueries
from code_context.server import build_mcp
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def backend(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "a.py").write_text("before\n")
    (tmp_path / "b" / "b.py").write_text("other project\n")
    return LiveQueries({"a": SourceAccess(tmp_path / "a"), "b": SourceAccess(tmp_path / "b")})


def test_overview_never_mirrors_bodies_and_read_is_current(backend):
    handle, _ = backend.resolve_snapshot("a")
    result = backend.repo_overview("a", handle)
    assert result["source_mode"] == "live" and not result["history_available"]
    assert backend.sources["a"].metrics["body_reads"] == 0
    assert backend.sources["b"].metrics["body_reads"] == 0
    assert backend.read_file("a", "a.py", handle)["content"] == "before\n"
    file = backend.sources["a"].root / "a.py"
    file.write_text("after\n")
    with pytest.raises(SourceError, match="LIVE_CONTEXT_INVALID"):
        backend.read_file("a", "a.py", handle)
    fresh, _ = backend.resolve_snapshot("a")
    assert backend.read_file("a", "a.py", fresh)["content"] == "after\n"
    assert backend.sources["b"].metrics["body_reads"] == 0


def test_project_context_and_history_never_silently_fall_back(backend):
    handle, _ = backend.resolve_snapshot("a")
    with pytest.raises(SourceError):
        backend.read_file("b", "b.py", handle)
    for selector in ["previous", "snap_" + "0" * 32, "live_unknown"]:
        with pytest.raises(SourceError):
            backend.resolve_snapshot("a", selector)
    with pytest.raises(SourceError, match="LIVE_HISTORY_UNAVAILABLE"):
        backend.get_recent_diff("a", handle)
    with pytest.raises(SourceError, match="PROJECT_NOT_AUTHORIZED"):
        backend.resolve_snapshot("unknown")


def test_search_and_pagination_are_source_bounded(backend):
    source = backend.sources["a"]
    (source.root / "long.py").write_text("x" * 4000 + "\n")
    handle, _ = backend.resolve_snapshot("a")
    result = backend.search_code("a", "before", handle)
    assert result["matches"][0]["path"] == "a.py"
    first = backend.read_file("a", "long.py", handle, max_chars=1000)
    second = backend.read_file(
        "a",
        "long.py",
        handle,
        start_line=first["next_start_line"],
        max_chars=1000,
        char_offset=first["next_char_offset"],
    )
    assert len(first["content"] + second["content"]) == 2000
    assert backend.sources["b"].metrics["body_reads"] == 0


def test_live_mcp_keeps_read_annotations_and_explains_contexts(backend):
    async def inspect():
        mcp = build_mcp(
            backend, project_names={"a": "A", "b": "B"}, status_provider=backend.mcp_status
        )
        tools = await mcp.list_tools()
        assert len(tools) == 15
        assert all(tool.annotations.read_only_hint for tool in tools)
        overview = next(tool for tool in tools if tool.name == "repo_overview")
        assert "not an immutable historical" in overview.description
        result = await mcp.call_tool("repo_overview", {"project_id": "a"})
        assert not result.is_error
        assert result.structured_content["source_mode"] == "live"

    asyncio.run(inspect())


def test_live_stdio_reads_new_saved_content_without_source_database(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    file = root / "a.py"
    file.write_text("before\n")
    data = tmp_path / "data"

    async def inspect():
        process = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                "-m",
                "code_context",
                "live",
                "--root",
                str(root),
                "--project",
                "sample",
                "--data-dir",
                str(data),
            ],
        )
        async with stdio_client(process) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                initialized = await session.initialize()
                assert "not immutable historical" in initialized.instructions
                overview = await session.call_tool("repo_overview", {"project_id": "sample"})
                snapshot = overview.structured_content["snapshot"]
                before = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "a.py", "snapshot": snapshot}
                )
                assert before.structured_content["content"] == "before\n"
                file.write_text("after\n")
                stale = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "a.py", "snapshot": snapshot}
                )
                assert stale.is_error
                fresh = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "a.py"}
                )
                assert fresh.structured_content["content"] == "after\n"

    asyncio.run(inspect())
    assert not data.exists()
