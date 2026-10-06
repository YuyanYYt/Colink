"""Bounded Python syntax facts for CoLink's read-only code intelligence.

Only the supplied text is parsed. Project imports, decorators, default values and
annotation expressions are never evaluated. The returned facts contain names,
safe type syntax and positions, rather than source snippets or retained ASTs.
"""

import ast
import symtable
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath

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

_CALLABLE_SCOPES = frozenset({"function", "method", "lambda", "comprehension"})
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_DYNAMIC = "<dynamic>"
_MAX_TYPE_TEXT = 512


class _LimitReached(Exception):
    def __init__(self, code: str):
        self.code = code


@dataclass
class _Frame:
    scope: Scope
    owner: str
    locals: set[str] = field(default_factory=set)
    imports: dict[str, ImportBinding | None] = field(default_factory=dict)
    receiver: str = ""
    receiver_class: str = ""


def _name(node: ast.AST) -> str:
    """Only identifier/attribute chains qualify as static target evidence."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return ".".join([_DYNAMIC, *reversed(parts)])


def _join(parent: str, name: str) -> str:
    return f"{parent}.{name}" if parent else name


def _declarations(nodes: list[ast.AST]) -> tuple[set[str], list[str], list[str]]:
    """Collect whole-scope declarations without entering a child lexical scope."""
    local, global_names, nonlocal_names = set(), set(), set()
    pending = list(nodes)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Global):
            global_names.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            nonlocal_names.update(node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local.add(node.name)
            pending.extend(node.decorator_list)
            pending.extend(node.args.defaults)
            pending.extend(value for value in node.args.kw_defaults if value is not None)
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None)
            pending.extend(arg.annotation for arg in arguments if arg.annotation is not None)
            if node.returns is not None:
                pending.append(node.returns)
            # Defaults/decorators execute in the enclosing scope; the body does not.
        elif isinstance(node, ast.ClassDef):
            local.add(node.name)
            pending.extend(node.decorator_list)
            pending.extend(node.bases)
            pending.extend(keyword.value for keyword in node.keywords)
        elif isinstance(node, ast.Lambda):
            pending.extend(node.args.defaults)
            pending.extend(value for value in node.args.kw_defaults if value is not None)
        elif isinstance(node, ast.comprehension):
            # Iteration variables belong to the comprehension, not its parent.
            pending.extend([node.iter, *node.ifs])
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            local.update(
                alias.asname
                or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                for alias in node.names
                if alias.name != "*"
            )
        else:
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                local.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                local.add(node.name)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                local.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                local.add(node.rest)
            pending.extend(ast.iter_child_nodes(node))
    return local - global_names - nonlocal_names, sorted(global_names), sorted(nonlocal_names)


def _count_nodes(tree: ast.AST, remaining: int) -> int:
    count, pending = 0, [tree]
    while pending:
        count += 1
        if count > remaining:
            raise _LimitReached("MAX_FILE_NODES")
        pending.extend(ast.iter_child_nodes(pending.pop()))
    return count


class _Extractor(ast.NodeVisitor):
    def __init__(self, parsed: ParsedFile, text: str, tree: ast.Module, nodes: int):
        self.parsed = parsed
        # Python's physical lines use CR/LF, not Unicode separators inside literals.
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.columns: dict[int, list[int]] = {}
        self.nodes = nodes
        self.type_only = False
        self.conditional = False
        self.future_annotations = any(
            isinstance(statement, ast.ImportFrom)
            and statement.module == "__future__"
            and statement.level == 0
            and any(alias.name == "annotations" for alias in statement.names)
            for statement in tree.body
        )
        self.owner_override: str | None = None
        self.reference_keys: set[tuple] = set()
        local, globals_, nonlocals = _declarations(tree.body)
        module_scope = parsed.scopes[0]
        module_scope.globals, module_scope.nonlocals = globals_, nonlocals
        self.frames = [_Frame(module_scope, "", local)]

    @property
    def frame(self) -> _Frame:
        return self.frames[-1]

    def _position(self, node: ast.AST) -> tuple[int, int]:
        line = max(1, getattr(node, "lineno", 1))
        offset = getattr(node, "col_offset", 0)
        if line <= len(self.lines) and not self.lines[line - 1].isascii():
            if line not in self.columns:
                offsets, total = [0], 0
                for character in self.lines[line - 1]:
                    total += len(character.encode("utf-8"))
                    offsets.append(total)
                self.columns[line] = offsets
            return line, bisect_right(self.columns[line], offset)
        return line, offset + 1

    def _ref(
        self,
        name: str,
        kind: str,
        node: ast.AST,
        *,
        arity: int | None = None,
        type_only: bool | None = None,
        origin: ast.AST | None = None,
    ) -> None:
        line, column = self._position(origin or node)
        owner = self.frame.owner if self.owner_override is None else self.owner_override
        key = (name, kind, line, column, self.frame.scope.qualname, owner)
        if key in self.reference_keys:
            return
        if len(self.parsed.references) >= MAX_FILE_REFERENCES:
            raise _LimitReached("MAX_FILE_REFERENCES")
        self.reference_keys.add(key)
        self.parsed.references.append(
            Reference(
                name,
                kind,
                line,
                self.frame.scope.qualname,
                owner,
                column,
                self.type_only if type_only is None else type_only,
                self.conditional,
                arity,
            )
        )

    def _symbol(
        self,
        name: str,
        qualname: str,
        kind: str,
        node: ast.AST,
        scope: str | None = None,
        **metadata,
    ) -> Symbol:
        if len(self.parsed.symbols) >= MAX_FILE_SYMBOLS:
            raise _LimitReached("MAX_FILE_SYMBOLS")
        line, column = self._position(node)
        symbol = Symbol(
            self.parsed.path,
            name,
            qualname,
            kind,
            line,
            max(line, getattr(node, "end_lineno", line)),
            self.frame.scope.qualname if scope is None else scope,
            column,
            **metadata,
        )
        self.parsed.symbols.append(symbol)
        return symbol

    def _binding_frame(self, name: str, *, walrus: bool = False) -> _Frame:
        index = len(self.frames) - 1
        if walrus:
            while index and self.frames[index].scope.kind == "comprehension":
                index -= 1
        frame = self.frames[index]
        if name in frame.scope.globals:
            return self.frames[0]
        if name in frame.scope.nonlocals:
            for outer in reversed(self.frames[1:index]):
                if outer.scope.kind != "class" and name in outer.locals:
                    return outer
        return frame

    def _bind(
        self,
        name: str,
        kind: str,
        node: ast.AST,
        annotation: str = "",
        value: str = "",
        *,
        frame: _Frame | None = None,
        walrus: bool = False,
    ) -> None:
        frame = frame or self._binding_frame(name, walrus=walrus)
        if name == frame.receiver and kind != "parameter":
            frame.receiver = frame.receiver_class = ""
        frame.imports[name] = None
        frame.locals.add(name)
        self.parsed.bindings.append(
            Binding(
                name,
                frame.scope.qualname,
                kind,
                self._position(node)[0],
                annotation,
                value,
            )
        )

    @contextmanager
    def _scope(
        self,
        qualname: str,
        kind: str,
        body: list[ast.AST],
        *,
        owner: str | None = None,
        receiver: str = "",
        receiver_class: str = "",
    ):
        parent = self.frame.scope.qualname
        if kind in {"lambda", "comprehension"}:
            # These closures cannot see a class namespace, including a class's imports.
            for frame in reversed(self.frames):
                if frame.scope.kind != "class":
                    parent = frame.scope.qualname
                    break
        local, globals_, nonlocals = _declarations(body)
        scope = Scope(qualname, kind, parent, globals_, nonlocals)
        self.parsed.scopes.append(scope)
        frame = _Frame(
            scope,
            self.frame.owner if owner is None else owner,
            local,
            receiver=receiver,
            receiver_class=receiver_class,
        )
        self.frames.append(frame)
        try:
            yield
        finally:
            self.frames.pop()

    @contextmanager
    def _owner(self, qualname: str):
        previous = self.owner_override
        self.owner_override = qualname
        try:
            yield
        finally:
            self.owner_override = previous

    @contextmanager
    def _condition(self, *, type_only: bool = False):
        previous = self.type_only, self.conditional
        self.type_only, self.conditional = self.type_only or type_only, True
        try:
            yield
        finally:
            self.type_only, self.conditional = previous

    def _lookup_import(self, name: str) -> ImportBinding | None:
        current = self.frame
        if name in current.scope.globals:
            frames = [self.frames[0]]
        elif name in current.scope.nonlocals:
            frames = list(reversed(self.frames[1:-1]))
        else:
            frames = list(reversed(self.frames))
        for frame in frames:
            if frame is not current and frame.scope.kind == "class":
                continue
            if name in frame.imports:
                return frame.imports[name]
            if name in frame.locals and frame.scope.kind in _CALLABLE_SCOPES:
                return None
        return None

    def _typing_name(self, node: ast.AST) -> str:
        name = _name(node)
        root, _, suffix = name.partition(".")
        imported = self._lookup_import(root)
        if imported and not imported.level and imported.module in {"typing", "typing_extensions"}:
            if imported.imported_name:
                return _join(imported.imported_name, suffix) if suffix else imported.imported_name
            return suffix
        # Treat these syntactic forms as typing constructs when not imported too.
        if name in {"Literal", "Annotated"}:
            return name
        return ""

    def _runtime_guard(self, node: ast.AST) -> tuple[bool | None, bool]:
        if self._typing_name(node) == "TYPE_CHECKING":
            imported = self._lookup_import(_name(node).partition(".")[0])
            if imported and not imported.conditional:
                return False, True
            return None, False
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value, False
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value, marker = self._runtime_guard(node.operand)
            return None if value is None else not value, marker
        if isinstance(node, ast.BoolOp):
            values = [self._runtime_guard(value) for value in node.values]
            truth = [value for value, _ in values]
            marker = any(marker for _, marker in values)
            if isinstance(node.op, ast.And):
                return (
                    False if False in truth else True if all(v is True for v in truth) else None
                ), marker
            return (
                True if True in truth else False if all(v is False for v in truth) else None
            ), marker
        return None, False

    def _forward(self, node: ast.Constant) -> ast.AST | None:
        if not isinstance(node.value, str) or len(node.value) > _MAX_TYPE_TEXT:
            return None
        try:
            tree = ast.parse(node.value, mode="eval")
            self.nodes += _count_nodes(tree, MAX_FILE_NODES - self.nodes)
            # The visitor controls which subtrees are type syntax. Literal
            # values and Annotated metadata are never scanned as forward types.
            if not isinstance(
                tree.body,
                (
                    ast.Name,
                    ast.Attribute,
                    ast.Subscript,
                    ast.Tuple,
                    ast.List,
                    ast.BinOp,
                    ast.Constant,
                ),
            ):
                return None
            return tree.body
        except (SyntaxError, ValueError, RecursionError, OverflowError):
            return None

    def _annotation(
        self,
        node: ast.AST | None,
        *,
        refs: bool = True,
        origin: ast.AST | None = None,
        depth: int = 0,
    ) -> str:
        previous = self.type_only
        # Ordinary annotations can be evaluated at runtime. Future annotations
        # and quoted forward types prove their names are not evaluated here.
        self.type_only = previous or self.future_annotations or origin is not None
        try:
            return self._annotation_value(node, refs=refs, origin=origin, depth=depth)
        finally:
            self.type_only = previous

    def _annotation_value(
        self,
        node: ast.AST | None,
        *,
        refs: bool = True,
        origin: ast.AST | None = None,
        depth: int = 0,
    ) -> str:
        if node is None or depth > 16:
            return ""
        if isinstance(node, ast.Constant):
            if node.value is None:
                return "None"
            if node.value is Ellipsis:
                return "..."
            if isinstance(node.value, str):
                forward = self._forward(node)
                if forward is not None:
                    return self._annotation(
                        forward, refs=refs, origin=origin or node, depth=depth + 1
                    )
            return ""
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = _name(node)
            if name.startswith(_DYNAMIC):
                if refs:
                    self._ref(name, "TYPE_REFERENCE", node, origin=origin)
                    if origin is None and isinstance(node, ast.Attribute):
                        self.visit(node.value)
                return ""
            if refs:
                self._ref(name, "TYPE_REFERENCE", node, origin=origin)
            return name[:_MAX_TYPE_TEXT]
        if isinstance(node, ast.Subscript):
            base = self._annotation(node.value, refs=refs, origin=origin, depth=depth + 1)
            values = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
            marker = self._typing_name(node.value)
            if marker == "Literal":
                if refs and origin is None:
                    for value in values:
                        self.visit(value)
                items = ["..."]
            else:
                if marker == "Annotated":
                    if refs and origin is None:
                        for metadata in values[1:]:
                            self.visit(metadata)
                    values = values[:1]
                items = [
                    self._annotation(value, refs=refs, origin=origin, depth=depth + 1) or "?"
                    for value in values
                ]
            return f"{base}[{', '.join(items)}]"[:_MAX_TYPE_TEXT] if base else ""
        if isinstance(node, (ast.Tuple, ast.List)):
            items = [
                self._annotation(value, refs=refs, origin=origin, depth=depth + 1) or "?"
                for value in node.elts
            ]
            text = ", ".join(items)
            return (f"[{text}]" if isinstance(node, ast.List) else text)[:_MAX_TYPE_TEXT]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left = self._annotation(node.left, refs=refs, origin=origin, depth=depth + 1)
            right = self._annotation(node.right, refs=refs, origin=origin, depth=depth + 1)
            return f"{left or '?'} | {right or '?'}"[:_MAX_TYPE_TEXT]
        if isinstance(node, ast.Starred):
            return "*" + self._annotation(node.value, refs=refs, origin=origin, depth=depth + 1)
        if refs and origin is None:
            # A real annotation expression can call project code at runtime.
            # Keep dynamic evidence without evaluating it or storing its text.
            self._ref(_DYNAMIC, "TYPE_REFERENCE", node)
            self.visit(node)
        return ""

    def _safe_value(self, node: ast.AST | None) -> str:
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = _name(node)
            if not name.startswith(_DYNAMIC):
                return name
        return ""

    def _target(
        self,
        node: ast.AST,
        value: ast.AST | None = None,
        annotation: str = "",
        *,
        walrus: bool = False,
        annotated: bool = False,
    ) -> None:
        if isinstance(node, ast.Name):
            kind = "field" if self.frame.scope.kind == "class" else "variable"
            self._bind(node.id, kind, node, annotation, self._safe_value(value), walrus=walrus)
            if annotated and kind == "field":
                self._symbol(node.id, _join(self.frame.scope.qualname, node.id), "field", node)
        elif isinstance(node, (ast.Tuple, ast.List)):
            values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else []
            exact = len(values) == len(node.elts) and not any(
                isinstance(item, ast.Starred) for item in [*node.elts, *values]
            )
            for index, item in enumerate(node.elts):
                self._target(item, values[index] if exact else None, walrus=walrus)
        elif isinstance(node, ast.Starred):
            self._target(node.value, walrus=walrus)
        elif isinstance(node, ast.Attribute):
            receiver = node.value.id if isinstance(node.value, ast.Name) else ""
            frame = self.frame
            if annotated and receiver and receiver == frame.receiver and frame.receiver_class:
                owner = next(f for f in self.frames if f.scope.qualname == frame.receiver_class)
                self._symbol(
                    node.attr,
                    _join(owner.scope.qualname, node.attr),
                    "field",
                    node,
                    scope=owner.scope.qualname,
                )
                self._bind(
                    node.attr, "field", node, annotation, self._safe_value(value), frame=owner
                )
            else:
                self._ref(_name(node), "REFERENCE", node)
            self.visit(node.value)
        elif isinstance(node, ast.Subscript):
            self.visit(node.value)
            self.visit(node.slice)

    def _arguments(self, args: ast.arguments) -> list[ast.arg]:
        return [
            *args.posonlyargs,
            *args.args,
            *([args.vararg] if args.vararg else []),
            *args.kwonlyargs,
            *([args.kwarg] if args.kwarg else []),
        ]

    def _parameters(self, args: ast.arguments, annotations: dict[str, str]) -> None:
        for arg in self._arguments(args):
            self._bind(arg.arg, "parameter", arg, annotations.get(arg.arg, ""))

    def _signature(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        annotations: dict[str, str],
        returns: str,
    ) -> str:
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        default_start = len(positional) - len(args.defaults)
        parts = []

        def argument(arg: ast.arg, prefix: str = "", default: bool = False) -> str:
            text = prefix + arg.arg
            if annotations.get(arg.arg):
                text += ": " + annotations[arg.arg]
            return text + (" = ..." if default else "")

        for index, arg in enumerate(positional):
            parts.append(argument(arg, default=index >= default_start))
            if args.posonlyargs and index + 1 == len(args.posonlyargs):
                parts.append("/")
        if args.vararg:
            parts.append(argument(args.vararg, "*"))
        elif args.kwonlyargs:
            parts.append("*")
        parts.extend(
            argument(arg, default=value is not None)
            for arg, value in zip(args.kwonlyargs, args.kw_defaults, strict=True)
        )
        if args.kwarg:
            parts.append(argument(args.kwarg, "**"))
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        text = f"{prefix} {node.name}({', '.join(parts)})"
        if returns:
            text += " -> " + returns
        return text[:1024]

    def _decorators(self, decorators: list[ast.expr]) -> None:
        for decorator in decorators:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            self._ref(_name(target), "DECORATOR", target)
            if isinstance(decorator, ast.Call):
                self.visit(decorator)
            elif _name(decorator).startswith(_DYNAMIC):
                self.visit(decorator)

    def _type_parameters(self, node: ast.AST) -> None:
        for parameter in getattr(node, "type_params", []):
            annotation = self._annotation(getattr(parameter, "bound", None))
            self._bind(parameter.name, "parameter", parameter, annotation)
            self._annotation(getattr(parameter, "default_value", None))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.frame
        qualname = _join(parent.scope.qualname, node.name)
        kind = "method" if parent.scope.kind == "class" else "function"
        with self._owner(qualname):
            self._decorators(node.decorator_list)
            annotations = {
                arg.arg: self._annotation(arg.annotation) for arg in self._arguments(node.args)
            }
            returns = self._annotation(node.returns)
            for value in [*node.args.defaults, *node.args.kw_defaults]:
                if value is not None:
                    self.visit(value)
        self._symbol(
            node.name,
            qualname,
            kind,
            node,
            signature=self._signature(node, annotations, returns),
            parameters=[arg.arg for arg in self._arguments(node.args)],
            return_type=returns,
        )
        self._bind(node.name, "symbol", node, value=qualname)
        positional = [*node.args.posonlyargs, *node.args.args]
        static = any(
            _name(decorator).rsplit(".", 1)[-1] == "staticmethod"
            for decorator in node.decorator_list
        )
        receiver = positional[0].arg if kind == "method" and positional and not static else ""
        with self._scope(
            qualname,
            kind,
            node.body,
            owner=qualname,
            receiver=receiver,
            receiver_class=parent.scope.qualname if receiver else "",
        ):
            self._type_parameters(node)
            self._parameters(node.args, annotations)
            for statement in node.body:
                self.visit(statement)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = _join(self.frame.scope.qualname, node.name)
        with self._owner(qualname):
            self._decorators(node.decorator_list)
            for base in node.bases:
                target = base.value if isinstance(base, ast.Subscript) else base
                self._ref(_name(target), "INHERITANCE", target)
                if isinstance(base, ast.Subscript):
                    self._annotation(base.slice)
                if _name(target).startswith(_DYNAMIC):
                    self.visit(target)
            for keyword in node.keywords:
                self.visit(keyword.value)
        self._symbol(node.name, qualname, "class", node)
        self._bind(node.name, "symbol", node, value=qualname)
        with self._scope(qualname, "class", node.body, owner=qualname):
            self._type_parameters(node)
            for statement in node.body:
                self.visit(statement)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for value in [*node.args.defaults, *node.args.kw_defaults]:
            if value is not None:
                self.visit(value)
        line, column = self._position(node)
        qualname = _join(self.frame.scope.qualname, f"<lambda>@{line}:{column}")
        with self._scope(qualname, "lambda", [node.body]):
            self._parameters(node.args, {})
            self.visit(node.body)

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
        # Python evaluates the first iterable before entering its implicit closure.
        self.visit(node.generators[0].iter)
        line, column = self._position(node)
        label = type(node).__name__.lower()
        qualname = _join(self.frame.scope.qualname, f"<{label}>@{line}:{column}")
        with self._scope(qualname, "comprehension", [g.target for g in node.generators]):
            for index, generator in enumerate(node.generators):
                if index:
                    self.visit(generator.iter)
                self._target(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            from_import = isinstance(node, ast.ImportFrom)
            module = (node.module or "") if from_import else alias.name
            imported = alias.name if from_import else ""
            local = alias.asname or (alias.name if from_import else alias.name.split(".")[0])
            frame = self._binding_frame(local)
            binding = ImportBinding(
                module,
                imported,
                local,
                self._position(alias)[0],
                frame.scope.qualname,
                node.level if from_import else 0,
                self.type_only,
                self.conditional,
            )
            self.parsed.imports.append(binding)
            if local != "*":
                frame.imports[local] = binding
                frame.locals.add(local)
            name = "." * binding.level + ".".join(filter(None, [module, imported]))
            self._ref(name, "IMPORT", alias)

    visit_ImportFrom = visit_Import

    def visit_Call(self, node: ast.Call) -> None:
        arity = (
            None
            if (
                any(isinstance(arg, ast.Starred) for arg in node.args)
                or any(keyword.arg is None for keyword in node.keywords)
            )
            else len(node.args) + len(node.keywords)
        )
        name = _name(node.func)
        self._ref(name, "CALL", node.func, arity=arity)
        if name.startswith(_DYNAMIC):
            self.visit(node.func.value if isinstance(node.func, ast.Attribute) else node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._ref(node.id, "REFERENCE", node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = _name(node)
        self._ref(name, "REFERENCE", node)
        if name.startswith(_DYNAMIC):
            self.visit(node.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._target(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        annotation = self._annotation(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self._target(node.target, node.value, annotation, annotated=True)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._ref(node.target.id, "REFERENCE", node.target)
        self._target(node.target)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._target(node.target, node.value, walrus=True)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._ref(target.id, "REFERENCE", target)
            self._target(target)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        runtime, marker = self._runtime_guard(node.test)
        with self._condition(type_only=marker and runtime is False):
            for statement in node.body:
                self.visit(statement)
        with self._condition(type_only=marker and runtime is True):
            for statement in node.orelse:
                self.visit(statement)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        runtime, marker = self._runtime_guard(node.test)
        with self._condition(type_only=marker and runtime is False):
            self.visit(node.body)
        with self._condition(type_only=marker and runtime is True):
            self.visit(node.orelse)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._target(node.target)
        with self._condition():
            for statement in [*node.body, *node.orelse]:
                self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        with self._condition():
            for statement in [*node.body, *node.orelse]:
                self.visit(statement)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._target(item.optional_vars)
        with self._condition():
            for statement in node.body:
                self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_Try(self, node: ast.Try) -> None:
        with self._condition():
            for statement in [*node.body, *node.handlers, *node.orelse, *node.finalbody]:
                self.visit(statement)

    visit_TryStar = visit_Try

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._bind(node.name, "variable", node)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        with self._condition():
            for case in node.cases:
                self.visit(case.pattern)
                if case.guard is not None:
                    self.visit(case.guard)
                for statement in case.body:
                    self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.pattern is not None:
            self.visit(node.pattern)
        if node.name:
            self._bind(node.name, "variable", node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._bind(node.name, "variable", node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        for item in [*node.keys, *node.patterns]:
            self.visit(item)
        if node.rest:
            self._bind(node.rest, "variable", node)

    def visit_TypeAlias(self, node: ast.AST) -> None:
        # Available on Python 3.12+. On older runtimes ast.parse rejects the syntax.
        self._target(node.name)
        self._annotation(node.value)


def parse_python(path: str, content: str) -> ParsedFile:
    """Extract bounded, unresolved facts from text; never access ``path`` on disk."""
    file_path = PurePosixPath(path)
    parts = list(file_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    # Avoid allocating a line list before rejecting an oversized source.
    source_count = content.count("\n") + content.count("\r") - content.count("\r\n")
    if content and not content.endswith(("\n", "\r")):
        source_count += 1
    parsed = ParsedFile(path, module=".".join(parts))
    parsed.symbols.append(Symbol(path, file_path.stem, "", "module", 0, source_count))
    parsed.scopes.append(Scope("", "module"))
    try:
        if len(content) > MAX_PARSE_BYTES or len(content.encode("utf-8")) > MAX_PARSE_BYTES:
            raise _LimitReached("MAX_PARSE_BYTES")
        tree = ast.parse(content, filename="<CoLink Python source>", type_comments=False)
        nodes = _count_nodes(tree, MAX_FILE_NODES)
        # Validate directives/duplicate parameters and comprehension restrictions
        # that ast.parse alone accepts. symtable compiles no executable code and
        # never imports or evaluates the supplied project.
        symtable.symtable(content, "<CoLink Python source>", "exec")
        extractor = _Extractor(parsed, content, tree, nodes)
        extractor.visit(tree)
    except _LimitReached as error:
        parsed.status = (
            "partial" if len(parsed.symbols) > 1 or parsed.references else "resource_limited"
        )
        parsed.diagnostics.append(
            {"code": error.code, "message": "Python fact extraction limit reached."}
        )
    except (SyntaxError, UnicodeError, ValueError) as error:
        parsed.status = "parse_error"
        diagnostic = {"code": "PYTHON_PARSE_ERROR", "message": "Python syntax could not be parsed."}
        if isinstance(error, SyntaxError) and error.lineno:
            diagnostic["line"] = max(1, error.lineno)
            diagnostic["column"] = max(1, error.offset or 1)
        parsed.diagnostics.append(diagnostic)
    except (MemoryError, RecursionError, OverflowError):
        parsed.status = (
            "partial" if len(parsed.symbols) > 1 or parsed.references else "resource_limited"
        )
        parsed.diagnostics.append(
            {
                "code": "PYTHON_PARSER_RESOURCE_LIMIT",
                "message": "Python parser capacity was exceeded.",
            }
        )
    except Exception:
        # Parser failures are isolated per file, and exception text may contain source.
        parsed.status = "parse_error"
        parsed.diagnostics.append(
            {"code": "PYTHON_PARSE_ERROR", "message": "Python facts could not be extracted."}
        )
    return parsed
