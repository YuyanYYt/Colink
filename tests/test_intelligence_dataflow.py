"""Request-local Python flow goldens with real extractor/resolver call evidence."""

import ast
import time
from dataclasses import asdict
from textwrap import dedent, indent

import pytest

from code_context.intelligence_dataflow import DataflowBudgetExceeded, analyze_dataflow
from code_context.intelligence_python import parse_python
from code_context.intelligence_resolver import Resolver


class Project:
    def __init__(self, source=None, files=None):
        self.files = files or {"flow.py": dedent(source).lstrip("\n")}
        parsed = {path: parse_python(path, text) for path, text in self.files.items()}
        assert all(file.status == "ready" for file in parsed.values())
        resolver = Resolver(parsed)
        self.symbols = {
            symbol.id: {**asdict(symbol), "symbol_id": symbol.id, "language": "python"}
            for file in parsed.values()
            for symbol in file.symbols
        }
        self.relations = [
            relation.to_dict() for path in self.files for relation in resolver.resolve_file(path)
        ]
        self.loaded = []

    def symbol(self, qualname):
        matches = [s for s in self.symbols.values() if s["qualname"] == qualname]
        assert len(matches) == 1
        return matches[0]["symbol_id"]

    def selector(self, qualname, kind="parameter", **location):
        return {"symbol_id": self.symbol(qualname), "value": {"kind": kind, **location}}

    def load_source(self, path):
        self.loaded.append(path)
        return self.files[path]

    def load_relations(self, identifier, *, direction):
        column = "source_symbol_id" if direction == "outgoing" else "target_symbol_id"
        return [relation for relation in self.relations if relation[column] == identifier]

    def analyze(self, qualname, *, mode="data_impact", kind="parameter", target=None, **kwargs):
        location = {key: kwargs.pop(key) for key in ("name", "line", "column") if key in kwargs}
        return analyze_dataflow(
            mode=mode,
            selector=self.selector(qualname, kind, **location),
            target=target,
            load_symbol=self.symbols.__getitem__,
            load_source=self.load_source,
            load_relations=self.load_relations,
            **kwargs,
        )


def node_names(result, kind=None):
    return {node["name"] for node in result["nodes"] if kind is None or node["kind"] == kind}


def codes(result):
    return {item["code"] for item in result["diagnostics"]}


def assert_consistent(result):
    identifiers = {node["id"] for node in result["nodes"]}
    assert all(
        edge["source"] in identifiers and edge["target"] in identifiers for edge in result["edges"]
    )
    assert all(
        edge["evidence"]["line"] >= 1 and edge["evidence"]["column"] >= 1
        for edge in result["edges"]
    )


def test_assignment_transforms_return_and_no_source_literal_disclosure():
    project = Project("""
        def f(x):
            y = x + "pretend-secret-token"
            return y
    """)
    result = project.analyze("f", name="x")
    assert result["analysis_status"] == "complete"
    assert {edge["kind"] for edge in result["edges"]} == {
        "DEF_USE",
        "TRANSFORM",
        "ASSIGN",
        "RETURN_FLOW",
    }
    assert "return" in node_names(result)
    assert "pretend-secret-token" not in str(result)
    assert_consistent(result)


def test_guarded_callback_budget_preserves_completed_local_facts():
    project = Project("def f(x):\n    y = x + 1\n    return y\n")

    def relations(identifier, *, direction):
        if direction == "incoming":
            raise DataflowBudgetExceeded
        return project.load_relations(identifier, direction=direction)

    result = analyze_dataflow(
        mode="data_impact",
        selector=project.selector("f", name="x"),
        load_symbol=project.symbols.__getitem__,
        load_source=project.load_source,
        load_relations=relations,
    )
    assert result["analysis_status"] == "partial"
    assert "time_budget" in result["truncated_reasons"]
    assert "return" in node_names(result)
    assert result["edges"]
    assert_consistent(result)


def test_reassignment_kills_prior_value_and_early_return_stops_unreachable_statements():
    project = Project("""
        def f(x):
            y = x
            y = 2
            return y
            z = x
    """)
    impact = project.analyze("f", name="x")
    assert "return" not in node_names(impact)
    assert "z" not in node_names(impact)
    origin = project.analyze("f", mode="data_origin", kind="return")
    assert "x" not in node_names(origin, "parameter")
    assert "int" in node_names(origin, "literal")


