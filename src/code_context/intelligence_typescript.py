"""Bounded JavaScript/JSX and TypeScript/TSX syntax facts.

Only supplied text is parsed. Imports, exports and direct calls remain static
evidence; no npm resolution, compiler, framework or project code is executed.
"""

from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from tree_sitter import Language, Parser

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

EXPORT_SCOPE = "<exports>"
_FUNCTIONS = {"function_declaration", "generator_function_declaration", "function_signature"}
_CLASSES = {"class_declaration", "abstract_class_declaration", "interface_declaration", "class"}
_IDENTIFIERS = {
    "identifier",
    "type_identifier",
    "property_identifier",
    "shorthand_property_identifier",
}
_MAX_NAME = 512


class _Limit(Exception):
    pass


@dataclass(frozen=True)
class _Context:
    scope: str = ""
    owner: str = ""
    class_name: str = ""
    conditional: bool = False
    type_only: bool = False


def _children(node):
    return [node.named_child(i) for i in range(node.named_child_count)]


def _same(first, second):
    return (
        first is not None
        and second is not None
        and first.start_byte == second.start_byte
        and first.end_byte == second.end_byte
    )


def _check_nodes(root):
    cursor, count = root.walk(), 0
    while True:
        count += 1
        if count > MAX_FILE_NODES:
            raise _Limit
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return


