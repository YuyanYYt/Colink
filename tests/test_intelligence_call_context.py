"""Definition-time evaluation and deferred bodies are not their declaration's calls."""

import json
from textwrap import dedent

import pytest

from code_context.fact_payloads import FactCacheError
from code_context.intelligence_analysis import analyze_structure
from code_context.intelligence_models import ParsedFile, Relation, is_traversable_call, module_id
from code_context.intelligence_python import parse_python
from code_context.intelligence_queries import decode_relation
from code_context.intelligence_resolver import Resolver
from code_context.models import FileChange, SyncBatch, content_hash
from code_context.storage import MirrorStore


def parse(text):
    return parse_python("phases.py", dedent(text).lstrip("\n"))


def calls(parsed, name):
    return [ref for ref in parsed.references if ref.kind == "CALL" and ref.name == name]


def relations(parsed):
    return [
        relation.to_dict() for relation in Resolver({parsed.path: parsed}).resolve_file(parsed.path)
    ]


def store_for(tmp_path, text):
    store = MirrorStore(tmp_path / "phases.sqlite3")
    text = dedent(text).lstrip("\n")
    store.apply(
        "phases",
        SyncBatch(
            request_id="fixture",
            base_revision=0,
            mode="full",
            changes=[
                FileChange(op="upsert", path="phases.py", content=text, sha256=content_hash(text))
            ],
        ),
    )
    return store


def find(store, name):
    found = store.code_query("phases", None, "symbol_search", query=name, exact=True)
    assert len(found["symbols"]) == 1
    return found["symbols"][0]["symbol_id"]


def path(store, left, right):
    with store.read_connection() as db:
        return analyze_structure(
            db,
            "phases",
            1,
            mode="call_path",
            selector={"path": "phases.py", "qualname": left},
            target={"path": "phases.py", "qualname": right},
            max_depth=4,
            max_chars=30000,
        )


def test_function_header_calls_belong_to_evaluator_not_defined_function():
    parsed = parse("""
        def decorate(fn): return fn
        def factory(): return decorate
        def make_default(): return 1
        @factory()
        def subject(value=make_default()): return value
    """)
    for name in ["factory", "make_default"]:
        ref = calls(parsed, name)[0]
        assert ref.source_qualname == ref.scope == ""
        assert ref.execution_context == "definition"
        relation = next(
            row for row in relations(parsed) if row["kind"] == "CALL" and row["name"] == name
        )
        assert relation["source_symbol_id"] == module_id("phases.py")
        assert relation["resolution"] == "resolved"
        assert is_traversable_call(relation)
    declaration = next(ref for ref in parsed.references if ref.kind == "DECORATOR")
    assert declaration.source_qualname == "subject"


def test_nested_definition_default_calls_remain_reachable_from_outer(tmp_path):
    text = """
        def make_default(): return 1
        def outer():
            def inner(value=make_default()): return value
            return inner
    """
    parsed = parse(text)
    ref = calls(parsed, "make_default")[0]
    assert ref.source_qualname == ref.scope == "outer"
    assert ref.execution_context == "definition"
    store = store_for(tmp_path, text)
    graph = store.code_query(
        "phases", None, "symbol_graph", symbol_id=find(store, "outer"), graph="call", depth=2
    )
    assert any(edge["target_qualname"] == "make_default" for edge in graph["edges"])
    assert not graph.get("execution_context_partial")


def test_class_header_calls_use_outer_evaluator_while_metadata_keeps_declaration():
    parsed = parse("""
        def decorate(value): return value
        def factory(): return decorate
        def base_factory(): return object
        def meta_factory(): return type
        def outer():
            @factory()
            class Subject(base_factory(), metaclass=meta_factory()):
                pass
            return Subject
    """)
    for name in ["factory", "base_factory", "meta_factory"]:
        ref = calls(parsed, name)[0]
        assert ref.source_qualname == ref.scope == "outer"
        assert ref.execution_context == "definition"
    declaration = next(ref for ref in parsed.references if ref.kind == "DECORATOR")
    assert declaration.source_qualname == "outer.Subject"


def test_bare_decorator_keeps_declaration_reference_without_fictitious_call():
    parsed = parse("""
        def decorate(fn): return fn
        @decorate
        def subject(): return 1
    """)
    assert not calls(parsed, "decorate")
    declaration = next(ref for ref in parsed.references if ref.kind == "DECORATOR")
    assert declaration.source_qualname == "subject"


