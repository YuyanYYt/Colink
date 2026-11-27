"""Large Live budgets propagate without changing Mirror limits or permissions."""

import asyncio
from dataclasses import replace

import pytest

from code_context.cli import parser
from code_context.live import LiveQueries
from code_context.live_capacity import EXTREME_CAPACITY, LARGE_CAPACITY
from code_context.live_index import LiveIndexService
from code_context.policy import MAX_FILES, MAX_TOTAL_BYTES
from code_context.project_index import ProjectIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess, SourceError


def source_backend(tmp_path, capacity=LARGE_CAPACITY):
    root = tmp_path / "source"
    root.mkdir()
    (root / "module.py").write_text(
        "".join(f"def function_{i}(value):\n    return value + {i}\n" for i in range(30))
    )
    return LiveQueries({"sample": SourceAccess(root)}, capacity=capacity)


@pytest.mark.parametrize("capacity", [LARGE_CAPACITY, EXTREME_CAPACITY])
def test_pool_live_status_and_mcp_limits_agree_without_eager_allocation(tmp_path, capacity):
    backend = source_backend(tmp_path, capacity)
    service = ProjectIndexService(tmp_path / "indexes", capacity=capacity)
    backend.index_service = service
    try:
        before = service.status("sample")
        assert before["capacity_usage"]["database_bytes"]["used"] == 0
        assert not service.path_for("sample").exists()
        mcp = build_mcp(backend)
        status = asyncio.run(mcp.call_tool("connection_status", {})).structured_content
        assert status["limits"]["max_files"] == capacity.files
        assert status["limits"]["index_facts"] == capacity.facts
        assert not status["write_enabled"] and not status["execution_enabled"]
        assert not service.path_for("sample").exists()
        result = backend.code_query("sample", None, "symbol_search", query="function_")
        assert result["total"] == 30
        after = service.status("sample")
        assert before["limits"] == after["limits"]
        usage = after["capacity_usage"]
        assert usage["index_facts"]["used"] > 30
        assert usage["index_facts"]["limit"] == capacity.facts
        assert 0 < usage["index_facts"]["percent"] < 100
        assert service.path_for("sample").stat().st_size < 2 * 1024**2
        status = asyncio.run(mcp.call_tool("connection_status", {})).structured_content
        assert status["code_indexes"][0]["capacity_usage"] == usage
        assert MAX_FILES == 50_000 and MAX_TOTAL_BYTES == 128 * 1024**2
    finally:
        backend.close()


@pytest.mark.parametrize("command", ["live", "workspace"])
def test_cli_capacity_is_explicit_local_configuration(command):
    options = parser().parse_args(
        [command, "--root", ".", "--project", "sample", "--capacity", "extreme"]
    )
    assert options.capacity == "extreme"


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"max_source_bytes": 1}, "source_bytes"),
        ({"max_parse_bytes": 500}, "parse_bytes"),
        ({"max_facts": 1}, "parse_facts"),
        ({"max_index_bytes": 2500}, "index_bytes"),
        ({"max_index_bytes": 1}, "metadata_bytes"),
    ],
)
def test_budget_diagnostics_identify_exhausted_resource_without_blocking_source(
    tmp_path, options, reason
):
    backend = source_backend(tmp_path)
    backend.index_service = LiveIndexService(tmp_path / "index", **options)
    try:
        result = backend.code_query("sample", None, "symbol_search", query="function_")
        assert result["index_partial"]
        stats = backend.index_service.status("sample")["stats"]
        assert stats["budget_exceeded_files"][reason] >= 1
        assert "return value" in backend.read_file("sample", "module.py")["content"]
    finally:
        backend.close()


def test_live_context_supports_more_than_old_file_limit(tmp_path):
    backend = source_backend(tmp_path)
    try:
        source = backend.source("sample")
        handle = backend.contexts.create("sample", source.source_id)
        for i in range(50_001):
            backend.contexts.observe("sample", source.source_id, handle, f"file{i}.py", "0" * 64)
        assert len(backend.contexts.get("sample", source.source_id, handle).files) == 50_001
    finally:
        backend.close()


def test_discovery_limits_report_actual_cause_and_never_follow_links(tmp_path):
    backend = source_backend(tmp_path, replace(LARGE_CAPACITY, files=1))
    try:
        source = backend.source("sample")
        (source.root / "second.py").write_text("VALUE = 2\n")
        (source.root / "link.py").symlink_to("module.py")
        result = backend.repo_overview("sample", None)
        assert result["discovery_partial"]
        assert result["discovery_limit_reasons"] == ["file_limit"]
        assert source.metrics["body_reads"] == 0
        with pytest.raises(SourceError, match="INVALID_BUDGET"):
            source.manifest(max_files=1_000_001)
    finally:
        backend.close()
