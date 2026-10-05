import builtins
import json
import os
import subprocess
from pathlib import Path

import pytest

import code_context.intelligence_java as java
from code_context.intelligence_models import RELATION_KINDS, ParsedFile


def symbols(parsed: ParsedFile) -> dict:
    return {symbol.qualname: symbol for symbol in parsed.symbols}


def references(parsed: ParsedFile, kind: str) -> list:
    return [reference for reference in parsed.references if reference.kind == kind]


def test_package_imports_and_module_contract() -> None:
    source = """package com.example;
import java.util.List;
import java.util.*;
import static demo.Tools.helper;
import static demo.Tools.*;
class Main {}
"""
    parsed = java.parse_java("src/Main.java", source)

    assert parsed.status == "ready"
    assert parsed.language == "java"
    assert parsed.module == "com.example"
    module = parsed.symbols[0]
    assert (module.path, module.name, module.qualname, module.kind) == (
        "src/Main.java",
        "Main",
        "",
        "module",
    )
    assert (module.start_line, module.end_line) == (0, 6)
    assert (parsed.scopes[0].qualname, parsed.scopes[0].kind) == ("", "module")
    assert [(i.module, i.imported_name, i.local_name, i.is_static) for i in parsed.imports] == [
        ("java.util", "List", "List", False),
        ("java.util", "*", "*", False),
        ("demo.Tools", "helper", "helper", True),
        ("demo.Tools", "*", "*", True),
    ]
    assert [r.name for r in references(parsed, "IMPORT")] == [
        "java.util.List",
        "java.util.*",
        "demo.Tools.helper",
        "demo.Tools.*",
    ]
    assert not references(parsed, "REFERENCE")
    assert all(not r.type_only for r in references(parsed, "IMPORT"))
    assert ParsedFile.from_dict(parsed.to_dict()) == parsed


def test_top_level_and_nested_java_type_kinds() -> None:
    parsed = java.parse_java(
        "Types.java",
        """class Outer {
  interface InnerApi {}
  class Inner {}
  enum Choice { FIRST, SECOND }
  record Entry(String value) {}
  @interface Label { String value(); }
}
interface TopApi {}
enum TopEnum { VALUE }
record TopRecord(int count) {}
@interface TopAnnotation {}
""",
    )

    assert parsed.status == "ready"
    found = symbols(parsed)
    assert {
        name: found[name].kind
        for name in (
            "Outer",
            "Outer.InnerApi",
            "Outer.Inner",
            "Outer.Choice",
            "Outer.Entry",
            "Outer.Label",
            "TopApi",
            "TopEnum",
            "TopRecord",
            "TopAnnotation",
        )
    } == {
        "Outer": "class",
        "Outer.InnerApi": "interface",
        "Outer.Inner": "class",
        "Outer.Choice": "enum",
        "Outer.Entry": "record",
        "Outer.Label": "annotation",
        "TopApi": "interface",
        "TopEnum": "enum",
        "TopRecord": "record",
        "TopAnnotation": "annotation",
    }
    assert found["Outer.Inner"].scope == "Outer"
    assert found["Outer.Choice.FIRST"].kind == "field"
    assert found["Outer.Label.value()"].kind == "method"
    assert all(s.kind == s.kind.lower() for s in parsed.symbols)
    assert all(r.kind in RELATION_KINDS for r in parsed.references)
    assert all(s.start_line >= 1 for s in parsed.symbols[1:])
    assert all(r.line >= 1 and r.column >= 1 for r in parsed.references)


def test_inheritance_implements_generic_bounds_and_nested_qualified_types() -> None:
    parsed = java.parse_java(
        "Child.java",
        """interface Api<T> extends Parent<T>, demo.Other {}
class Child<T extends Number & Comparable<T>> extends demo.Base<T> implements Api<T>, Runnable {
  java.util.Map<String, ? extends Number> cache;
  <R extends Result> R map(java.util.List<? super T> items) throws Problem { return null; }
}
""",
    )

    assert parsed.status == "ready"
    assert {(r.source_qualname, r.name) for r in references(parsed, "INHERITANCE")} == {
        ("Api", "Parent"),
        ("Api", "demo.Other"),
        ("Child", "demo.Base"),
    }
    assert {r.name for r in references(parsed, "IMPLEMENTS")} == {"Api", "Runnable"}
    assert all(not r.type_only for r in parsed.references)
    types = {r.name for r in references(parsed, "TYPE_REFERENCE")}
    assert {
        "Number",
        "Comparable",
        "T",
        "demo.Base",
        "java.util.Map",
        "String",
        "Result",
        "R",
        "java.util.List",
        "Problem",
    } <= types
    method = symbols(parsed)["Child.map(java.util.List<? super T>)"]
    assert method.parameters == ["java.util.List<? super T>"]
    assert method.return_type == "R"
    assert not {"Child", "map", "items", "cache"} & {
        r.name for r in references(parsed, "REFERENCE")
    }


