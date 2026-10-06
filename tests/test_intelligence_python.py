import ast
import builtins
import gc
import json
import weakref
from pathlib import Path
from textwrap import dedent

import pytest

import code_context.intelligence_models as intelligence_models
import code_context.intelligence_python as python_parser
from code_context.intelligence_models import RELATION_KINDS, ParsedFile
from code_context.intelligence_python import parse_python


def parse(source: str, path: str = "pkg/example.py") -> ParsedFile:
    return parse_python(path, dedent(source).lstrip("\n"))


def references(parsed: ParsedFile, kind: str, name: str = ""):
    return [ref for ref in parsed.references if ref.kind == kind and (not name or ref.name == name)]


def bindings(parsed: ParsedFile, name: str):
    return [binding for binding in parsed.bindings if binding.name == name]


def test_module_positions_serialization_and_empty_package():
    parsed = parse_python("pkg/entry.py", "# comment\r\n\r\ndef run():\r\n    pass\r\n")
    module, function = parsed.symbols
    assert parsed.status == "ready" and parsed.language == "python"
    assert parsed.module == "pkg.entry"
    assert (module.name, module.qualname, module.kind, module.start_line, module.end_line) == (
        "entry",
        "",
        "module",
        0,
        4,
    )
    assert (function.start_line, function.end_line, function.column) == (3, 4, 1)
    assert parsed.scopes[0].qualname == "" and parsed.scopes[0].kind == "module"
    assert ParsedFile.from_dict(parsed.to_dict()) == parsed
    assert parse_python("pkg/__init__.py", "").module == "pkg"
    assert parse_python("pkg/__init__.py", "").symbols[0].end_line == 0


def test_functions_async_methods_nested_classes_and_definition_bindings():
    parsed = parse("""
        def outer():
            def same():
                return 1
            async def work():
                return same()
            class Inner:
                async def method(self):
                    def same():
                        return 2
                    return same()
            return work()
        def other():
            def same():
                return 3
            return same()
    """)
    symbols = {symbol.qualname: symbol for symbol in parsed.symbols}
    assert set(symbols) == {
        "",
        "outer",
        "outer.same",
        "outer.work",
        "outer.Inner",
        "outer.Inner.method",
        "outer.Inner.method.same",
        "other",
        "other.same",
    }
    assert symbols["outer.Inner.method"].kind == "method"
    assert symbols["outer.Inner.method.same"].kind == "function"
    assert symbols["outer.work"].signature.startswith("async def work")
    assert symbols["outer.Inner"].scope == "outer"
    assert {ref.scope for ref in references(parsed, "CALL", "same")} == {
        "outer.work",
        "outer.Inner.method",
        "other",
    }
    assert all(
        binding.kind == "symbol" and binding.value.endswith("same")
        for binding in bindings(parsed, "same")
    )
    assert {ref.source_qualname for ref in references(parsed, "CALL", "same")} == {
        "outer.work",
        "outer.Inner.method",
        "other",
    }


def test_overloads_keep_same_qualname_and_distinct_symbol_ids():
    parsed = parse("""
        from typing import overload
        @overload
        def convert(value: int) -> int: ...
        @overload
        def convert(value: str) -> str: ...
        def convert(value): return value
    """)
    overloaded = [symbol for symbol in parsed.symbols if symbol.name == "convert"]
    assert len(overloaded) == 3
    assert {symbol.qualname for symbol in overloaded} == {"convert"}
    assert len({symbol.id for symbol in overloaded}) == 3
    assert len(references(parsed, "DECORATOR", "overload")) == 2


def test_decorators_inheritance_and_annotations_keep_declaration_owner():
    parsed = parse("""
        from types_api import Base, Entity, Result, deco, configure
        @deco
        class Service(Base[Entity]):
            @configure(Entity)
            def run(self, value: Entity) -> Result:
                return Result(value)
    """)
    inheritance = references(parsed, "INHERITANCE", "Base")[0]
    assert inheritance.source_qualname == "Service" and inheritance.scope == ""
    deco = references(parsed, "DECORATOR", "deco")[0]
    assert deco.source_qualname == "Service" and deco.scope == ""
    configure = references(parsed, "DECORATOR", "configure")[0]
    assert configure.source_qualname == "Service.run" and configure.scope == "Service"
    parameter_type = next(
        ref
        for ref in references(parsed, "TYPE_REFERENCE", "Entity")
        if ref.source_qualname == "Service.run"
    )
    assert parameter_type.scope == "Service"
    constructor = references(parsed, "CALL", "Result")[0]
    assert constructor.source_qualname == constructor.scope == "Service.run"
    assert constructor.arity == 1
    assert not references(parsed, "INSTANTIATION")
    assert not references(parsed, "REFERENCE", "deco")