def test_branch_merges_are_may_flow_and_condition_is_not_data_dependency():
    project = Project("""
        def f(x, y, flag):
            if flag:
                z = x
            else:
                z = y
            return z
    """)
    origin = project.analyze("f", mode="data_origin", kind="return")
    assert node_names(origin, "parameter") == {"x", "y"}
    assert any(edge["resolution"] == "possible" for edge in origin["edges"])
    assert_consistent(origin)


def test_terminated_branch_not_in_later_environment():
    project = Project("""
        def f(x, y, flag):
            if flag:
                z = x
                return z
            z = y
            return z
    """)
    result = project.analyze("f", mode="data_origin", kind="return", line=6)
    assert node_names(result, "parameter") == {"y"}
    ambiguous = project.analyze("f", mode="data_origin", kind="return")
    assert ambiguous["analysis_status"] == "selection_ambiguous"
    assert len(ambiguous["candidates"]) == 2


def test_nested_function_scope_does_not_leak_and_same_name_is_distinct():
    project = Project("""
        def f(x):
            def nested(x):
                return x
            return 7
    """)
    outer = project.analyze("f", name="x")
    assert "return" not in node_names(outer)
    assert "NESTED_SCOPE_PARTIAL" in codes(outer)
    inner = project.analyze("f.nested", name="x")
    assert "return" in node_names(inner)
    assert all(node["symbol_id"] == project.symbol("f.nested") for node in inner["nodes"])


def test_self_field_roundtrip_is_finite_same_method_evidence():
    project = Project("""
        class Box:
            def run(self, value):
                self.item = value
                return self.item
    """)
    result = project.analyze("Box.run", name="value")
    assert {"field_write", "field_read", "return"} <= {node["kind"] for node in result["nodes"]}
    assert "UNKNOWN_FIELD" not in codes(result)


def test_unknown_field_and_other_object_alias_are_reported():
    project = Project("""
        class Box:
            def run(self, other):
                other.item = self.item
                return other.item
    """)
    result = project.analyze("Box.run", mode="data_origin", kind="return")
    assert {"UNKNOWN_FIELD", "ATTRIBUTE_ALIAS_PARTIAL", "MUTATION_ALIAS_PARTIAL"} <= codes(result)
    assert any(edge["resolution"] == "unknown" for edge in result["edges"])


def test_resolved_callee_argument_and_real_return_flow():
    project = Project("""
        def helper(value):
            return value + 1
        def entry(x):
            return helper(x)
    """)
    result = project.analyze("entry", name="x", max_nodes=100)
    assert {"ARG_TO_PARAM", "RETURN_TO_CALLSITE"} <= {edge["kind"] for edge in result["edges"]}
    assert project.symbol("helper") in {node["symbol_id"] for node in result["nodes"]}
    assert result["analysis_status"] == "complete"
    assert_consistent(result)


def test_constant_callee_return_does_not_inherit_call_argument():
    project = Project("""
        def helper(value):
            return 99
        def entry(x):
            return helper(x)
    """)
    impact = project.analyze("entry", name="x")
    assert "call" not in node_names(impact, "call_result")
    assert "return" not in node_names(impact)
    origin = project.analyze("entry", mode="data_origin", kind="return")
    assert "x" not in node_names(origin, "parameter")
    assert "int" in node_names(origin, "literal")


def test_known_keyword_posonly_kwonly_and_literal_default_binding():
    project = Project("""
        def helper(first, /, second=8, *, third=9):
            return first + second + third
        def entry(x, y):
            return helper(x, third=y)
    """)
    result = project.analyze("entry", mode="data_origin", kind="return", max_nodes=100)
    assert node_names(result, "parameter") == {"first", "second", "third", "x", "y"}
    assert "int" in node_names(result, "literal")
    assert "ARGUMENT_BINDING_PARTIAL" not in codes(result)
    assert_consistent(result)


@pytest.mark.parametrize("call", ["helper(x, first=x)", "helper(first=x)", "helper(x, z=x)"])
def test_invalid_binding_does_not_publish_confirmed_argument_edges(call):
    project = Project(f"""
        def helper(first, /):
            return first
        def entry(x):
            return {call}
    """)
    result = project.analyze("entry", name="x")
    assert "ARGUMENT_BINDING_PARTIAL" in codes(result)
    assert not any(edge["kind"] == "ARG_TO_PARAM" for edge in result["edges"])
    assert any(edge["resolution"] == "unknown" for edge in result["edges"])