def test_callback_body_is_marked_deferred_with_separate_execution_scope():
    parsed = parse("""
        def target(): return 1
        def ignore(operation): return 0
        def subject(): return ignore(lambda: target())
    """)
    ref = calls(parsed, "target")[0]
    assert ref.source_qualname == "subject"
    assert ref.scope == ref.execution_scope
    assert ref.execution_scope.startswith("subject.<lambda>@")
    assert ref.execution_context == "deferred_lambda"
    relation = next(
        row for row in relations(parsed) if row["kind"] == "CALL" and row["name"] == "target"
    )
    assert relation["resolution"] == "resolved"
    assert relation["execution_scope"] == ref.scope
    assert not is_traversable_call(relation)


def test_deferred_reference_remains_visible_without_expanding_graph(tmp_path):
    store = store_for(
        tmp_path,
        """
        def further(): return 1
        def target(): return further()
        def ignore(operation): return 0
        def subject(): return ignore(lambda: target())
        """,
    )
    graph = store.code_query(
        "phases", None, "symbol_graph", symbol_id=find(store, "subject"), graph="call", depth=3
    )
    deferred = next(edge for edge in graph["edges"] if edge["name"] == "target")
    assert deferred["execution_context"] == "deferred_lambda"
    assert graph["execution_context_partial"]
    assert not any(edge["name"] == "further" for edge in graph["edges"])
    refs = store.code_query("phases", None, "find_references", symbol_id=find(store, "target"))
    assert len(refs["references"]) == 1
    assert refs["references"][0]["execution_context"] == "deferred_lambda"


def test_later_ordinary_edge_can_expand_endpoint_first_seen_as_deferred(tmp_path):
    store = store_for(
        tmp_path,
        """
        def leaf(): return 1
        def target(): return leaf()
        def middle(): return target()
        def ignore(operation): return 0
        def subject():
            ignore(lambda: target())
            return middle()
        """,
    )
    graph = store.code_query(
        "phases", None, "symbol_graph", symbol_id=find(store, "subject"), graph="call", depth=4
    )
    assert graph["execution_context_partial"]
    assert any(edge["name"] == "leaf" for edge in graph["edges"])


@pytest.mark.parametrize(
    "expression,context",
    [
        ("(lambda: target())()", "immediate_lambda"),
        ("(lambda: (lambda: target())())()", "immediate_lambda"),
        ("(lambda: (lambda: target()))()", "deferred_lambda"),
        ("lambda: (lambda: target())()", "deferred_lambda"),
    ],
)
def test_literal_lambda_invocation_respects_outer_deferral(expression, context):
    parsed = parse(f"def target(): return 1\ndef subject(): return {expression}\n")
    ref = calls(parsed, "target")[0]
    assert ref.execution_context == context
    row = next(
        item for item in relations(parsed) if item["kind"] == "CALL" and item["name"] == "target"
    )
    assert is_traversable_call(row) is (context == "immediate_lambda")


def test_lambda_defaults_evaluate_in_creator_and_nested_defaults_stay_deferred():
    parsed = parse("""
        def make(): return 1
        def consume(value): return value
        def subject(value):
            callback = lambda captured=make(): consume(value)
            nested = lambda: (lambda captured=make(): consume(captured))
            return callback, nested
    """)
    defaults = calls(parsed, "make")
    assert defaults[0].source_qualname == "subject"
    assert defaults[0].scope == "subject"
    assert defaults[0].execution_context == "definition"
    assert defaults[1].execution_context == "deferred_lambda"
    captures = calls(parsed, "consume")
    assert all(ref.execution_context == "deferred_lambda" for ref in captures)
    assert all(ref.execution_scope.startswith("subject.<lambda>@") for ref in captures)
    assert not any(
        row["resolution"] == "resolved" and row.get("target_qualname") == "value"
        for row in relations(parsed)
    )


@pytest.mark.parametrize("immediate", [True, False])
def test_lambda_capture_does_not_bind_shadowed_outer_parameter_to_global(immediate):
    expression = "(lambda: target())()" if immediate else "lambda: target()"
    parsed = parse(f"def target(): return 1\ndef subject(target): return {expression}\n")
    captured = next(
        row for row in relations(parsed) if row["kind"] == "CALL" and row["name"] == "target"
    )
    assert captured["resolution"] == "unresolved"
    assert captured["evidence"] == "local_or_parameter_shadowing"
    assert captured["execution_context"] == ("immediate_lambda" if immediate else "deferred_lambda")


