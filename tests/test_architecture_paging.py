"""Architecture pages preserve every file/edge across hard response cropping."""

import asyncio
import json
import sqlite3

import pytest

from code_context import intelligence_queries as queries
from code_context.intelligence_index import SCHEMA
from code_context.intelligence_models import Relation
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess
from code_context.storage import MirrorStore


class SyntheticLiveFacts:
    source_mode = "live"

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)


def graph_db(size, *, cycle=False, prefix="pkg/"):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    paths = [f"{prefix}f{i:05}.py" for i in range(size)]
    for index, path in enumerate(paths):
        db.execute("INSERT INTO ci_files VALUES ('sample',1,?,'python','ready','','{}')", (path,))
        target = paths[(index + 1) % size]
        if index + 1 < size or cycle:
            relation = Relation(
                source_path=path,
                target_path=target,
                kind="IMPORT",
                name=target,
                line=1,
                resolution="resolved",
                evidence="fixture",
            )
            db.execute(
                "INSERT INTO ci_relations VALUES ('sample',1,?,NULL,?,NULL,'IMPORT',"
                "'resolved',1,?)",
                (path, target, json.dumps(relation.to_dict())),
            )
    return SyntheticLiveFacts(db), paths


def test_all_graph_partitions_resume_after_both_budget_crops():
    db, paths = graph_db(70, cycle=True)
    seen_files, seen_edges, cursor = [], [], None
    try:
        for _ in range(200):
            result = queries.project_architecture(
                db,
                "sample",
                1,
                cursor=cursor,
                max_nodes=11,
                max_edges=13,
            )
            result = queries.bound_result(result, 4200)
            # Public MCP wrappers add display_name and run the bound a second time.
            result = queries.bound_result({**result, "display_name": "name" * 100}, 4200)
            assert len(json.dumps(result, ensure_ascii=False)) <= 4200
            assert result["total_files"] == result["total_edges"] == 70
            assert result["file_offset"] == len(seen_files)
            assert result["edge_offset"] == len(seen_edges)
            seen_files.extend(item["path"] for item in result["files"])
            seen_edges.extend((edge["source"], edge["target"]) for edge in result["edges"])
            assert result["files"] or result["edges"]
            cursor = result["next_cursor"]
            if cursor is None:
                break
        else:
            pytest.fail("architecture paging did not finish")
        assert set(seen_files) == set(paths) and len(seen_files) == len(paths)
        assert set(seen_edges) == set(zip(paths, paths[1:] + paths[:1], strict=True))
        assert len(seen_edges) == len(paths)
    finally:
        db.close()


def test_large_scc_has_bounded_samples_and_complete_mergeable_memberships():
    db, paths = graph_db(2500, cycle=True)
    try:
        result = queries.project_architecture(db, "sample", 1, max_nodes=5000, max_edges=10_000)
        assert len(result["files"]) == len(paths)
        assert result["next_cursor"] is None
        assert result["cycle_summaries_truncated"]
        assert {item["cycle_group_size"] for item in result["files"]} == {2500}
        assert len({item["cycle_group_id"] for item in result["files"]}) == 1
        assert all(len(item["cycle_group"]) == 32 for item in result["files"])
        assert all(item["cycle_group_sampled"] for item in result["files"])
        assert len(json.dumps(result, ensure_ascii=False)) < 4_000_000
    finally:
        db.close()


