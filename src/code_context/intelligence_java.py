"""Bounded Java facts for CoLink, extracted only from supplied source text.

Tree-sitter recovers syntax, not Java types. The resolver receives lexical names
and locations; neither a compiler nor project code or dependencies are loaded.
"""

from dataclasses import dataclass, replace
from pathlib import PurePosixPath

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from code_context.intelligence_models import (
    MAX_FILE_NODES,
    MAX_FILE_REFERENCES,
    MAX_FILE_SYMBOLS,
    MAX_PARSE_BYTES,
    Binding,
    ImportBinding,
    ParsedFile,
    Reference,
    Scope,
    Symbol,
)

_CLASS_TYPES = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
_METHOD_TYPES = {
    "method_declaration": "method",
    "constructor_declaration": "constructor",
    "compact_constructor_declaration": "constructor",
    "annotation_type_element_declaration": "method",
}
_TYPE_ATOMS = {
    "type_identifier",
    "scoped_type_identifier",
    "integral_type",
    "floating_point_type",
    "boolean_type",
    "void_type",
}
_TYPE_TYPES = _TYPE_ATOMS | {"generic_type", "array_type", "annotated_type"}
_ANNOTATIONS = {"annotation", "marker_annotation"}
_COMMENTS = {"line_comment", "block_comment"}
_LITERALS = {
    "string_literal",
    "character_literal",
    "decimal_integer_literal",
    "hex_integer_literal",
    "octal_integer_literal",
    "binary_integer_literal",
    "decimal_floating_point_literal",
    "hex_floating_point_literal",
    "true",
    "false",
    "null_literal",
}
_MAX_FACT_TEXT = 2_048
_MAX_DIAGNOSTICS = 32


class _ResourceLimit(Exception):
    def __init__(self, resource: str) -> None:
        self.resource = resource


def _children(node: Node, named: bool = True) -> list[Node]:
    # Direct enumeration avoids caching child wrappers on each native node.
    count = node.named_child_count if named else node.child_count
    child = node.named_child if named else node.child
    return [child(index) for index in range(count)]


def _same_node(first: Node | None, second: Node | None) -> bool:
    # Positions identify children without retaining or comparing native wrappers.
    if first is None or second is None:
        return first is second
    return first.start_byte == second.start_byte and first.end_byte == second.end_byte


@dataclass(frozen=True)
class _Context:
    scope: str = ""
    owner: str = ""
    class_name: str = ""
    conditional: bool = False
    record_parameters: tuple[tuple[str, str, int], ...] = ()


