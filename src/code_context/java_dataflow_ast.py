"""Request-local Java syntax lowered to the existing, non-executing flow IR.

Python AST classes are an internal representation only. Neither Java nor the
lowered nodes are compiled, imported or evaluated. Unsupported behavior stays
explicitly unknown, and original Java byte positions and relation columns stay
separate because the saved Java facts use character columns.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from code_context.intelligence_java import _JavaFacts, _ResourceLimit
from code_context.intelligence_models import MAX_FILE_NODES, MAX_PARSE_BYTES, ParsedFile

_CLASSES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
}
_METHODS = {
    "method_declaration",
    "constructor_declaration",
    "compact_constructor_declaration",
    "annotation_type_element_declaration",
}
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
}
_BINARY = {
    "+": ast.Add,
    "-": ast.Sub,
    "*": ast.Mult,
    "/": ast.Div,
    "%": ast.Mod,
    "&": ast.BitAnd,
    "|": ast.BitOr,
    "^": ast.BitXor,
    "<<": ast.LShift,
    ">>": ast.RShift,
    ">>>": ast.RShift,
}
_COMPARE = {
    "==": ast.Eq,
    "!=": ast.NotEq,
    "<": ast.Lt,
    "<=": ast.LtE,
    ">": ast.Gt,
    ">=": ast.GtE,
}


class JavaUnknownExpr(ast.expr):
    """A value with explicit unknown Java semantics and bounded child evidence."""

    _fields = ("values",)
    _java_unknown = True

    def __init__(self, values):
        self.values = values


def _children(node):
    return [node.named_child(index) for index in range(node.named_child_count)]


def _loc(value, node):
    value.lineno, value.col_offset = node.start_point[0] + 1, node.start_point[1]
    value.end_lineno, value.end_col_offset = node.end_point[0] + 1, node.end_point[1]
    return value


@dataclass
class _Method:
    node: Node
    qualname: str
    owner: str
    parameters: list[Node]
    fields: frozenset[str]


class JavaFlowSource:
    """One parsed saved document; exact definitions are lowered only on demand.

    diagnostics contains file diagnostics plus any failure of the most recent
    function selection. A successful function owns its _java_diagnostics list.
    """

    def __init__(self, text, check):
        self.check = check
        self.diagnostics = []
        self._file_diagnostics = []
        self.methods = {}
        self.source = b""
        self.tree = None
        self.facts = None
        check()
        if not isinstance(text, str):
            raise ValueError("Java flow source requires text")
        if len(text) > MAX_PARSE_BYTES:
            self._file_diagnostics.append(
                ("JAVA_FLOW_RESOURCE_LIMIT", ast.Pass(lineno=1, col_offset=0))
            )
        else:
            try:
                self.source = text.encode("utf-8")
                if len(self.source) > MAX_PARSE_BYTES:
                    raise _ResourceLimit("bytes")
                self.tree = Parser(Language(tree_sitter_java.language())).parse(self.source)
                check()
                root = self.tree.root_node
                cursor, count = root.walk(), 0
                while True:
                    count += 1
                    if count > MAX_FILE_NODES:
                        raise _ResourceLimit("nodes")
                    if count % 128 == 0:
                        check()
                    if cursor.goto_first_child():
                        continue
                    while not cursor.goto_next_sibling():
                        if not cursor.goto_parent():
                            break
                    else:
                        continue
                    break
                self.facts = _JavaFacts(
                    ParsedFile(path="<flow>.java", language="java"), self.source
                )
                if root.has_error:
                    self._file_diagnostics.append(("JAVA_PARSE_ERROR", _loc(ast.Pass(), root)))
                self._index(root)
            except (_ResourceLimit, MemoryError, RecursionError):
                self.methods.clear()
                self._file_diagnostics.append(
                    ("JAVA_FLOW_RESOURCE_LIMIT", ast.Pass(lineno=1, col_offset=0))
                )
            except (UnicodeError, ValueError):
                self.methods.clear()
                self._file_diagnostics.append(
                    ("JAVA_PARSE_ERROR", ast.Pass(lineno=1, col_offset=0))
                )
        self.diagnostics = list(self._file_diagnostics)
        check()

    def _index(self, root):
        pending = [(root, "", [], frozenset())]
        while pending:
            self.check()
            node, owner, record_parameters, fields = pending.pop()
            if node.type in _CLASSES:
                name = self.facts.text(node.child_by_field_name("name"))
                owner = f"{owner}.{name}" if owner else name
                params = node.child_by_field_name("parameters")
                record_parameters = (
                    []
                    if params is None
                    else [
                        child
                        for child in _children(params)
                        if child.type in {"formal_parameter", "spread_parameter"}
                    ]
                )
                body = node.child_by_field_name("body")
                class_fields = {self.facts.parameter(param)[0] for param in record_parameters}
                if body is not None:
                    for declaration in _children(body):
                        if declaration.type in {"field_declaration", "constant_declaration"}:
                            class_fields.update(
                                self.facts.text(child.child_by_field_name("name"))
                                for child in _children(declaration)
                                if child.type == "variable_declarator"
                            )
                fields = frozenset(class_fields)
            if node.type in _METHODS:
                name = self.facts.text(node.child_by_field_name("name"))
                params = node.child_by_field_name("parameters")
                parameters = (
                    [
                        child
                        for child in _children(params)
                        if child.type in {"formal_parameter", "spread_parameter"}
                    ]
                    if params is not None
                    else record_parameters
                    if node.type == "compact_constructor_declaration"
                    else []
                )
                signature = (
                    f"{name}({','.join(self.facts.parameter(param)[1] for param in parameters)})"
                )
                qualname = f"{owner}.{signature}" if owner else signature
                key = (qualname, node.start_point[0] + 1)
                self.methods.setdefault(key, []).append(
                    _Method(node, qualname, owner, parameters, fields)
                )
                # Local/anonymous type scopes are not guessed from class names.
                continue
            pending.extend(
                (child, owner, record_parameters, fields) for child in reversed(_children(node))
            )

    def function(self, symbol):
        self.check()
        self.diagnostics = list(self._file_diagnostics)
        matches = self.methods.get((symbol.get("qualname"), symbol.get("start_line")), [])
        if len(matches) != 1:
            self.diagnostics.append(
                (
                    "JAVA_METHOD_SOURCE_MISMATCH",
                    ast.Pass(lineno=max(1, symbol.get("start_line", 1)), col_offset=0),
                )
            )
            return None
        method = matches[0]
        body = method.node.child_by_field_name("body")
        if body is None:
            self.diagnostics.append(("JAVA_BODY_UNAVAILABLE", _loc(ast.Pass(), method.node)))
            return None
        try:
            return _Lowerer(self, method).function(body)
        except (_ResourceLimit, MemoryError, RecursionError):
            self.diagnostics.append(("JAVA_FLOW_RESOURCE_LIMIT", _loc(ast.Pass(), method.node)))
            return None


class _Lowerer:
    def __init__(self, source, method):
        self.source, self.method = source, method
        self.facts, self.check = source.facts, source.check
        self.diagnostics = list(source._file_diagnostics)
        self.keys = set()
        self.work = 0
        self.locals = {self.facts.parameter(param)[0] for param in method.parameters}
        self.scoped_locals = set()
        body = method.node.child_by_field_name("body")
        pending = [(body, 0)]
        while pending:
            node, depth = pending.pop()
            if node is None:
                continue
            self.step()
            if node.type in _CLASSES | _METHODS | {"lambda_expression"}:
                continue
            if node.type == "variable_declarator":
                name = self.facts.text(node.child_by_field_name("name"))
                self.locals.add(name)
                if depth:
                    self.scoped_locals.add(name)
                    self.diagnostic("JAVA_SCOPE_PARTIAL", node)
            elif node.type == "enhanced_for_statement":
                name = self.facts.text(node.child_by_field_name("name"))
                self.locals.add(name)
                self.scoped_locals.add(name)
                self.diagnostic("JAVA_SCOPE_PARTIAL", node)
            inner_depth = depth + int(
                (node.type == "block" and node is not body)
                or node.type in {"for_statement", "enhanced_for_statement"}
            )
            pending.extend((child, inner_depth) for child in _children(node))

    def step(self):
        self.work += 1
        if self.work > MAX_FILE_NODES:
            raise _ResourceLimit("nodes")
        if self.work % 64 == 0:
            self.check()

    def diagnostic(self, code, node):
        key = code, node.start_byte, node.end_byte
        if key not in self.keys:
            self.keys.add(key)
            self.diagnostics.append((code, _loc(ast.Pass(), node)))

    def function(self, body):
        args, vararg = [], None
        for parameter in self.method.parameters:
            name, _, name_node = self.facts.parameter(parameter)
            if not name or name_node is None:
                self.diagnostic("JAVA_PARAMETER_PARTIAL", parameter)
                continue
            arg = _loc(ast.arg(arg=name, annotation=None), name_node)
            if parameter.type == "spread_parameter":
                vararg = arg
                self.diagnostic("JAVA_VARARGS_PARTIAL", parameter)
            else:
                args.append(arg)
        node = self.method.node
        modifiers = next((child for child in _children(node) if child.type == "modifiers"), None)
        static = modifiers is not None and any(
            child.type == "static" for child in modifiers.children
        )
        constructor = node.type in {"constructor_declaration", "compact_constructor_declaration"}
        if constructor:
            self.diagnostic("JAVA_CONSTRUCTOR_FLOW_PARTIAL", node)
        function = _loc(
            ast.FunctionDef(
                name=self.facts.text(node.child_by_field_name("name")),
                args=ast.arguments(
                    posonlyargs=[],
                    args=args,
                    vararg=vararg,
                    kwonlyargs=[],
                    kw_defaults=[],
                    kwarg=None,
                    defaults=[],
                ),
                body=self.statements(body),
                decorator_list=[],
                returns=None,
                type_comment=None,
            ),
            node,
        )
        function._java_static = static
        function._java_receiver = None if static else "this"
        function._java_constructor = constructor
        function._java_qualname = self.method.qualname
        function._java_owner = self.method.owner
        function._java_start_line = node.start_point[0] + 1
        function._java_fields = self.method.fields
        function._java_diagnostics = self.diagnostics
        return function

    def unknown(self, node, code, values=None):
        self.diagnostic(code, node)
        return _loc(JavaUnknownExpr([] if values is None else values), node)

    def expression(self, node, *, qualifier=False):
        self.step()
        if node is None:
            return ast.Constant(value=None, lineno=1, col_offset=0)
        kind = node.type
        if kind in {"identifier", "this", "super"}:
            name = self.facts.text(node)
            if name in self.scoped_locals:
                return self.unknown(node, "JAVA_SCOPE_PARTIAL")
            if kind == "identifier" and not qualifier and name not in self.locals:
                return self.unknown(node, "JAVA_FIELD_PARTIAL")
            return _loc(ast.Name(id=name, ctx=ast.Load()), node)
        if kind in {"true", "false", "null_literal"}:
            return _loc(
                ast.Constant(value={"true": True, "false": False, "null_literal": None}[kind]), node
            )
        if kind in _LITERALS:
            # Contents of strings/numbers may be secrets. Only the literal type
            # is needed for provenance; boolean branch facts were handled above.
            value = (
                ""
                if kind in {"string_literal", "character_literal"}
                else 0.0
                if "floating" in kind
                else 0
            )
            return _loc(ast.Constant(value=value), node)
        if kind == "parenthesized_expression":
            children = _children(node)
            return (
                self.expression(children[0])
                if children
                else self.unknown(node, "JAVA_EXPRESSION_PARTIAL")
            )
        if kind == "field_access":
            obj, field = node.child_by_field_name("object"), node.child_by_field_name("field")
            if obj is None or field is None:
                return self.unknown(node, "JAVA_FIELD_PARTIAL")
            if obj.type != "this":
                self.diagnostic("JAVA_HEAP_ALIAS_PARTIAL", node)
            return _loc(
                ast.Attribute(
                    value=self.expression(obj, qualifier=True),
                    attr=self.facts.text(field),
                    ctx=ast.Load(),
                ),
                node,
            )
        if kind == "array_access":
            self.diagnostic("JAVA_HEAP_ALIAS_PARTIAL", node)
            return _loc(
                ast.Subscript(
                    value=self.expression(node.child_by_field_name("array")),
                    slice=self.expression(node.child_by_field_name("index")),
                    ctx=ast.Load(),
                ),
                node,
            )
        if kind == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return self.unknown(node, "JAVA_CALL_PARTIAL")
            obj = node.child_by_field_name("object")
            name = self.facts.text(name_node)
            func = (
                ast.Name(id=name, ctx=ast.Load())
                if obj is None
                else ast.Attribute(
                    value=self.expression(obj, qualifier=True), attr=name, ctx=ast.Load()
                )
            )
            _loc(func, name_node)
            func._java_relation_line = name_node.start_point[0] + 1
            func._java_relation_column = self.facts.column(name_node)
            arguments = node.child_by_field_name("arguments")
            args = (
                []
                if arguments is None
                else [
                    self.expression(child)
                    for child in _children(arguments)
                    if child.type not in _COMMENTS
                ]
            )
            return _loc(ast.Call(func=func, args=args, keywords=[]), node)
        if kind == "object_creation_expression":
            target = node.child_by_field_name("type") or node
            func = _loc(ast.Name(id=self.facts.type_name(target), ctx=ast.Load()), target)
            func._java_relation_line = target.start_point[0] + 1
            func._java_relation_column = self.facts.column(target)
            arguments = node.child_by_field_name("arguments")
            args = (
                []
                if arguments is None
                else [self.expression(child) for child in _children(arguments)]
            )
            call = _loc(ast.Call(func=func, args=args, keywords=[]), node)
            call._java_unsupported_call = "JAVA_CONSTRUCTOR_FLOW_PARTIAL"
            self.diagnostic("JAVA_CONSTRUCTOR_FLOW_PARTIAL", node)
            return call
        if kind == "binary_expression":
            left, right = (
                self.expression(node.child_by_field_name("left")),
                self.expression(node.child_by_field_name("right")),
            )
            operator = self.facts.text(node.child_by_field_name("operator"))
            if operator in _BINARY:
                return _loc(ast.BinOp(left=left, op=_BINARY[operator](), right=right), node)
            if operator in _COMPARE:
                return _loc(
                    ast.Compare(left=left, ops=[_COMPARE[operator]()], comparators=[right]), node
                )
            if operator in {"&&", "||"}:
                return _loc(
                    ast.BoolOp(
                        op=ast.And() if operator == "&&" else ast.Or(), values=[left, right]
                    ),
                    node,
                )
            return self.unknown(node, "JAVA_EXPRESSION_PARTIAL", [left, right])
        if kind == "unary_expression":
            operator = self.facts.text(node.child_by_field_name("operator"))
            operand = self.expression(node.child_by_field_name("operand"))
            op = {"!": ast.Not, "~": ast.Invert, "-": ast.USub, "+": ast.UAdd}.get(operator)
            return (
                _loc(ast.UnaryOp(op=op(), operand=operand), node)
                if op
                else self.unknown(node, "JAVA_EXPRESSION_PARTIAL", [operand])
            )
        if kind == "ternary_expression":
            self.diagnostic("JAVA_BRANCH_PARTIAL", node)
            return _loc(
                ast.IfExp(
                    test=self.expression(node.child_by_field_name("condition")),
                    body=self.expression(node.child_by_field_name("consequence")),
                    orelse=self.expression(node.child_by_field_name("alternative")),
                ),
                node,
            )
        if kind == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            operator = self.facts.text(node.child_by_field_name("operator"))
            target, value = self.target(left), self.expression(right)
            if operator != "=":
                self.diagnostic("JAVA_UPDATE_PARTIAL", node)
                op = _BINARY.get(operator[:-1], ast.Add)
                value = _loc(ast.BinOp(left=self.expression(left), op=op(), right=value), node)
                value._java_unknown = True
            if isinstance(target, ast.Name):
                return _loc(ast.NamedExpr(target=target, value=value), node)
            return self.unknown(node, "JAVA_HEAP_ALIAS_PARTIAL", [self.expression(left), value])
        if kind in {"lambda_expression", "method_reference"}:
            return self.unknown(node, "JAVA_FUNCTIONAL_FLOW_PARTIAL")
        if kind == "update_expression":
            return self.unknown(
                node,
                "JAVA_UPDATE_PARTIAL",
                [self.expression(child) for child in _children(node)],
            )
        if kind == "class_literal":
            return self.unknown(node, "JAVA_EXPRESSION_PARTIAL")
        if kind == "cast_expression":
            return self.unknown(
                node, "JAVA_CAST_PARTIAL", [self.expression(node.child_by_field_name("value"))]
            )
        values = []
        for child in _children(node):
            if child.type not in _CLASSES | _METHODS | _COMMENTS and not child.type.endswith(
                "type"
            ):
                values.append(self.expression(child))
        return self.unknown(node, "JAVA_EXPRESSION_PARTIAL", values)

    def target(self, node):
        target = self.expression(node)
        if isinstance(target, (ast.Name, ast.Attribute, ast.Subscript)):
            target.ctx = ast.Store()
        return target

    def statements(self, node):
        self.step()
        if node is None:
            return []
        kind = node.type
        if kind in _COMMENTS | {";"}:
            return []
        if kind in {"block", "constructor_body"}:
            return [statement for child in _children(node) for statement in self.statements(child)]
        if kind == "local_variable_declaration":
            statements = []
            for declarator in _children(node):
                if declarator.type != "variable_declarator":
                    continue
                name = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name is None:
                    self.diagnostic("JAVA_STATEMENT_PARTIAL", declarator)
                    continue
                self.locals.add(self.facts.text(name))
                lowered = (
                    self.expression(value)
                    if value is not None
                    else self.unknown(declarator, "JAVA_UNINITIALIZED_LOCAL")
                )
                statements.append(
                    _loc(
                        ast.Assign(targets=[self.target(name)], value=lowered, type_comment=None),
                        declarator,
                    )
                )
            return statements
        if kind == "expression_statement":
            children = _children(node)
            return self.expression_statement(children[0]) if children else []
        if kind in {"assignment_expression", "update_expression"}:
            return self.expression_statement(node)
        if kind == "return_statement":
            children = [child for child in _children(node) if child.type not in _COMMENTS]
            return [
                _loc(ast.Return(value=self.expression(children[0]) if children else None), node)
            ]
        if kind == "if_statement":
            self.diagnostic("JAVA_BRANCH_PARTIAL", node)
            return [
                _loc(
                    ast.If(
                        test=self.expression(node.child_by_field_name("condition")),
                        body=self.statements(node.child_by_field_name("consequence")),
                        orelse=self.statements(node.child_by_field_name("alternative")),
                    ),
                    node,
                )
            ]
        if kind in {"while_statement", "do_statement", "for_statement"}:
            self.diagnostic("JAVA_LOOP_PARTIAL", node)
            condition = node.child_by_field_name("condition")
            body = self.statements(node.child_by_field_name("body"))
            prefix = []
            if kind == "for_statement":
                for child in node.children_by_field_name("init"):
                    prefix.extend(self.statements(child))
                for child in node.children_by_field_name("update"):
                    body.extend(self.expression_statement(child))
            test = (
                self.expression(condition)
                if condition is not None
                else _loc(ast.Constant(value=True), node)
            )
            loop = _loc(ast.While(test=test, body=body, orelse=[]), node)
            return [*body, loop] if kind == "do_statement" else [*prefix, loop]
        if kind == "enhanced_for_statement":
            self.diagnostic("JAVA_LOOP_PARTIAL", node)
            name = node.child_by_field_name("name")
            return [
                _loc(
                    ast.For(
                        target=self.target(name),
                        iter=self.expression(node.child_by_field_name("value")),
                        body=self.statements(node.child_by_field_name("body")),
                        orelse=[],
                        type_comment=None,
                    ),
                    node,
                )
            ]
        if kind in {"break_statement", "continue_statement"}:
            if _children(node):
                self.diagnostic("JAVA_CONTROL_FLOW_PARTIAL", node)
            return [_loc(ast.Break() if kind == "break_statement" else ast.Continue(), node)]
        if kind == "throw_statement":
            self.diagnostic("JAVA_EXCEPTION_FLOW_PARTIAL", node)
            children = _children(node)
            return [
                _loc(
                    ast.Raise(exc=self.expression(children[0]) if children else None, cause=None),
                    node,
                )
            ]
        if kind == "explicit_constructor_invocation":
            arguments = node.child_by_field_name("arguments")
            values = (
                []
                if arguments is None
                else [self.expression(child) for child in _children(arguments)]
            )
            return [
                _loc(
                    ast.Expr(value=self.unknown(node, "JAVA_CONSTRUCTOR_FLOW_PARTIAL", values)),
                    node,
                )
            ]
        if kind in _CLASSES | _METHODS:
            return [_loc(ast.Expr(value=self.unknown(node, "JAVA_LOCAL_SCOPE_PARTIAL")), node)]
        return [_loc(ast.Expr(value=self.unknown(node, "JAVA_STATEMENT_PARTIAL")), node)]

    def expression_statement(self, node):
        if node.type == "assignment_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            operator = self.facts.text(node.child_by_field_name("operator"))
            if operator == "=":
                return [
                    _loc(
                        ast.Assign(
                            targets=[self.target(left)],
                            value=self.expression(right),
                            type_comment=None,
                        ),
                        node,
                    )
                ]
            self.diagnostic("JAVA_UPDATE_PARTIAL", node)
            statement = _loc(
                ast.AugAssign(
                    target=self.target(left),
                    op=_BINARY.get(operator[:-1], ast.Add)(),
                    value=self.expression(right),
                ),
                node,
            )
            statement._java_unknown = True
            return [statement]
        if node.type == "update_expression":
            self.diagnostic("JAVA_UPDATE_PARTIAL", node)
            children = _children(node)
            if not children:
                return [_loc(ast.Expr(value=self.unknown(node, "JAVA_EXPRESSION_PARTIAL")), node)]
            operator = (
                ast.Sub() if any(child.type == "--" for child in node.children) else ast.Add()
            )
            statement = _loc(
                ast.AugAssign(
                    target=self.target(children[0]),
                    op=operator,
                    value=_loc(ast.Constant(value=1), node),
                ),
                node,
            )
            statement._java_unknown = True
            return [statement]
        return [_loc(ast.Expr(value=self.expression(node)), node)]


def parse_java_flow(text, check):
    """Parse bounded Java text into a request-local, source-positioned flow view."""
    return JavaFlowSource(text, check)
