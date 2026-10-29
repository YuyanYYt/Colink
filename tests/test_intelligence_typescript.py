"""Conservative script structure, export binding and real guarded MCP queries."""

import asyncio
import json

import pytest

import code_context.intelligence_typescript as script
from code_context.intelligence_index import parse_file
from code_context.intelligence_resolver import Resolver
from code_context.language_frontends import detect_language, structure_language
from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess


def resolve(files, path):
    parsed = {name: parse_file(name, source) for name, source in files.items()}
    return parsed, Resolver(parsed).resolve_file(path)


@pytest.mark.parametrize(
    "path,detected,supported",
    [
        ("main.py", "python", "python"),
        ("Types.pyi", "python", "python"),
        ("Main.java", "java", "java"),
        ("main.ts", "typescript", "typescript"),
        ("View.TSX", "typescript", "typescript"),
        ("main.js", "javascript", "javascript"),
        ("view.jsx", "javascript", "javascript"),
        ("main.mjs", "javascript", "javascript"),
        ("main.cjs", "javascript", "javascript"),
        ("Main.swift", "swift", "unsupported"),
        ("lib.rs", "rust", "unsupported"),
        ("View.vue", "vue", "unsupported"),
    ],
)
def test_detection_is_independent_of_structure_support(path, detected, supported):
    assert detect_language(path) == detected
    assert structure_language(path) == supported


@pytest.mark.parametrize("suffix", ["ts", "tsx", "js", "jsx", "mjs", "cjs"])
def test_definitions_scopes_and_unicode_source_free_facts(suffix):
    typed = suffix in {"ts", "tsx"}
    source = (
        "// 中文\nexport class Service { run(value"
        + (": number" if typed else "")
        + ") { return helper(value); } }\n"
        "export const helper = (item) => item;\n"
        "const secret = 'LITERAL_SENTINEL_DO_NOT_KEEP';\n"
    )
    parsed = parse_file("main." + suffix, source)
    assert parsed.status == "ready", parsed.diagnostics
    names = {s.qualname: s for s in parsed.symbols}
    assert names["Service"].kind == "class"
    assert names["Service.run"].parameters == ["value"]
    assert names["helper"].kind == "function" and names["helper"].parameters == ["item"]
    assert "LITERAL_SENTINEL_DO_NOT_KEEP" not in json.dumps(parsed.to_dict())
    assert any(r.kind == "CALL" and r.name == "helper" for r in parsed.references)


def test_relative_import_alias_reexport_and_cross_script_language_call():
    files = {
        "lib.ts": "export function helper(x: number) { return x; }\nfunction privateFn() {}",
        "barrel.ts": "export {helper as normalized} from './lib';",
        "user.js": (
            "import {normalized as clean} from './barrel'; function run(x){ return clean(x); }"
        ),
    }
    _, relations = resolve(files, "user.js")
    call = next(r for r in relations if r.kind == "CALL")
    assert call.resolution == "resolved" and call.target_path == "lib.ts"
    assert call.target_qualname == "helper" and call.evidence == "script_explicit_export"
    assert next(r for r in relations if r.kind == "IMPORT").target_path == "lib.ts"


def test_private_definition_and_wildcard_reexport_are_not_guessed():
    files = {
        "lib.ts": "function hidden(){}; export function visible(){}",
        "barrel.ts": "export * from './lib'",
        "user.ts": (
            "import {hidden, visible} from './lib'; "
            "import {visible as viaStar} from './barrel'; hidden(); visible(); viaStar();"
        ),
    }
    _, relations = resolve(files, "user.ts")
    calls = {r.name: r for r in relations if r.kind == "CALL"}
    assert calls["visible"].resolution == "resolved"
    assert calls["hidden"].resolution == "unresolved"
    assert calls["viaStar"].resolution == "unresolved"
    assert calls["viaStar"].evidence == "script_wildcard_export_partial"


