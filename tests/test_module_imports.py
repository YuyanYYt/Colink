from code_context.binding_dependencies import declaration_summary, summary_file
from code_context.intelligence_resolver import Resolver
from code_context.intelligence_roots import PythonSourceRoots
from code_context.language_frontends import parse_live_source


def test_direct_python_module_survives_an_unbound_member_and_retains_callable_reexports():
    files = {
        "pkg/__init__.py": "",
        "pkg/main.py": (
            "from .chunking import PIPELINE_VERSION, typo\n"
            "from .facade import run\n"
            "run()\n"
            "typo()\n"
            "from . import chunking\n"
            "from .chunking import *\n"
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from .chunking import MyType\n"
        ),
        "pkg/chunking.py": "PIPELINE_VERSION=1\ndef run(): pass\nclass MyType: pass\n",
        "pkg/facade.py": "from .chunking import run\n",
    }
    parsed = {p: parse_live_source(p, s) for p, s in files.items()}
    facts = Resolver(parsed, live_modules=True).resolve_file("pkg/main.py")
    modules = [r for r in facts if r.kind == "MODULE_IMPORT" and r.target_path]
    assert [(r.line, r.target_path, r.type_only) for r in modules] == [
        (1, "pkg/chunking.py", False),
        (2, "pkg/facade.py", False),
        (5, "pkg/chunking.py", False),
        (6, "pkg/chunking.py", False),
        (9, "pkg/chunking.py", True),
    ]
    assert all(r.target_symbol_id is None for r in modules)
    original = [r for r in facts if r.kind == "IMPORT" and r.line == 1]
    assert all(r.resolution == "unresolved" and r.target_symbol_id is None for r in original)
    calls = {r.name: r for r in facts if r.kind == "CALL"}
    assert calls["run"].target_path == "pkg/chunking.py"
    assert calls["typo"].resolution != "resolved"
    assert not any(r.kind == "MODULE_IMPORT" for r in Resolver(parsed).resolve_file("pkg/main.py"))


def test_multiple_roots_missing_and_filtered_sources_never_create_a_file_target():
    files = {
        "caller.py": (
            "from pkg import missing\nfrom unknown import member\n"
            "from blocked import name\nfrom . import wrong\n"
        ),
        "one/pkg/__init__.py": "",
        "two/pkg/__init__.py": "",
        "one/pkg/missing.py": "def name(): pass\n",
        "blocked.py": "",
    }
    parsed = {p: parse_live_source(p, s) for p, s in files.items()}
    parsed["blocked.py"].status = "resource_limited"
    facts = Resolver(parsed, PythonSourceRoots(("", "one", "two")), live_modules=True).resolve_file(
        "caller.py"
    )
    modules = [r for r in facts if r.kind == "MODULE_IMPORT"]
    assert [r.resolution for r in modules] == [
        "ambiguous",
        "external_or_unavailable",
        "unavailable",
        "unresolved",
    ]
    assert all(r.target_path is None and r.target_symbol_id is None for r in modules)
    assert not any(r.resolution == "resolved" and r.target_path for r in facts)
    assert all(r.target_path is None for r in facts if r.line == 3)


def test_type_stub_package_relative_import_is_anchored_in_the_package():
    files = {
        "pkg/__init__.pyi": "from .helper import Kind\n",
        "pkg/helper.pyi": "class Kind: ...\n",
    }
    parsed = {p: parse_live_source(p, s) for p, s in files.items()}
    facts = Resolver(parsed, live_modules=True).resolve_file("pkg/__init__.pyi")
    assert next(r for r in facts if r.kind == "MODULE_IMPORT").target_path == "pkg/helper.pyi"


def test_css_file_only_exact_import_keeps_source_authorization_on_warm_summaries():
    item = parse_live_source(
        "src/main.ts",
        """import './theme.css'
           import theme from './theme.css'
           import './other'
           theme()
           import('./lazy.css')
           import './forbidden.css'
        """,
    )
    paths = {"src/theme.css", "src/other.css", "src/lazy.css"}
    resolver = Resolver({item.path: item}, live_modules=True, stylesheet_paths=paths)
    summary = summary_file(declaration_summary(item), item.path, "typescript")
    assert {r.name for r in summary.references} == {"./theme.css", "./lazy.css", "./forbidden.css"}
    assert {
        t for r in summary.references if (t := resolver.stylesheet_target(summary, r.name))
    } == {"src/lazy.css", "src/theme.css"}
    facts = resolver.resolve_file(item.path)
    assert {r.target_path for r in facts if r.resolution == "resolved"} == {
        "src/theme.css",
        "src/lazy.css",
    }
    assert all(
        r.kind == "MODULE_IMPORT" and r.target_symbol_id is None for r in facts if r.target_path
    )
    assert all(r.target_path is None for r in facts if r.kind == "CALL" or r.name == "./other")