class _ScriptFacts:
    def __init__(self, result, source):
        self.result, self.source = result, source
        self.reference_keys = set()
        self.scope_map = {"": Scope("", "module")}

    def text(self, node):
        if node is None:
            return ""
        if node.end_byte - node.start_byte > _MAX_NAME * 4:
            raise _Limit
        value = self.source[node.start_byte : node.end_byte].decode("utf-8")
        if len(value) > _MAX_NAME:
            raise _Limit
        return value

    def column(self, node):
        start = self.source.rfind(b"\n", 0, node.start_byte) + 1
        return len(self.source[start : node.start_byte].decode("utf-8")) + 1

    def name(self, node):
        if node is None:
            return ""
        if node.type in _IDENTIFIERS or node.type in {"this", "super"}:
            return self.text(node)
        if node.type in {"member_expression", "nested_type_identifier"}:
            left = node.child_by_field_name("object") or node.child_by_field_name("module")
            right = node.child_by_field_name("property") or node.child_by_field_name("name")
            prefix, suffix = self.name(left), self.name(right)
            if prefix and suffix:
                return prefix + "." + suffix
        return ""

    def partial(self, code, node):
        self.result.status = "partial"
        if len(self.result.diagnostics) < 20:
            item = {"code": code, "line": node.start_point[0] + 1, "column": self.column(node)}
            if item not in self.result.diagnostics:
                self.result.diagnostics.append(item)

    def binding(self, name, kind, node, ctx, *, value=""):
        if not name:
            return
        if len(self.result.bindings) >= MAX_FILE_REFERENCES:
            raise _Limit
        self.result.bindings.append(
            Binding(name, ctx.scope, kind, node.start_point[0] + 1, value=value)
        )

    def reference(self, name, kind, node, ctx, *, arity=None, type_only=None):
        if not name:
            name = "<dynamic>"
        if name.startswith("this.") and not ctx.class_name:
            name = "<dynamic>" + name[4:]
        key = (kind, name, node.start_byte, ctx.scope)
        if key in self.reference_keys:
            return
        if len(self.result.references) >= MAX_FILE_REFERENCES:
            raise _Limit
        self.reference_keys.add(key)
        self.result.references.append(
            Reference(
                name,
                kind,
                node.start_point[0] + 1,
                ctx.scope,
                ctx.owner,
                self.column(node),
                ctx.type_only if type_only is None else type_only,
                ctx.conditional,
                arity,
            )
        )

    def declare(self, name, kind, node, ctx, params=()):
        if len(self.result.symbols) >= MAX_FILE_SYMBOLS:
            raise _Limit
        qualname = ".".join(filter(None, [ctx.scope, name]))
        symbol = Symbol(
            self.result.path,
            name,
            qualname,
            kind,
            node.start_point[0] + 1,
            node.end_point[0] + 1,
            ctx.scope,
            self.column(node),
            f"{name}({', '.join(params)})" if kind in {"function", "method"} else name,
            list(params),
        )
        self.result.symbols.append(symbol)
        self.result.scopes.append(Scope(qualname, kind, ctx.scope))
        self.scope_map[qualname] = self.result.scopes[-1]
        self.binding(name, "symbol", node, ctx)
        return symbol

    def parameters(self, node):
        if node is None:
            return []
        result = []
        for param in _children(node):
            name = param.child_by_field_name("pattern") or param.child_by_field_name("name")
            if param.type == "identifier":
                name = param
            if name is not None and name.type == "identifier":
                result.append((self.text(name), name))
            elif param.type != "comment":
                self.partial("SCRIPT_PARAMETER_PATTERN_PARTIAL", param)
                if name is not None:
                    result.extend(self.pattern_names(name))
        return result

    def pattern_names(self, node):
        result, stack = [], [node]
        while stack:
            part = stack.pop()
            if part.type in {"identifier", "shorthand_property_identifier_pattern"}:
                result.append((self.text(part), part))
            else:
                stack.extend(_children(part))
        return result

    def variable_context(self, node, ctx):
        if node.parent is None or node.parent.type != "variable_declaration":
            return ctx
        scope = ctx.scope
        while scope and self.scope_map[scope].kind not in {"function", "method"}:
            scope = self.scope_map[scope].parent
        return replace(ctx, scope=scope)

    def callable(self, node, ctx, *, name_node=None, name=None, kind="function"):
        name_node = name_node or node.child_by_field_name("name")
        name = name or self.name(name_node)
        if not name:
            self.partial("SCRIPT_ANONYMOUS_CALLABLE_PARTIAL", node)
            name = f"<anonymous:{node.start_point[0] + 1}:{self.column(node)}>"
        parameters = self.parameters(node.child_by_field_name("parameters"))
        if node.type == "arrow_function" and node.child_by_field_name("parameter") is not None:
            param = node.child_by_field_name("parameter")
            if param.type == "identifier":
                parameters = [(self.text(param), param)]
        symbol = self.declare(name, kind, node, ctx, [p[0] for p in parameters])
        inner = replace(ctx, scope=symbol.qualname, owner=symbol.qualname)
        if kind == "function" and node.type != "arrow_function":
            inner = replace(inner, class_name="")
        for parameter, param in parameters:
            self.binding(parameter, "parameter", param, inner)
        tasks = []
        for child in _children(node):
            if _same(child, name_node):
                continue
            if child.type == "formal_parameters":
                for param in _children(child):
                    tasks.extend(
                        (part, replace(inner, type_only=part.type == "type_annotation"))
                        for part in _children(param)
                        if part.type
                        not in {"identifier", "rest_pattern", "object_pattern", "array_pattern"}
                    )
            elif not _same(child, node.child_by_field_name("parameter")):
                tasks.append((child, inner))
        return tasks

    def source_specifier(self, node):
        source = node.child_by_field_name("source")
        if source is None:
            return ""
        fragments = _children(source)
        if len(fragments) != 1 or fragments[0].type != "string_fragment":
            self.partial("SCRIPT_MODULE_SPECIFIER_PARTIAL", node)
            return ""
        return self.text(fragments[0])

    def import_binding(self, module, imported, local, node, ctx, *, export=False, type_only=False):
        if len(self.result.imports) >= MAX_FILE_REFERENCES:
            raise _Limit
        self.result.imports.append(
            ImportBinding(
                module,
                imported,
                local,
                node.start_point[0] + 1,
                EXPORT_SCOPE if export else ctx.scope,
                type_only=type_only,
                conditional=ctx.conditional,
            )
        )
        if module:
            self.reference(
                ".".join(filter(None, [module, imported])), "IMPORT", node, ctx, type_only=type_only
            )

    def imports(self, node, ctx):
        module = self.source_specifier(node)
        if not module:
            return []
        clause = next((c for c in _children(node) if c.type == "import_clause"), None)
        all_type = any(c.type == "type" for c in node.children)
        if clause is None:
            self.import_binding(module, "", "", node, ctx, type_only=all_type)
            return []
        for child in _children(clause):
            if child.type == "identifier":
                self.import_binding(
                    module, "default", self.text(child), child, ctx, type_only=all_type
                )
            elif child.type == "namespace_import":
                name = next((c for c in _children(child) if c.type == "identifier"), None)
                if name is not None:
                    self.import_binding(
                        module, "*", self.text(name), child, ctx, type_only=all_type
                    )
            elif child.type == "named_imports":
                for spec in _children(child):
                    if spec.type != "import_specifier":
                        continue
                    name = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias") or name
                    if name is None or name.type not in _IDENTIFIERS:
                        self.partial("SCRIPT_IMPORT_BINDING_PARTIAL", spec)
                        continue
                    only_type = all_type or any(c.type == "type" for c in spec.children)
                    self.import_binding(
                        module, self.text(name), self.text(alias), spec, ctx, type_only=only_type
                    )
        return []

    def exports(self, node, ctx):
        if ctx.scope:
            self.partial("SCRIPT_EXPORT_SCOPE_PARTIAL", node)
        module = self.source_specifier(node)
        clause = next((c for c in _children(node) if c.type == "export_clause"), None)
        default = any(c.type == "default" for c in node.children)
        all_type = any(c.type == "type" for c in node.children)
        if clause is not None:
            for spec in _children(clause):
                if spec.type != "export_specifier":
                    continue
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias") or name
                if name is not None and name.type in _IDENTIFIERS:
                    self.import_binding(
                        module,
                        self.text(name),
                        self.text(alias),
                        spec,
                        ctx,
                        export=True,
                        type_only=all_type,
                    )
            return []
        declaration = node.child_by_field_name("declaration")
        if declaration is None:
            declaration = next(
                (
                    c
                    for c in _children(node)
                    if c.type
                    in _FUNCTIONS
                    | _CLASSES
                    | {
                        "lexical_declaration",
                        "variable_declaration",
                        "type_alias_declaration",
                        "enum_declaration",
                    }
                ),
                None,
            )
        if declaration is not None:
            name_node = declaration.child_by_field_name("name")
            names = [self.name(name_node)] if name_node is not None else []
            if declaration.type in {"lexical_declaration", "variable_declaration"}:
                names = [
                    self.name(c.child_by_field_name("name"))
                    for c in _children(declaration)
                    if c.type == "variable_declarator"
                ]
            if not names and default and declaration.type in _FUNCTIONS | _CLASSES:
                names = ["default"]
            for name in filter(None, names):
                self.import_binding(
                    "",
                    name,
                    "default" if default else name,
                    node,
                    ctx,
                    export=True,
                    type_only=all_type,
                )
            return [(declaration, ctx)]
        value = node.child_by_field_name("value")
        if default and value is not None and value.type == "identifier":
            self.import_binding("", self.text(value), "default", node, ctx, export=True)
            return [(value, ctx)]
        if (
            default
            and value is not None
            and value.type in {"arrow_function", "function_expression"}
        ):
            self.import_binding("", "default", "default", node, ctx, export=True)
            return self.callable(value, ctx, name="default")
        if default and value is not None and value.type == "class":
            self.import_binding("", "default", "default", node, ctx, export=True)
            return [(value, ctx)]
        if module:
            self.import_binding(module, "*", "*", node, ctx, export=True, type_only=all_type)
        self.partial("SCRIPT_EXPORT_PARTIAL", node)
        return []

    def visit(self, node, ctx):
        kind = node.type
        if kind in {
            "comment",
            "string",
            "regex",
            "number",
            "true",
            "false",
            "null",
            "undefined",
        }:
            return []
        if kind == "template_string":
            return [(c, ctx) for c in _children(node) if c.type == "template_substitution"]
        if kind == "import_statement":
            return self.imports(node, ctx)
        if kind == "export_statement":
            return self.exports(node, ctx)
        if kind in _FUNCTIONS:
            return self.callable(
                node, ctx, name=self.name(node.child_by_field_name("name")) or "default"
            )
        if kind in _CLASSES:
            name_node = node.child_by_field_name("name")
            name = self.name(name_node) or "default"
            symbol = self.declare(
                name, "interface" if kind == "interface_declaration" else "class", node, ctx
            )
            inner = replace(
                ctx, scope=symbol.qualname, owner=symbol.qualname, class_name=symbol.qualname
            )
            return [(c, inner) for c in _children(node) if not _same(c, name_node)]
        if kind in {"method_definition", "method_signature", "abstract_method_signature"}:
            return self.callable(node, ctx, kind="method")
        if kind == "internal_module":
            self.partial("SCRIPT_NAMESPACE_PARTIAL", node)
            name_node = node.child_by_field_name("name")
            name = self.name(name_node) or f"<namespace:{node.start_point[0] + 1}>"
            symbol = self.declare(name, "namespace", node, ctx)
            inner = replace(ctx, scope=symbol.qualname, owner=symbol.qualname, class_name="")
            return [(c, inner) for c in _children(node) if not _same(c, name_node)]
        if kind in {"type_alias_declaration", "enum_declaration"}:
            name_node = node.child_by_field_name("name")
            self.declare(
                self.name(name_node),
                "enum" if kind == "enum_declaration" else "type_alias",
                node,
                ctx,
            )
            return [
                (c, replace(ctx, type_only=True))
                for c in _children(node)
                if not _same(c, name_node)
            ]
        if kind == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            name = self.name(name_node)
            variable_ctx = self.variable_context(node, ctx)
            if (
                value is not None
                and value.type in {"arrow_function", "function_expression", "generator_function"}
                and name
            ):
                return self.callable(value, variable_ctx, name_node=name_node, name=name)
            if name:
                self.binding(name, "local", name_node, variable_ctx)
            else:
                self.partial("SCRIPT_BINDING_PATTERN_PARTIAL", node)
                for name, child in self.pattern_names(name_node) if name_node is not None else []:
                    self.binding(name, "local", child, variable_ctx)
            return [(c, ctx) for c in _children(node) if not _same(c, name_node)]
        if kind in {"function_expression", "arrow_function", "generator_function"}:
            return self.callable(node, ctx)
        if kind in {"statement_block", "object"}:
            scope = ".".join(
                filter(None, [ctx.scope, f"<block:{node.start_point[0] + 1}:{self.column(node)}>"])
            )
            self.result.scopes.append(Scope(scope, "block", ctx.scope))
            self.scope_map[scope] = self.result.scopes[-1]
            return [(c, replace(ctx, scope=scope)) for c in _children(node)]
        if kind in {"for_statement", "for_in_statement", "catch_clause"}:
            scope = ".".join(
                filter(None, [ctx.scope, f"<scope:{node.start_point[0] + 1}:{self.column(node)}>"])
            )
            self.result.scopes.append(Scope(scope, "block", ctx.scope))
            self.scope_map[scope] = self.result.scopes[-1]
            inner = replace(ctx, scope=scope)
            parameter = node.child_by_field_name("parameter")
            if parameter is not None and parameter.type == "identifier":
                self.binding(self.text(parameter), "local", parameter, inner)
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                self.binding(self.text(left), "local", left, inner)
            return [(c, inner) for c in _children(node) if not _same(c, parameter)]
        if kind in {"call_expression", "new_expression"}:
            function = node.child_by_field_name("function") or node.child_by_field_name(
                "constructor"
            )
            name = self.name(function)
            arguments = node.child_by_field_name("arguments")
            arity = (
                len([c for c in _children(arguments) if c.type != "comment"])
                if arguments is not None
                else 0
            )
            if name in {"require", "eval"} or (function is not None and function.type == "import"):
                self.partial("SCRIPT_DYNAMIC_EXECUTION_PARTIAL", node)
            self.reference(
                name,
                "CALL" if kind == "call_expression" else "INSTANTIATION",
                function or node,
                ctx,
                arity=arity,
            )
            return [(c, ctx) for c in _children(node) if not _same(c, function)]
        if kind in {
            "assignment_expression",
            "augmented_assignment_expression",
            "update_expression",
        }:
            left = node.child_by_field_name("left") or node.child_by_field_name("argument")
            if left is not None and left.type == "identifier":
                self.binding(self.text(left), "local", left, ctx)
            else:
                self.partial("SCRIPT_DYNAMIC_ASSIGNMENT_PARTIAL", node)
            return [(c, ctx) for c in _children(node)]
        if kind in {"member_expression", "nested_type_identifier"}:
            self.reference(
                self.name(node), "TYPE_REFERENCE" if ctx.type_only else "REFERENCE", node, ctx
            )
            return []
        if kind in {"type_annotation", "type_arguments", "type_parameters", "generic_type"}:
            return [(c, replace(ctx, type_only=True)) for c in _children(node)]
        if kind == "type_parameter":
            name = node.child_by_field_name("name")
            if name is not None:
                self.binding(self.text(name), "parameter", name, ctx)
            return [
                (c, replace(ctx, type_only=True)) for c in _children(node) if not _same(c, name)
            ]
        if kind in {"extends_clause", "extends_type_clause", "implements_clause"}:
            relation = "IMPLEMENTS" if kind == "implements_clause" else "INHERITANCE"
            for child in _children(node):
                if self.name(child):
                    self.reference(self.name(child), relation, child, ctx, type_only=True)
            return []
        if kind in {"public_field_definition", "field_definition", "property_signature", "pair"}:
            name = node.child_by_field_name("name") or node.child_by_field_name("key")
            return [(c, ctx) for c in _children(node) if not _same(c, name)]
        if kind in {
            "jsx_element",
            "jsx_self_closing_element",
            "decorator",
            "internal_module",
            "ambient_declaration",
        }:
            self.partial("SCRIPT_FRAMEWORK_OR_AMBIENT_PARTIAL", node)
        if kind in _IDENTIFIERS:
            self.reference(
                self.text(node),
                "TYPE_REFERENCE" if kind == "type_identifier" or ctx.type_only else "REFERENCE",
                node,
                ctx,
            )
            return []
        inner = (
            replace(ctx, conditional=True)
            if kind in {"if_statement", "ternary_expression", "switch_statement"}
            else ctx
        )
        return [(c, inner) for c in _children(node)]

    def extract(self, root):
        stack = [(root, _Context())]
        while stack:
            node, ctx = stack.pop()
            stack.extend(reversed(self.visit(node, ctx)))


