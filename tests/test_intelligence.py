"""Integration contracts: producer/index isolation, bindings, budgets and retention."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.server import READ_TOOL_NAMES, build_mcp
from code_context.storage import MirrorError, MirrorStore, RevisionConflict

PYTHON_FILES = {
    "pkg/__init__.py": "from .models import Document\n",
    "pkg/models.py": "class Base:\n    pass\n\nclass Document(Base):\n    value: int\n",
    "pkg/helper.py": "from .models import Document\n\ndef normalize(doc: Document):\n"
    "    return doc.value\n",
    "pkg/service.py": "from .helper import normalize as clean\nfrom .models import Document\n"
    "from pydantic import BaseModel\n\ndef ingest(doc: Document):\n"
    "    return clean(doc)\n\ndef dynamic(obj):\n    return obj.clean()\n",
}
JAVA_FILES = {
    "src/main/java/demo/Base.java": "package demo; public class Base {}\n",
    "src/main/java/demo/Readable.java": "package demo; public interface Readable {"
    " String text(); }\n",
    "src/main/java/demo/Document.java": "package demo; public class Document extends Base "
    "implements Readable { public String text() { "
    'return "ok"; } }\n',
    "src/main/java/demo/Helper.java": "package demo; public class Helper { "
    "public static String clean(Document d) { "
    "return d.text(); } }\n",
    "src/main/java/api/Service.java": "package api; import demo.Document; import demo.Helper; "
    "import java.util.List; public class Service { "
    "public String ingest(Document d) { "
    "return Helper.clean(d); } }\n",
}


def upsert(path, content):
    return FileChange(op="upsert", path=path, content=content, sha256=content_hash(content))


def apply(store, files, revision=0, request_id="initial", project="sample", deletes=()):
    changes = [upsert(path, text) for path, text in files.items()]
    changes += [FileChange(op="delete", path=path) for path in deletes]
    return store.apply(
        project,
        SyncBatch(
            request_id=request_id,
            base_revision=revision,
            mode="full" if revision == 0 else "delta",
            changes=changes,
        ),
    )


def find(store, name, snapshot=None, **filters):
    result = store.code_query(
        "sample", snapshot, "symbol_search", query=name, exact=True, **filters
    )
    assert result["symbols"], (name, result)
    return result["symbols"][0]


@pytest.fixture
def indexed(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    apply(store, {**PYTHON_FILES, **JAVA_FILES, "README.md": "not source intelligence\n"})
    return store


def test_four_relationship_families_python(indexed):
    doc = find(indexed, "Document", language="python")
    clean = find(indexed, "normalize")
    ingest = find(indexed, "ingest", language="python")
    text = indexed.code_query("sample", None, "read_symbol", symbol_id=clean["symbol_id"])
    assert "def normalize" in text["content"] and "return doc.value" in text["content"]
    classes = indexed.code_query(
        "sample",
        None,
        "symbol_graph",
        symbol_id=doc["symbol_id"],
        graph="class",
        direction="outgoing",
    )
    assert any(
        e["kind"] == "INHERITANCE" and e["target_qualname"] == "Base" for e in classes["edges"]
    )
    calls = indexed.code_query(
        "sample", None, "symbol_graph", symbol_id=ingest["symbol_id"], graph="call"
    )
    assert any(e["target_symbol_id"] == clean["symbol_id"] for e in calls["edges"])
    refs = indexed.code_query("sample", None, "find_references", symbol_id=clean["symbol_id"])
    assert {e["kind"] for e in refs["references"]} >= {"IMPORT", "CALL"}
    dependencies = indexed.code_query(
        "sample", None, "file_dependencies", path="pkg/service.py", depth=3
    )
    assert {e["target"] for e in dependencies["edges"]} >= {"pkg/helper.py", "pkg/models.py"}
    reverse = indexed.code_query(
        "sample", None, "file_dependencies", path="pkg/models.py", direction="incoming", depth=3
    )
    assert {n["path"] for n in reverse["nodes"]} >= {"pkg/helper.py", "pkg/service.py"}
    external = indexed.code_query("sample", None, "external_dependencies")
    assert any(d["module"].startswith("pydantic") for d in external["dependencies"])
    assert all(
        d["classification"] in {"unavailable", "external_or_unavailable"}
        for d in external["dependencies"]
    )


def test_four_relationship_families_java(indexed):
    doc = find(indexed, "Document", language="java")
    clean = find(indexed, "clean", language="java")
    ingest = find(indexed, "ingest", language="java")
    classes = indexed.code_query(
        "sample",
        None,
        "symbol_graph",
        symbol_id=doc["symbol_id"],
        graph="class",
        direction="outgoing",
    )
    assert {e["kind"] for e in classes["edges"]} >= {"INHERITANCE", "IMPLEMENTS", "CONTAINS"}
    assert any(e["target_qualname"] == "Base" for e in classes["edges"])
    calls = indexed.code_query(
        "sample", None, "symbol_graph", symbol_id=ingest["symbol_id"], graph="call"
    )
    assert any(e["target_symbol_id"] == clean["symbol_id"] for e in calls["edges"])
    source = indexed.code_query("sample", None, "read_symbol", symbol_id=ingest["symbol_id"])
    assert "Helper.clean(d)" in source["content"]
    dependency = indexed.code_query(
        "sample", None, "file_dependencies", path=ingest["path"], depth=3
    )
    assert any(e["target"] == clean["path"] for e in dependency["edges"])


def test_architecture_layers_cycles_and_impact(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    apply(
        store,
        {
            "base.py": "class Base: pass\n",
            "mid.py": "from base import Base\n",
            "top.py": "import mid\n",
            "a.py": "import b\n",
            "b.py": "import a\n",
            "self_import.py": "import self_import\n",
        },
    )
    graph = store.code_query("sample", None, "project_architecture")
    layers = {item["path"]: item["layer"] for item in graph["files"]}
    assert layers["base.py"] < layers["mid.py"] < layers["top.py"]
    assert ["a.py", "b.py"] in graph["cycles"]
    assert ["self_import.py"] in graph["cycles"]
    assert graph["total_cycles"] == 2
    impact = store.code_query("sample", None, "impact_analysis", path="base.py")
    assert {n["path"] for n in impact["affected_files"]} == {"mid.py", "top.py"}
    assert ["base.py", "mid.py", "top.py"] in impact["chains"]


def test_static_shadowing_reexport_and_unknown_dynamic_targets(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    apply(
        store,
        {
            "lib.py": "def target(): pass\n",
            "facade.py": "from lib import target\n",
            "user.py": "from facade import target\ndef caller():\n    target()\n"
            "def shadow(target):\n    target()\n"
            "def unknown(obj):\n    obj.target()\n",
        },
    )
    symbol = find(store, "target")
    refs = store.code_query("sample", None, "find_references", symbol_id=symbol["symbol_id"])
    assert len([e for e in refs["references"] if e["kind"] == "CALL"]) == 1
    dynamic = find(store, "unknown")
    graph = store.code_query(
        "sample", None, "symbol_graph", symbol_id=dynamic["symbol_id"], graph="call"
    )
    assert graph["edges"] and all(e["resolution"] != "resolved" for e in graph["edges"])


def test_receiver_rebinding_and_nested_shadowing_are_not_guessed(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    apply(
        store,
        {
            "a.py": "class Item:\n    def target(self): pass\n"
            "    def normal(self):\n        self.target()\n"
            "    def rebound(self):\n        self = object()\n        self.target()\n"
            "    def outer(self):\n        def inner(self):\n            self.target()\n"
            "    @staticmethod\n    def static(self):\n        self.target()\n"
        },
    )
    for name in ("normal", "rebound", "inner", "static"):
        symbol = find(store, name)
        graph = store.code_query(
            "sample", None, "symbol_graph", symbol_id=symbol["symbol_id"], graph="call"
        )
        edges = [e for e in graph["edges"] if e["name"] == "self.target"]
        assert len(edges) == 1
        assert (edges[0]["resolution"] == "resolved") == (name == "normal")


def test_conditional_reexports_are_not_guessed(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    apply(
        store,
        {
            "lib.py": "def target(): pass\n",
            "facade.py": "if flag:\n    from lib import target\n",
            "consumer.py": "from facade import target\ndef caller():\n    target()\n",
        },
    )
    caller = find(store, "caller")
    graph = store.code_query(
        "sample", None, "symbol_graph", symbol_id=caller["symbol_id"], graph="call"
    )
    assert graph["edges"][0]["resolution"] == "ambiguous"
    assert graph["edges"][0]["evidence"] == "conditional_reexport_binding"


def test_java_nested_imports_and_same_arity_overloads_are_conservative(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    apply(
        store,
        {
            "demo/Outer.java": "package demo; public class Outer { public static class Inner {"
            " public static int zero() { return 0; }"
            " public static int same(int n) { return n; }"
            " public static int same(String s) { return 1; } } }\n",
            "api/User.java": "package api; import demo.Outer.Inner;"
            " import static demo.Outer.Inner.zero; public class User extends Inner {"
            " public int run() { return zero() + Inner.same(1); } }\n",
        },
    )
    run = find(store, "run", language="java")
    graph = store.code_query(
        "sample", None, "symbol_graph", symbol_id=run["symbol_id"], graph="call"
    )
    calls = {edge["name"]: edge for edge in graph["edges"]}
    assert calls["zero"]["target_qualname"] == "Outer.Inner.zero()"
    assert calls["zero"]["resolution"] == "resolved"
    assert calls["Inner.same"]["resolution"] == "ambiguous"
    user = find(store, "User", language="java")
    classes = store.code_query(
        "sample", None, "symbol_graph", symbol_id=user["symbol_id"], graph="class"
    )
    assert any(
        edge["kind"] == "INHERITANCE" and edge["target_qualname"] == "Outer.Inner"
        for edge in classes["edges"]
    )


def test_only_changed_hash_parsed_stable_relations_reused(indexed, monkeypatch):
    import code_context.intelligence_index as module

    original = module.parse_file
    parsed = []

    def observed(path, content):
        parsed.append(path)
        return original(path, content)

    monkeypatch.setattr(module, "parse_file", observed)
    apply(
        indexed,
        {
            "pkg/helper.py": PYTHON_FILES["pkg/helper.py"].replace(
                "return doc.value", "return doc.value + 1"
            )
        },
        revision=1,
        request_id="edit",
    )
    assert parsed == ["pkg/helper.py"]
    stats = indexed.code_index_status("sample", 2)
    assert stats["parsed_files"] == 1 and stats["reused_parse_files"] == 8
    assert stats["reused_relation_files"] >= 8


def test_definition_rename_rebinds_unchanged_importer_and_old_snapshot(indexed):
    first = indexed.resolve_snapshot("sample")[1]
    original = find(indexed, "normalize")
    apply(
        indexed,
        {"pkg/helper.py": PYTHON_FILES["pkg/helper.py"].replace("normalize", "changed")},
        revision=1,
        request_id="rename",
    )
    old = indexed.code_query("sample", first, "find_references", symbol_id=original["symbol_id"])
    assert any(e["kind"] == "CALL" for e in old["references"])
    new_ingest = find(indexed, "ingest", language="python")
    current = indexed.code_query(
        "sample", None, "symbol_graph", symbol_id=new_ingest["symbol_id"], graph="call"
    )
    assert all(e["target_symbol_id"] != original["symbol_id"] for e in current["edges"])
    with pytest.raises(MirrorError, match="SYMBOL_NOT_FOUND"):
        indexed.code_query("sample", None, "read_symbol", symbol_id=original["symbol_id"])


def test_delete_rename_invalid_source_restart_and_context_expiry(tmp_path):
    path = tmp_path / "mirror.db"
    store = MirrorStore(path)
    apply(store, {"a.py": "def one(): pass\n"})
    first = store.resolve_snapshot("sample")[1]
    apply(store, {"b.py": "def two(): pass\n"}, revision=1, request_id="rename", deletes=["a.py"])
    assert not store.code_query("sample", None, "symbol_search", query="one")["symbols"]
    restarted = MirrorStore(path)
    assert find(restarted, "one", first)
    apply(restarted, {"b.py": "def invalid(:\n"}, revision=2, request_id="invalid")
    status = restarted.code_index_status("sample", 3)
    assert status["partial"] and status["files_by_status"].get("parse_error", 0) >= 1
    assert restarted.read_file("sample", "b.py")["content"] == "def invalid(:\n"
    with pytest.raises(MirrorError, match="SNAPSHOT_EXPIRED"):
        restarted.code_query("sample", first, "symbol_search", query="one")


def test_query_never_parses_syncs_or_mutates(indexed, monkeypatch):
    import code_context.intelligence_index as module

    def forbidden(*args, **kwargs):
        raise AssertionError("query must never build/parse")

    monkeypatch.setattr(module, "parse_file", forbidden)
    monkeypatch.setattr(module, "build_index", forbidden)
    with indexed.read_connection() as db:
        before = list(db.iterdump())
    symbol = find(indexed, "normalize")
    for operation, parameters in [
        ("read_symbol", {"symbol_id": symbol["symbol_id"]}),
        ("find_references", {"symbol_id": symbol["symbol_id"]}),
        ("symbol_graph", {"symbol_id": symbol["symbol_id"], "graph": "call"}),
        ("file_dependencies", {"path": "pkg/service.py"}),
        ("project_architecture", {}),
        ("external_dependencies", {}),
        ("impact_analysis", {"path": "pkg/models.py"}),
    ]:
        indexed.code_query("sample", None, operation, **parameters)
    with indexed.read_connection() as db:
        assert before == list(db.iterdump())


def test_budget_caps_keep_source_available(tmp_path, monkeypatch):
    import code_context.intelligence_index as module

    monkeypatch.setattr(module, "MAX_SNAPSHOT_INDEX_BYTES", 100)
    store = MirrorStore(tmp_path / "mirror.db")
    apply(store, {"a.py": "def sample(): return 1\n"})
    stats = store.code_index_status("sample", 1)
    assert stats["partial"] and stats["index_payload_bytes"] <= 100
    assert store.read_file("sample", "a.py")["content"] == "def sample(): return 1\n"
    assert not store.code_query("sample", None, "symbol_search", query="sample")["symbols"]


def test_two_state_cache_and_index_retention_300_small_edits(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    content = "def value(): return 1\n#" + "x" * (100 * 1024 - 50)
    apply(store, {"a.py": content})
    for i in range(1, 301):
        apply(
            store, {"a.py": content + f"\n# small edit {i}\n"}, revision=i, request_id=f"edit-{i}"
        )
    with store.read_connection() as db:
        for table in ("snapshots", "ci_snapshots", "ci_files", "ci_parse_cache"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM ci_symbols").fetchone()[0] <= 4
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.database.stat().st_size < 1024 * 1024


def test_atomic_failure_idempotence_and_conflicts(tmp_path, monkeypatch):
    import code_context.storage as module

    store = MirrorStore(tmp_path / "mirror.db")
    apply(store, {"a.py": "def one(): pass\n"})
    first = store.resolve_snapshot("sample")[1]
    original = module.build_index

    def broken(db, project, revision):
        original(db, project, revision)
        raise sqlite3.OperationalError("test commit failure")

    monkeypatch.setattr(module, "build_index", broken)
    with pytest.raises(sqlite3.OperationalError):
        apply(store, {"b.py": "def two(): pass\n"}, revision=1, request_id="failed")
    assert store.resolve_snapshot("sample")[1] == first
    assert not store.code_query("sample", None, "symbol_search", query="two")["symbols"]
    monkeypatch.setattr(module, "build_index", original)
    apply(store, {"b.py": "def two(): pass\n"}, revision=1, request_id="retry")
    result = apply(store, {"b.py": "def two(): pass\n"}, revision=1, request_id="retry")
    assert result["replayed"]
    with pytest.raises(RevisionConflict):
        apply(store, {"c.py": "def three(): pass\n"}, revision=1, request_id="stale")


def test_pinned_read_transaction_while_writer_prunes(indexed):
    handle = indexed.resolve_snapshot("sample")[1]
    with indexed.read_connection() as db:
        revision, _ = indexed._resolve_snapshot(db, "sample", handle)
        before = db.execute(
            "SELECT COUNT(*) FROM ci_symbols WHERE revision=?", (revision,)
        ).fetchone()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(apply, indexed, {"new.py": "def new(): pass\n"}, 1, "second").result()
            pool.submit(apply, indexed, {"new.py": "def newer(): pass\n"}, 2, "third").result()
        after = db.execute(
            "SELECT COUNT(*) FROM ci_symbols WHERE revision=?", (revision,)
        ).fetchone()
        assert before[0] == after[0] > 0
    with pytest.raises(MirrorError, match="SNAPSHOT_EXPIRED"):
        indexed.code_query("sample", handle, "symbol_search", query="Document")


@pytest.mark.parametrize(
    "operation,parameters",
    [
        ("symbol_search", {"query": "x", "path_prefix": "../"}),
        ("symbol_search", {"query": "x", "limit": 0}),
        ("read_symbol", {"symbol_id": "wrong"}),
        ("file_dependencies", {"path": "/etc/passwd"}),
        ("file_dependencies", {"path": "pkg/service.py", "depth": 99}),
        ("project_architecture", {"max_nodes": 10000}),
    ],
)
def test_rejects_unbounded_or_outside_queries(indexed, operation, parameters):
    with pytest.raises(MirrorError):
        indexed.code_query("sample", None, operation, **parameters)


def test_output_budget_and_pagination(indexed):
    result = indexed.code_query("sample", None, "symbol_search", query="a", max_chars=2000)
    assert len(json.dumps(result, ensure_ascii=False)) <= 2000
    if result.get("truncated"):
        assert result["next_offset"] == len(result["symbols"])
    graph = indexed.code_query(
        "sample", None, "file_dependencies", path="pkg/service.py", depth=3, max_chars=1800
    )
    assert len(json.dumps(graph, ensure_ascii=False)) <= 1800
    nodes = {n["path"] for n in graph["nodes"]}
    assert all(e["source"] in nodes and e["target"] in nodes for e in graph["edges"])


def test_mcp_readonly_annotations_scope_and_actual_new_tools(indexed):
    import asyncio

    async def check():
        mcp = build_mcp(indexed, project_scope="sample", project_names={"sample": "CoLink Example"})
        tools = await mcp.list_tools()
        assert {tool.name for tool in tools} == READ_TOOL_NAMES and len(tools) == 15
        assert all(
            tool.annotations.read_only_hint and not tool.annotations.destructive_hint
            for tool in tools
        )
        result = await mcp.call_tool(
            "symbol_search", {"project_id": "sample", "query": "normalize"}
        )
        payload = result.structured_content
        assert payload["symbols"] and payload["display_name"] == "CoLink Example"
        symbol = payload["symbols"][0]
        source = await mcp.call_tool(
            "read_symbol",
            {
                "project_id": "sample",
                "snapshot": payload["snapshot"],
                "symbol_id": symbol["symbol_id"],
            },
        )
        assert "def normalize" in source.structured_content["content"]
        assert "revision" not in json.dumps(source.structured_content)
        with pytest.raises(ToolError, match="outside"):
            await mcp.call_tool("symbol_search", {"project_id": "other", "query": "normalize"})
        with pytest.raises(ToolError, match="refresh"):
            await mcp.call_tool(
                "get_call_graph",
                {"project_id": "sample", "revision": 1, "symbol_id": symbol["symbol_id"]},
            )

    asyncio.run(check())