def test_method_constructor_overload_signatures_parameters_and_arity() -> None:
    parsed = java.parse_java(
        "Service.java",
        """class Service {
  Service() { this(1); }
  Service(int count) { super(); }
  String call(String value) { return value; }
  String call(int count) { return null; }
  String call(String[] values, int... flags) { return null; }
  void run() { this.call(1); Service.call("literal", 2); call(/*note*/); }
}
""",
    )

    assert parsed.status == "ready"
    found = symbols(parsed)
    assert {
        "Service.Service()",
        "Service.Service(int)",
        "Service.call(String)",
        "Service.call(int)",
        "Service.call(String[],int...)",
        "Service.run()",
    } <= set(found)
    method = found["Service.call(String[],int...)"]
    assert (method.name, method.signature, method.parameters, method.return_type) == (
        "call",
        "call(String[],int...)",
        ["String[]", "int..."],
        "String",
    )
    assert found["Service.Service(int)"].kind == "constructor"
    assert {(b.name, b.scope, b.annotation) for b in parsed.bindings if b.kind == "parameter"} >= {
        ("count", "Service.Service(int)", "int"),
        ("value", "Service.call(String)", "String"),
        ("flags", "Service.call(String[],int...)", "int..."),
    }
    assert {(r.name, r.arity) for r in references(parsed, "CALL")} == {
        ("this", 1),
        ("super", 0),
        ("this.call", 1),
        ("Service.call", 2),
        ("call", 0),
    }
    assert {d["count"] for d in parsed.diagnostics if d["code"] == "overload_ambiguity"} == {2, 3}
    assert all(b.value in found for b in parsed.bindings if b.kind == "symbol")


def test_post_name_array_dimensions_are_part_of_signatures_and_bindings() -> None:
    parsed = java.parse_java(
        "Arrays.java",
        "class Arrays { Item values[], grid[][]; Item[] run(Item input[])[] { return input; } }",
    )

    assert parsed.status == "ready"
    method = symbols(parsed)["Arrays.run(Item[])"]
    assert method.return_type == "Item[][]"
    assert {(b.name, b.annotation) for b in parsed.bindings if b.kind == "field"} == {
        ("values", "Item[]"),
        ("grid", "Item[][]"),
    }


def test_record_components_and_compact_constructor_scope() -> None:
    parsed = java.parse_java(
        "User.java",
        """record User(String name, java.util.List<Item> items) implements Named {
  User { validate(name, items); }
  User(String name) { this(name, List.of()); }
  String label() { return name; }
}
""",
    )

    assert parsed.status == "ready"
    found = symbols(parsed)
    compact = "User.User(String,java.util.List<Item>)"
    assert found[compact].parameters == ["String", "java.util.List<Item>"]
    assert found["User.name"].kind == found["User.items"].kind == "field"
    assert {(b.name, b.annotation) for b in parsed.bindings if b.kind == "field"} == {
        ("name", "String"),
        ("items", "java.util.List<Item>"),
    }
    assert {(b.name, b.scope) for b in parsed.bindings if b.kind == "parameter"} >= {
        ("name", compact),
        ("items", compact),
    }
    validate = next(r for r in references(parsed, "CALL") if r.name == "validate")
    assert validate.source_qualname == compact
    assert validate.scope.startswith(compact + ".<block@")
    assert validate.arity == 2
    assert {r.name for r in references(parsed, "IMPLEMENTS")} == {"Named"}


def test_fields_local_type_annotations_safe_values_and_instantiations() -> None:
    parsed = java.parse_java(
        "Store.java",
        """class Store {
  Service service = new demo.Service(1), alias = service;
  String message = "SOURCE_SENTINEL";
  Service dynamic = factory();
  void run() {
    var first = new Widget<String>();
    Service copy = service;
    Service fromField = this.service;
    Service unknown = factory();
    Widget[] array = new Widget[3];
  }
}
""",
    )

    assert parsed.status == "ready"
    bindings = {b.name: b for b in parsed.bindings}
    assert (
        bindings["service"].kind,
        bindings["service"].annotation,
        bindings["service"].value,
    ) == ("field", "Service", "demo.Service")
    assert bindings["alias"].value == bindings["copy"].value == "service"
    assert bindings["fromField"].value == "this.service"
    assert (bindings["first"].annotation, bindings["first"].value) == ("", "Widget")
    assert bindings["dynamic"].value == bindings["unknown"].value == bindings["message"].value == ""
    assert {(r.name, r.arity) for r in references(parsed, "INSTANTIATION")} == {
        ("demo.Service", 1),
        ("Widget", 0),
        ("Widget", None),
    }
    assert "SOURCE_SENTINEL" not in json.dumps(parsed.to_dict())