def test_default_definition_scope_is_not_guessed_from_caller_locals():
    project = Project("""
        VALUE = 9
        def helper(x=VALUE):
            return x
        def entry(VALUE):
            return helper()
    """)
    result = project.analyze("entry", name="VALUE")
    assert "DEFAULT_DEFINITION_SCOPE_PARTIAL" in codes(result)
    assert "return" not in node_names(result)


def test_each_callsite_has_distinct_parameter_context():
    project = Project("""
        def helper(value):
            return value
        def entry(x, y):
            a = helper(x)
            b = helper(y)
            return b
    """)
    impact = project.analyze("entry", name="x", max_nodes=100)
    assert "return" not in {
        node["name"] for node in impact["nodes"] if node["symbol_id"] == project.symbol("entry")
    }
    origin = project.analyze("entry", mode="data_origin", kind="return", max_nodes=100)
    assert "x" not in node_names(origin, "parameter")
    assert "y" in node_names(origin, "parameter")


def test_data_origin_parameter_follows_incoming_static_callsite():
    project = Project("""
        def helper(value):
            return value
        def entry(x):
            return helper(x + 1)
    """)
    result = project.analyze("helper", mode="data_origin", name="value", max_nodes=100)
    assert {"value", "x"} == node_names(result, "parameter")
    assert "ARG_TO_PARAM" in {edge["kind"] for edge in result["edges"]}
    assert "RECURSIVE_CALL_PARTIAL" not in codes(result)


def test_two_level_incoming_origin_is_bounded():
    project = Project("""
        def inner(value):
            return value
        def middle(a):
            return inner(a)
        def outer(b):
            return middle(b)
    """)
    shallow = project.analyze("inner", mode="data_origin", name="value", max_depth=1)
    deep = project.analyze("inner", mode="data_origin", name="value", max_depth=2)
    assert "b" not in node_names(shallow, "parameter")
    assert "b" in node_names(deep, "parameter")


def test_real_data_path_and_constant_return_no_path():
    project = Project("""
        def helper(value):
            return value
        def entry(x):
            return helper(x)
    """)
    result = project.analyze(
        "entry",
        mode="data_path",
        name="x",
        target=project.selector("entry", "return"),
        max_nodes=100,
    )
    assert result["path_found"]
    assert result["paths"][0][0] == result["root"]
    assert result["paths"][0][-1] == result["target"]
    assert_consistent(result)
    project.files["flow.py"] = project.files["flow.py"].replace("return value", "return 99")
    result = project.analyze(
        "entry", mode="data_path", name="x", target=project.selector("entry", "return")
    )
    assert not result["path_found"]
    assert "runtime reachability is unknown" in result["no_path_claim"]
    assert result["conclusion"] == {
        "status": "no_path_in_analyzed_slice",
        "runtime_reachability": "unknown",
        "absence_proven": False,
    }


def test_exact_occurrence_uses_utf8_column_and_does_not_guess_name():
    project = Project("""
        def f(数据):
            结果 = 数据
            return 结果
    """)
    tree = ast.parse(project.files["flow.py"])
    use = tree.body[0].body[0].value
    result = project.analyze("f", kind="occurrence", line=use.lineno, column=use.col_offset + 1)
    assert "return" in node_names(result)
    missing = project.analyze("f", kind="occurrence", line=use.lineno, column=1)
    assert missing["analysis_status"] == "selection_not_found"


@pytest.mark.parametrize(
    "body,diagnostic",
    [
        ("for n in x:\n        y = n\n    return y", "LOOP_FIXED_POINT_PARTIAL"),
        (
            "try:\n        return x\n    except Exception:\n        return 0",
            "EXCEPTION_FLOW_PARTIAL",
        ),
        ("y = [n for n in x]\n    return y", "EXPRESSION_SCOPE_PARTIAL"),
        ("global y\n    y = x\n    return y", "GLOBAL_OR_NONLOCAL_PARTIAL"),
        ("yield x", "ASYNC_OR_GENERATOR_PARTIAL"),
    ],
)
def test_unsupported_control_and_scope_semantics_are_partial(body, diagnostic):
    project = Project("def f(x):\n    " + body + "\n")
    result = project.analyze("f", name="x")
    assert result["analysis_status"] == "partial"
    assert diagnostic in codes(result)


