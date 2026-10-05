"""Source-root binding is snapshot-local, conservative, and producer-owned."""

import json

import pytest

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.storage import MirrorStore


def submit(store, files, revision=0, deletes=()):
    changes = [
        FileChange(op="upsert", path=path, content=text, sha256=content_hash(text))
        for path, text in files.items()
    ]
    changes.extend(FileChange(op="delete", path=path) for path in deletes)
    return store.apply(
        "sample",
        SyncBatch(
            request_id=f"roots-{revision}",
            base_revision=revision,
            mode="full" if revision == 0 else "delta",
            changes=changes,
        ),
    )


def symbol(store, name, snapshot=None):
    result = store.code_query("sample", snapshot, "symbol_search", query=name, exact=True)
    assert len(result["symbols"]) == 1
    return result["symbols"][0]


def calls(store, name="ingest", snapshot=None):
    return store.code_query(
        "sample",
        snapshot,
        "symbol_graph",
        symbol_id=symbol(store, name, snapshot)["symbol_id"],
        graph="call",
    )["edges"]


def configuration(roots):
    return "[tool.colink]\npython_source_roots = " + json.dumps(roots) + "\n"


def package_files(root):
    prefix = f"{root}/" if root else ""
    return {
        prefix + "pkg/__init__.py": "",
        prefix + "pkg/models.py": "class Base: pass\n",
        prefix + "pkg/helper.py": "from pkg.models import Base\n"
        "class Document(Base): pass\n"
        "def normalize(doc: Document): return doc\n",
        prefix + "pkg/service.py": "from pkg.helper import normalize as clean\n"
        "from pydantic import BaseModel\n"
        "def ingest(doc): return clean(doc)\n",
    }


@pytest.mark.parametrize("root", ["", "src", "Code", "backend", "python", "services/foo/src"])
def test_regular_package_source_roots_restore_all_relationship_families(tmp_path, root):
    store = MirrorStore(tmp_path / "mirror.db")
    files = package_files(root)
    submit(store, files)
    prefix = f"{root}/" if root else ""
    normalize = symbol(store, "normalize")
    assert any(e["target_symbol_id"] == normalize["symbol_id"] for e in calls(store))
    doc = symbol(store, "Document")
    classes = store.code_query(
        "sample", None, "symbol_graph", symbol_id=doc["symbol_id"], graph="class"
    )
    assert any(
        e["kind"] == "INHERITANCE" and e["target_path"] == prefix + "pkg/models.py"
        for e in classes["edges"]
    )
    refs = store.code_query("sample", None, "find_references", symbol_id=normalize["symbol_id"])
    assert {e["kind"] for e in refs["references"]} >= {"IMPORT", "CALL"}
    deps = store.code_query(
        "sample", None, "file_dependencies", path=prefix + "pkg/service.py", depth=3
    )
    assert {e["target"] for e in deps["edges"]} >= {
        prefix + "pkg/helper.py",
        prefix + "pkg/models.py",
    }
    architecture = store.code_query("sample", None, "project_architecture")
    layers = {f["path"]: f["layer"] for f in architecture["files"]}
    assert layers[prefix + "pkg/models.py"] < layers[prefix + "pkg/helper.py"]
    assert layers[prefix + "pkg/helper.py"] < layers[prefix + "pkg/service.py"]
    impact = store.code_query("sample", None, "impact_analysis", path=prefix + "pkg/models.py")
    assert {f["path"] for f in impact["affected_files"]} >= {
        prefix + "pkg/helper.py",
        prefix + "pkg/service.py",
    }
    external = store.code_query("sample", None, "external_dependencies")
    assert any(d["module"].startswith("pydantic") for d in external["dependencies"])
    assert not any(d["module"].startswith("pkg") for d in external["dependencies"])