def test_default_namespace_and_side_effect_imports():
    _, relations = resolve(
        {
            "lib.js": (
                "export default function normalize(x){ return x }; export function helper(){}"
            ),
            "user.ts": (
                "import normalize from './lib'; import * as ns from './lib'; "
                "import './lib'; normalize(1); ns.helper(); ns();"
            ),
        },
        "user.ts",
    )
    calls = {r.name: r for r in relations if r.kind == "CALL"}
    assert calls["normalize"].target_qualname == "normalize"
    assert calls["ns.helper"].target_qualname == "helper"
    assert calls["ns"].resolution == "unresolved"
    assert all(r.target_path == "lib.js" for r in relations if r.kind == "IMPORT")


@pytest.mark.parametrize(
    "body",
    [
        "function run(helper) { helper(); }",
        "function run({helper}) { helper(); }",
        "function run() { const {helper} = obj; helper(); }",
        "function run() { {var helper = obj;} helper(); }",
        "function run() { let helper = obj; helper(); }",
    ],
)
def test_parameter_pattern_and_variable_shadowing_do_not_bind_global(body):
    _, relations = resolve(
        {
            "lib.ts": "export function helper() {}",
            "user.ts": "import {helper} from './lib'; " + body,
        },
        "user.ts",
    )
    calls = [r for r in relations if r.kind == "CALL" and r.name == "helper"]
    assert calls and all(r.resolution == "unresolved" for r in calls)


def test_local_export_alias_cycle_and_ambiguous_source_extensions():
    _, local = resolve(
        {
            "lib.ts": (
                "function privateName(v:number) {return v}; export {privateName as publicName}"
            ),
            "user.ts": "import {publicName} from './lib'; publicName(1)",
        },
        "user.ts",
    )
    assert next(r for r in local if r.kind == "CALL").target_qualname == "privateName"
    _, cyclic = resolve(
        {
            "a.ts": "export {work} from './b'",
            "b.ts": "export {work} from './a'",
            "user.ts": "import {work} from './a'; work()",
        },
        "user.ts",
    )
    assert next(r for r in cyclic if r.kind == "CALL").evidence == "cyclic_or_deep_reexport"
    _, ambiguous = resolve(
        {
            "lib.ts": "export function work(){}",
            "lib.js": "export function work(){}",
            "user.ts": "import {work} from './lib'; work()",
        },
        "user.ts",
    )
    assert next(r for r in ambiguous if r.kind == "CALL").resolution == "ambiguous"


def test_bare_packages_type_only_imports_dynamic_members_and_commonjs_are_limited():
    parsed, relations = resolve(
        {
            "lib.ts": "export interface Model {}; export function helper(){}",
            "user.ts": (
                "import type {helper, Model} from './lib'; import {remote} from '@alias/lib'; "
                "function run(obj: Model) { helper(); remote(); obj.method(); "
                "require('LITERAL_SECRET'); }"
            ),
        },
        "user.ts",
    )
    calls = {r.name: r for r in relations if r.kind == "CALL"}
    assert calls["helper"].evidence == "type_only_import_is_not_runtime_value"
    assert calls["remote"].resolution == "external_or_unavailable"
    assert calls["obj.method"].resolution == "unresolved"
    assert parsed["user.ts"].status == "partial"
    assert "LITERAL_SECRET" not in json.dumps(parsed["user.ts"].to_dict())


def test_resource_error_and_parser_exception_never_echo_source(monkeypatch):
    monkeypatch.setattr(script, "MAX_FILE_NODES", 5)
    limited = parse_file("a.ts", "function helper(value:number){return value}")
    assert limited.status == "resource_limited"
    monkeypatch.setattr(script, "MAX_FILE_NODES", 250_000)

    def fail(_):
        raise RuntimeError("PRIVATE_SOURCE_SENTINEL")

    monkeypatch.setattr(script, "Parser", fail)
    failed = parse_file("a.ts", "const secret = 'PRIVATE_SOURCE_SENTINEL'")
    assert failed.status == "parse_error"
    assert "PRIVATE_SOURCE_SENTINEL" not in json.dumps(failed.to_dict())