@pytest.mark.parametrize("context", ["type_checking", "future_annotation", "runtime_annotation"])
def test_type_only_execution_is_kept_as_evidence_without_a_call_path(tmp_path, context):
    if context == "type_checking":
        text = """
            from typing import TYPE_CHECKING
            def target(): return 1
            def outer():
                if TYPE_CHECKING:
                    target()
        """
    else:
        text = dedent("""
            def target(): return 1
            def outer():
                def inner(value: target()): return value
                return inner
        """).lstrip("\n")
        if context == "future_annotation":
            text = "from __future__ import annotations\n" + text
    store = store_for(tmp_path, text)
    refs = store.code_query("phases", None, "find_references", symbol_id=find(store, "target"))
    call = next(row for row in refs["references"] if row["kind"] == "CALL")
    assert call["source_qualname"] == "outer"
    assert call["type_only"] is (context != "runtime_annotation")
    result = path(store, "outer", "target")
    if context == "runtime_annotation":
        assert result["path_found"]
        assert call["execution_context"] == "definition"
        assert is_traversable_call(call)
    else:
        assert not result["path_found"]
        assert result["analysis_status"] == "partial"
        assert result["execution_context_partial"]
        assert result["not_traversed"]
        assert not is_traversable_call(call)


@pytest.mark.parametrize(
    "text,left,right,found,partial",
    [
        (
            "def decorate(fn): return fn\n@decorate\ndef subject(): return 1\n",
            "subject",
            "decorate",
            False,
            False,
        ),
        (
            "def decorate(fn): return fn\ndef factory(): return decorate\n"
            "@factory()\ndef subject(): return 1\n",
            "subject",
            "factory",
            False,
            False,
        ),
        (
            "def make(): return 1\ndef subject(value=make()): return value\n",
            "subject",
            "make",
            False,
            False,
        ),
        (
            "def make(): return 1\ndef outer():\n"
            "    def inner(value=make()): return value\n    return inner\n",
            "outer",
            "make",
            True,
            False,
        ),
        (
            "def target(): return 1\ndef ignore(callback): return 0\n"
            "def subject(): return ignore(lambda: target())\n",
            "subject",
            "target",
            False,
            True,
        ),
        (
            "def target(): return 1\ndef subject(): return (lambda: target())()\n",
            "subject",
            "target",
            True,
            False,
        ),
        (
            "def target(): return 1\ndef subject():\n"
            "    callback=lambda: target()\n    return callback()\n",
            "subject",
            "target",
            False,
            True,
        ),
        (
            "class Model: pass\ndef subject(): return Model()\n",
            "subject",
            "Model",
            True,
            False,
        ),
    ],
)
def test_execution_path_positive_and_negative_boundaries(
    tmp_path, text, left, right, found, partial
):
    result = path(store_for(tmp_path, text), left, right)
    assert result["path_found"] is found
    assert result.get("execution_context_partial", False) is partial
    if partial:
        assert result["analysis_status"] == "partial"
        assert result["not_traversed"]


def test_actual_direct_call_and_its_five_references_keep_existing_owners(tmp_path):
    text = "def run_agent_call(operation): return operation\n"
    names = ["chat", "story_chat", "analyze_video", "recommend", "vectorize"]
    text += "\n".join(
        f"def {name}(request): return run_agent_call(lambda: request)" for name in names
    )
    store = store_for(tmp_path, text)
    refs = store.code_query(
        "phases", None, "find_references", symbol_id=find(store, "run_agent_call")
    )
    assert len(refs["references"]) == 5
    assert {row["source_qualname"] for row in refs["references"]} == set(names)
    assert all(is_traversable_call(row) for row in refs["references"])
    assert all("execution_context" not in row for row in refs["references"])


@pytest.mark.parametrize("context", ["body", "definition", "immediate_lambda"])
def test_traversal_helper_keeps_actual_evaluator_calls(context):
    assert is_traversable_call({"kind": "CALL", "execution_context": context})
    assert is_traversable_call({"kind": "INSTANTIATION", "execution_context": context})


@pytest.mark.parametrize("context", ["deferred_lambda", "unknown", None, []])
def test_traversal_helper_is_conservative_for_deferred_or_invalid_context(context):
    assert not is_traversable_call({"kind": "CALL", "execution_context": context})


def test_old_parsed_facts_and_non_python_relation_defaults_remain_compatible():
    parsed = parse("def subject(): return helper()\n")
    legacy = parsed.to_dict()
    for ref in legacy["references"]:
        ref.pop("execution_context")
        ref.pop("execution_scope")
    restored = ParsedFile.from_dict(legacy)
    assert all(
        ref.execution_context == "body" and ref.execution_scope == "" for ref in restored.references
    )
    legacy_relation = Relation("sample.java", "CALL", "helper", 1).to_dict()
    assert "execution_context" not in legacy_relation
    assert "execution_scope" not in legacy_relation
    assert decode_relation(json.dumps(legacy_relation)) == legacy_relation
    assert is_traversable_call(legacy_relation)
    explicit = {**legacy_relation, "execution_context": "body", "execution_scope": ""}
    assert decode_relation(json.dumps(explicit)) == explicit
    with pytest.raises(FactCacheError):
        decode_relation(json.dumps({**legacy_relation, "execution_context": []}))
    with pytest.raises(FactCacheError):
        decode_relation(json.dumps({**legacy_relation, "unknown": True}))
