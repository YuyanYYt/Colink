"""Read a bounded static subset of Vite configuration, without evaluating project code."""

import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath

from tree_sitter import Language, Parser

from code_context.policy import excluded_path, validate_path

MAX_VITE_CONFIG_BYTES = 64 * 1024
MAX_VITE_ALIASES = 64
_CONFIG_NAMES = frozenset("vite.config." + ext for ext in ("js", "mjs", "cjs", "ts", "mts", "cts"))
_DYNAMIC = "VITE_ALIAS_CONFIG_DYNAMIC"
_AMBIGUOUS = "VITE_ALIAS_AMBIGUOUS"
_OUTSIDE = "VITE_ALIAS_OUTSIDE_INVENTORY"


@dataclass(frozen=True)
class ViteAlias:
    key: str
    path: str = ""
    diagnostic: str = ""


@dataclass(frozen=True)
class ViteConfig:
    path: str
    aliases: tuple[ViteAlias, ...] = ()
    diagnostics: tuple[str, ...] = ()
    # A computed key, spread or unknown config object can affect any bare specifier.
    blocked: str = ""


class _Unsupported(ValueError):
    pass


def is_vite_config(path: str) -> bool:
    return PurePosixPath(path).name in _CONFIG_NAMES


def _children(node):
    return [item for item in node.named_children if item.type != "comment"] if node else []


def _identifier(node):
    if node and node.type in {"identifier", "property_identifier", "shorthand_property_identifier"}:
        return node.text.decode("utf-8")
    return ""


def _literal(node):
    parts = _children(node)
    if node and node.type == "string" and len(parts) == 1 and parts[0].type == "string_fragment":
        return parts[0].text.decode("utf-8")
    return ""


def _unwrap(node):
    while node and node.type in {
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
    }:
        parts = _children(node)
        node = parts[0] if parts else None
    return node


def _member(node):
    if node and node.type == "member_expression":
        return (
            _identifier(node.child_by_field_name("object")),
            _identifier(node.child_by_field_name("property")),
        )
    return "", ""


def _property(node, name):
    if not node or node.type != "object":
        raise _Unsupported(_DYNAMIC)
    result = None
    for child in _children(node):
        key = child.child_by_field_name("key") or child.child_by_field_name("name")
        key_name = _identifier(key) or _literal(key) or _identifier(child)
        if not key_name:
            raise _Unsupported(_DYNAMIC)
        if key_name == name:
            if result is not None:
                raise _Unsupported(_AMBIGUOUS)
            if child.type != "pair":
                raise _Unsupported(_DYNAMIC)
            result = child.child_by_field_name("value")
    return result


def _bound_names(declarations):
    """Over-approximate names in declaration patterns so shadowed helpers are never trusted."""
    result = set()
    stack = [node for node in declarations if node]
    while stack:
        node = stack.pop()
        if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
            result.add(node.text.decode())
        else:
            stack.extend(_children(node))
    return result


def _imports(statement, helpers):
    module = _literal(statement.child_by_field_name("source"))
    if module in {"node:path", "node:url"}:
        module = module.removeprefix("node:")
    allowed = {
        "vite": {"defineConfig"},
        "path": {"resolve", "join"},
        "url": {"fileURLToPath", "URL"},
    }
    if any(c.type == "type" for c in statement.children):
        return set()
    local_names = set()
    clause = next((c for c in _children(statement) if c.type == "import_clause"), None)
    for item in _children(clause):
        if item.type in {"identifier", "namespace_import"}:
            name = _identifier(item) or _identifier(next(iter(_children(item)), None))
            if name:
                local_names.add(name)
                if module in allowed and (module != "vite" or item.type == "namespace_import"):
                    helpers[name] = (module, "*")
        elif item.type == "named_imports":
            for spec in _children(item):
                if any(c.type == "type" for c in spec.children):
                    continue
                name = _identifier(spec.child_by_field_name("name"))
                local = _identifier(spec.child_by_field_name("alias")) or name
                local_names.add(local)
                if name in allowed.get(module, ()):
                    helpers[local] = (module, name)
    return local_names