def test_parameters_annotations_and_literal_free_signatures():
    parsed = parse("""
        def run(first: "Node", /, second: list["Node"] = "__DEFAULT_TEXT__",
                *items: Node, flag: bool = True, **options: "Options") -> "Node | None":
            return first
    """)
    function = parsed.symbols[1]
    assert function.parameters == ["first", "second", "items", "flag", "options"]
    assert function.return_type == "Node | None"
    assert function.signature == (
        "def run(first: Node, /, second: list[Node] = ..., *items: Node, "
        "flag: bool = ..., **options: Options) -> Node | None"
    )
    assert {
        binding.name: binding.annotation
        for binding in parsed.bindings
        if binding.kind == "parameter"
    } == {
        "first": "Node",
        "second": "list[Node]",
        "items": "Node",
        "flag": "bool",
        "options": "Options",
    }
    assert "__DEFAULT_TEXT__" not in json.dumps(parsed.to_dict())
    assert not references(parsed, "TYPE_REFERENCE", "bool")[0].type_only
    assert not references(parsed, "TYPE_REFERENCE", "list")[0].type_only
    assert references(parsed, "TYPE_REFERENCE", "Options")[0].type_only
    assert all(ref.line > 0 and ref.column > 0 for ref in parsed.references)


def test_forward_annotations_skip_literal_and_annotated_metadata():
    parsed = parse("""
        from typing import Literal as Choice, Annotated as Meta
        import typing as t
        value: "list[Node | None]"
        choice: Choice["__LITERAL_TEXT__"]
        marked: Meta[Node, "__METADATA_TEXT__"]
        nested: "tuple[t.Literal['__FORWARD_LITERAL__'], Node]"
        unsafe: "danger('__ANNOTATION_TEXT__')"
        broken: "Not a type!"
        string = "__ORDINARY_TEXT__"
    """)
    names = {ref.name for ref in references(parsed, "TYPE_REFERENCE")}
    assert {"list", "Node", "Choice", "Meta", "tuple", "t.Literal"} <= names
    assert not names & {"danger", "Not", "string"}
    serialized = json.dumps(parsed.to_dict())
    for excluded in [
        "__LITERAL_TEXT__",
        "__METADATA_TEXT__",
        "__FORWARD_LITERAL__",
        "__ANNOTATION_TEXT__",
        "__ORDINARY_TEXT__",
    ]:
        assert excluded not in serialized
    assert bindings(parsed, "choice")[0].annotation == "Choice[...]"
    assert bindings(parsed, "marked")[0].annotation == "Meta[Node]"
    assert parsed.status == "ready"


def test_forward_annotation_numeric_literals_and_complex_metadata_are_ignored():
    parsed = parse("""
        from typing import Literal, Annotated
        status: "tuple[Literal[1, True, '__FORWARD_VALUE__'], Node]"
        metadata: "Annotated[Node, {'note': '__FORWARD_METADATA__'}]"
        call_metadata: "Annotated[Node, dangerous('__ANNOTATION_METADATA__')]"
    """)
    assert {ref.name for ref in references(parsed, "TYPE_REFERENCE")} == {
        "tuple",
        "Literal",
        "Node",
        "Annotated",
    }
    assert not references(parsed, "CALL")
    serialized = json.dumps(parsed.to_dict())
    assert "__FORWARD_VALUE__" not in serialized
    assert "__FORWARD_METADATA__" not in serialized
    assert "__ANNOTATION_METADATA__" not in serialized
    assert parsed.status == "ready"


@pytest.mark.parametrize(
    "context,type_only",
    [
        ("runtime", False),
        ("future", True),
        ("type_checking", True),
    ],
)
def test_annotation_kind_does_not_alone_imply_type_only(context, type_only):
    function = "def run(item: Node) -> Node:\n    return construct(item)\n"
    if context == "future":
        source = "from __future__ import annotations\n" + function
    elif context == "type_checking":
        source = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        source += "\n".join("    " + line for line in function.splitlines()) + "\n"
    else:
        source = function
    parsed = parse_python("types.py", source)
    assert len(references(parsed, "TYPE_REFERENCE", "Node")) == 2
    assert all(ref.type_only is type_only for ref in references(parsed, "TYPE_REFERENCE"))
    assert references(parsed, "CALL", "construct")[0].type_only is (context == "type_checking")
    assert all(
        ref.conditional is (context == "type_checking")
        for ref in references(parsed, "TYPE_REFERENCE")
    )


def test_forward_string_is_type_only_while_real_annotation_call_can_evaluate():
    parsed = parse("""
        def run(item: "Node") -> factory():
            return item
    """)
    assert references(parsed, "TYPE_REFERENCE", "Node")[0].type_only
    assert not references(parsed, "TYPE_REFERENCE", "<dynamic>")[0].type_only
    assert not references(parsed, "CALL", "factory")[0].type_only


