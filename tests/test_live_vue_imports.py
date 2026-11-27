"""Live-only file import facts retain original Vue locations and mirror semantics."""

import json

import pytest

from code_context import intelligence_vue
from code_context.intelligence_models import ParsedFile
from code_context.language_frontends import (
    live_structure_language,
    parse_live_source,
    parse_source,
    structure_language,
)


def modules(parsed):
    return [ref for ref in parsed.references if ref.kind.startswith("MODULE_")]


def test_original_vue_coordinates_for_both_scripts_and_unicode():
    content = (
        '<template title=">"><div><script>import "./nested"</script></div>'
        '{{ "<script>" }} {{ "</template>" }}</template>\r\n'
        '<!-- <script>import "./comment"</script> --><script>/* 中文 */ '
        'export {fn} from "./common";</script>\r\n'
        '<script setup generic="T"\r\n lang="ts">/* 🙂 */ import type {Model} from "./model";\r\n'
        'function open(value: Model) { if (value) return import("./detail").then(use); }\r\n'
        "</script><style>.x::before{content:\"<script>import 'bad'</script>\"}</style>"
    )
    parsed = parse_live_source("src/View.vue", content)
    assert parsed.language == live_structure_language("src/View.vue") == "vue"
    assert {d["code"] for d in parsed.diagnostics} == {
        "VUE_TEMPLATE_NOT_ANALYZED",
        "VUE_SCRIPT_ATTRIBUTES_PARTIAL",
    }
    refs = modules(parsed)
    assert [(r.kind, r.name, r.line) for r in refs] == [
        ("MODULE_REEXPORT", "./common", 2),
        ("MODULE_IMPORT", "./model", 4),
        ("MODULE_DYNAMIC_IMPORT", "./detail", 5),
    ]
    for ref in refs:
        assert ref.column == content.splitlines()[ref.line - 1].index(f'"{ref.name}"') + 1
    assert refs[1].type_only
    assert refs[2].conditional and refs[2].source_qualname.endswith(".open")
    assert next(s for s in parsed.symbols if s.name == "open").start_line == 5
    assert len([s for s in parsed.symbols if s.kind == "module"]) == 1
    assert ParsedFile.from_dict(parsed.to_dict()) == parsed
    assert structure_language("src/View.vue") == "unsupported"
    assert parse_source("src/View.vue", content).status == "unsupported"


@pytest.mark.parametrize("path", ["routes.js", "routes.ts"])
def test_live_script_module_occurrences_are_independent_of_members_and_calls(path):
    content = (
        "import a, {b, c} from './barrel'; import './side';\n"
        "export {a} from './barrel'; export * from './star';\n"
        "const routes = [{load: () => import( /* chunk */ './page.vue').then(fn)}];\n"
        "const constant = import(`./固定`);\n"
        "import('./' + computed); import(`./${part}`); import.meta.glob('./pages/*.vue');"
    )
    parsed = parse_live_source(path, content)
    assert [(r.kind, r.name, r.line) for r in modules(parsed)] == [
        ("MODULE_IMPORT", "./barrel", 1),
        ("MODULE_IMPORT", "./side", 1),
        ("MODULE_REEXPORT", "./barrel", 2),
        ("MODULE_REEXPORT", "./star", 2),
        ("MODULE_DYNAMIC_IMPORT", "./page.vue", 3),
        ("MODULE_DYNAMIC_IMPORT", "./固定", 4),
    ]
    assert not modules(parse_source(path, content))
    assert {r.name for r in parsed.references if r.kind == "IMPORT"} >= {
        "./barrel.default",
        "./barrel.b",
        "./barrel.c",
    }
    issues = {d["code"] for d in parsed.diagnostics}
    assert {"SCRIPT_DYNAMIC_IMPORT_PARTIAL", "SCRIPT_PLUGIN_IMPORT_PARTIAL"} <= issues
    assert not any(
        r.kind == "CALL" and r.name in {"./page.vue", "./固定"} for r in parsed.references
    )