def test_5000_files_and_10000_edges_merge_without_gaps_or_duplicates():
    """Synthetic stored graph, not a claim about 5000 real parsed source files."""
    db, paths = graph_db(5000)
    expected = set(zip(paths[:-1], paths[1:], strict=True))
    extra = list(zip(paths[:-2], paths[2:], strict=True)) + [(path, path) for path in paths[:3]]
    for source, target in extra:
        relation = Relation(
            source_path=source,
            target_path=target,
            kind="IMPORT",
            name=target,
            line=2,
            resolution="resolved",
            evidence="fixture",
        )
        db.execute(
            "INSERT INTO ci_relations VALUES ('sample',1,?,NULL,?,NULL,'IMPORT','resolved',2,?)",
            (source, target, json.dumps(relation.to_dict())),
        )
        expected.add((source, target))
    assert len(expected) == 10_000
    seen_files, seen_edges, cursor = [], [], None
    try:
        for _ in range(200):
            page = queries.bound_result(
                queries.project_architecture(
                    db, "sample", 1, max_nodes=5000, max_edges=10_000, cursor=cursor
                ),
                50_000,
            )
            assert len(json.dumps(page, ensure_ascii=False)) <= 50_000
            assert page["file_offset"] == len(seen_files)
            assert page["edge_offset"] == len(seen_edges)
            seen_files.extend(item["path"] for item in page["files"])
            seen_edges.extend((edge["source"], edge["target"]) for edge in page["edges"])
            assert page["files"] or page["edges"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        else:
            pytest.fail("synthetic architecture paging did not finish")
        assert len(seen_files) == len(set(seen_files)) == len(paths)
        assert set(seen_files) == set(paths)
        assert len(seen_edges) == len(set(seen_edges)) == len(expected)
        assert set(seen_edges) == expected
    finally:
        db.close()


@pytest.mark.parametrize("prefix", ["pkg_%/", "pkg/" + chr(0x10FFFF)])
def test_prefix_limits_decoding_and_is_literal_case_sensitive(monkeypatch, prefix):
    db, paths = graph_db(25, prefix=prefix)
    db.execute("INSERT INTO ci_files VALUES ('sample',1,'PKG_%/other.py','python','ready','','{}')")
    db.execute(
        "INSERT INTO ci_relations VALUES ('sample',1,'unrelated.py',NULL,'elsewhere.py',"
        "NULL,'IMPORT','resolved',1,'not valid fact JSON')"
    )
    decoded, original = [], queries.decode_relation

    def decode(payload):
        result = original(payload)
        decoded.append(result["source_path"])
        return result

    monkeypatch.setattr(queries, "decode_relation", decode)
    try:
        result = queries.project_architecture(
            db, "sample", 1, path_prefix=prefix.rstrip(chr(0x10FFFF))
        )
        assert {item["path"] for item in result["files"]} == set(paths)
        assert len(decoded) == 24
    finally:
        db.close()


@pytest.mark.parametrize("cursor", ["invalid", "e30", "WzEsImdyYXBoIiwwLHRydWVd", 3])
def test_invalid_cursor_is_explicit(cursor):
    db, _ = graph_db(3)
    try:
        with pytest.raises(ValueError, match="INVALID_ARCHITECTURE_CURSOR"):
            queries.project_architecture(db, "sample", 1, cursor=cursor)
    finally:
        db.close()


def test_cursor_rejects_changed_query_scope_and_tiny_response_budget():
    db, _ = graph_db(25)
    try:
        page = queries.project_architecture(db, "sample", 1, max_nodes=1)
        with pytest.raises(ValueError, match="STALE_ARCHITECTURE_CURSOR"):
            queries.project_architecture(
                db, "sample", 1, cursor=page["next_cursor"], path_prefix="pkg/f0000"
            )
        with pytest.raises(ValueError, match="budget|BUDGET"):
            queries.bound_result({**page, "display_name": "long name" * 200}, 1000)
    finally:
        db.close()


def test_live_cursor_rejects_source_edit_with_identical_graph(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("import b\n")
    (root / "b.py").write_text("value = 1\n")
    service = LiveIndexService(tmp_path / "index")
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = service
    try:
        snapshot = backend.resolve_snapshot("sample")[0]
        first = backend.code_query("sample", snapshot, "project_architecture", max_nodes=1)
        assert first["next_cursor"]
        (root / "b.py").write_text("value = 2\n")
        snapshot = backend.resolve_snapshot("sample")[0]
        with pytest.raises(ValueError, match="STALE_ARCHITECTURE_CURSOR"):
            backend.code_query(
                "sample", snapshot, "project_architecture", cursor=first["next_cursor"]
            )
    finally:
        backend.close()
        service.close()


def test_live_module_imports_rebind_after_config_and_vue_edits(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for directory in ("first", "second"):
        folder = root / directory
        folder.mkdir()
        (folder / "Child.vue").write_text("<script setup>const value = 1</script>\n")
    (root / "extra.js").write_text("export const value = 1\n")
    (root / "App.vue").write_text(
        "<template><div/></template>\n<script setup>\nimport Child from '@/Child.vue'\n</script>\n"
    )

    def alias(directory):
        (root / "vite.config.js").write_text(
            "import { resolve } from 'node:path'\n"
            "export default {resolve: {alias: {'@': resolve(__dirname, './" + directory + "')}}}\n"
        )

    def open_index(name):
        backend = LiveQueries({"sample": SourceAccess(root)})
        backend.index_service = LiveIndexService(tmp_path / name)
        return backend

    def graph(backend):
        snapshot = backend.resolve_snapshot("sample")[0]
        result = backend.code_query("sample", snapshot, "project_architecture")
        return {(e["source"], e["target"]): e["evidence"] for e in result["edges"]}

    alias("first")
    live = open_index("live")
    try:
        initial = graph(live)
        pair = ("App.vue", "first/Child.vue")
        assert [e["line"] for e in initial[pair] if e.get("scope") == "module"] == [3]
        assert graph(live) == initial
        alias("second")
        updated = graph(live)
        assert pair not in updated and ("App.vue", "second/Child.vue") in updated
        (root / "App.vue").write_text("<script setup>import './extra.js'</script>\n")
        edited = graph(live)
        assert ("App.vue", "extra.js") in edited and ("App.vue", "second/Child.vue") not in edited
        fresh = open_index("fresh")
        try:
            assert graph(fresh) == edited
        finally:
            fresh.close()
    finally:
        live.close()


def test_file_and_member_evidence_remain_separate_at_the_public_entrypoint(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "constants.py").write_text("VERSION = 3\n")
    (root / "types_only.py").write_text("class Document: pass\n")
    (root / "main.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "from constants import VERSION, misspelled\n"
        "if TYPE_CHECKING:\n    from types_only import Document\n"
        "def run():\n    return misspelled()\n"
    )
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = LiveIndexService(tmp_path / "index")
    try:
        mcp = build_mcp(backend)

        def query(name, **args):
            reply = asyncio.run(mcp.call_tool(name, {"project_id": "sample", **args}))
            assert not reply.is_error
            return reply.structured_content

        graph = query("get_project_architecture", include_type_only=True)
        assert {e["target"] for e in graph["edges"]} == {"constants.py", "types_only.py"}
        runtime = query("get_project_architecture", include_type_only=False)
        assert [(e["source"], e["target"]) for e in runtime["edges"]] == [
            ("main.py", "constants.py")
        ]
        result = query(
            "analyze_code",
            selector={"path": "main.py"},
            include=["dependencies", "calls"],
            direction="outgoing",
            max_chars=35000,
        )
        deps = result["dependencies"]
        evidence = next(e["evidence"] for e in deps["edges"] if e["target"] == "constants.py")
        assert [item["line"] for item in evidence if item.get("scope") == "module"] == [2]
        members = [d for d in deps["diagnostics"] if d["scope"] == "member"]
        assert any(d["line"] == 2 and d["resolution"] == "unresolved" for d in members)
        call = next(e for e in result["calls"]["edges"] if e["name"] == "misspelled")
        assert call["resolution"] != "resolved" and call.get("target_symbol_id") is None
        reverse = query(
            "analyze_code",
            selector={"path": "constants.py"},
            include=["dependencies"],
            direction="incoming",
            max_chars=20000,
        )
        assert [e["source"] for e in reverse["dependencies"]["edges"]] == ["main.py"]
    finally:
        backend.close()


def test_css_file_endpoint_obeys_source_filters_and_requires_an_explicit_import(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.js").write_text("import './theme.css'\nimport './private.css'\n")
    (root / "theme.css").write_text("body { margin: 0 }\n")
    (root / "unused.css").write_text("body { padding: 0 }\n")
    (root / "private.css").write_text("password = fixture_value\n")
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = LiveIndexService(tmp_path / "index")
    try:

        def graph():
            handle = backend.resolve_snapshot("sample")[0]
            return backend.code_query("sample", handle, "project_architecture")

        first = graph()
        assert first["structure_file_count"] == first["stylesheet_dependency_count"] == 1
        assert first["total_files"] == 2
        assert [(e["source"], e["target"]) for e in first["edges"]] == [("app.js", "theme.css")]
        assert (
            next(f for f in first["files"] if f["path"] == "theme.css")["role"]
            == "stylesheet_dependency"
        )
        (root / "private.css").write_text("p { margin: 0 }\n")
        assert {e["target"] for e in graph()["edges"]} == {"private.css", "theme.css"}
        (root / "app.js").write_text("import './theme'\n")
        assert not graph()["edges"] and graph()["stylesheet_dependency_count"] == 0
    finally:
        backend.close()


def test_multiple_vite_configs_report_scope_conflict_even_without_importers(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    config = "export default {resolve: {alias: {'@': './src'}}}\n"
    (root / "vite.config.mts").write_text(config)
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = LiveIndexService(tmp_path / "index")
    try:

        def scope_conflicts():
            handle = backend.resolve_snapshot("sample")[0]
            result = backend.code_query("sample", handle, "symbol_search", query="unused")
            return result["index_coverage"]["limitation_codes"].get(
                "VITE_CONFIG_SCOPE_AMBIGUOUS", 0
            )

        assert scope_conflicts() == 0
        second = root / "vite.config.cts"
        second.write_text(config)
        assert scope_conflicts() == 2
        second.rename(root / "archived.cts")
        assert scope_conflicts() == 0
    finally:
        backend.close()


def test_public_mcp_schema_exposes_continuation_and_follows_pages(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for i in range(4):
        (root / f"f{i}.py").write_text("value = 1\n")
    service = LiveIndexService(tmp_path / "index")
    backend = LiveQueries({"sample": SourceAccess(root)})
    backend.index_service = service
    try:
        mcp = build_mcp(backend)
        tools = asyncio.run(mcp.list_tools())
        tool = next(tool for tool in tools if tool.name == "get_project_architecture")
        assert "cursor" in tool.input_schema["properties"]
        seen, cursor = [], None
        for _ in range(4):
            result = asyncio.run(
                mcp.call_tool(
                    "get_project_architecture",
                    {
                        "project_id": "sample",
                        "max_nodes": 1,
                        "cursor": cursor,
                    },
                )
            )
            assert not result.is_error
            page = result.structured_content
            seen.extend(item["path"] for item in page["files"])
            cursor = page["next_cursor"]
        assert set(seen) == {f"f{i}.py" for i in range(4)} and cursor is None
    finally:
        backend.close()
        service.close()


def test_mirror_keeps_legacy_schema_node_selection_and_bounds(tmp_path):
    db, paths = graph_db(25)
    try:
        page = queries.project_architecture(db.connection, "sample", 1, max_nodes=1)
        assert page["files"][0]["path"] == paths[0]
        assert not page["edges"]
        assert "next_cursor" not in page
        with pytest.raises(ValueError, match="bounds"):
            queries.project_architecture(db.connection, "sample", 1, max_nodes=501)
    finally:
        db.close()
    store = MirrorStore(tmp_path / "mirror.db")
    mcp = build_mcp(store)
    tools = asyncio.run(mcp.list_tools())
    tool = next(tool for tool in tools if tool.name == "get_project_architecture")
    assert "cursor" not in tool.input_schema["properties"]