def test_real_annotation_metadata_and_enum_values_are_lexical_evidence():
    parsed = parse("""
        from typing import Annotated, Literal
        def run(item: Annotated[Node, marker(Config, '__METADATA_LITERAL__')],
                status: Literal[Status.READY, '__LITERAL_VALUE__']):
            return item
    """)
    assert references(parsed, "CALL", "marker")[0].source_qualname == "run"
    assert not references(parsed, "CALL", "marker")[0].type_only
    assert references(parsed, "REFERENCE", "Config")
    assert references(parsed, "REFERENCE", "Status.READY")
    assert {ref.name for ref in references(parsed, "TYPE_REFERENCE")} == {
        "Annotated",
        "Node",
        "Literal",
    }
    serialized = json.dumps(parsed.to_dict())
    assert "__METADATA_LITERAL__" not in serialized
    assert "__LITERAL_VALUE__" not in serialized


def test_dynamic_real_annotations_and_bases_keep_unknown_evidence():
    parsed = parse("""
        class Model(factory()[Argument]):
            field: choose('__ANNOTATION_LITERAL__')
            def run(self, item: maker().Type) -> select():
                self.other: choose() = item
    """)
    assert {symbol.qualname for symbol in parsed.symbols if symbol.kind == "field"} == {
        "Model.field",
        "Model.other",
    }
    assert references(parsed, "INHERITANCE", "<dynamic>")
    assert {ref.name for ref in references(parsed, "CALL")} == {
        "factory",
        "choose",
        "maker",
        "select",
    }
    assert references(parsed, "TYPE_REFERENCE", "<dynamic>.Type")
    assert not bindings(parsed, "field")[0].annotation
    assert "__ANNOTATION_LITERAL__" not in json.dumps(parsed.to_dict())


def test_annotated_fields_class_and_explicit_receiver():
    parsed = parse("""
        class Model:
            title: str
            peer: "Model" = Model()
            raw = "__FIELD_LITERAL__"
            def initialize(self):
                self.child: Model = Model()
                self.cache = {}
            @staticmethod
            def unrelated(obj):
                obj.unknown: Model = Model()
    """)
    fields = [symbol for symbol in parsed.symbols if symbol.kind == "field"]
    assert {symbol.qualname for symbol in fields} == {"Model.title", "Model.peer", "Model.child"}
    assert all(symbol.scope == "Model" for symbol in fields)
    assert bindings(parsed, "title")[0].kind == "field"
    peer = bindings(parsed, "peer")[0]
    assert peer.kind == "field" and peer.annotation == peer.value == "Model"
    assert bindings(parsed, "child")[0].scope == "Model"
    assert not bindings(parsed, "unknown")
    assert references(parsed, "REFERENCE", "obj.unknown")
    assert "__FIELD_LITERAL__" not in json.dumps(parsed.to_dict())


def test_reassigned_method_receiver_does_not_invent_class_field():
    parsed = parse("""
        class Model:
            def initialize(self):
                self.owned: Node
                self = replacement
                self.unknown: Node
            @classmethod
            def configure(cls):
                cls.setting: Node
    """)
    assert {symbol.qualname for symbol in parsed.symbols if symbol.kind == "field"} == {
        "Model.owned",
        "Model.setting",
    }
    assert references(parsed, "REFERENCE", "self.unknown")
    assert not bindings(parsed, "unknown")


def test_parameters_assignments_and_imports_preserve_shadowing():
    parsed = parse("""
        from api import run
        def parent():
            def run(): pass
            def parameter(run): return run()
            def local():
                run()
                run = replacement
                return run()
            def imported():
                from other import run
                return run()
            return run()
    """)
    parameter = bindings(parsed, "run")
    assert {(binding.scope, binding.kind) for binding in parameter} == {
        ("parent", "symbol"),
        ("parent.parameter", "parameter"),
        ("parent.local", "variable"),
    }
    assert {(imp.scope, imp.module, imp.local_name) for imp in parsed.imports} == {
        ("", "api", "run"),
        ("parent.imported", "other", "run"),
    }
    assert {ref.scope for ref in references(parsed, "CALL", "run")} == {
        "parent.parameter",
        "parent.local",
        "parent.imported",
        "parent",
    }
    assert bindings(parsed, "run")[-1].value == "replacement"


def test_global_nonlocal_and_late_outer_assignment():
    parsed = parse("""
        target = Original
        def outer():
            def change():
                global target
                nonlocal captured
                target = Replacement()
                captured = target
                return captured.run()
            captured = Original()
            return change()
    """)
    change = next(scope for scope in parsed.scopes if scope.qualname == "outer.change")
    assert change.globals == ["target"] and change.nonlocals == ["captured"]
    assert {binding.scope for binding in bindings(parsed, "target")} == {""}
    assert {binding.scope for binding in bindings(parsed, "captured")} == {"outer"}
    assert references(parsed, "CALL", "captured.run")[0].scope == "outer.change"
    assert not references(parsed, "REFERENCE", "captured")
    assert [ref.line for ref in references(parsed, "REFERENCE", "target")] == [7]