def test_annotations_use_decorator_evidence_without_literal_values_or_keys() -> None:
    parsed = java.parse_java(
        "Annotated.java",
        """@demo.Label(value="ANNOTATION_SENTINEL", flag=true)
class Annotated {
  @Inject Service field;
  @Check(code=Labels.PUBLIC) @TypeUse(note="TYPE_SENTINEL") String run(@Valid String arg) {
    return arg;
  }
}
@interface Label { String value() default "DEFAULT_SENTINEL"; }
""",
    )

    assert parsed.status == "ready"
    assert {r.name for r in references(parsed, "DECORATOR")} == {
        "demo.Label",
        "Inject",
        "Check",
        "TypeUse",
        "Valid",
    }
    usages = {r.name for r in references(parsed, "REFERENCE")}
    assert "Labels.PUBLIC" in usages
    assert not {"value", "flag", "code", "note", "field", "run"} & usages
    serialized = json.dumps(parsed.to_dict())
    assert all(
        token not in serialized
        for token in ("ANNOTATION_SENTINEL", "TYPE_SENTINEL", "DEFAULT_SENTINEL")
    )


def test_receiver_names_and_unknown_chains_keep_static_evidence() -> None:
    parsed = java.parse_java(
        "Calls.java",
        """class Calls {
  void run(Service item) {
    this.work(); Tools.work(item); item.work(); this.field.work(); super.work();
    factory("CALL_SENTINEL").work(1);
    items[123].work();
    ("RECEIVER_SENTINEL").work();
    consume(Calls::work);
    missingName();
  }
}
""",
    )

    assert parsed.status == "ready"
    calls = {r.name for r in references(parsed, "CALL")}
    assert {
        "this.work",
        "Tools.work",
        "item.work",
        "this.field.work",
        "super.work",
        "factory().work",
        "factory",
        "items[].work",
        "(<expression>).work",
        "consume",
        "missingName",
    } == calls
    assert "Calls::work" in {r.name for r in references(parsed, "REFERENCE")}
    assert "work" not in calls
    serialized = json.dumps(parsed.to_dict())
    assert "CALL_SENTINEL" not in serialized and "RECEIVER_SENTINEL" not in serialized


def test_method_and_constructor_references_are_lexical_evidence() -> None:
    parsed = java.parse_java(
        "Refs.java",
        "class Refs { void run() { use(Widget::new); use(Outer.Inner::<Item>work); } }",
    )

    assert parsed.status == "ready"
    assert {"Widget::new", "Outer.Inner::work"} <= {r.name for r in references(parsed, "REFERENCE")}
    assert "Item" in {r.name for r in references(parsed, "TYPE_REFERENCE")}
    assert {r.name for r in references(parsed, "CALL")} == {"use"}
    assert not references(parsed, "INSTANTIATION")


def test_blocks_shadowing_and_source_ownership_are_independent() -> None:
    source = """class ScopeTest {
  void run(Service item) {
    { First item = new First(); item.call(); }
    { Second item = new Second(); item.call(); }
    item.call();
  }
  { initialize(); }
}
"""
    parsed = java.parse_java("ScopeTest.java", source)

    assert parsed.status == "ready"
    calls = [r for r in references(parsed, "CALL") if r.name == "item.call"]
    assert len({r.scope for r in calls}) == 3
    assert {r.source_qualname for r in calls} == {"ScopeTest.run(Service)"}
    declared = [b for b in parsed.bindings if b.name == "item"]
    assert len({b.scope for b in declared}) == 3
    scopes = {s.qualname: s for s in parsed.scopes}
    assert scopes[calls[0].scope].parent == scopes[calls[1].scope].parent == calls[2].scope
    assert next(
        r for r in references(parsed, "CALL") if r.name == "initialize"
    ).source_qualname == ("ScopeTest")