def test_module_record_is_not_a_shadowing_lexical_definition():
    _, relations = resolve(
        {
            "main.js": "export function main(value){return value}; main(1)",
            "user.ts": "import {main} from './main'; main(2)",
        },
        "main.js",
    )
    assert next(r for r in relations if r.kind == "CALL").target_qualname == "main"
    _, imported = resolve(
        {
            "main.js": "export function main(value){return value}",
            "user.ts": "import {main} from './main'; main(2)",
        },
        "user.ts",
    )
    assert next(r for r in imported if r.kind == "CALL").target_qualname == "main"


def test_language_dispatch_tracks_language_without_unrelated_provider_declarations():
    files = {
        "provider.py": "def helper(value):\n    return value\n",
        "caller.py": "from provider import helper\ndef run(value):\n    return helper(value)\n",
    }
    parsed = {path: parse_file(path, source) for path, source in files.items()}
    before = Resolver(parsed, track_dependencies=True)
    before.resolve_file("caller.py")
    reads = before.binding_reads.files["caller.py"]
    keys = [json.loads(key) for key in reads]
    assert ["languages", "provider.py"] in keys
    assert ["files", "provider.py"] not in keys
    changed = dict(files)
    changed["provider.py"] += "\ndef unrelated():\n    pass\n"
    after = Resolver(
        {path: parse_file(path, source) for path, source in changed.items()},
        track_dependencies=True,
    )
    assert after.binding_reads.unchanged(reads)
    retyped = {path: parse_file(path, source) for path, source in files.items()}
    retyped["provider.py"].language = "javascript"
    changed_language = Resolver(retyped, track_dependencies=True)
    assert not changed_language.binding_reads.unchanged(reads)


def test_namespace_object_methods_and_nested_this_do_not_leak_bindings():
    _, relations = resolve(
        {
            "main.ts": (
                "namespace Hidden { function helper(){} }; helper(); "
                "const obj={ run(){ helper(); } }; run(); "
                "class Service { run(){ function normal(){this.run();} "
                "const arrow=()=>this.run(); } }"
            )
        },
        "main.ts",
    )
    calls = [r for r in relations if r.kind == "CALL"]
    assert all(r.resolution == "unresolved" for r in calls if r.name in {"helper", "run"})
    assert next(r for r in calls if r.name == "<dynamic>.run").resolution == "unresolved"
    assert (
        next(r for r in calls if r.name == "this.run").evidence
        == "script_receiver_possible_dispatch"
    )


def test_type_parameter_and_type_only_reexport_do_not_create_runtime_calls():
    _, relations = resolve(
        {
            "lib.ts": "export function helper(){}; export class Model{}",
            "barrel.ts": "export type {helper} from './lib'",
            "main.ts": (
                "import {helper} from './barrel'; import {Model} from './lib'; "
                "function run<Model>(value: Model) {helper(); return value;}"
            ),
        },
        "main.ts",
    )
    assert next(r for r in relations if r.kind == "CALL").resolution == "unresolved"
    types = [r for r in relations if r.kind == "TYPE_REFERENCE" and r.name == "Model"]
    assert types and all(r.resolution == "unresolved" for r in types)


def test_java_method_reference_is_declaration_evidence_and_never_call():
    parsed, relations = resolve(
        {
            "Refs.java": (
                "class Refs { static int target(int v){return v;} void overloaded(){} "
                "void overloaded(int v){} void run(Refs item){use(Refs::target); "
                "use(item::target); use(Refs::overloaded); use(Refs::new);} }"
            )
        },
        "Refs.java",
    )
    refs = {r.name: r for r in relations if r.kind == "REFERENCE" and "::" in r.name}
    assert refs["Refs::target"].target_qualname == "Refs.target(int)"
    assert refs["Refs::target"].evidence == "java_method_reference_possible_dispatch"
    assert refs["item::target"].target_qualname == "Refs.target(int)"
    assert refs["Refs::overloaded"].resolution == "ambiguous"
    assert refs["Refs::new"].resolution == "unresolved"
    assert all("::" not in r.name for r in relations if r.kind == "CALL")
    assert parsed["Refs.java"].status == "ready"