def test_import_alias_relative_level_star_and_no_identifier_duplicates():
    parsed = parse("""
        import pkg.mod
        import pkg.other as other
        from . import neighbor as sibling
        from ..shared import Entity as E, worker
        from foreign import *
        other.run()
        E()
    """)
    assert [
        (imp.module, imp.imported_name, imp.local_name, imp.level) for imp in parsed.imports
    ] == [
        ("pkg.mod", "", "pkg", 0),
        ("pkg.other", "", "other", 0),
        ("", "neighbor", "sibling", 1),
        ("shared", "Entity", "E", 2),
        ("shared", "worker", "worker", 2),
        ("foreign", "*", "*", 0),
    ]
    assert len(references(parsed, "IMPORT")) == 6
    assert {ref.name for ref in references(parsed, "IMPORT")} == {
        "pkg.mod",
        "pkg.other",
        ".neighbor",
        "..shared.Entity",
        "..shared.worker",
        "foreign.*",
    }
    assert not references(parsed, "REFERENCE")
    assert not parsed.bindings


def test_type_checking_aliases_negation_nested_and_conditional_imports():
    parsed = parse("""
        import typing as t
        from typing import TYPE_CHECKING as TC
        if TC:
            from only_types import Entity
            if enabled:
                import nested_types
        if not t.TYPE_CHECKING:
            import runtime_api
        else:
            import deferred_api
        if TC and enabled:
            import both_types
        if TC or enabled:
            import maybe_runtime
        try:
            import optional_api
        except ImportError:
            import fallback_api
    """)
    by_module = {imp.module: imp for imp in parsed.imports}
    type_modules = {"only_types", "nested_types", "deferred_api", "both_types"}
    assert {imp.module for imp in parsed.imports if imp.type_only} == type_modules
    assert all(by_module[module].conditional for module in type_modules)
    assert not by_module["runtime_api"].type_only
    assert not by_module["maybe_runtime"].type_only
    assert by_module["optional_api"].conditional and by_module["fallback_api"].conditional
    assert not by_module["typing"].conditional
    assert {ref.name for ref in references(parsed, "IMPORT") if ref.type_only} == {
        "only_types.Entity",
        "nested_types",
        "deferred_api",
        "both_types",
    }


@pytest.mark.parametrize("shadow", ["parameter", "late_assignment", "class_namespace"])
def test_type_checking_markers_respect_lexical_shadowing(shadow):
    bodies = {
        "parameter": "def run(TC):\n    if TC:\n        import runtime_api\n",
        "late_assignment": ("def run():\n    if TC:\n        import runtime_api\n    TC = True\n"),
        "class_namespace": (
            "class Owner:\n    from typing import TYPE_CHECKING as TC\n"
            "    def run(self, TC):\n        if TC:\n            import runtime_api\n"
        ),
    }
    parsed = parse_python("markers.py", "from typing import TYPE_CHECKING as TC\n" + bodies[shadow])
    assert not next(imp for imp in parsed.imports if imp.module == "runtime_api").type_only


@pytest.mark.parametrize(
    "import_statement",
    [
        "from .typing import TYPE_CHECKING as t",
        "from typing import SomethingElse as t",
    ],
)
def test_typing_marker_requires_the_typing_module_or_exact_import(import_statement):
    condition = "t" if import_statement.startswith("from .") else "t.TYPE_CHECKING"
    source = f"{import_statement}\nif {condition}:\n    import runtime_api\n"
    parsed = parse_python("pkg/markers.py", source)
    assert not next(imp for imp in parsed.imports if imp.module == "runtime_api").type_only


def test_annotation_assignment_preserves_whole_function_marker_shadowing():
    parsed = parse("""
        from typing import TYPE_CHECKING as TC
        def outer():
            if TC:
                import runtime_api
            def inner(item: (TC := Type)):
                return item
    """)
    assert not next(imp for imp in parsed.imports if imp.module == "runtime_api").type_only
    assert bindings(parsed, "TC")[0].scope == "outer"


def test_conditional_typing_marker_is_not_proof_of_type_only_context():
    parsed = parse("""
        if enabled:
            TC = user_check
        else:
            from typing import TYPE_CHECKING as TC
        if TC:
            import runtime_api
    """)
    assert not next(imp for imp in parsed.imports if imp.module == "runtime_api").type_only
    assert next(imp for imp in parsed.imports if imp.module == "runtime_api").conditional