def test_loop_lambda_catch_and_resource_bindings_do_not_escape() -> None:
    parsed = java.parse_java(
        "Scoped.java",
        """class Scoped { void run(List<Item> items) {
  for (Item item : items) item.call();
  for (int index=0; index<2; index++) use(index);
  items.forEach(item -> item.call());
  try (Reader reader = new Reader()) { reader.read(); }
  catch (Problem | Other error) { error.print(); }
  finally { cleanup(); }
  after();
} }
""",
    )

    assert parsed.status == "ready"
    names = {b.name: b for b in parsed.bindings if b.name in {"index", "reader", "error"}}
    assert names["index"].scope != names["reader"].scope != names["error"].scope
    assert names["error"].annotation == "Problem|Other"
    item_bindings = [b for b in parsed.bindings if b.name == "item"]
    assert len(item_bindings) == 2
    assert item_bindings[0].scope != item_bindings[1].scope
    after = next(r for r in references(parsed, "CALL") if r.name == "after")
    assert all(b.scope != after.scope for b in item_bindings + list(names.values()))
    assert {r.source_qualname for r in references(parsed, "CALL")} == {"Scoped.run(List<Item>)"}


def test_anonymous_and_local_classes_keep_distinct_scopes_and_partial_evidence() -> None:
    parsed = java.parse_java(
        "Anon.java",
        """class Anon {
  Runnable first = new Runnable() { public void run() { firstCall(); } };
  Runnable second = new Runnable() { public void run() { secondCall(); } };
  void outer() {
    { class Local { void run() { localCall(); } } }
    { class Local { void run() { otherCall(); } } }
  }
}
""",
    )

    assert parsed.status == "partial"
    methods = [s for s in parsed.symbols if s.kind == "method" and s.name == "run"]
    assert len(methods) == len({s.qualname for s in methods}) == 4
    assert "Anon.run()" not in symbols(parsed)
    assert {d["code"] for d in parsed.diagnostics} == {"anonymous_class"}
    assert {r.source_qualname for r in references(parsed, "CALL")} == {s.qualname for s in methods}
    assert all(b.value == "" for b in parsed.bindings if b.name in {"first", "second"})


def test_unicode_identifiers_columns_and_long_point_coordinates() -> None:
    source = "\n" * 300 + "class 用户 { void 调用(服务 值) { " + " " * 270 + "值.执行(); } }"
    parsed = java.parse_java("源码/用户.java", source)

    assert parsed.status == "ready"
    found = symbols(parsed)
    assert "用户.调用(服务)" in found
    assert parsed.symbols[0].name == "用户"
    call = references(parsed, "CALL")[0]
    assert (call.name, call.line) == ("值.执行", 301)
    line = source.splitlines()[-1]
    assert call.column == line.index("执行") + 1
    assert call.column > 256


def test_combined_declarations_on_a_long_line_do_not_crash() -> None:
    parsed = java.parse_java(
        "Mixed.java",
        "package demo; import a.b.C; import a.b.*; import static a.b.C.helper; "
        '@Mark(note="LITERAL_SENTINEL") class Outer<T extends Base> extends Parent<T> '
        "implements Api { Service service=new Service(); class Inner { int[] f(String v[], "
        "int... xs) { { Item item=new Item(); item.go(); } "
        "this.go(); C.helper(v); return null; } } } "
        "record User(String name, int age) { User { check(name); } } "
        "enum Color { RED(1), BLUE(2); Color(int n) {} } "
        '@interface Mark { String note() default "DEFAULT_SENTINEL"; }',
    )

    assert parsed.status == "ready"
    assert "Outer.Inner.f(String[],int...)" in symbols(parsed)
    assert "User.User(String,int)" in symbols(parsed)
    assert "Color.Color(int)" in symbols(parsed)
    assert any(r.column > 256 for r in parsed.references)
    assert "LITERAL_SENTINEL" not in json.dumps(parsed.to_dict())


@pytest.mark.parametrize(
    "source",
    [
        'class Broken { void run( { "BROKEN_SENTINEL"; }',
        "class Kept {} class Broken { int x = ; }",
        'not valid Java @@@ "BROKEN_SENTINEL"',
    ],
)
def test_invalid_syntax_returns_sanitized_partial_or_parse_error(source: str) -> None:
    parsed = java.parse_java("Broken.java", source)

    assert parsed.status in {"partial", "parse_error"}
    assert parsed.diagnostics
    assert "BROKEN_SENTINEL" not in json.dumps(parsed.diagnostics)
    assert all(set(d) <= {"code", "message", "line", "column", "count"} for d in parsed.diagnostics)
    assert parsed.symbols[0].kind == "module"