def _call_identity(node, helpers):
    if not node or node.type != "call_expression":
        return None
    callee = node.child_by_field_name("function")
    if name := _identifier(callee):
        return helpers.get(name)
    name, method = _member(callee)
    module = helpers.get(name)
    return (module[0], method) if module and module[1] == "*" else None


def _config_object(root):
    helpers, exports, locals_ = {}, [], []
    imported = set()
    commonjs = False
    for item in _children(root):
        if item.type == "import_statement":
            names = _imports(item, helpers)
            if imported & names:
                raise _Unsupported(_AMBIGUOUS)
            imported.update(names)
        elif item.type == "export_statement" and any(c.type == "default" for c in item.children):
            exports.append(item.child_by_field_name("value"))
        elif item.type == "export_statement":
            item = item.child_by_field_name("declaration")
            if item is None:
                continue
        elif item.type == "expression_statement":
            expression = next(iter(_children(item)), None)
            if expression and expression.type == "string":
                continue
            if (
                not expression
                or expression.type != "assignment_expression"
                or _member(expression.child_by_field_name("left")) != ("module", "exports")
            ):
                raise _Unsupported(_DYNAMIC)
            exports.append(expression.child_by_field_name("right"))
            commonjs = True
        elif item.type not in {
            "lexical_declaration",
            "variable_declaration",
            "function_declaration",
            "class_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "empty_statement",
            "hash_bang_line",
        }:
            # Top-level control flow can replace a CommonJS export or the helpers.
            raise _Unsupported(_DYNAMIC)
        if item.type in {"lexical_declaration", "variable_declaration"}:
            locals_.extend(c.child_by_field_name("name") for c in _children(item))
        else:
            locals_.append(item.child_by_field_name("name"))
    if len(exports) != 1:
        raise _Unsupported(_DYNAMIC)
    shadowed = _bound_names(locals_)
    if commonjs and "module" in (shadowed | imported):
        raise _Unsupported(_DYNAMIC)
    helpers = {name: role for name, role in helpers.items() if name not in shadowed}
    value = _unwrap(exports[0])
    if _call_identity(value, helpers) == ("vite", "defineConfig"):
        arguments = _children(value.child_by_field_name("arguments"))
        value = _unwrap(arguments[0]) if len(arguments) == 1 else None
    if value and value.type in {"arrow_function", "function_expression"}:
        shadowed.update(
            _bound_names(
                [
                    value.child_by_field_name("parameters"),
                    value.child_by_field_name("parameter"),
                    value.child_by_field_name("name"),
                ]
            )
        )
        value = _unwrap(value.child_by_field_name("body"))
        if value and value.type == "statement_block":
            statements = _children(value)
            if not statements or statements[-1].type != "return_statement":
                raise _Unsupported(_DYNAMIC)
            # Vite callbacks often prepare unrelated server/plugin variables first.
            for statement in statements[:-1]:
                if statement.type not in {"lexical_declaration", "variable_declaration"}:
                    raise _Unsupported(_DYNAMIC)
                shadowed.update(
                    _bound_names(c.child_by_field_name("name") for c in _children(statement))
                )
            value = _unwrap(next(iter(_children(statements[-1])), None))
    return (
        value,
        {name: role for name, role in helpers.items() if name not in shadowed},
        shadowed | imported,
    )


def _replacement(node, helpers, shadowed):
    node = _unwrap(node)
    if value := _literal(node):
        # A bare replacement can name an npm package. Only an explicitly relative
        # string establishes a project path without a path-building expression.
        if value in {".", ".."} or value.startswith(("./", "../", "/")):
            return value
        return ""
    identity = _call_identity(node, helpers)
    args = _children(node.child_by_field_name("arguments")) if identity else []
    if identity in {("path", "resolve"), ("path", "join")} and len(args) == 2:
        if _identifier(args[0]) == "__dirname" and "__dirname" not in shadowed:
            return _literal(args[1])
    if identity == ("url", "fileURLToPath") and len(args) == 1 and args[0].type == "new_expression":
        ctor = args[0].child_by_field_name("constructor")
        url_args = _children(args[0].child_by_field_name("arguments"))
        name = _identifier(ctor)
        native_url = (name == "URL" and name not in shadowed) or helpers.get(name) == ("url", "URL")
        if native_url and len(url_args) == 2:
            base = url_args[1]
            if (
                base.type == "member_expression"
                and _identifier(base.child_by_field_name("property")) == "url"
            ):
                meta = base.child_by_field_name("object")
                if meta and meta.type == "meta_property" and meta.text == b"import.meta":
                    return _literal(url_args[0])
    return ""