def test_lambda_and_all_comprehensions_have_isolated_bindings():
    parsed = parse("""
        def run(): pass
        results = [run() for run in sources if allowed(run)]
        unique = {run() for run in sources}
        mapping = {key: run() for key, run in pairs}
        pending = (run() for run in sources)
        callback = lambda run: run()
        run()
    """)
    calls = references(parsed, "CALL", "run")
    assert len(calls) == 6
    assert calls[-1].scope == calls[-1].source_qualname == ""
    assert all(
        ref.scope.startswith(
            ("<lambda>@", "<listcomp>@", "<setcomp>@", "<dictcomp>@", "<generatorexp>@")
        )
        for ref in calls[:-1]
    )
    assert all(ref.source_qualname == "" for ref in calls)
    assert {binding.kind for binding in bindings(parsed, "run") if binding.scope} == {
        "variable",
        "parameter",
    }
    assert not any(
        binding.scope == "" and binding.kind != "symbol" for binding in bindings(parsed, "run")
    )
    source_reads = references(parsed, "REFERENCE", "sources")
    assert all(ref.scope == "" for ref in source_reads)


def test_comprehensions_lambda_skip_class_namespace_and_walrus_binds_outer():
    parsed = parse("""
        def run(): pass
        class Owner:
            run = replacement
            values = [run() for item in sources]
            callback = lambda item: run(item)
        def outer():
            values = [result for item in inputs if (result := make(item))]
            return result
    """)
    isolated = [scope for scope in parsed.scopes if scope.kind in {"lambda", "comprehension"}]
    assert [scope.parent for scope in isolated] == ["", "", "outer"]
    assert bindings(parsed, "result")[0].scope == "outer"
    assert {ref.source_qualname for ref in references(parsed, "CALL", "run")} == {"Owner"}
    make = references(parsed, "CALL", "make")[0]
    assert make.source_qualname == "outer" and "<listcomp>@" in make.scope


def test_nested_comprehensions_and_lambda_defaults_use_correct_scope():
    parsed = parse("""
        def outer():
            values = [[consume(inner) for inner in row] for row in inputs]
            callback = lambda param=make(): (lambda param: consume(param))(param)
            return values
    """)
    comps = [scope for scope in parsed.scopes if scope.kind == "comprehension"]
    assert len(comps) == 2 and comps[1].parent == comps[0].qualname
    assert bindings(parsed, "inner")[0].scope == comps[1].qualname
    assert references(parsed, "REFERENCE", "row")[0].scope == comps[0].qualname
    assert references(parsed, "CALL", "make")[0].scope == "outer"
    lambdas = [scope for scope in parsed.scopes if scope.kind == "lambda"]
    assert lambdas[1].parent == lambdas[0].qualname
    assert references(parsed, "CALL", "consume")[-1].scope == lambdas[1].qualname
    assert all(ref.source_qualname == "outer" for ref in parsed.references)


def test_comprehension_later_target_masks_type_checking_in_earlier_filter():
    parsed = parse("""
        from typing import TYPE_CHECKING as TC
        values = [item for item in inputs if (guard() if TC else fallback()) for TC in flags]
    """)
    assert parsed.status == "ready"
    assert not references(parsed, "CALL", "guard")[0].type_only
    assert not references(parsed, "CALL", "fallback")[0].type_only
    assert "<listcomp>@" in bindings(parsed, "TC")[0].scope


def test_global_imports_and_nonlocal_imports_bind_directive_namespace():
    parsed = parse("""
        def outer():
            imported = None
            def inner():
                global external
                nonlocal imported
                import api as external
                from api import Entity as imported
                return external.run(imported)
    """)
    assert {(imp.local_name, imp.scope) for imp in parsed.imports} == {
        ("external", ""),
        ("imported", "outer"),
    }
    assert all(ref.scope == "outer.inner" for ref in references(parsed, "IMPORT"))
    assert references(parsed, "CALL", "external.run")[0].scope == "outer.inner"


def test_dotted_definition_binding_value_with_global_declaration():
    parsed = parse("""
        def outer():
            global exported
            def exported():
                return 1
            return exported()
    """)
    definition = next(symbol for symbol in parsed.symbols if symbol.name == "exported")
    binding = bindings(parsed, "exported")[0]
    assert definition.qualname == "outer.exported" and definition.scope == "outer"
    assert binding.kind == "symbol" and binding.scope == "" and binding.value == "outer.exported"


def test_call_arity_static_keyword_expansion_and_dynamic_target_evidence():
    parsed = parse("""
        simple()
        simple(one, two, option=three)
        simple(*args)
        simple(**options)
        obj.run(one)
        make().run(two)
        registry["__DYNAMIC_KEY__"](three)
        getattr(obj, "__METHOD_TEXT__")()
        (lambda x: x)(one)
    """)
    assert [ref.arity for ref in references(parsed, "CALL", "simple")] == [0, 3, None, None]
    assert references(parsed, "CALL", "obj.run")[0].arity == 1
    assert references(parsed, "CALL", "<dynamic>.run")[0].arity == 1
    assert len(references(parsed, "CALL", "<dynamic>")) == 3
    assert references(parsed, "CALL", "make") and references(parsed, "CALL", "getattr")
    assert not references(parsed, "CALL", "run")
    assert not references(parsed, "CALL", "__METHOD_TEXT__")
    assert "__DYNAMIC_KEY__" not in json.dumps(parsed.to_dict())
    assert "__METHOD_TEXT__" not in json.dumps(parsed.to_dict())


