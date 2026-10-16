"""Partial indexes describe usable facts separately from unavailable analysis."""

import json

import pytest

from code_context.intelligence_coverage import public_index_stats
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.models import FileChange, SyncBatch, content_hash
from code_context.read_code import execute_read_code
from code_context.source_access import SourceAccess
from code_context.storage import MirrorStore


@pytest.fixture
def mixed_source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    files = {
        "good.py": "def run(value):\n    return value\n",
        "bad.py": "def broken(\n",
        "Service.java": "class Service { int run(int value) { return value; } }\n",
        "client.ts": "export const run = (value: number) => value;\n",
        "README.md": "Source description\n",
    }
    for path, content in files.items():
        (root / path).write_text(content)
    return root, files


def _live(root, tmp_path, **options):
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = LiveIndexService(tmp_path / "index", **options)
    handle = backend.resolve_snapshot("sample")[0]
    return backend, handle


def test_live_partial_success_reports_files_and_preserves_source_reading(mixed_source, tmp_path):
    root, files = mixed_source
    backend, handle = _live(root, tmp_path)
    try:
        result = backend.code_query("sample", handle, "symbol_search", query="run")
        coverage = result["index_coverage"]
        assert result["symbols"] and result["index_partial"]
        assert coverage["status"] == "partial"
        assert coverage["files"] == {
            "known": 5,
            "structure_ready": 2,
            "structure_limited": 1,
            "not_analyzed": 2,
            "metadata_omitted": 0,
        }
        assert coverage["limitations"] == {"parse_error": 1}
        assert coverage["limitation_codes"] == {"PYTHON_PARSE_ERROR": 1}
        details = {item["path"]: item for item in coverage["details"]}
        assert details["bad.py"]["diagnostics"][0]["code"] == "PYTHON_PARSE_ERROR"
        assert details["good.py"]["capability"] == "structure_extracted"
        assert details["client.ts"]["capability"] == "structure_unavailable"
        assert coverage["details_are_samples"] and coverage["static_only"]
        assert backend.read_file("sample", "bad.py", handle)["content"] == files["bad.py"]
    finally:
        backend.close()


def test_requested_file_is_prioritized_over_partial_index_examples(mixed_source, tmp_path):
    root, _ = mixed_source
    backend, handle = _live(root, tmp_path)
    try:
        result = backend.code_query(
            "sample",
            handle,
            "analyze_code",
            mode="file_context",
            selector={"path": "client.ts"},
            max_chars=6000,
        )
        assert result["analysis_status"] == "partial"
        assert result["index_coverage"]["details"][0]["path"] == "client.ts"
        assert result["index_coverage"]["details"][0]["status"] == "unsupported"
        assert result["index_coverage"]["details"][0]["language"] == "typescript"
    finally:
        backend.close()


def test_unsupported_file_is_partial_even_when_supported_files_indexed_successfully(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "good.py").write_text("def run(): return 1\n")
    (root / "client.ts").write_text("export const run = () => 1;\n")
    backend, handle = _live(root, tmp_path)
    try:
        result = backend.code_query(
            "sample",
            handle,
            "analyze_code",
            mode="file_context",
            selector={"path": "client.ts"},
            max_chars=1000,
        )
        assert not result["index_partial"]
        assert result["analysis_status"] == "partial"
        assert result["index_coverage"]["details_omitted"]
    finally:
        backend.close()


def test_metadata_budget_omissions_are_distinct_from_parse_failures(mixed_source, tmp_path):
    root, files = mixed_source
    backend, handle = _live(root, tmp_path, max_index_bytes=1)
    try:
        result = backend.code_query("sample", handle, "symbol_search", query="run")
        coverage = result["index_coverage"]
        assert result["index_partial"] and not result["symbols"]
        assert coverage["files"]["known"] == 5
        assert coverage["files"]["metadata_omitted"] == 5
        assert coverage["limitations"]["metadata_budget"] == 5
        assert "parse_error" not in coverage["limitations"]
        assert backend.read_file("sample", "good.py", handle)["content"] == files["good.py"]
    finally:
        backend.close()