def internal_path(directory: str, value: str) -> str | None:
    """Lexical path arithmetic only; authorization is established separately by inventory."""
    if not value or value.startswith("/") or any(c in value for c in "\\:?#%\0"):
        return None
    normalized = posixpath.normpath(posixpath.join(directory, value))
    if normalized == ".":
        return normalized
    try:
        validate_path(normalized)
    except ValueError:
        return None
    return None if excluded_path(normalized) else normalized


def _rules(alias_node):
    if alias_node.type == "object":
        for pair in _children(alias_node):
            if pair.type != "pair":
                raise _Unsupported(_DYNAMIC)
            key = pair.child_by_field_name("key")
            yield _literal(key) or _identifier(key), pair.child_by_field_name("value")
    elif alias_node.type == "array":
        for item in _children(alias_node):
            if _property(item, "customResolver") is not None:
                raise _Unsupported(_DYNAMIC)
            yield _literal(_property(item, "find")), _property(item, "replacement")
    else:
        raise _Unsupported(_DYNAMIC)


def parse_vite_config(path: str, content: str | None) -> ViteConfig:
    """Use only the supplied authorized text. Diagnostics never retain dynamic expressions."""
    try:
        if content is None:
            raise _Unsupported("VITE_ALIAS_CONFIG_UNAVAILABLE")
        if len(content) > MAX_VITE_CONFIG_BYTES:
            raise _Unsupported("VITE_ALIAS_CONFIG_LIMIT")
        source = content.encode("utf-8")
        if len(source) > MAX_VITE_CONFIG_BYTES:
            raise _Unsupported("VITE_ALIAS_CONFIG_LIMIT")
        if path.endswith((".ts", ".mts", ".cts")):
            import tree_sitter_typescript

            grammar = tree_sitter_typescript.language_typescript()
        else:
            import tree_sitter_javascript

            grammar = tree_sitter_javascript.language()
        root = Parser(Language(grammar)).parse(source).root_node
        if root.has_error:
            raise _Unsupported("VITE_ALIAS_CONFIG_PARSE_ERROR")
        value, helpers, shadowed = _config_object(root)
        resolver = _property(value, "resolve")
        alias_node = _property(resolver, "alias") if resolver else None
        aliases = {}
        if alias_node is not None:
            for index, (key, expression) in enumerate(_rules(alias_node)):
                if index >= MAX_VITE_ALIASES:
                    raise _Unsupported("VITE_ALIAS_CONFIG_LIMIT")
                if (
                    not key
                    or len(key) > 128
                    or any(c.isspace() or c in "\\:?#%" for c in key)
                    or any(part in {"", ".", ".."} for part in key.split("/"))
                ):
                    raise _Unsupported(_DYNAMIC)
                raw = _replacement(expression, helpers, shadowed)
                if key in aliases:
                    alias = ViteAlias(key, diagnostic=_AMBIGUOUS)
                elif not raw:
                    alias = ViteAlias(key, diagnostic=_DYNAMIC)
                else:
                    base = internal_path(posixpath.dirname(path), raw)
                    alias = (
                        ViteAlias(key, base)
                        if base is not None
                        else ViteAlias(key, diagnostic=_OUTSIDE)
                    )
                aliases[key] = alias
        rules = tuple(aliases.values())
        return ViteConfig(path, rules, tuple(sorted({a.diagnostic for a in rules if a.diagnostic})))
    except _Unsupported as error:
        diagnostic = str(error)
    except (MemoryError, RecursionError, UnicodeError, ValueError):
        diagnostic = "VITE_ALIAS_CONFIG_PARSE_ERROR"
    return ViteConfig(path, diagnostics=(diagnostic,), blocked=diagnostic)