def test_recursion_dynamic_calls_and_starargs_have_unknown_evidence():
    project = Project("""
        def f(x):
            y = f(x)
            z = x.method()
            return f(*x)
    """)
    result = project.analyze("f", name="x", max_nodes=100)
    assert {
        "RECURSIVE_CALL_PARTIAL",
        "CALL_TARGET_UNKNOWN_OR_AMBIGUOUS",
        "EXPANDED_ARGUMENTS_PARTIAL",
    } <= codes(result)
    assert all(edge["kind"] != "ARG_TO_PARAM" for edge in result["edges"])
    assert any(edge["resolution"] == "unknown" for edge in result["edges"])


def test_call_depth_and_decorators_do_not_claim_complete():
    project = Project("""
        def decorate(f):
            return f
        @decorate
        def helper(x):
            return x
        def entry(x):
            return helper(x)
    """)
    result = project.analyze("entry", name="x")
    assert "DECORATED_CALLABLE_PARTIAL" in codes(result)
    assert not any(edge["kind"] == "ARG_TO_PARAM" for edge in result["edges"])
    result = project.analyze("entry", name="x", max_depth=0)
    assert "CALL_DEPTH_PARTIAL" in codes(result)


def test_combined_output_budget_remains_consistent():
    project = Project("""
        def f(x):
            a = x + 1
            b = a + 2
            c = b + 3
            return c
    """)
    for mode in ("data_origin", "data_impact", "combined"):
        result = project.analyze("f", mode=mode, name="x", max_nodes=3, max_edges=2)
        assert len(result["nodes"]) <= 3
        assert len(result["edges"]) <= 2
        assert_consistent(result)
        if mode != "data_origin":
            assert result["truncated"]
            assert result["analysis_status"] == "partial"


def test_compute_deadline_is_partial_without_source_error_leak():
    project = Project("def f(x):\n    return x\n")
    original = project.load_source

    def slow(path):
        time.sleep(0.005)
        return original(path)

    project.load_source = slow
    result = project.analyze("f", name="x", max_ms=1)
    assert result["truncated"]
    assert "max_ms" in result["truncated_reasons"]
    assert not result["nodes"]