def test_discovery_limits_and_exclusions_do_not_claim_complete_inventory(
    mixed_source, tmp_path, monkeypatch
):
    root, _ = mixed_source
    source = SourceAccess(root)
    original = source.manifest

    def manifest(**options):
        return {
            **original(**options),
            "partial": True,
            "skipped": {"too_large.py": "file_size_limit"},
        }

    monkeypatch.setattr(source, "manifest", manifest)
    backend = LiveQueries({"sample": source})
    backend.index_service = LiveIndexService(tmp_path / "index")
    try:
        handle = backend.resolve_snapshot("sample")[0]
        result = backend.code_query("sample", handle, "symbol_search", query="run")
        coverage = result["index_coverage"]
        assert not coverage["inventory_complete"]
        assert coverage["limitations"]["discovery_incomplete"] == 1
        assert coverage["limitations"]["source_discovery_excluded"] == 1
        excluded = next(item for item in coverage["details"] if item["path"] == "too_large.py")
        assert excluded["status"] == "discovery_excluded"
        assert excluded["source_reading"].startswith("excluded")
    finally:
        backend.close()


def test_mirror_has_same_partial_success_contract_and_bounded_pages(mixed_source, tmp_path):
    _, files = mixed_source
    files["long.py"] = "def long_method():\n" + "    value = 1\n" * 300
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    store.apply(
        "sample",
        SyncBatch(
            request_id="coverage",
            base_revision=0,
            mode="full",
            changes=[
                FileChange(op="upsert", path=path, content=text, sha256=content_hash(text))
                for path, text in files.items()
            ],
        ),
    )
    result = store.code_query("sample", None, "symbol_search", query="run")
    assert result["index_coverage"]["files"]["structure_ready"] == 3
    assert result["index_coverage"]["files"]["not_analyzed"] == 2
    assert result["index_coverage"]["limitations"]["parse_error"] == 1
    symbol = store.code_query("sample", None, "symbol_search", query="long_method")["symbols"][0]
    page = store.code_query(
        "sample", None, "read_symbol", symbol_id=symbol["symbol_id"], max_chars=2000
    )
    assert len(json.dumps(page, ensure_ascii=False)) <= 2000
    assert page["content"] and page["has_more"]
    assert "index_coverage" in page


@pytest.mark.parametrize("budget", [1000, 2000, 6000])
def test_coverage_does_not_break_complete_response_budget(mixed_source, tmp_path, budget):
    root, _ = mixed_source
    backend, handle = _live(root, tmp_path)
    try:
        result = backend.code_query(
            "sample", handle, "symbol_search", query="run", limit=1, max_chars=budget
        )
        assert len(json.dumps(result, ensure_ascii=False)) <= budget
        assert result["index_coverage"]["files"]["structure_limited"] == 1
    finally:
        backend.close()


def test_public_stats_do_not_expose_internal_dependency_declarations():
    stats = {
        "partial": False,
        "dependency_metadata": {
            "manifests": [{"path": "pom.xml", "declarations": [{"name": "private_name"}]}],
            "partial": True,
            "diagnostics": [{"code": "DEPENDENCY_MANIFEST_LIMIT"}],
        },
    }
    public = public_index_stats(stats)
    assert "dependency_metadata" not in public
    assert "private_name" not in json.dumps(public)
    assert public["dependency_manifest_summary"]["manifest_count"] == 1
    assert public["dependency_manifest_summary"]["partial"]


@pytest.mark.parametrize("budget", [1000, 2000, 6000])
@pytest.mark.parametrize("mode", ["symbol", "search"])
def test_unified_reader_refits_internal_coverage_to_outer_budget(
    mixed_source, tmp_path, budget, mode
):
    root, _ = mixed_source
    backend, handle = _live(root, tmp_path)
    request = (
        {"mode": "symbol", "path": "good.py", "qualname": "run"}
        if mode == "symbol"
        else {"mode": "search", "kind": "symbol", "query": "run", "limit": 1}
    )
    try:
        result = execute_read_code(backend, "sample", request, handle, max_chars=budget)
        assert len(json.dumps(result, ensure_ascii=False)) <= budget
        assert result["index_coverage"]["status"] == "partial"
        if budget < 3200:
            assert result["index_coverage"]["details_omitted"]
        if mode == "symbol":
            assert result["content"]
    finally:
        backend.close()