class _JavaFacts:
    def __init__(self, result: ParsedFile, source: bytes) -> None:
        self.result = result
        self.source = source
        self.reference_keys: set[tuple] = set()
        self.callables: dict[tuple[str, str], list[Symbol]] = {}

    def diagnostic(self, code: str, message: str, node: Node | None = None, **extra) -> None:
        if len(self.result.diagnostics) >= _MAX_DIAGNOSTICS:
            return
        item = {"code": code, "message": message, **extra}
        if node is not None:
            item.update(line=node.start_point[0] + 1, column=self.column(node))
        self.result.diagnostics.append(item)

    def partial(self, code: str, message: str, node: Node) -> None:
        self.result.status = "partial"
        self.diagnostic(code, message, node)

    def text(self, node: Node | None) -> str:
        if node is None:
            return ""
        if node.end_byte - node.start_byte > _MAX_FACT_TEXT * 4:
            raise _ResourceLimit("fact_text")
        value = self.source[node.start_byte : node.end_byte].decode("utf-8")
        return self.bounded(value)

    @staticmethod
    def bounded(value: str) -> str:
        if len(value) > _MAX_FACT_TEXT:
            raise _ResourceLimit("fact_text")
        return value

    def column(self, node: Node) -> int:
        # Point tuple access avoids the 0.26 wheel's borrowed-reference row/column
        # properties, which corrupt non-small integers. Columns count characters.
        start = node.start_byte - node.start_point[1]
        return len(self.source[start : node.start_byte].decode("utf-8")) + 1

    def type_text(self, node: Node | None) -> str:
        """Keep type syntax, omitting annotation arguments, comments and literals."""
        if node is None:
            return ""
        stack = [node]
        parts: list[str] = []
        length = 0
        while stack:
            current = stack.pop()
            if (
                current.type in _ANNOTATIONS | _COMMENTS | _LITERALS
                or current.type == "ERROR"
                or current.is_missing
            ):
                continue
            if current.child_count:
                stack.extend(reversed(_children(current, named=False)))
                continue
            token = self.text(current)
            if token in {"extends", "super"}:
                token = f" {token} "
            parts.append(token)
            length += len(token)
            if length > _MAX_FACT_TEXT:
                raise _ResourceLimit("fact_text")
        return "".join(parts).strip()

    def type_name(self, node: Node | None) -> str:
        while node is not None:
            if node.type == "generic_type":
                node = next((child for child in _children(node) if child.type in _TYPE_ATOMS), None)
            elif node.type == "array_type":
                node = node.child_by_field_name("element")
            elif node.type == "annotated_type":
                node = next((child for child in _children(node) if child.type in _TYPE_TYPES), None)
            else:
                return self.type_text(node)
        return ""

    def expression_name(self, node: Node | None) -> str:
        """Describe receivers without retaining arguments or literal contents."""
        stack: list[Node | str | None] = [node]
        parts: list[str] = []
        length = 0
        while stack:
            current = stack.pop()
            if isinstance(current, str):
                value = current
            elif current is None:
                value = "<expression>"
            elif current.type in {"identifier", "scoped_identifier", "this", "super"}:
                value = self.type_text(current)
            elif current.type in _TYPE_TYPES:
                value = self.type_text(current)
            elif current.type == "field_access":
                stack.extend(
                    [
                        current.child_by_field_name("field"),
                        ".",
                        current.child_by_field_name("object"),
                    ]
                )
                continue
            elif current.type == "method_invocation":
                stack.extend(["()", current.child_by_field_name("name")])
                receiver = current.child_by_field_name("object")
                if receiver is not None:
                    stack.extend([".", receiver])
                continue
            elif current.type == "object_creation_expression":
                value = self.type_name(current.child_by_field_name("type")) + "()"
            elif current.type == "array_access":
                stack.extend(["[]", current.child_by_field_name("array")])
                continue
            elif current.type == "parenthesized_expression" and current.named_child_count == 1:
                stack.extend([")", current.named_child(0), "("])
                continue
            else:
                value = "<expression>"
            parts.append(value)
            length += len(value)
            if length > _MAX_FACT_TEXT:
                raise _ResourceLimit("fact_text")
        return "".join(parts)

    def reference(
        self,
        name: str,
        kind: str,
        node: Node,
        ctx: _Context,
        *,
        arity: int | None = None,
    ) -> None:
        if not name:
            return
        self.bounded(name)
        line, column = node.start_point[0] + 1, self.column(node)
        key = (kind, name, line, column, ctx.scope, ctx.owner)
        if key in self.reference_keys:
            return
        if len(self.result.references) >= MAX_FILE_REFERENCES:
            raise _ResourceLimit("references")
        self.reference_keys.add(key)
        self.result.references.append(
            Reference(
                name=name,
                kind=kind,
                line=line,
                column=column,
                scope=ctx.scope,
                source_qualname=ctx.owner,
                conditional=ctx.conditional,
                # Java type syntax may participate in runtime linkage. This
                # extractor does not prove a compile-only conditional context.
                type_only=False,
                arity=arity,
            )
        )

    def binding(
        self, name: str, kind: str, node: Node, ctx: _Context, annotation: str = "", value: str = ""
    ) -> None:
        if not name:
            return
        if len(self.result.bindings) >= MAX_FILE_REFERENCES:
            raise _ResourceLimit("bindings")
        self.result.bindings.append(
            Binding(name, ctx.scope, kind, node.start_point[0] + 1, annotation, value)
        )

    def symbol(
        self, node: Node, name: str, qualname: str, kind: str, ctx: _Context, **extra
    ) -> Symbol:
        if len(self.result.symbols) >= MAX_FILE_SYMBOLS:
            raise _ResourceLimit("symbols")
        symbol = Symbol(
            path=self.result.path,
            name=self.bounded(name),
            qualname=self.bounded(qualname),
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            scope=ctx.scope,
            column=self.column(node),
            **extra,
        )
        self.result.symbols.append(symbol)
        return symbol

    def block(self, node: Node, ctx: _Context, kind: str = "block") -> _Context:
        scope = self.bounded(f"{ctx.scope}.<{kind}@{node.start_point[0] + 1}:{self.column(node)}>")
        self.result.scopes.append(Scope(scope, kind, ctx.scope))
        return replace(ctx, scope=scope)

    def safe_value(self, node: Node | None) -> str:
        if node is None or node.has_error:
            return ""
        if node.type == "object_creation_expression":
            if any(child.type == "class_body" for child in _children(node)):
                return ""
            return self.type_name(node.child_by_field_name("type"))
        if node.type == "identifier":
            return self.text(node)
        if node.type == "field_access":
            value = self.expression_name(node)
            if "<" not in value and "(" not in value and "[" not in value:
                return value
        return ""

    @staticmethod
    def arity(node: Node | None) -> int | None:
        if node is None or node.has_error:
            return None
        return sum(child.type not in _COMMENTS for child in _children(node))

    def parameter(self, node: Node) -> tuple[str, str, Node | None]:
        name = node.child_by_field_name("name")
        type_node = node.child_by_field_name("type")
        if node.type == "spread_parameter":
            type_node = next((c for c in _children(node) if c.type in _TYPE_TYPES), None)
            declarator = next((c for c in _children(node) if c.type == "variable_declarator"), None)
            name = declarator.child_by_field_name("name") if declarator is not None else None
        elif node.type == "catch_formal_parameter":
            type_node = next((c for c in _children(node) if c.type == "catch_type"), None)
        annotation = self.type_text(type_node) + self.type_text(
            node.child_by_field_name("dimensions")
        )
        if node.type == "spread_parameter":
            annotation += "..."
        return self.text(name), annotation, name

    def class_declaration(self, node: Node, ctx: _Context) -> list[tuple[Node, _Context]]:
        name_node = node.child_by_field_name("name")
        name = self.text(name_node)
        if not name:
            self.partial(
                "incomplete_declaration", "Java type declaration has no usable name.", node
            )
            return []
        parent = ctx.class_name if ctx.scope == ctx.class_name else ctx.scope
        qualname = self.bounded(f"{parent}.{name}" if parent else name)
        kind = _CLASS_TYPES[node.type]
        self.symbol(node, name, qualname, kind, ctx)
        self.binding(name, "symbol", name_node, ctx, value=qualname)
        self.result.scopes.append(Scope(qualname, kind, ctx.scope))
        inner = replace(
            ctx, scope=qualname, owner=qualname, class_name=qualname, record_parameters=()
        )
        parameters = node.child_by_field_name("parameters")
        if kind == "record" and parameters is not None:
            record_parameters = []
            for param in _children(parameters):
                if param.type not in {"formal_parameter", "spread_parameter"}:
                    continue
                pname, annotation, pnode = self.parameter(param)
                if pnode is None:
                    continue
                self.symbol(param, pname, f"{qualname}.{pname}", "field", inner)
                self.binding(pname, "field", pnode, inner, annotation)
                record_parameters.append((pname, annotation, pnode.start_point[0] + 1))
            inner = replace(inner, record_parameters=tuple(record_parameters))
        tasks = []
        for child in _children(node):
            if _same_node(child, name_node):
                continue
            if _same_node(child, parameters) and kind == "record":
                for param in _children(child):
                    _, _, pnode = self.parameter(param)
                    tasks.extend((c, inner) for c in _children(param) if not _same_node(c, pnode))
            else:
                tasks.append((child, inner))
        return tasks

    def method_declaration(self, node: Node, ctx: _Context) -> list[tuple[Node, _Context]]:
        name_node = node.child_by_field_name("name")
        name = self.text(name_node)
        if not name:
            self.partial("incomplete_declaration", "Java callable has no usable name.", node)
            return []
        params_node = node.child_by_field_name("parameters")
        parameters = []
        if params_node is not None:
            parameters = [
                self.parameter(p)[1]
                for p in _children(params_node)
                if p.type in {"formal_parameter", "spread_parameter"}
            ]
        elif node.type == "compact_constructor_declaration":
            parameters = [annotation for _, annotation, _ in ctx.record_parameters]
        signature = self.bounded(f"{name}({','.join(parameters)})")
        qualname = self.bounded(f"{ctx.class_name}.{signature}" if ctx.class_name else signature)
        kind = _METHOD_TYPES[node.type]
        return_type = self.type_text(node.child_by_field_name("type"))
        return_type += self.type_text(node.child_by_field_name("dimensions"))
        symbol = self.symbol(
            node,
            name,
            qualname,
            kind,
            ctx,
            signature=signature,
            parameters=parameters,
            return_type=return_type,
        )
        self.binding(name, "symbol", name_node, ctx, value=qualname)
        self.callables.setdefault((ctx.scope, name), []).append(symbol)
        self.result.scopes.append(Scope(qualname, kind, ctx.scope))
        inner = replace(ctx, scope=qualname, owner=qualname)
        if node.type == "compact_constructor_declaration":
            for pname, annotation, line in ctx.record_parameters:
                if len(self.result.bindings) >= MAX_FILE_REFERENCES:
                    raise _ResourceLimit("bindings")
                self.result.bindings.append(Binding(pname, qualname, "parameter", line, annotation))
        return [(c, inner) for c in _children(node) if not _same_node(c, name_node)]

    def variables(self, node: Node, ctx: _Context) -> list[tuple[Node, _Context]]:
        type_node = node.child_by_field_name("type")
        annotation = self.type_text(type_node)
        if annotation == "var":
            annotation = ""
        is_field = node.type in {"field_declaration", "constant_declaration"}
        tasks = []
        for child in _children(node):
            if child.type != "variable_declarator":
                tasks.append((child, ctx))
                continue
            name_node = child.child_by_field_name("name")
            name = self.text(name_node)
            value_node = child.child_by_field_name("value")
            declared_type = annotation + self.type_text(child.child_by_field_name("dimensions"))
            owner = ctx
            if is_field and name:
                qualname = self.bounded(f"{ctx.class_name}.{name}")
                self.symbol(child, name, qualname, "field", ctx)
            self.binding(
                name,
                "field" if is_field else "variable",
                name_node,
                ctx,
                declared_type,
                self.safe_value(value_node),
            )
            tasks.extend((c, owner) for c in _children(child) if not _same_node(c, name_node))
        return tasks

    def import_declaration(self, node: Node, ctx: _Context) -> None:
        target_node = next(
            (c for c in _children(node) if c.type in {"identifier", "scoped_identifier"}), None
        )
        target = self.type_text(target_node)
        wildcard = any(c.type == "asterisk" for c in _children(node))
        if not target or node.has_error:
            self.partial("incomplete_import", "Java import could not be extracted reliably.", node)
            return
        if wildcard:
            module, imported = target, "*"
            target += ".*"
        else:
            module, _, imported = target.rpartition(".")
        self.result.imports.append(
            ImportBinding(
                module=module,
                imported_name=imported,
                local_name=imported,
                line=node.start_point[0] + 1,
                scope=ctx.scope,
                is_static=any(c.type == "static" for c in _children(node, named=False)),
            )
        )
        self.reference(target, "IMPORT", target_node, ctx)

    def inheritance(self, node: Node, ctx: _Context) -> list[tuple[Node, _Context]]:
        kind = "IMPLEMENTS" if node.type == "super_interfaces" else "INHERITANCE"
        tasks = []
        for child in _children(node):
            types = _children(child) if child.type == "type_list" else [child]
            for type_node in types:
                self.reference(self.type_name(type_node), kind, type_node, ctx)
                tasks.append((type_node, ctx))
        return tasks

    def visit(self, node: Node, ctx: _Context) -> list[tuple[Node, _Context]]:
        kind = node.type
        if kind in _LITERALS | _COMMENTS:
            return []
        if node.is_missing or kind == "ERROR":
            self.partial("syntax_recovery", "Java syntax required parser recovery.", node)
            return [(child, ctx) for child in _children(node)]
        if kind in _CLASS_TYPES:
            return self.class_declaration(node, ctx)
        if kind in _METHOD_TYPES:
            return self.method_declaration(node, ctx)
        if kind == "package_declaration":
            target = next(
                (c for c in _children(node) if c.type in {"identifier", "scoped_identifier"}), None
            )
            if target is not None and not node.has_error:
                self.result.module = self.type_text(target)
            return [(c, ctx) for c in _children(node) if c.type in _ANNOTATIONS]
        if kind == "import_declaration":
            self.import_declaration(node, ctx)
            return []
        if kind in {"superclass", "super_interfaces", "extends_interfaces"}:
            return self.inheritance(node, ctx)
        if kind in {"field_declaration", "constant_declaration", "local_variable_declaration"}:
            return self.variables(node, ctx)
        if kind in {"formal_parameter", "spread_parameter", "catch_formal_parameter"}:
            name, annotation, name_node = self.parameter(node)
            self.binding(name, "parameter", name_node, ctx, annotation)
            tasks = []
            for child in _children(node):
                if _same_node(child, name_node):
                    continue
                if child.type == "variable_declarator":
                    tasks.extend((c, ctx) for c in _children(child) if not _same_node(c, name_node))
                else:
                    tasks.append((child, ctx))
            return tasks
        if kind == "type_parameter":
            name_node = next((c for c in _children(node) if c.type == "type_identifier"), None)
            self.binding(self.text(name_node), "variable", name_node, ctx)
            return [(c, ctx) for c in _children(node) if not _same_node(c, name_node)]
        if kind in _TYPE_ATOMS:
            name = self.type_text(node)
            if name != "var":
                self.reference(name, "TYPE_REFERENCE", node, ctx)
            # A qualified type is one fact, not separate references to package segments.
            return []
        if kind in _ANNOTATIONS:
            name_node = node.child_by_field_name("name")
            self.reference(self.type_text(name_node), "DECORATOR", name_node or node, ctx)
            return [(c, ctx) for c in _children(node) if not _same_node(c, name_node)]
        if kind == "method_invocation":
            name_node = node.child_by_field_name("name")
            name = self.text(name_node)
            receiver = node.child_by_field_name("object")
            if receiver is not None:
                name = self.expression_name(receiver) + "." + name
            self.reference(
                name,
                "CALL",
                name_node or node,
                ctx,
                arity=self.arity(node.child_by_field_name("arguments")),
            )
            return [(c, ctx) for c in _children(node) if not _same_node(c, name_node)]
        if kind in {"object_creation_expression", "array_creation_expression"}:
            type_node = node.child_by_field_name("type")
            self.reference(
                self.type_name(type_node),
                "INSTANTIATION",
                type_node or node,
                ctx,
                arity=self.arity(node.child_by_field_name("arguments")),
            )
            tasks = []
            for child in _children(node):
                inner = ctx
                if child.type == "class_body":
                    self.partial(
                        "anonymous_class",
                        "Anonymous class targets require conservative resolution.",
                        child,
                    )
                    inner = self.block(child, ctx, "anonymous")
                    inner = replace(inner, class_name=inner.scope, record_parameters=())
                tasks.append((child, inner))
            return tasks
        if kind == "explicit_constructor_invocation":
            constructor = node.child_by_field_name("constructor")
            self.reference(
                self.text(constructor),
                "CALL",
                constructor or node,
                ctx,
                arity=self.arity(node.child_by_field_name("arguments")),
            )
            return [(c, ctx) for c in _children(node) if not _same_node(c, constructor)]
        if kind == "field_access":
            field = node.child_by_field_name("field")
            self.reference(self.expression_name(node), "REFERENCE", field or node, ctx)
            return [(c, ctx) for c in _children(node) if not _same_node(c, field)]
        if kind == "method_reference":
            children = [c for c in _children(node) if c.type not in _COMMENTS]
            constructor = next((c for c in _children(node, named=False) if c.type == "new"), None)
            if children and (len(children) >= 2 or constructor is not None):
                receiver = children[0]
                method = constructor if constructor is not None else children[-1]
                self.reference(
                    self.expression_name(receiver) + "::" + self.type_text(method),
                    "REFERENCE",
                    method,
                    ctx,
                )
                return [(c, ctx) for c in children if not _same_node(c, method)]
        if kind in {"block", "constructor_body", "for_statement", "catch_clause", "switch_block"}:
            inner = self.block(node, ctx)
            return [(c, inner) for c in _children(node)]
        if kind == "enhanced_for_statement":
            inner = self.block(node, ctx, "for")
            name_node = node.child_by_field_name("name")
            self.binding(
                self.text(name_node),
                "variable",
                name_node,
                inner,
                self.type_text(node.child_by_field_name("type"))
                + self.type_text(node.child_by_field_name("dimensions")),
            )
            value = node.child_by_field_name("value")
            return [
                (c, ctx if _same_node(c, value) else inner)
                for c in _children(node)
                if not _same_node(c, name_node)
            ]
        if kind == "try_with_resources_statement":
            inner = self.block(node, ctx, "resources")
            return [
                (c, ctx if c.type in {"catch_clause", "finally_clause"} else inner)
                for c in _children(node)
            ]
        if kind == "resource":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                self.binding(
                    self.text(name_node),
                    "variable",
                    name_node,
                    ctx,
                    self.type_text(node.child_by_field_name("type")),
                    self.safe_value(node.child_by_field_name("value")),
                )
            return [(c, ctx) for c in _children(node) if not _same_node(c, name_node)]
        if kind == "lambda_expression":
            inner = self.block(node, ctx, "lambda")
            params = node.child_by_field_name("parameters")
            tasks = []
            for child in _children(node):
                if _same_node(child, params) and child.type in {
                    "identifier",
                    "inferred_parameters",
                }:
                    names = [child] if child.type == "identifier" else _children(child)
                    for name in names:
                        if name.type == "identifier":
                            self.binding(self.text(name), "parameter", name, inner)
                else:
                    tasks.append((child, inner))
            return tasks
        if kind == "enum_constant":
            name_node = node.child_by_field_name("name")
            name = self.text(name_node)
            qualname = self.bounded(f"{ctx.class_name}.{name}")
            self.symbol(node, name, qualname, "field", ctx)
            self.binding(name, "field", name_node, ctx, ctx.class_name)
            inner = ctx
            args = node.child_by_field_name("arguments")
            if args is not None:
                self.reference(
                    ctx.class_name, "INSTANTIATION", name_node, inner, arity=self.arity(args)
                )
            tasks = []
            for child in _children(node):
                if _same_node(child, name_node):
                    continue
                owner = inner
                if child.type == "class_body":
                    self.partial(
                        "anonymous_class", "Enum constant has an anonymous class body.", child
                    )
                    owner = self.block(child, inner, "anonymous")
                    owner = replace(owner, class_name=owner.scope, record_parameters=())
                tasks.append((child, owner))
            return tasks
        if kind in {"type_pattern", "record_pattern", "record_pattern_component"}:
            self.partial(
                "pattern_binding", "Pattern variable visibility requires flow analysis.", node
            )
            return [(c, ctx) for c in _children(node) if c.type != "identifier"]
        if kind == "instanceof_expression" and node.child_by_field_name("name") is not None:
            self.partial(
                "pattern_binding", "Pattern variable visibility requires flow analysis.", node
            )
            name_node = node.child_by_field_name("name")
            return [(c, ctx) for c in _children(node) if not _same_node(c, name_node)]
        if kind == "module_declaration":
            self.partial(
                "java_module", "Java module directives are retained as lexical evidence.", node
            )
        if kind in {"variable_declarator", "element_value_pair", "labeled_statement"}:
            excluded = node.child_by_field_name("name") or node.child_by_field_name("key")
            return [(c, ctx) for c in _children(node) if not _same_node(c, excluded)]
        if kind in {"break_statement", "continue_statement", "receiver_parameter"}:
            return [(c, ctx) for c in _children(node) if c.type not in {"identifier", "this"}]
        if kind == "identifier":
            self.reference(self.text(node), "REFERENCE", node, ctx)
            return []
        child_ctx = (
            replace(ctx, conditional=True)
            if kind in {"if_statement", "ternary_expression"}
            else ctx
        )
        return [(child, child_ctx) for child in _children(node)]

    def extract(self, root: Node) -> None:
        stack = [(root, _Context())]
        while stack:
            node, ctx = stack.pop()
            stack.extend(reversed(self.visit(node, ctx)))
        for symbols in self.callables.values():
            if len(symbols) > 1:
                self.diagnostic(
                    "overload_ambiguity",
                    "Overloaded Java declarations require argument and type evidence to resolve.",
                    line=min(s.start_line for s in symbols),
                    count=len(symbols),
                )