def test_assignments_unpacking_loop_with_exception_and_match_bindings():
    parsed = parse("""
        direct = Alias
        object_ = Entity()
        dynamic = maker().field
        literal = "__VALUE_TEXT__"
        left, right = (First(), Second)
        head, *tail = items
        for item in inputs:
            item += offset
        with manager() as resource:
            consume(resource)
        try:
            fail()
        except Error as error:
            consume(error)
        match payload:
            case {"__MATCH_KEY__": capture, **rest}:
                consume(capture, rest)
            case [first, *others]:
                consume(first, others)
        del direct
    """)
    assert bindings(parsed, "direct")[0].value == "Alias"
    assert bindings(parsed, "object_")[0].value == "Entity"
    assert bindings(parsed, "dynamic")[0].value == bindings(parsed, "literal")[0].value == ""
    assert bindings(parsed, "left")[0].value == "First"
    assert bindings(parsed, "right")[0].value == "Second"
    for name in ["head", "tail", "item", "resource", "error", "capture", "rest", "first", "others"]:
        assert bindings(parsed, name) and bindings(parsed, name)[0].kind == "variable"
    assert bindings(parsed, "direct")[-1].value == ""
    assert references(parsed, "REFERENCE", "offset")
    assert "__MATCH_KEY__" not in json.dumps(parsed.to_dict())


def test_comments_docstrings_literals_and_import_names_are_not_reads():
    parsed = parse('''
        """__MODULE_DOC__ phantom()"""
        from lib import run as execute
        # __COMMENT_TEXT__ run()
        class Subject:
            """__CLASS_DOC__ run()"""
            def method(self):
                """__FUNCTION_DOC__ run()"""
                text = "__STRING_TEXT__ run()"
                return f"__FORMAT_TEXT__ {execute()}"
    ''')
    assert [ref.name for ref in references(parsed, "CALL")] == ["execute"]
    assert not references(parsed, "REFERENCE", "run")
    assert not references(parsed, "REFERENCE", "execute")
    assert len(references(parsed, "IMPORT")) == 1
    serialized = json.dumps(parsed.to_dict())
    for forbidden in [
        "__MODULE_DOC__",
        "phantom",
        "__COMMENT_TEXT__",
        "__CLASS_DOC__",
        "__FUNCTION_DOC__",
        "__STRING_TEXT__",
        "__FORMAT_TEXT__",
    ]:
        assert forbidden not in serialized


def test_unicode_character_columns_forward_string_origin_and_source_lines():
    parsed = parse_python(
        "目录/入口.py", 'class 模型:\n    字段: "模型"\n结果 = 模型(); 结果.运行()\n'
    )
    assert parsed.symbols[0].name == "入口" and parsed.symbols[0].end_line == 3
    calls = references(parsed, "CALL")
    assert [(ref.name, ref.line, ref.column) for ref in calls] == [
        ("模型", 3, 6),
        ("结果.运行", 3, 12),
    ]
    forward = references(parsed, "TYPE_REFERENCE", "模型")[0]
    assert (forward.line, forward.column) == (2, 9)
    assert parsed.status == "ready"


def test_unicode_literal_line_separators_do_not_shift_source_positions():
    source = 'note = "a\u2028b\u0085c"; 结果 = 调用()\n'
    parsed = parse_python("unicode.py", source)
    assert parsed.symbols[0].end_line == 1
    assert [(ref.line, ref.column) for ref in references(parsed, "CALL", "调用")] == [
        (1, source.index("调用") + 1),
    ]
    assert parsed.status == "ready"


@pytest.mark.parametrize(
    "source",
    [
        "def broken(:\n    pass",
        "def __SOURCE_TEXT__(",
        "x = '\x00'",
        "x = '\ud800'",
    ],
)
def test_invalid_syntax_and_encoding_do_not_echo_input(source):
    parsed = parse_python("invalid.py", source)
    assert parsed.status == "parse_error"
    assert parsed.diagnostics[0]["code"] == "PYTHON_PARSE_ERROR"
    assert "__SOURCE_TEXT__" not in json.dumps(parsed.diagnostics)
    assert len(parsed.symbols) == len(parsed.scopes) == 1
    assert not parsed.references