def parse_script(path: str, content: str, *, language: str) -> ParsedFile:
    """Extract bounded source-free facts without filesystem or runtime resolution."""
    if language not in {"typescript", "javascript"}:
        raise ValueError("unsupported script language")
    result = ParsedFile(path=path, language=language, module=path)
    result.symbols.append(
        Symbol(
            path,
            PurePosixPath(path).stem,
            "",
            "module",
            0,
            content.count("\n") + int(bool(content) and not content.endswith("\n")),
        )
    )
    result.scopes.append(Scope("", "module"))
    try:
        if len(content) > MAX_PARSE_BYTES:
            raise _Limit
        source = content.encode("utf-8")
        if len(source) > MAX_PARSE_BYTES:
            raise _Limit
        if language == "typescript":
            import tree_sitter_typescript

            grammar = (
                tree_sitter_typescript.language_tsx()
                if path.lower().endswith(".tsx")
                else tree_sitter_typescript.language_typescript()
            )
        else:
            import tree_sitter_javascript

            grammar = tree_sitter_javascript.language()
        tree = Parser(Language(grammar)).parse(source)
        _check_nodes(tree.root_node)
        facts = _ScriptFacts(result, source)
        if tree.root_node.has_error:
            facts.partial("SCRIPT_PARSE_ERROR", tree.root_node)
        facts.extract(tree.root_node)
    except _Limit:
        result.status = "resource_limited"
        result.diagnostics = [{"code": "SCRIPT_RESOURCE_LIMIT"}]
    except MemoryError:
        result.status = "resource_limited"
        result.diagnostics = [{"code": "SCRIPT_RESOURCE_LIMIT"}]
    except Exception:
        result.status = "parse_error"
        result.diagnostics = [{"code": "SCRIPT_PARSE_ERROR"}]
    return result