def test_parsing_never_imports_or_executes_project_and_source_load_is_request_local(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    project = Project(f"""
        import pathlib
        pathlib.Path({str(sentinel)!r}).write_text("executed")
        def f(x):
            return x
    """)
    project.analyze("f", name="x")
    project.analyze("f", name="x")
    assert not sentinel.exists()
    assert project.loaded == ["flow.py", "flow.py"]


def test_source_mismatch_and_non_python_are_explicit_partial():
    project = Project("def f(x):\n    return x\n")
    project.files["flow.py"] = "def different(x):\n    return x\n"
    result = project.analyze("f", name="x")
    assert "SYMBOL_SOURCE_MISMATCH" in codes(result)
    assert result["diagnostic_summary"]["by_category"] == {"source_context": 1}
    assert result["coverage"]["availability"] == "source_unavailable"
    assert result["conclusion"]["status"] == "analysis_unavailable"
    project.symbols[project.symbol("f")]["language"] = "typescript"
    result = project.analyze("f", name="x")
    assert "PYTHON_ONLY" in codes(result)
    assert result["analysis_status"] == "selection_not_found"  # Compatibility status.
    assert result["coverage"]["requested_language"] == "typescript"
    assert result["coverage"]["availability"] == "unsupported_language"
    assert result["diagnostic_summary"]["by_category"] == {"language_support": 1}
    assert result["diagnostics"][0]["path"] == "flow.py"
    assert result["conclusion"]["status"] == "analysis_unavailable"
    assert result["conclusion"]["absence_proven"] is False


def test_import_rebind_and_deleted_field_kill_previous_definitions():
    project = Project("""
        def f(x):
            y = x
            import math as y
            return y
        class Box:
            def run(self, x):
                self.item = x
                del self.item
                return self.item
    """)
    result = project.analyze("f", mode="data_origin", kind="return")
    assert "x" not in node_names(result, "parameter")
    assert "IMPORT_VALUE_PARTIAL" in codes(result)
    result = project.analyze("Box.run", mode="data_origin", kind="return")
    assert "x" not in node_names(result, "parameter")
    assert "UNKNOWN_FIELD" in codes(result)


def test_receiver_rebind_or_call_side_effects_do_not_keep_stale_field_proof():
    project = Project("""
        class Box:
            def run(self, value, other):
                self.item = value
                self = other
                return self.item
            def dynamic(self, value):
                self.item = value
                modify(self)
                return self.item
    """)
    result = project.analyze("Box.run", mode="data_origin", kind="return")
    assert "value" not in node_names(result, "parameter")
    assert "RECEIVER_ALIAS_PARTIAL" in codes(result)
    result = project.analyze("Box.dynamic", mode="data_origin", kind="return")
    assert "value" not in node_names(result, "parameter")
    assert {"CALL_SIDE_EFFECTS_PARTIAL", "UNKNOWN_FIELD"} <= codes(result)


@pytest.mark.parametrize("expression", ["((z := x) if flag else (z := y))", "flag and (z := x)"])
def test_expression_branch_assignments_merge_instead_of_always_overwriting(expression):
    project = Project(f"""
        def f(x, y, flag):
            z = y
            {expression}
            return z
    """)
    result = project.analyze("f", mode="data_origin", kind="return")
    assert {"x", "y"} == node_names(result, "parameter")
    assert "flag" not in node_names(result, "parameter")
    assert any(edge["resolution"] == "possible" for edge in result["edges"])


def test_branch_missing_definition_is_explicitly_partial():
    project = Project("""
        def f(x, flag):
            if flag:
                z = x
            return z
    """)
    result = project.analyze("f", mode="data_origin", kind="return")
    assert "BRANCH_UNBOUND_VALUE_PARTIAL" in codes(result)
    assert result["analysis_status"] == "partial"


def test_unknown_function_object_is_possible_input_to_unknown_call():
    project = Project("""
        def f(fn, x):
            return fn(x)
    """)
    result = project.analyze("f", name="fn")
    assert "return" in node_names(result)
    assert "CALL_TARGET_UNKNOWN_OR_AMBIGUOUS" in codes(result)
    assert any(edge["resolution"] == "unknown" for edge in result["edges"])


def test_generator_and_async_calls_never_get_synchronous_return_bridge():
    project = Project("""
        def gen(x):
            yield x
        async def coroutine(x):
            return x
        def entry(x):
            a = gen(x)
            return coroutine(x)
    """)
    result = project.analyze("entry", name="x", max_nodes=100)
    assert {"ASYNC_OR_GENERATOR_PARTIAL", "ASYNC_SEMANTICS_PARTIAL"} <= codes(result)
    assert not any(edge["kind"] == "RETURN_TO_CALLSITE" for edge in result["edges"])


def test_incoming_impact_and_path_can_reach_caller_return():
    project = Project("""
        def helper(value):
            return value
        def entry(x):
            return helper(x)
    """)
    result = project.analyze("helper", name="value", max_nodes=100)
    assert project.symbol("entry") in {node["symbol_id"] for node in result["nodes"]}
    result = project.analyze(
        "helper",
        mode="data_path",
        name="value",
        target=project.selector("entry", "return"),
        max_nodes=100,
    )
    assert result["path_found"]
    assert_consistent(result)


def test_cross_file_import_alias_uses_resolved_target_not_name_guessing():
    project = Project(
        files={
            "pkg/__init__.py": "",
            "pkg/helper.py": "def clean(value):\n    return value + 1\n",
            "pkg/entry.py": "from .helper import clean as normalize\n"
            "def entry(x):\n    return normalize(x)\n",
        }
    )
    result = project.analyze("entry", name="x", max_nodes=100)
    assert result["analysis_status"] == "complete"
    assert {"ARG_TO_PARAM", "RETURN_TO_CALLSITE"} <= {edge["kind"] for edge in result["edges"]}
    assert "pkg/helper.py" in project.loaded


def test_static_method_does_not_consume_a_receiver_argument():
    project = Project("""
        class Helper:
            @staticmethod
            def clean(value):
                return value
        def entry(x):
            return Helper.clean(x)
    """)
    result = project.analyze("entry", name="x", max_nodes=100)
    assert "ARGUMENT_BINDING_PARTIAL" not in codes(result)
    assert "ARG_TO_PARAM" in {edge["kind"] for edge in result["edges"]}


def test_ambiguous_call_fact_never_produces_confirmed_binding():
    project = Project("""
        def helper(value):
            return value
        def other(value):
            return 1
        def entry(x):
            return helper(x)
    """)
    relation = next(r for r in project.relations if r["kind"] == "CALL")
    project.relations.append({**relation, "target_symbol_id": project.symbol("other")})
    result = project.analyze("entry", name="x")
    assert "CALL_TARGET_UNKNOWN_OR_AMBIGUOUS" in codes(result)
    assert not any(edge["kind"] == "ARG_TO_PARAM" for edge in result["edges"])


def test_output_path_budget_does_not_cut_a_path_into_false_success():
    project = Project("def f(x):\n    y = x + 1\n    return y\n")
    result = project.analyze(
        "f", mode="data_path", name="x", max_nodes=2, target=project.selector("f", "return")
    )
    assert not result["path_found"]
    assert "path_output_budget" in result["truncated_reasons"]
    assert result["analysis_status"] == "partial"


def test_fixed_analysis_work_cap_is_distinct_from_output_budget():
    project = Project(
        "def f(x):\n"
        + "".join(f"    value_{i} = x\n" for i in range(6_000))
        + "    return value_5999\n"
    )
    result = project.analyze("f", name="x", max_nodes=3, max_edges=2)
    assert "analysis_work_budget" in result["truncated_reasons"]
    assert len(result["nodes"]) <= 3
    assert_consistent(result)
    result = project.analyze("f", mode="data_origin", kind="return")
    assert result["analysis_status"] == "partial"
    assert result["selection_status"] == "selection_not_computed"


def test_type_stub_does_not_create_a_fake_implicit_none_return():
    project = Project(files={"flow.pyi": "def f(x): ...\n"})
    result = project.analyze("f", name="x")
    assert "PYTHON_STUB_UNSUPPORTED" in codes(result)
    assert not result["nodes"]


def test_same_line_distinct_call_columns_preserve_argument_identity():
    project = Project("""
        def helper(value):
            return value
        def entry(x, y):
            a = helper(x); b = helper(y)
            return b
    """)
    result = project.analyze("entry", mode="data_origin", kind="return", max_nodes=100)
    assert "x" not in node_names(result, "parameter")
    assert "y" in node_names(result, "parameter")


@pytest.mark.parametrize(
    "source",
    [
        "def f(x):\n    return 0\n    return x\n",
        "def f(x, flag):\n    if flag:\n        return 0\n"
        "    else:\n        return 1\n    return x\n",
        "def f(x):\n    for n in [1]:\n        break\n        y = x\n    return 0\n",
        "def f(x):\n    if False:\n        return x\n    return 0\n",
    ],
)
def test_unreachable_returns_and_assignments_are_not_analyzed(source):
    project = Project(source)
    result = project.analyze("f", name="x")
    assert "return" not in node_names(result)


@pytest.mark.parametrize(
    "source",
    [
        "def f(x):\n    y = 0\n    while True:\n        break\n"
        "    else:\n        y = x\n    return y\n",
        "def f(x):\n    y = 0\n    for n in [1]:\n        break\n"
        "    else:\n        y = x\n    return y\n",
        "def f(x):\n    while False:\n        pass\n    else:\n        return 0\n    return x\n",
        "def f(x):\n    for n in []:\n        pass\n    else:\n        return 0\n    return x\n",
        "def f(x):\n    while True:\n        continue\n    return x\n",
        "def f(x):\n    for n in [1]:\n        while True:\n            break\n"
        "    else:\n        return 0\n    return x\n",
    ],
)
def test_loop_exit_and_else_do_not_create_impossible_parameter_returns(source):
    project = Project(source)
    result = project.analyze("f", name="x")
    assert "return" not in node_names(result)
    assert result["analysis_status"] == "partial"


def test_unknown_loop_break_paths_and_else_are_never_static():
    project = Project("""
        def f(x, flag):
            y = 0
            for n in [1]:
                if flag:
                    break
            else:
                y = x
            return y
    """)
    result = project.analyze(
        "f", mode="data_path", name="x", target=project.selector("f", "return")
    )
    assert result["path_found"]
    assert result["path_resolution"] == "unknown"
    assert result["analysis_status"] == "partial"


def test_try_else_requires_normal_completion_and_return_skips_later_statements():
    project = Project("""
        def f(x):
            try:
                return 0
            except Exception:
                pass
            else:
                return x
            return 0
    """)
    result = project.analyze("f", name="x")
    assert "return" not in node_names(result)
    project = Project("""
        def f(x):
            try:
                return 0
            finally:
                pass
            return x
    """)
    result = project.analyze("f", name="x")
    assert "return" not in node_names(result)


@pytest.mark.parametrize(
    "final_statement",
    [
        "return 0",
        "raise RuntimeError()",
        "if flag:\n    return 0\nelse:\n    return 1",
    ],
)
def test_finally_termination_replaces_pending_callee_return_bridge(final_statement):
    project = Project(
        "def helper(value, flag):\n    try:\n        return value\n    finally:\n"
        + indent(final_statement, "        ")
        + "\ndef f(x, flag):\n    return helper(x, flag)\n"
    )
    result = project.analyze("f", name="x", max_nodes=100)
    assert not any(
        node["kind"] == "return" and node["symbol_id"] == project.symbol("f")
        for node in result["nodes"]
    )
    assert "EXCEPTION_FLOW_PARTIAL" in codes(result)


@pytest.mark.parametrize(
    "body",
    [
        'd = {"input": x, "constant": 0}\n    return d["constant"]',
        "d = [x, 0]\n    return d[1]",
        'd = {"input": x}\n    d["input"] = 0\n    return d["input"]',
        "d = [x]\n    d[0] = 0\n    return d[0]",
        'd = {"input": x}\n    del d["input"]\n    return d["input"]',
        "d = [x]\n    del d[0]\n    return d[0]",
        "d = [x, 0]\n    return d[1:]",
    ],
)
def test_container_projection_and_mutation_do_not_claim_confirmed_flow(body):
    project = Project("def f(x):\n    " + body + "\n")
    result = project.analyze(
        "f", mode="data_path", name="x", target=project.selector("f", "return"), max_nodes=100
    )
    assert result["path_found"]  # An unknown candidate is permitted; a proof is not.
    assert result["path_resolution"] == "unknown"
    assert result["analysis_status"] == "partial"
    assert "SUBSCRIPT_SELECTION_PARTIAL" in codes(result)


def test_destructuring_container_provenance_does_not_prove_every_component():
    project = Project("def f(x):\n    a, b = (x, 0)\n    return b\n")
    result = project.analyze(
        "f", mode="data_path", name="x", target=project.selector("f", "return")
    )
    assert result["path_resolution"] == "unknown"
    assert "DESTRUCTURING_PARTIAL" in codes(result)


@pytest.mark.parametrize(
    "source",
    [
        "def f(x):\n    return helper(x)\n    def helper(value):\n        return value\n",
        "def f(x):\n    if False:\n        def helper(value):\n            return value\n"
        "    return helper(x)\n",
    ],
)
def test_lexical_call_fact_does_not_prove_an_unexecuted_local_definition(source):
    project = Project(source)
    result = project.analyze("f", name="x")
    assert "LOCAL_CALLABLE_NOT_BOUND" in codes(result)
    assert result["analysis_status"] == "partial"
    assert not any(
        edge["kind"] in {"ARG_TO_PARAM", "RETURN_TO_CALLSITE"} for edge in result["edges"]
    )
    path = project.analyze(
        "f",
        mode="data_path",
        name="x",
        target=project.selector("f", "return", line=2 if "if False" not in source else 5),
    )
    assert path["path_resolution"] == "unknown"


def test_conditional_local_definition_has_uncertain_binding_and_executed_shadow_is_distinct():
    project = Project("""
        def helper(value):
            return value
        def f(x, flag):
            if flag:
                def helper(value):
                    return value
            return helper(x)
    """)
    result = project.analyze(
        "f", mode="data_path", name="x", target=project.selector("f", "return"), max_nodes=100
    )
    assert "LOCAL_CALLABLE_CONDITIONAL_PARTIAL" in codes(result)
    assert result["path_resolution"] == "unknown"
    project = Project("""
        def helper(value):
            return value
        def f(x):
            def helper(value):
                return 0
            return helper(x)
    """)
    result = project.analyze("f", name="x", max_nodes=100)
    assert project.symbol("helper") not in {node["symbol_id"] for node in result["nodes"]}
    assert "LOCAL_CALLABLE_NOT_BOUND" not in codes(result)
    assert not any(
        node["kind"] == "return" and node["symbol_id"] == project.symbol("f")
        for node in result["nodes"]
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"max_depth": -1},
        {"max_depth": 6},
        {"max_nodes": 0},
        {"max_edges": 501},
        {"max_ms": 0},
        {"max_nodes": True},
    ],
)
def test_strict_bounds(parameters):
    project = Project("def f(x):\n    return x\n")
    with pytest.raises(ValueError):
        project.analyze("f", name="x", **parameters)