def test_type_only_file_occurrences_and_separate_setup_scope():
    source = (
        '<script lang="ts">export {type A, type B} from "./only";'
        'import {type A, V} from "./mixed"; type M = import("./type").M;</script>\n'
        '<script setup lang="ts">function local() {} local()</script>'
    )
    parsed = parse_live_source("Model.vue", source)
    assert [r.type_only for r in modules(parsed)] == [True, False, True]
    assert all(s.scope == "<script setup>" for s in parsed.symbols if s.name == "local")
    assert next(r for r in parsed.references if r.kind == "CALL").scope == "<script setup>"


@pytest.mark.parametrize("failed_first", [True, False])
def test_blocks_keep_independent_diagnostics_and_facts(monkeypatch, failed_first):
    import tree_sitter_typescript

    def unavailable():
        raise RuntimeError("PRIVATE_PARSER_ERROR")

    monkeypatch.setattr(tree_sitter_typescript, "language_typescript", unavailable)
    failed = '<script lang="ts">import "./lost"</script>'
    good = '<script setup>export {item} from "./present"; import "./used"</script>'
    content = "\n".join((failed, good) if failed_first else (good, failed))
    parsed = parse_live_source("Split.vue", content)
    assert parsed.status == "partial"
    assert [(ref.name, ref.line) for ref in modules(parsed)] == [
        ("./present", 2 if failed_first else 1),
        ("./used", 2 if failed_first else 1),
    ]
    assert not any(item.local_name == "item" for item in parsed.imports)
    assert {
        "code": "VUE_SCRIPT_PARSE_ERROR",
        "line": 1 if failed_first else 2,
        "column": failed.index(">") + 2,
    } in parsed.diagnostics
    assert "PRIVATE_PARSER_ERROR" not in json.dumps(parsed.to_dict())


@pytest.mark.parametrize(
    "body, code",
    [
        ('<script src="./external"></script>', "VUE_EXTERNAL_SCRIPT_PARTIAL"),
        ('<script lang="coffee">import "./bad"</script>', "VUE_SCRIPT_LANGUAGE_UNSUPPORTED"),
        ('<script :lang="mode">import "./bad"</script>', "VUE_SCRIPT_ATTRIBUTES_PARTIAL"),
        (
            '<script>import "./one"</script><script>import "./two"</script>',
            "VUE_DUPLICATE_SCRIPT_BLOCK",
        ),
        ('<script>import "./incomplete"', "VUE_MALFORMED_BLOCK"),
        ("<template><input/><b>text</b></template>", "VUE_NO_INLINE_SCRIPT"),
    ],
)
def test_partial_coverage_never_guesses_a_file(body, code):
    parsed = parse_live_source("Partial.vue", body)
    assert parsed.status == "partial" and not modules(parsed)
    assert code in {d["code"] for d in parsed.diagnostics}


def test_custom_language_raw_text_and_node_and_byte_limits(monkeypatch):
    content = (
        '<docs><script>import "./invented"</script></docs>'
        '<template lang="pug">p <script>import "./wrong"</script></template>'
        '<script setup>import "./real"</script>'
    )
    parsed = parse_live_source("My.vue", content)
    assert [r.name for r in modules(parsed)] == ["./real"]
    assert "VUE_CUSTOM_BLOCK_NOT_ANALYZED" in {d["code"] for d in parsed.diagnostics}
    first = '<script>import "./one"</script>'
    # Each script fits on its own but the two scripts share one file budget.
    monkeypatch.setattr(intelligence_vue, "MAX_FILE_NODES", 12)
    assert parse_live_source("Tiny.vue", first).status == "ready"
    both = parse_live_source("Tiny.vue", first + first.replace("<script>", "<script setup>"))
    assert both.status == "resource_limited" and both.diagnostics == [
        {"code": "VUE_RESOURCE_LIMIT"}
    ]
    monkeypatch.setattr(intelligence_vue, "MAX_PARSE_BYTES", 20)
    limited = parse_live_source("Long.vue", "<script>/*私密源码*/</script>")
    assert limited.status == "resource_limited" and not modules(limited)
    assert "私密源码" not in json.dumps(limited.to_dict(), ensure_ascii=False)