def test_real_mcp_script_search_call_path_references_architecture_and_unsupported_flow(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "lib.ts").write_text("export function helper(value:number){return value+1}\n")
    (root / "main.js").write_text(
        "import {helper} from './lib'; export function run(value){return helper(value)}\n"
    )
    backend = LiveQueries({"sample": SourceAccess(root)})
    service = LiveIndexService(tmp_path / "index")
    backend.index_service = service
    mcp = build_mcp(backend, project_scope="sample")

    def call(name, **arguments):
        result = asyncio.run(mcp.call_tool(name, {"project_id": "sample", **arguments}))
        assert not result.is_error, result
        return result.structured_content

    try:
        found = call(
            "read_code",
            request={
                "mode": "search",
                "kind": "symbol",
                "query": "helper",
                "exact": True,
                "language": "typescript",
            },
        )
        handle = found["snapshot"]
        helper = found["symbols"][0]
        result = call(
            "analyze_code",
            snapshot=handle,
            mode="call_path",
            selector={"path": "main.js", "qualname": "run"},
            target={"symbol_id": helper["symbol_id"]},
        )
        assert any(e.get("target_symbol_id") == helper["symbol_id"] for e in result["edges"])
        refs = call("find_references", snapshot=handle, symbol_id=helper["symbol_id"])
        assert {r["kind"] for r in refs["references"]} >= {"IMPORT", "CALL"}
        architecture = call("get_project_architecture", snapshot=handle)
        assert "lib.ts" in json.dumps(architecture) and "main.js" in json.dumps(architecture)
        flow = call(
            "analyze_code",
            snapshot=handle,
            mode="data_impact",
            selector={
                "symbol_id": helper["symbol_id"],
                "value": {"kind": "parameter", "name": "value"},
            },
        )
        assert flow["coverage"]["availability"] == "unsupported_language"
        assert flow["nodes"] == flow["edges"] == []
        assert "DATAFLOW_LANGUAGE_UNSUPPORTED" in {d["code"] for d in flow["diagnostics"]}
        assert flow["conclusion"]["absence_proven"] is False
        assert len(json.dumps(flow, ensure_ascii=False)) <= 12000
    finally:
        backend.close()
        service.close()


def test_export_rename_rebinds_unchanged_importer_in_new_live_context(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "lib.ts").write_text("export function helper(value:number){return value}\n")
    (root / "main.js").write_text(
        "import {helper} from './lib'; function run(value){return helper(value)}\n"
    )
    backend = LiveQueries({"sample": SourceAccess(root)})
    service = LiveIndexService(tmp_path / "index")
    backend.index_service = service
    try:
        handle = backend.resolve_snapshot("sample")[0]
        found = backend.code_query("sample", handle, "symbol_search", query="run", exact=True)
        run_id = found["symbols"][0]["symbol_id"]
        before = backend.code_query(
            "sample", handle, "symbol_graph", symbol_id=run_id, graph="call"
        )
        assert any(e.get("target_qualname") == "helper" for e in before["edges"])
        (root / "lib.ts").write_text("export function renamed(value:number){return value}\n")
        fresh = backend.resolve_snapshot("sample")[0]
        backend.code_query("sample", fresh, "symbol_search", query="renamed", exact=True)
        after = backend.code_query("sample", fresh, "symbol_graph", symbol_id=run_id, graph="call")
        assert not any(e.get("target_qualname") == "helper" for e in after["edges"])
        assert any(
            e.get("name") == "helper" and e.get("resolution") == "unresolved"
            for e in after["edges"]
        )
    finally:
        backend.close()
        service.close()