@pytest.mark.parametrize(
    "source,options,code,category",
    [
        (
            "def f(x):\n    return external(x)\n",
            {},
            "CALL_TARGET_UNKNOWN_OR_AMBIGUOUS",
            "call_resolution",
        ),
        (
            "def f(x):\n    for value in x:\n        x = value\n    return x\n",
            {},
            "LOOP_FIXED_POINT_PARTIAL",
            "control_flow",
        ),
        (
            "def f(x):\n    return x[0]\n",
            {},
            "SUBSCRIPT_SELECTION_PARTIAL",
            "alias_side_effect",
        ),
        (
            "def helper(x):\n    return x\ndef f(x):\n    return helper(x)\n",
            {"max_depth": 0},
            "CALL_DEPTH_PARTIAL",
            "resource_budget",
        ),
        (
            "def f(x):\n    return unbound\n",
            {},
            "EXTERNAL_OR_UNBOUND_NAME",
            "source_context",
        ),
    ],
)
def test_uncertainty_is_classified_without_losing_original_evidence(
    source, options, code, category
):
    result = Project(source).analyze("f", name="x", **options)
    diagnostic = next(item for item in result["diagnostics"] if item["code"] == code)
    assert diagnostic["category"] == category
    assert diagnostic["message"] and diagnostic["path"] == "flow.py"
    assert diagnostic["line"] >= 1 and diagnostic["column"] >= 1
    assert result["diagnostic_summary"]["by_category"][category] >= 1
    assert result["coverage"]["availability"] == "available"
    assert result["conclusion"]["runtime_reachability"] == "unknown"
    assert result["conclusion"]["absence_proven"] is False