@pytest.mark.parametrize(
    "source",
    [
        "def run(value, value): pass\n",
        "def run():\n    nonlocal missing\n",
        "def run():\n    value = 1\n    global value\n",
        "class Model:\n    field = [item for item in inputs if (captured := item)]\n",
        "def run():\n    from api import *\n",
    ],
)
def test_invalid_lexical_syntax_is_rejected_before_facts(source):
    parsed = parse_python("invalid_scope.py", source)
    assert parsed.status == "parse_error"
    assert len(parsed.symbols) == len(parsed.scopes) == 1
    assert not parsed.bindings and not parsed.references
    assert parsed.diagnostics[0]["code"] == "PYTHON_PARSE_ERROR"


def test_parse_bytes_limit_counts_utf8_and_does_not_invoke_ast(monkeypatch):
    monkeypatch.setattr(python_parser, "MAX_PARSE_BYTES", 20)

    def forbidden(*args, **kwargs):
        pytest.fail("byte-limited input reached ast.parse")

    monkeypatch.setattr(python_parser.ast, "parse", forbidden)
    parsed = parse_python("large.py", "#" + "中" * 7)
    assert parsed.status == "resource_limited"
    assert parsed.diagnostics[0]["code"] == "MAX_PARSE_BYTES"


def test_uses_shared_bigger_project_limits():
    expected = {
        "MAX_PARSE_BYTES": 4 * 1024 * 1024,
        "MAX_FILE_NODES": 250_000,
        "MAX_FILE_SYMBOLS": 5_000,
        "MAX_FILE_REFERENCES": 20_000,
    }
    for name, limit in expected.items():
        assert getattr(python_parser, name) == getattr(intelligence_models, name) == limit


@pytest.mark.parametrize("padding", ["x", "中"])
def test_exact_four_mib_source_is_accepted_without_retaining_comment_text(padding):
    body = "def run():\n    return work()\n"
    padding_bytes = python_parser.MAX_PARSE_BYTES - len(body.encode("utf-8")) - 2
    characters, remainder = divmod(padding_bytes, len(padding.encode("utf-8")))
    source = "#" + padding * characters + " " * remainder + "\n" + body
    assert len(source.encode("utf-8")) == 4 * 1024 * 1024
    parsed = parse_python("large.py", source)
    assert parsed.status == "ready" and not parsed.diagnostics
    assert (parsed.symbols[1].qualname, parsed.symbols[1].start_line) == ("run", 2)
    assert references(parsed, "CALL", "work")[0].line == 3
    assert len(json.dumps(parsed.to_dict(), ensure_ascii=False).encode("utf-8")) < 2_000


@pytest.mark.parametrize("facts", ["nodes", "symbols", "references"])
def test_fact_counts_above_previous_limits_are_supported(facts):
    if facts == "nodes":
        source = "entry = 0\n" * 25_001
    elif facts == "symbols":
        source = "".join(f"def defined_{index}(): pass\n" for index in range(2_501))
    else:
        source = "target()\n" * 12_001
    parsed = parse_python("expanded.py", source)
    assert parsed.status == "ready" and not parsed.diagnostics
    if facts == "nodes":
        assert len(parsed.bindings) == 25_001
        assert len(parsed.symbols) == 1 and not parsed.references
    elif facts == "symbols":
        assert len(parsed.symbols) == 2_502
    else:
        assert len(references(parsed, "CALL", "target")) == 12_001