def _check_nodes(root: Node) -> None:
    """An iterative cursor bounds all nodes, including punctuation and error nodes."""
    cursor = root.walk()
    count = 0
    while True:
        count += 1
        if count > MAX_FILE_NODES:
            raise _ResourceLimit("nodes")
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return


def parse_java(path: str, content: str) -> ParsedFile:
    """Parse supplied Java text into bounded facts; never access the source path."""
    result = ParsedFile(path=path, language="java")
    result.symbols.append(
        Symbol(
            path,
            PurePosixPath(path.replace("\\", "/")).stem,
            "",
            "module",
            0,
            content.count("\n") + int(bool(content) and not content.endswith("\n")),
        )
    )
    result.scopes.append(Scope("", "module"))
    try:
        # Avoid first allocating up to four times the cap for oversized Unicode text.
        if len(content) > MAX_PARSE_BYTES:
            raise _ResourceLimit("bytes")
        source = content.encode("utf-8")
        if len(source) > MAX_PARSE_BYTES:
            raise _ResourceLimit("bytes")
        parser = Parser(Language(tree_sitter_java.language()))
        tree = parser.parse(source)
        root = tree.root_node
        _check_nodes(root)
        facts = _JavaFacts(result, source)
        if root.has_error:
            facts.partial("syntax_error", "Java source contains missing or invalid syntax.", root)
        facts.extract(root)
        if root.has_error and len(result.symbols) == 1 and not result.imports:
            result.status = "parse_error"
    except _ResourceLimit as exc:
        result.status = "resource_limited"
        result.diagnostics.append(
            {
                "code": "resource_limit",
                "message": "Java extraction reached a static resource limit.",
                "resource": exc.resource,
            }
        )
    except MemoryError:
        result.status = "resource_limited"
        result.diagnostics.append(
            {"code": "resource_limit", "message": "Java parser memory limit."}
        )
    except Exception:
        # Exception text and parser/node representations may contain indexed source.
        result.status = "parse_error"
        result.diagnostics.append({"code": "parser_error", "message": "Java static parser failed."})
    return result