def test_aggregate_counts_survive_diagnostic_detail_budget():
    source = (
        "def f(x):\n"
        + "".join(f"    value_{i} = external_{i}(x)\n" for i in range(80))
        + "    return x\n"
    )
    result = Project(source).analyze("f", name="x", max_nodes=200, max_edges=500)
    assert len(result["diagnostics"]) == 50
    assert "diagnostics_budget" in result["truncated_reasons"]
    assert result["diagnostic_summary"]["by_category"]["call_resolution"] == 80
    assert result["diagnostic_summary"]["by_category"]["resource_budget"] >= 1
    assert result["diagnostic_summary"]["total"] > len(result["diagnostics"])


def test_unknown_path_is_not_upgraded_to_proven_flow():
    project = Project("def f(x):\n    return external(x)\n")
    result = project.analyze(
        "f", mode="data_path", name="x", target=project.selector("f", "return")
    )
    assert result["path_found"] and result["path_resolution"] == "unknown"
    assert result["conclusion"]["status"] == "uncertain_path_found"
    assert result["conclusion"]["absence_proven"] is False


def test_source_parse_failure_is_safe_context_diagnostic():
    project = Project("def f(x):\n    return x\n")
    project.files["flow.py"] = 'def f(x):\n    broken "SOURCE_DIAGNOSTIC_SENTINEL" @@@\n'
    result = project.analyze("f", name="x")
    assert codes(result) == {"PYTHON_PARSE_ERROR"}
    assert result["diagnostic_summary"]["by_category"] == {"source_context": 1}
    assert result["coverage"]["availability"] == "source_unavailable"
    assert "SOURCE_DIAGNOSTIC_SENTINEL" not in str(result)