def test_source_exceeding_current_byte_budget_never_reaches_ast(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("oversized source reached ast.parse")

    monkeypatch.setattr(python_parser.ast, "parse", forbidden)
    parsed = parse_python("oversized.py", "#" + "x" * python_parser.MAX_PARSE_BYTES)
    assert parsed.status == "resource_limited"
    assert parsed.diagnostics[0]["code"] == "MAX_PARSE_BYTES"
    assert len(parsed.symbols) == len(parsed.scopes) == 1


@pytest.mark.parametrize("cap", ["MAX_FILE_NODES", "MAX_FILE_SYMBOLS", "MAX_FILE_REFERENCES"])
def test_current_real_fact_budgets_still_bound_extraction(cap):
    if cap == "MAX_FILE_NODES":
        source = "entry = 0\n" * (python_parser.MAX_FILE_NODES // 4 + 1)
    elif cap == "MAX_FILE_SYMBOLS":
        source = "".join(
            f"def defined_{index}(): pass\n" for index in range(python_parser.MAX_FILE_SYMBOLS)
        )
    else:
        source = "target()\n" * (python_parser.MAX_FILE_REFERENCES + 1)
    parsed = parse_python("bounded_large.py", source)
    assert parsed.status == ("resource_limited" if cap == "MAX_FILE_NODES" else "partial")
    assert parsed.diagnostics[0]["code"] == cap
    if cap == "MAX_FILE_SYMBOLS":
        assert len(parsed.symbols) == python_parser.MAX_FILE_SYMBOLS
    elif cap == "MAX_FILE_REFERENCES":
        assert len(parsed.references) == python_parser.MAX_FILE_REFERENCES
    else:
        assert len(parsed.symbols) == 1 and not parsed.bindings


@pytest.mark.parametrize(
    "cap,limit,source,expected_status",
    [
        ("MAX_FILE_NODES", 5, "a = b\nc = d\n", "resource_limited"),
        ("MAX_FILE_SYMBOLS", 2, "def one(): pass\ndef two(): pass\n", "partial"),
        ("MAX_FILE_REFERENCES", 2, "first()\nsecond()\nthird()\n", "partial"),
    ],
)
def test_caps_keep_bounded_partial_facts(monkeypatch, cap, limit, source, expected_status):
    monkeypatch.setattr(python_parser, cap, limit)
    parsed = parse_python("bounded.py", source)
    assert parsed.status == expected_status
    assert parsed.diagnostics[0]["code"] == cap
    if cap == "MAX_FILE_SYMBOLS":
        assert len(parsed.symbols) == limit
    if cap == "MAX_FILE_REFERENCES":
        assert len(parsed.references) == limit
    assert "def one" not in json.dumps(parsed.diagnostics)


def test_forward_annotation_nodes_share_the_file_budget(monkeypatch):
    source = 'def run(value: "First | Second") -> "Third | Fourth": pass\n'
    ordinary_nodes = sum(1 for _ in ast.walk(ast.parse(source)))
    monkeypatch.setattr(python_parser, "MAX_FILE_NODES", ordinary_nodes + 3)
    parsed = parse_python("types.py", source)
    assert parsed.status in {"partial", "resource_limited"}
    assert parsed.diagnostics[0]["code"] == "MAX_FILE_NODES"


@pytest.mark.parametrize("failure", [RecursionError, MemoryError, OverflowError, RuntimeError])
def test_parser_failures_are_isolated_and_source_free(monkeypatch, failure):
    def broken(*args, **kwargs):
        raise failure("__PRIVATE_SOURCE_TEXT__")

    monkeypatch.setattr(python_parser.ast, "parse", broken)
    parsed = parse_python("broken.py", "x = 1")
    assert parsed.status in {"resource_limited", "parse_error"}
    assert "__PRIVATE_SOURCE_TEXT__" not in json.dumps(parsed.to_dict())


def test_deep_syntax_is_bounded_without_uncaught_parser_failures():
    parsed = parse_python("deep.py", "value = " + "[" * 500 + "0" + "]" * 500)
    assert parsed.status in {"parse_error", "resource_limited", "partial"}
    assert parsed.diagnostics


def test_never_reads_source_path_imports_or_executes_project_code(tmp_path, monkeypatch):
    marker = tmp_path / "executed.txt"
    nonexistent = tmp_path / "does-not-exist.py"
    source = (
        "import __colink_unavailable_project__\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('execution')\n"
        "exec('raise RuntimeError()')\n"
        "eval('1 + 2')\n"
        "@dangerous_decorator()\n"
        "def run(value: dangerous_annotation() = dangerous_default()):\n"
        "    raise RuntimeError()\n"
    )
    original_import, original_compile = builtins.__import__, builtins.compile

    def guarded_import(name, *args, **kwargs):
        assert name != "__colink_unavailable_project__"
        return original_import(name, *args, **kwargs)

    def guarded_compile(*args, **kwargs):
        flags = kwargs.get("flags", args[3] if len(args) > 3 else 0)
        assert flags & ast.PyCF_ONLY_AST
        return original_compile(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("project code execution or filesystem access")

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "__import__", guarded_import)
        guarded.setattr(builtins, "compile", guarded_compile)
        guarded.setattr(builtins, "open", forbidden)
        guarded.setattr(builtins, "eval", forbidden)
        guarded.setattr(builtins, "exec", forbidden)
        guarded.setattr(Path, "read_text", forbidden)
        guarded.setattr(Path, "read_bytes", forbidden)
        parsed = parse_python(str(nonexistent), source)
    assert parsed.status == "ready"
    assert not marker.exists() and not nonexistent.exists()
    assert references(parsed, "CALL", "dangerous_default")
    assert references(parsed, "DECORATOR", "dangerous_decorator")


def test_returned_facts_do_not_retain_ast(monkeypatch):
    original = ast.parse
    trees = []

    def capture(*args, **kwargs):
        tree = original(*args, **kwargs)
        trees.append(weakref.ref(tree))
        return tree

    monkeypatch.setattr(python_parser.ast, "parse", capture)
    parsed = parse("def run(item: 'Item'): return item.work()")
    gc.collect()
    assert trees and all(tree() is None for tree in trees)
    assert parsed.status == "ready"
    assert all(ref.kind in RELATION_KINDS for ref in parsed.references)
    assert all(symbol.kind == symbol.kind.lower() for symbol in parsed.symbols)