def test_pattern_variable_scope_is_reported_as_partial() -> None:
    parsed = java.parse_java(
        "Pattern.java",
        "class Pattern { void run(Object value) { if (value instanceof Item item) item.run(); } }",
    )

    assert parsed.status == "partial"
    assert any(d["code"] == "pattern_binding" for d in parsed.diagnostics)
    assert not any(b.name == "item" for b in parsed.bindings)
    assert "item.run" in {r.name for r in references(parsed, "CALL")}


@pytest.mark.parametrize(
    ("limit", "value", "source", "resource"),
    [
        ("MAX_PARSE_BYTES", 20, "class Example { void run() {} }", "bytes"),
        ("MAX_PARSE_BYTES", 30, "class 中文类 { 服务 字段; }", "bytes"),
        ("MAX_FILE_NODES", 4, "class Example {}", "nodes"),
        ("MAX_FILE_SYMBOLS", 3, "class Example { int a; int b; }", "symbols"),
        (
            "MAX_FILE_REFERENCES",
            2,
            "class Example { void run() { first(); second(); third(); } }",
            "references",
        ),
    ],
)
def test_resource_caps_return_bounded_facts(
    monkeypatch: pytest.MonkeyPatch, limit: str, value: int, source: str, resource: str
) -> None:
    monkeypatch.setattr(java, limit, value)
    parsed = java.parse_java("Example.java", source)

    assert parsed.status == "resource_limited"
    assert parsed.diagnostics[-1]["resource"] == resource
    assert len(parsed.symbols) <= java.MAX_FILE_SYMBOLS
    assert len(parsed.references) <= java.MAX_FILE_REFERENCES
    if resource in {"bytes", "nodes"}:
        assert len(parsed.symbols) == 1 and not parsed.references


def test_deep_scopes_and_large_names_have_bounded_facts() -> None:
    deep = "class Deep { void run() { " + "{" * 1_200 + "call();" + "}" * 1_200 + "} }"
    parsed = java.parse_java("Deep.java", deep)
    assert parsed.status == "resource_limited"
    assert parsed.diagnostics[-1]["resource"] == "fact_text"
    long_name = "class " + "A" * 2_100 + " {}"
    assert java.parse_java("Long.java", long_name).status == "resource_limited"


@pytest.mark.parametrize(
    "exception", [RuntimeError("EXCEPTION_SENTINEL"), MemoryError("MEMORY_SENTINEL")]
)
def test_parser_failures_never_echo_exception_text(
    monkeypatch: pytest.MonkeyPatch, exception: Exception
) -> None:
    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(java, "Parser", fail)
    parsed = java.parse_java("Fail.java", "class Fail {}")
    assert parsed.status == (
        "resource_limited" if isinstance(exception, MemoryError) else "parse_error"
    )
    assert "SENTINEL" not in json.dumps(parsed.diagnostics)


def test_invalid_utf8_text_is_sanitized() -> None:
    parsed = java.parse_java("Invalid.java", "class Invalid {}\ud800UTF_SENTINEL")
    assert parsed.status == "parse_error"
    assert "UTF_SENTINEL" not in json.dumps(parsed.diagnostics)


def test_parse_never_reads_imports_or_executes_project_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = """import untrusted.Project;
class Dangerous {
  static { System.exit(1); new ProcessBuilder("EXECUTION_SENTINEL").start(); }
  void run() { Class.forName("IMPORT_SENTINEL"); Files.write(null, null); }
}
"""

    def forbidden(*args, **kwargs):
        raise AssertionError("Java extraction attempted filesystem, import or execution access")

    path = tmp_path / "Dangerous.java"
    with monkeypatch.context() as guard:
        for name in ("open", "eval", "exec"):
            guard.setattr(builtins, name, forbidden)
        guard.setattr(Path, "read_text", forbidden)
        guard.setattr(Path, "read_bytes", forbidden)
        guard.setattr(subprocess, "run", forbidden)
        guard.setattr(subprocess, "Popen", forbidden)
        guard.setattr(os, "system", forbidden)
        guard.setattr(builtins, "__import__", forbidden)
        parsed = java.parse_java(str(path), source)

    assert parsed.status == "ready"
    assert not path.exists()
    assert {r.name for r in references(parsed, "CALL")} >= {
        "System.exit",
        "Class.forName",
        "Files.write",
    }
    assert "EXECUTION_SENTINEL" not in json.dumps(parsed.to_dict())
    assert "IMPORT_SENTINEL" not in json.dumps(parsed.to_dict())
