import pytest

from code_context.intelligence_resolver import Resolver
from code_context.language_frontends import parse_live_source, parse_source
from code_context.vite_aliases import MAX_VITE_CONFIG_BYTES, is_vite_config, parse_vite_config


def config(source, path="web/vite.config.ts"):
    return parse_vite_config(path, source)


CALLBACK = """
import { defineConfig, loadEnv } from 'vite'
import { resolve as rootPath } from 'node:path'
export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, process.cwd(), '')
    return {server: {target: env.API}, resolve: {alias: {'@': rootPath(__dirname, './src')}}}
})
"""


@pytest.mark.parametrize(
    "source",
    [
        CALLBACK,
        """import * as path from 'path'
           export default {resolve:{alias:{'@':path.join(__dirname, 'src')}}}""",
        """import {fileURLToPath} from 'node:url'
           export default {resolve:{alias:[{
             find:'@',replacement:fileURLToPath(new URL('./src', import.meta.url))
           }]}}""",
        """import {fileURLToPath, URL} from 'node:url'
           export default {resolve:{alias:{
             '@':fileURLToPath(new URL('./src', import.meta.url))
           }}}""",
        "export default {resolve: {alias: {'@': './src'}}} satisfies UserConfig",
        "module.exports = {resolve: {alias: {'@': './src'}}}",
    ],
)
def test_static_forms_resolve_relative_to_the_config(source):
    value = config(source)
    assert [(a.key, a.path) for a in value.aliases] == [("@", "web/src")]
    assert not value.blocked and not value.diagnostics


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    [
        (
            "const nested = {resolve:{alias:{'@':'./trap'}}}; export default selectConfig()",
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        ("export default {resolve:{alias:{[getKey()]:'./src'}}}", "VITE_ALIAS_CONFIG_DYNAMIC"),
        ("export default {resolve:{alias:{'@':'./src', ...extra}}}", "VITE_ALIAS_CONFIG_DYNAMIC"),
        ("export default {resolve:{alias:{'@':'./src', '@':'./lib'}}}", "VITE_ALIAS_AMBIGUOUS"),
        (
            "export default {resolve:{alias:[{find:/^@/, replacement:'./src'}]}}",
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        ("export default {resolve:{alias:{'@':readSecret()}}}", "VITE_ALIAS_CONFIG_DYNAMIC"),
        ("export default {resolve:{alias:{'@':'third-party'}}}", "VITE_ALIAS_CONFIG_DYNAMIC"),
        ("export default {resolve:{alias:{'@':'../../private'}}}", "VITE_ALIAS_OUTSIDE_INVENTORY"),
        ("export default {resolve:{alias:{'@':'/host/private'}}}", "VITE_ALIAS_OUTSIDE_INVENTORY"),
        (
            "export default {resolve:{alias:{'@':'./node_modules/private'}}}",
            "VITE_ALIAS_OUTSIDE_INVENTORY",
        ),
        (
            "const resolve = fake(); "
            "export default {resolve:{alias:{'@':resolve(__dirname, './src')}}}",
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        (
            CALLBACK.replace("const env =", "const rootPath = custom(); const env ="),
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        (
            CALLBACK.replace("const env =", "const __dirname = custom(); const env ="),
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        (
            CALLBACK.replace("const env =", "if (mode) return other; const env ="),
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        (CALLBACK.replace("'vite'", "'node:vite'"), "VITE_ALIAS_CONFIG_DYNAMIC"),
        (CALLBACK.replace("'node:path'", "'./path'"), "VITE_ALIAS_CONFIG_DYNAMIC"),
        (
            CALLBACK.replace("({ mode }) =>", "function rootPath({ mode })"),
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        (
            "module.exports={resolve:{alias:{'@':'./src'}}};"
            "if (mode()) module.exports=otherConfig()",
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        (
            "import {fileURLToPath} from 'url'; export const URL=Shadow;"
            "export default {resolve:{alias:{'@':"
            "fileURLToPath(new URL('./src',import.meta.url))}}}",
            "VITE_ALIAS_CONFIG_DYNAMIC",
        ),
        ("export default {resolve:{alias:", "VITE_ALIAS_CONFIG_PARSE_ERROR"),
        (None, "VITE_ALIAS_CONFIG_UNAVAILABLE"),
        (" " * (MAX_VITE_CONFIG_BYTES + 1), "VITE_ALIAS_CONFIG_LIMIT"),
    ],
)
def test_dynamic_ambiguous_or_unavailable_config_returns_source_free_diagnostics(
    source, diagnostic
):
    value = config(source)
    assert value.diagnostics == (diagnostic,)
    assert not any(alias.path for alias in value.aliases)
    assert "readSecret" not in repr(value) and "/host/private" not in repr(value)


def test_nearest_config_segment_matching_and_unique_script_candidates():
    content = {
        "web/src/stores/watch.js": """import {http} from '@/api/http'
            import x from '@other/api/http'
            import missing from '@/none'
            import outside from '@/../../../host'
            export {unknown} from './barrel'
            const component = () => import('@/App.vue')
            const dynamic = (suffix) => import('@/pages/' + suffix)
            http()
        """,
        "web/src/App.vue": "<script setup>const component = true</script>",
        "web/src/api/http.js": "export function http() {}",
        "web/src/stores/barrel.js": "export const value = 1",
        "other/main.ts": "import x from '@/api/http'",
        "other/src/api/http.js": "export function http() {}",
    }
    parsed = {path: parse_live_source(path, value) for path, value in content.items()}
    local = config(CALLBACK)
    resolver = Resolver(parsed, live_modules=True, vite_configs={local.path: local})
    facts = resolver.resolve_file("web/src/stores/watch.js")
    modules = [r for r in facts if r.kind == "MODULE_IMPORT"]
    by_name = {r.name: r for r in modules}
    assert by_name["@/api/http"].target_path == "web/src/api/http.js"
    assert by_name["@/api/http"].config_path == "web/vite.config.ts"
    assert by_name["@other/api/http"].resolution == "external_or_unavailable"
    assert by_name["@/none"].resolution == "unavailable"
    assert by_name["@/../../../host"].evidence == "vite_alias_outside_inventory"
    assert by_name["./barrel"].module_syntax == "reexport"
    assert by_name["@/App.vue"].module_syntax == "dynamic_import"
    assert len(by_name) == 6 and all(r.target_symbol_id is None for r in modules)
    assert [
        (r.name, r.target_path) for r in facts if r.kind == "CALL" and r.resolution == "resolved"
    ] == [("http", "web/src/api/http.js")]
    assert all(
        r.resolution != "resolved"
        for r in resolver.resolve_file("other/main.ts")
        if r.kind == "MODULE_IMPORT"
    )
    legacy = Resolver({p: parse_source(p, s) for p, s in content.items()})
    legacy_facts = legacy.resolve_file("web/src/stores/watch.js")
    assert all(r.kind != "MODULE_IMPORT" for r in legacy_facts)
    assert not any(r.name == "http" and r.resolution == "resolved" for r in legacy_facts)


def test_binding_reads_notice_new_closer_configs_ambiguous_targets_and_unavailable_shadow():
    src = "web/nested/main.ts"
    sources = {src: "import item from '@/item'", "web/src/item.ts": "export const item = 1"}
    files = {p: parse_live_source(p, s) for p, s in sources.items()}
    ancestor = config(CALLBACK)
    configs = {ancestor.path: ancestor}

    def build(inputs, rules):
        resolver = Resolver(inputs, live_modules=True, vite_configs=rules, track_dependencies=True)
        output = next(r for r in resolver.resolve_file(src) if r.kind == "MODULE_IMPORT")
        return resolver, output

    original, module = build(files, configs)
    prior_reads = original.binding_reads.files[src]
    assert module.target_path == "web/src/item.ts"
    unrelated = config(None, "other/vite.config.js")
    after, _ = build(files, {**configs, unrelated.path: unrelated})
    assert after.binding_reads.unchanged(prior_reads)
    child = config(None, "web/nested/vite.config.js")
    after, module = build(files, {**configs, child.path: child})
    assert not after.binding_reads.unchanged(prior_reads)
    assert module.target_path is None and module.config_path == child.path
    assert module.evidence == "vite_alias_config_unavailable"
    files["web/src/item.js"] = parse_live_source("web/src/item.js", "")
    after, module = build(files, configs)
    assert not after.binding_reads.unchanged(prior_reads)
    assert module.resolution == "ambiguous" and module.target_path is None
    files["web/src/item.js"].status = "resource_limited"
    _, module = build(files, configs)
    assert module.resolution == "ambiguous" and module.target_path is None
    second = config(CALLBACK, "web/vite.config.js")
    _, module = build(files, {**configs, second.path: second})
    assert module.evidence == "vite_config_scope_ambiguous"
    assert is_vite_config("nested/vite.config.mts") and not is_vite_config("vite.config.example.ts")