def test_langgraph_layout_type_reference_binds_to_visible_metadata_definition(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    model = "Code/agent_runtime/rag/ingestion/models/metadata_models.py"
    document = "Code/agent_runtime/rag/ingestion/models/document_models.py"
    submit(
        store,
        {
            "Code/agent_runtime/__init__.py": "",
            model: "class MetadataClaim: pass\n",
            document: "from agent_runtime.rag.ingestion.models.metadata_models import (\n"
            "    MetadataClaim,\n)\n"
            "class ParsedDocument:\n    claims: list[MetadataClaim]\n",
        },
    )
    doc = symbol(store, "ParsedDocument")
    classes = store.code_query(
        "sample", None, "symbol_graph", symbol_id=doc["symbol_id"], graph="class"
    )
    assert any(
        e["kind"] == "TYPE_REFERENCE" and e["target_path"] == model for e in classes["edges"]
    )
    deps = store.code_query("sample", None, "file_dependencies", path=document)
    assert any(e["target"] == model for e in deps["edges"])
    assert "Code" in store.code_index_status("sample", 1)["python_source_roots"]


def test_multiple_inferred_roots_bind_distinct_packages(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(
        store,
        {
            "backend/pkg_a/__init__.py": "",
            "backend/pkg_a/model.py": "class Item: pass\n",
            "frontend/pkg_b/__init__.py": "",
            "frontend/pkg_b/service.py": "from pkg_a.model import Item\n",
        },
    )
    deps = store.code_query("sample", None, "file_dependencies", path="frontend/pkg_b/service.py")
    assert any(e["target"] == "backend/pkg_a/model.py" for e in deps["edges"])


def test_explicit_roots_override_inference_without_hiding_source(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(store, {**package_files("Code"), "pyproject.toml": configuration([""])})
    assert all(e["resolution"] != "resolved" for e in calls(store))
    assert "return clean(doc)" in store.read_file("sample", "Code/pkg/service.py")["content"]
    stats = store.code_index_status("sample", 1)
    assert stats["python_source_roots"] == [""]
    assert stats["python_source_roots_mode"] == "configured"


def test_namespace_packages_across_explicit_roots(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(
        store,
        {
            "pyproject.toml": configuration(["library_a", "library_b"]),
            "library_a/acme/models.py": "class Base: pass\n",
            "library_b/acme/client.py": "from acme.models import Base\nclass Client(Base): pass\n",
        },
    )
    deps = store.code_query("sample", None, "file_dependencies", path="library_b/acme/client.py")
    assert any(e["target"] == "library_a/acme/models.py" for e in deps["edges"])


@pytest.mark.parametrize("configured", [False, True])
def test_duplicate_modules_are_ambiguous_not_first_root_wins(tmp_path, configured):
    store = MirrorStore(tmp_path / "mirror.db")
    files = {
        "backend/pkg/__init__.py": "",
        "backend/pkg/helpers.py": "def value(): return 1\n",
        "legacy/pkg/__init__.py": "",
        "legacy/pkg/helpers.py": "def value(): return 2\n",
        "consumer.py": "from pkg.helpers import value\ndef ingest(): return value()\n",
    }
    if configured:
        files["pyproject.toml"] = configuration(["", "backend", "legacy"])
    submit(store, files)
    assert calls(store)[0]["resolution"] == "ambiguous"
    assert calls(store)[0]["target_symbol_id"] is None
    deps = store.code_query("sample", None, "file_dependencies", path="consumer.py")
    assert deps["edges"] == []


def test_bare_duplicate_modules_across_configured_roots(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(
        store,
        {
            "pyproject.toml": configuration(["", "src", "legacy"]),
            "src/foo.py": "def value(): return 1\n",
            "legacy/foo.py": "def value(): return 2\n",
            "consumer.py": "from foo import value\ndef ingest(): return value()\n",
        },
    )
    assert calls(store)[0]["resolution"] == "ambiguous"


def test_no_arbitrary_suffix_matching_without_package_or_configuration(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(
        store,
        {
            "unknown/vendor.py": "def value(): return 1\n",
            "consumer.py": "from vendor import value\ndef ingest(): return value()\n",
        },
    )
    assert calls(store)[0]["resolution"] != "resolved"


def test_regular_parent_package_is_not_silently_stripped(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(store, {**package_files("Code"), "Code/__init__.py": ""})
    assert calls(store)[0]["resolution"] != "resolved"


def test_root_change_rebinds_unchanged_importer_and_preserves_old_snapshot(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    files = package_files("Code")
    files.pop("Code/pkg/__init__.py")
    submit(store, files)
    first = store.resolve_snapshot("sample")[1]
    assert calls(store)[0]["resolution"] != "resolved"
    submit(store, {"Code/pkg/__init__.py": ""}, revision=1)
    assert calls(store)[0]["resolution"] == "resolved"
    assert calls(store, snapshot=first)[0]["resolution"] != "resolved"
    assert store.code_index_status("sample", 2)["reused_relation_files"] == 0
    submit(store, {}, revision=2, deletes=["Code/pkg/__init__.py"])
    assert calls(store)[0]["resolution"] != "resolved"
    assert calls(store, snapshot="previous")[0]["resolution"] == "resolved"


def test_config_change_rebinds_without_source_changes(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(store, {**package_files("Code"), "pyproject.toml": configuration(["Code"])})
    first = store.resolve_snapshot("sample")[1]
    assert calls(store)[0]["resolution"] == "resolved"
    submit(store, {"pyproject.toml": configuration([""])}, revision=1)
    assert calls(store)[0]["resolution"] != "resolved"
    assert calls(store, snapshot=first)[0]["resolution"] == "resolved"
    assert store.code_index_status("sample", 2)["parsed_files"] == 0
    assert store.code_index_status("sample", 2)["reused_relation_files"] == 0


@pytest.mark.parametrize("roots", [["../outside"], ["/outside"], ["C:\\outside"], "Code", []])
def test_invalid_config_keeps_source_and_exposes_only_generic_diagnostic(tmp_path, roots):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(store, {**package_files("Code"), "pyproject.toml": configuration(roots)})
    stats = store.code_index_status("sample", 1)
    assert stats["partial"]
    assert stats["python_source_roots_mode"] == "invalid"
    assert stats["python_source_roots_diagnostics"] == [
        {"code": "PYTHON_SOURCE_ROOT_CONFIG_INVALID", "path": "pyproject.toml"}
    ]
    assert calls(store)[0]["resolution"] != "resolved"
    assert "return clean(doc)" in store.read_file("sample", "Code/pkg/service.py")["content"]


def test_old_derived_index_rebuild_keeps_source_handles_and_retention(tmp_path):
    path = tmp_path / "mirror.db"
    store = MirrorStore(path)
    submit(store, package_files("Code"))
    submit(store, {"README.md": "second retained state\n"}, revision=1)
    with store.connection() as db:
        before = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("snapshots", "files", "blobs", "requests")
        }
        db.execute("UPDATE ci_snapshots SET parser_version='old-index-fixture'")
        db.execute("UPDATE ci_relations SET resolution='unresolved', target_path=NULL")
    assert (
        store.code_query("sample", None, "file_dependencies", path="Code/pkg/service.py")["edges"]
        == []
    )
    restarted = MirrorStore(path)
    assert calls(restarted)[0]["resolution"] == "resolved"
    assert calls(restarted, snapshot="previous")[0]["resolution"] == "resolved"
    assert restarted.code_index_status("sample", 1)["parsed_files"] == 0
    assert restarted.code_index_status("sample", 2)["parsed_files"] == 0
    with restarted.read_connection() as db:
        for table, rows in before.items():
            assert rows == [
                tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
        assert db.execute("SELECT COUNT(*) FROM ci_snapshots").fetchone()[0] == 2


def test_inference_budget_does_not_choose_an_arbitrary_subset(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    files = {}
    imports = []
    for i in range(33):
        files[f"source_{i}/pkg_{i}/__init__.py"] = ""
        files[f"source_{i}/pkg_{i}/model.py"] = "class Item: pass\n"
        imports.append(f"from pkg_{i}.model import Item as Item{i}\n")
    files["consumer.py"] = "".join(imports)
    submit(store, files)
    stats = store.code_index_status("sample", 1)
    assert stats["partial"] and stats["python_source_roots"] == [""]
    assert stats["python_source_roots_diagnostics"] == [
        {"code": "PYTHON_SOURCE_ROOT_LIMIT_EXCEEDED"}
    ]
    assert store.code_query("sample", None, "file_dependencies", path="consumer.py")["edges"] == []


@pytest.mark.parametrize(
    "config",
    [
        "[tool.colink\npython_source_roots = ['Code']",
        configuration([f"root_{i}" for i in range(33)]),
        configuration(["x" * 1025]),
        configuration([1]),
        "[tool.colink]\npython_source_roots = ['Code']\n#" + "x" * (64 * 1024),
    ],
)
def test_malformed_or_unbounded_configuration_does_not_break_source_reading(tmp_path, config):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(store, {**package_files("Code"), "pyproject.toml": config})
    stats = store.code_index_status("sample", 1)
    assert stats["partial"] and stats["python_source_roots_mode"] == "invalid"
    assert stats["python_source_roots_diagnostics"] == [
        {"code": "PYTHON_SOURCE_ROOT_CONFIG_INVALID", "path": "pyproject.toml"}
    ]
    assert "return clean(doc)" in store.read_file("sample", "Code/pkg/service.py")["content"]


def test_relative_imports_respect_custom_root_and_do_not_cross_it(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(
        store,
        {
            "pyproject.toml": configuration(["Code"]),
            "Code/pkg/__init__.py": "raise RuntimeError('must not execute project imports')\n",
            "Code/pkg/models.py": "class Item: pass\n",
            "Code/pkg/client.py": "from .models import Item\n"
            "from ...pkg.models import Item as Bad\n",
        },
    )
    deps = store.code_query("sample", None, "file_dependencies", path="Code/pkg/client.py")
    assert {e["target"] for e in deps["edges"]} == {"Code/pkg/models.py"}
    with store.read_connection() as db:
        relations = [
            json.loads(row["data"])
            for row in db.execute("SELECT data FROM ci_relations WHERE kind='IMPORT'")
        ]
    assert any(r["evidence"] == "invalid_relative_import" for r in relations)


def test_real_src_package_is_not_mistaken_for_a_source_container(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    submit(store, {**package_files("src"), "src/__init__.py": ""})
    assert calls(store)[0]["resolution"] != "resolved"


def test_custom_source_root_relationships_through_mcp_tools(tmp_path):
    import asyncio

    from code_context.server import build_mcp

    store = MirrorStore(tmp_path / "mirror.db")
    submit(store, package_files("Code"))

    async def check():
        mcp = build_mcp(store, project_scope="sample")
        overview = await mcp.call_tool("repo_overview", {"project_id": "sample"})
        context = {"project_id": "sample", "snapshot": overview.structured_content["snapshot"]}

        async def query(tool, **parameters):
            result = await mcp.call_tool(tool, {**context, **parameters})
            payload = result.structured_content
            assert payload["snapshot"] == context["snapshot"]
            return payload

        ingest = (await query("symbol_search", query="ingest", exact=True))["symbols"][0]
        normalize = (await query("symbol_search", query="normalize", exact=True))["symbols"][0]
        document = (await query("symbol_search", query="Document", exact=True))["symbols"][0]
        assert (
            "def normalize"
            in (await query("read_symbol", symbol_id=normalize["symbol_id"]))["content"]
        )
        graph = await query("get_call_graph", symbol_id=ingest["symbol_id"])
        assert any(e["target_symbol_id"] == normalize["symbol_id"] for e in graph["edges"])
        classes = await query("get_class_graph", symbol_id=document["symbol_id"])
        assert any(e["target_path"] == "Code/pkg/models.py" for e in classes["edges"])
        refs = await query("find_references", symbol_id=normalize["symbol_id"])
        assert {e["kind"] for e in refs["references"]} >= {"IMPORT", "CALL"}
        deps = await query("get_file_dependencies", path="Code/pkg/service.py", depth=3)
        assert {e["target"] for e in deps["edges"]} >= {"Code/pkg/helper.py", "Code/pkg/models.py"}
        architecture = await query("get_project_architecture")
        assert {f["path"] for f in architecture["files"]} == set(package_files("Code"))
        external = await query("get_external_dependencies")
        assert not any(d["module"].startswith("pkg") for d in external["dependencies"])
        impact = await query("get_impact_analysis", path="Code/pkg/models.py")
        assert {f["path"] for f in impact["affected_files"]} >= {
            "Code/pkg/helper.py",
            "Code/pkg/service.py",
        }

    asyncio.run(check())
