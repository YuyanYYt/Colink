"""Bounded, request-local Python/Java value flow over validated source text.

This module does not open a database, import project modules, or execute project
code. The caller supplies guarded source/fact callbacks and validates the read
context again after computation. Edges are static evidence, not runtime traces.
It is deliberately a may-flow analysis, not SSA, control-flow or alias analysis.
"""

from __future__ import annotations

import ast
import hashlib
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from code_context.dataflow_diagnostics import annotate_dataflow_result, diagnostic_details
from code_context.intelligence_models import MAX_FILE_NODES, MAX_PARSE_BYTES

_MODES = frozenset({"data_origin", "data_impact", "data_path", "combined"})
_MAX_WORK_NODES = 10_000
_MAX_WORK_EDGES = 30_000
_MAX_CONTEXTS = 100
_MAX_DIAGNOSTICS = 50
_MAX_RELATIONS = 5_000


class DataflowBudgetExceeded(RuntimeError):
    """A guarded fact/source callback exhausted the shared request deadline."""


class _WorkLimit(Exception):
    pass


@dataclass
class _Frame:
    context: str
    symbol: dict
    function: ast.FunctionDef | ast.AsyncFunctionDef
    depth: int
    stack: tuple[str, ...]
    env: dict[str, set[str]] = field(default_factory=dict)
    parameters: dict[str, str] = field(default_factory=dict)
    returns: list[str] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    directives: set[str] = field(default_factory=set)
    receiver: str | None = None
    conditional: bool = False
    generator: bool = False
    uncertain_flow: bool = False
    exit_reason: str | None = None


def _symbol_id(symbol: dict) -> str:
    value = symbol.get("symbol_id", symbol.get("id"))
    if not isinstance(value, str) or not value.startswith("sym_") or len(value) != 36:
        raise ValueError("invalid data-flow symbol identifier")
    return value


def _position(node: ast.AST) -> tuple[int, int]:
    return getattr(node, "lineno", 1), getattr(node, "col_offset", 0) + 1


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = node.args
    return [
        *args.posonlyargs,
        *args.args,
        *([args.vararg] if args.vararg else []),
        *args.kwonlyargs,
        *([args.kwarg] if args.kwarg else []),
    ]


def _constant_truth(node: ast.AST) -> bool | None:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    return None


def _loop_breaks(statements: list[ast.stmt]) -> bool:
    """Find break for this loop only, excluding nested loops and lexical scopes."""
    pending = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Break):
            return True
        if not isinstance(
            node,
            (
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
            ),
        ):
            pending.extend(ast.iter_child_nodes(node))
    return False


def _functions(tree: ast.AST, prefix: str = "") -> Iterable[tuple[str, ast.AST]]:
    """Match extractor qualnames without walking expression scopes as functions."""
    for statement in getattr(tree, "body", []):
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}.{statement.name}" if prefix else statement.name
            if not isinstance(statement, ast.ClassDef):
                yield name, statement
            yield from _functions(statement, name)
        else:
            # Functions may be conditionally defined, but their scope is unchanged.
            for field_name in ("body", "orelse", "finalbody", "handlers", "cases"):
                for child in getattr(statement, field_name, []):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        name = f"{prefix}.{child.name}" if prefix else child.name
                        if not isinstance(child, ast.ClassDef):
                            yield name, child
                        yield from _functions(child, name)
                    else:
                        yield from _functions(child, prefix)


class _Analysis:
    def __init__(
        self, load_symbol, load_source, load_relations, max_depth: int, max_ms: int, load_ast=None
    ):
        self.load_symbol: Callable[[str], dict] = load_symbol
        self.load_source: Callable[[str], str] = load_source
        self.load_relations: Callable[..., Iterable[dict]] = load_relations
        self.load_ast = load_ast
        self.max_depth = max_depth
        self.deadline = time.monotonic() + max_ms / 1000
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.edge_keys: set[tuple] = set()
        self.outgoing: dict[str, list[dict]] = defaultdict(list)
        self.incoming: dict[str, list[dict]] = defaultdict(list)
        self.frames: dict[str, _Frame] = {}
        self.trees: dict[str, ast.Module] = {}
        self.java_sources: dict[str, Any] = {}
        self.symbols: dict[str, dict] = {}
        self.diagnostics: list[dict] = []
        self.diagnostic_keys: set[tuple] = set()
        self.diagnostic_counts: Counter[str] = Counter()
        self.truncated_reasons: list[str] = []
        self.overrides: dict[tuple[str, int, int], str] = {}
        self.visited_incoming: set[tuple[str, str, int]] = set()

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            self.limit("max_ms")
        if len(self.nodes) >= _MAX_WORK_NODES or len(self.edges) >= _MAX_WORK_EDGES:
            self.limit("analysis_work_budget")

    def limit(self, reason: str) -> None:
        if reason not in self.truncated_reasons:
            self.truncated_reasons.append(reason)
        raise _WorkLimit

    def diagnostic(self, code: str, frame: _Frame | None = None, node=None, *, symbol=None) -> None:
        line, column = _position(node) if node is not None else (None, None)
        symbol = frame.symbol if frame else symbol
        path = symbol["path"] if symbol else None
        key = code, path, line, column
        if key in self.diagnostic_keys:
            return
        self.diagnostic_keys.add(key)
        self.diagnostic_counts[diagnostic_details(code)["category"]] += 1
        if len(self.diagnostics) < _MAX_DIAGNOSTICS:
            item = {"code": code, **diagnostic_details(code)}
            if symbol:
                item.update(path=path, symbol_id=_symbol_id(symbol))
            if node is not None:
                item.update(line=line, column=column)
            self.diagnostics.append(item)
        elif "diagnostics_budget" not in self.truncated_reasons:
            self.truncated_reasons.append("diagnostics_budget")

    def describe_result(self, result: dict, identifier: str) -> dict:
        symbol = self.symbols.get(identifier, {})
        language = symbol.get("language", "python" if symbol else "unknown")
        if "root" in self.frames:
            availability = "available"
        elif language not in {"python", "java", "unknown"}:
            availability = "unsupported_language"
        elif symbol.get("path", "").lower().endswith(".pyi"):
            availability = "unsupported_source"
        elif self.truncated_reasons or self.diagnostic_counts["resource_budget"]:
            availability = "budget_limited"
        else:
            availability = "source_unavailable"
        result["coverage"].update(requested_language=language, availability=availability)
        if language == "java":
            result["coverage"].update(
                frontend="java_tree_sitter_ir_v1",
                type_resolution="existing static bindings; no JVM or runtime dispatch proof",
            )
        return annotate_dataflow_result(result, diagnostic_counts=self.diagnostic_counts)

    def node(self, frame: _Frame, node: ast.AST, kind: str, name: str = "") -> str:
        self.check()
        line, column = _position(node)
        key = f"{frame.context}\0{line}\0{column}\0{kind}\0{name}"
        identifier = "df_" + hashlib.sha256(key.encode()).hexdigest()[:24]
        if identifier not in self.nodes:
            self.nodes[identifier] = {
                "id": identifier,
                "kind": kind,
                "name": name[:200],
                "path": frame.symbol["path"],
                "symbol_id": _symbol_id(frame.symbol),
                "context": frame.context,
                "line": line,
                "column": column,
            }
        return identifier

    def edge(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        resolution: str = "static",
        basis: str | None = None,
    ) -> None:
        self.check()
        if source == target:
            return
        if basis is None:
            symbol = self.symbols.get(self.nodes[source]["symbol_id"], {})
            basis = "java_tree_sitter_ir" if symbol.get("language") == "java" else "python_ast"
        key = source, target, kind, resolution, basis
        if key in self.edge_keys:
            return
        self.edge_keys.add(key)
        node = self.nodes[target]
        edge = {
            "source": source,
            "target": target,
            "kind": kind,
            "resolution": resolution,
            "evidence": {
                "path": node["path"],
                "line": node["line"],
                "column": node["column"],
                "basis": basis,
            },
        }
        self.edges.append(edge)
        self.outgoing[source].append(edge)
        self.incoming[target].append(edge)

    def symbol(self, identifier: str) -> dict:
        self.check()
        if identifier not in self.symbols:
            value = self.load_symbol(identifier)
            if _symbol_id(value) != identifier:
                raise ValueError("data-flow symbol callback returned a different symbol")
            self.symbols[identifier] = value
        self.check()
        return self.symbols[identifier]

    def function(self, symbol: dict):
        self.check()
        path = symbol["path"]
        if symbol.get("language") == "java" and path.lower().endswith(".java"):
            return self.java_function(symbol)
        if symbol.get("language", "python") != "python" or not path.lower().endswith(
            (".py", ".pyi")
        ):
            self.diagnostic("PYTHON_ONLY", symbol=symbol)
            return None
        if path.lower().endswith(".pyi"):
            self.diagnostic("PYTHON_STUB_UNSUPPORTED", symbol=symbol)
            return None
        if symbol.get("kind") not in {"function", "method", "constructor"}:
            self.diagnostic("CALLABLE_REQUIRED", symbol=symbol)
            return None
        if path not in self.trees:
            text = self.load_source(path)
            self.check()
            if not isinstance(text, str):
                raise ValueError("data-flow source callback must return source text")
            if len(text) > MAX_PARSE_BYTES or len(text.encode("utf-8")) > MAX_PARSE_BYTES:
                self.diagnostic("SOURCE_PARSE_BUDGET", symbol=symbol)
                return None
            try:
                tree = (
                    ast.parse(text, filename="<CoLink Python data flow>")
                    if self.load_ast is None
                    else self.load_ast(path, text, self.check)
                )
                count = 0
                for _ in ast.walk(tree):
                    count += 1
                    if count > MAX_FILE_NODES:
                        self.diagnostic("AST_NODE_BUDGET", symbol=symbol)
                        return None
                    if count % 256 == 0:
                        self.check()
            except (SyntaxError, ValueError, UnicodeError, RecursionError, MemoryError):
                self.diagnostic("PYTHON_PARSE_ERROR", symbol=symbol)
                return None
            self.trees[path] = tree
        matches = [
            node
            for qualname, node in _functions(self.trees[path])
            if qualname == symbol["qualname"] and node.lineno == symbol["start_line"]
        ]
        self.check()
        if len(matches) != 1:
            self.diagnostic("SYMBOL_SOURCE_MISMATCH", symbol=symbol)
            return None
        return matches[0]

    def java_function(self, symbol: dict):
        """Use guarded source callbacks and a request-local Java syntax adapter."""
        from code_context.java_dataflow_ast import parse_java_flow

        if symbol.get("kind") not in {"method", "constructor"}:
            self.diagnostic("CALLABLE_REQUIRED", symbol=symbol)
            return None
        path = symbol["path"]
        if path not in self.java_sources:
            text = self.load_source(path)
            self.check()
            if not isinstance(text, str):
                raise ValueError("data-flow source callback must return source text")
            if len(text) > MAX_PARSE_BYTES or len(text.encode("utf-8")) > MAX_PARSE_BYTES:
                self.diagnostic("SOURCE_PARSE_BUDGET", symbol=symbol)
                return None
            try:
                self.java_sources[path] = parse_java_flow(text, self.check)
            except (ValueError, UnicodeError, RecursionError, MemoryError):
                self.diagnostic("JAVA_PARSE_ERROR", symbol=symbol)
                return None
        source = self.java_sources[path]
        function = source.function(symbol)
        for code, node in source.diagnostics:
            self.diagnostic(code, node=node, symbol=symbol)
        self.check()
        if function is None:
            self.diagnostic("JAVA_BODY_UNAVAILABLE", symbol=symbol)
        return function

    def frame(self, identifier: str, context: str, depth: int, stack: tuple[str, ...]):
        self.check()
        if context in self.frames:
            return self.frames[context]
        if len(self.frames) >= _MAX_CONTEXTS:
            self.limit("analysis_context_budget")
        symbol = self.symbol(identifier)
        function = self.function(symbol)
        if function is None:
            return None
        frame = _Frame(context, symbol, function, depth, (*stack, identifier))
        self.frames[context] = frame
        for relation in self.load_relations(identifier, direction="outgoing"):
            self.check()
            if len(frame.relations) >= _MAX_RELATIONS:
                self.diagnostic("RELATION_FACTS_PARTIAL", frame, function)
                break
            frame.relations.append(relation)
        self.check()
        for parameter in _parameters(function):
            value = self.node(frame, parameter, "parameter", parameter.arg)
            frame.env[parameter.arg] = {value}
            frame.parameters[parameter.arg] = value
        positional = [*function.args.posonlyargs, *function.args.args]
        static = any(
            isinstance(d, ast.Name) and d.id == "staticmethod" for d in function.decorator_list
        )
        if symbol.get("language") == "java":
            frame.receiver = getattr(function, "_java_receiver", None)
            if frame.receiver:
                frame.env[frame.receiver] = {self.node(frame, function, "receiver", frame.receiver)}
            for code, node in getattr(function, "_java_diagnostics", ()):
                self.diagnostic(code, frame, node)
                if code != "JAVA_BRANCH_PARTIAL":
                    frame.uncertain_flow = True
        elif symbol.get("kind") == "method" and positional and not static:
            frame.receiver = positional[0].arg
        if isinstance(function, ast.AsyncFunctionDef):
            self.diagnostic("ASYNC_SEMANTICS_PARTIAL", frame, function)
        if any(
            not (isinstance(d, ast.Name) and d.id == "staticmethod")
            for d in function.decorator_list
        ):
            self.diagnostic("DECORATED_CALLABLE_PARTIAL", frame, function)
        # Collect directives while avoiding nested lexical scopes.
        pending = list(function.body)
        while pending:
            statement = pending.pop()
            self.check()
            if isinstance(statement, (ast.Global, ast.Nonlocal)):
                frame.directives.update(statement.names)
                self.diagnostic("GLOBAL_OR_NONLOCAL_PARTIAL", frame, statement)
            elif isinstance(statement, (ast.Yield, ast.YieldFrom)):
                frame.generator = True
                self.diagnostic("ASYNC_OR_GENERATOR_PARTIAL", frame, statement)
            elif not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                pending.extend(ast.iter_child_nodes(statement))
        alive = self.block(frame, function.body)
        if alive and not frame.generator:
            implicit = self.node(
                frame,
                function,
                "return",
                "implicit_void" if symbol.get("language") == "java" else "implicit_none",
            )
            frame.returns.append(implicit)
        return frame

    def resolution(self, frame: _Frame) -> str:
        if frame.uncertain_flow:
            return "unknown"
        return "possible" if frame.conditional else "static"

    def read(self, frame: _Frame, node: ast.AST, name: str, *, field_read=False) -> str:
        kind = "field_read" if field_read else "read"
        value = self.node(frame, node, kind, name)
        definitions = frame.env.get(name, set()) if name not in frame.directives else set()
        if definitions:
            for definition in sorted(definitions):
                self.edge(
                    definition,
                    value,
                    "DEF_USE",
                    resolution="possible" if len(definitions) > 1 else self.resolution(frame),
                )
        else:
            self.diagnostic(
                "UNKNOWN_FIELD" if field_read else "EXTERNAL_OR_UNBOUND_NAME", frame, node
            )
        return value

    def expression(self, frame: _Frame, node: ast.AST | None) -> str:
        self.check()
        if node is None:
            return self.node(frame, frame.function, "literal", "None")
        if isinstance(node, ast.Name):
            return self.read(frame, node, node.id)
        if isinstance(node, ast.Constant):
            # Do not copy source literals (which may be credentials) into diagnostics/results.
            return self.node(frame, node, "literal", type(node.value).__name__)
        if isinstance(node, ast.Call):
            return self.call(frame, node)
        if isinstance(node, ast.NamedExpr):
            value = self.expression(frame, node.value)
            self.assign(frame, node.target, value)
            return value
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == frame.receiver:
                return self.read(frame, node, f"{frame.receiver}.{node.attr}", field_read=True)
            result = self.node(frame, node, "field_read", node.attr)
            self.edge(self.expression(frame, node.value), result, "TRANSFORM", resolution="unknown")
            self.diagnostic("ATTRIBUTE_ALIAS_PARTIAL", frame, node)
            return result
        if isinstance(node, ast.Subscript):
            # A container's aggregate provenance does not prove the selected
            # element's provenance, nor the semantics of user __getitem__.
            result = self.node(frame, node, "expression", "Subscript")
            self.diagnostic("SUBSCRIPT_SELECTION_PARTIAL", frame, node)
            children = [node.value]
            if isinstance(node.slice, ast.Slice):
                children.extend(
                    value
                    for value in (node.slice.lower, node.slice.upper, node.slice.step)
                    if value is not None
                )
            else:
                children.append(node.slice)
            for value in children:
                self.edge(
                    self.expression(frame, value),
                    result,
                    "TRANSFORM",
                    resolution="unknown",
                    basis="unknown_container_projection",
                )
            return result
        if isinstance(
            node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            result = self.node(frame, node, "unknown", type(node).__name__)
            self.diagnostic("EXPRESSION_SCOPE_PARTIAL", frame, node)
            return result
        if isinstance(node, ast.IfExp):
            self.expression(frame, node.test)  # Control dependence is not a value-flow edge.
            result = self.node(frame, node, "expression", "IfExp")
            previous = frame.conditional
            frame.conditional = True
            base = {name: set(values) for name, values in frame.env.items()}
            states = []
            for branch in (node.body, node.orelse):
                frame.env = {name: set(values) for name, values in base.items()}
                self.edge(
                    self.expression(frame, branch), result, "TRANSFORM", resolution="possible"
                )
                states.append(frame.env)
            frame.conditional = previous
            self.merge_states(frame, states)
            return result
        if isinstance(node, ast.BoolOp):
            result = self.node(frame, node, "expression", "BoolOp")
            previous = frame.conditional
            states = []
            for index, value in enumerate(node.values):
                if index:
                    frame.conditional = True
                self.edge(self.expression(frame, value), result, "TRANSFORM", resolution="possible")
                states.append({name: set(values) for name, values in frame.env.items()})
            frame.conditional = previous
            self.merge_states(frame, states)
            return result
        result = self.node(frame, node, "expression", type(node).__name__)
        python_async = isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom))
        unknown = python_async or getattr(node, "_java_unknown", False)
        if python_async:
            self.diagnostic("ASYNC_OR_GENERATOR_PARTIAL", frame, node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.edge(
                    self.expression(frame, child),
                    result,
                    "TRANSFORM",
                    resolution="unknown" if unknown else self.resolution(frame),
                )
        return result

    def assign(self, frame: _Frame, target: ast.AST, value: str) -> None:
        self.check()
        if isinstance(target, ast.Name):
            name = target.id
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == frame.receiver
        ):
            name = f"{frame.receiver}.{target.attr}"
        elif isinstance(target, (ast.Tuple, ast.List)):
            # Without type/shape proof each component may originate in the RHS aggregate.
            for element in target.elts:
                projection = self.node(frame, element, "expression", "unpack")
                self.edge(
                    value,
                    projection,
                    "TRANSFORM",
                    resolution="unknown",
                    basis="unknown_aggregate_projection",
                )
                self.assign(frame, element, projection)
            self.diagnostic("DESTRUCTURING_PARTIAL", frame, target)
            return
        elif isinstance(target, ast.Subscript):
            self.mutate_container(frame, target, value)
            return
        else:
            self.diagnostic("MUTATION_ALIAS_PARTIAL", frame, target)
            return
        definition = self.node(frame, target, "field_write" if "." in name else "definition", name)
        self.edge(value, definition, "ASSIGN", resolution=self.resolution(frame))
        if name not in frame.directives:
            frame.env[name] = {definition}  # Reassignment kills the preceding local definition.
        if isinstance(target, ast.Name) and target.id == frame.receiver:
            self.clear_fields(frame, target)
            frame.receiver = None
            self.diagnostic("RECEIVER_ALIAS_PARTIAL", frame, target)

    def clear_fields(self, frame: _Frame, node: ast.AST) -> None:
        fields = [name for name in frame.env if "." in name]
        for name in fields:
            frame.env.pop(name, None)
        if fields:
            self.diagnostic("CALL_SIDE_EFFECTS_PARTIAL", frame, node)

    def mutate_container(self, frame: _Frame, target: ast.Subscript, value=None) -> None:
        self.diagnostic("MUTATION_ALIAS_PARTIAL", frame, target)
        base = target.value
        while isinstance(base, ast.Subscript):
            base = base.value
        name = base.id if isinstance(base, ast.Name) else None
        if (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id == frame.receiver
        ):
            name = f"{frame.receiver}.{base.attr}"
        if name is None:
            return
        mutation = self.node(frame, target, "unknown", "container_mutation")
        for definition in sorted(frame.env.get(name, ())):
            self.edge(
                definition,
                mutation,
                "TRANSFORM",
                resolution="unknown",
                basis="unknown_container_mutation",
            )
        if value is not None:
            self.edge(
                value,
                mutation,
                "TRANSFORM",
                resolution="unknown",
                basis="unknown_container_mutation",
            )
        if name not in frame.directives:
            frame.env[name] = {mutation}

    def merge_states(self, frame: _Frame, states: list[dict[str, set[str]]]) -> None:
        frame.env = {}
        for state in states:
            for name, values in state.items():
                frame.env.setdefault(name, set()).update(values)
        if states and any(any(name not in state for state in states) for name in frame.env):
            self.diagnostic("BRANCH_UNBOUND_VALUE_PARTIAL", frame, frame.function)

    def block(self, frame: _Frame, statements: list[ast.stmt]) -> bool:
        frame.exit_reason = None
        for statement in statements:
            self.check()
            if not self.statement(frame, statement):
                return False
        return True

    def branches(self, frame: _Frame, branches: list[list[ast.stmt]]) -> bool:
        base = {name: set(values) for name, values in frame.env.items()}
        previous = frame.conditional
        frame.conditional = True
        states = []
        exits = []
        for branch in branches:
            frame.env = {name: set(values) for name, values in base.items()}
            if self.block(frame, branch):
                states.append(frame.env)
            else:
                exits.append(frame.exit_reason)
        frame.conditional = previous
        self.merge_states(frame, states)
        frame.exit_reason = None if states else (exits[0] if len(set(exits)) == 1 else "mixed")
        return bool(states)

    def statement(self, frame: _Frame, node: ast.stmt) -> bool:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is not None:
                value = self.expression(frame, node.value)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    self.assign(frame, target, value)
        elif isinstance(node, ast.AugAssign):
            # An augmented assignment reads the old definition before publishing the new one.
            previous = self.expression(frame, node.target)
            value = self.expression(frame, node.value)
            result = self.node(frame, node, "expression", "AugAssign")
            for source in (previous, value):
                self.edge(source, result, "TRANSFORM", resolution=self.resolution(frame))
            self.assign(frame, node.target, result)
        elif isinstance(node, ast.Return):
            value = self.expression(frame, node.value)
            result = self.node(frame, node, "return", "return")
            self.edge(value, result, "RETURN_FLOW", resolution=self.resolution(frame))
            frame.returns.append(result)
            frame.exit_reason = "return"
            return False
        elif isinstance(node, ast.Expr):
            self.expression(frame, node.value)
        elif isinstance(node, ast.If):
            self.expression(frame, node.test)
            truth = _constant_truth(node.test)
            if truth is not None:
                return self.block(frame, node.body if truth else node.orelse)
            return self.branches(frame, [node.body, node.orelse])
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            return self.loop(frame, node)
        elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            return self.exception_block(frame, node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            self.diagnostic("CONTEXT_MANAGER_PARTIAL", frame, node)
            for item in node.items:
                value = self.expression(frame, item.context_expr)
                if item.optional_vars:
                    self.assign(frame, item.optional_vars, value)
            return self.block(frame, node.body)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    frame.env.pop(target.id, None)
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == frame.receiver
                ):
                    frame.env.pop(f"{frame.receiver}.{target.attr}", None)
                elif isinstance(target, ast.Subscript):
                    self.mutate_container(frame, target)
                else:
                    self.diagnostic("MUTATION_ALIAS_PARTIAL", frame, target)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A nested scope has its own parameters; never accidentally read its body in ours.
            self.diagnostic("NESTED_SCOPE_PARTIAL", frame, node)
            value = self.node(frame, node, "unknown", node.name)
            self.nodes[value]["callable_qualname"] = frame.symbol["qualname"] + "." + node.name
            self.nodes[value]["conditional_definition"] = frame.conditional or frame.uncertain_flow
            frame.env[node.name] = {value}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.diagnostic("IMPORT_VALUE_PARTIAL", frame, node)
            for alias in node.names:
                name = alias.asname or (
                    alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                )
                if name != "*":
                    frame.env[name] = {self.node(frame, node, "unknown", name)}
        elif isinstance(node, ast.Assert):
            self.expression(frame, node.test)
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.Pass)):
            pass
        elif isinstance(node, ast.Raise):
            self.diagnostic("EXCEPTION_FLOW_PARTIAL", frame, node)
            if node.exc:
                self.expression(frame, node.exc)
            frame.exit_reason = "raise"
            return False
        elif isinstance(node, (ast.Break, ast.Continue)):
            self.diagnostic("LOOP_FIXED_POINT_PARTIAL", frame, node)
            frame.exit_reason = "break" if isinstance(node, ast.Break) else "continue"
            return False
        else:
            self.diagnostic("STATEMENT_PARTIAL", frame, node)
            frame.uncertain_flow = True
        return True

    def loop(self, frame: _Frame, node: ast.For | ast.AsyncFor | ast.While) -> bool:
        self.diagnostic("LOOP_FIXED_POINT_PARTIAL", frame, node)
        # Unknown iteration/fixed points must never upgrade an edge to static.
        frame.uncertain_flow = True
        truth = _constant_truth(node.test) if isinstance(node, ast.While) else None
        if isinstance(node, ast.While):
            self.expression(frame, node.test)
            empty = truth is False
            guaranteed_entry = truth is True
        else:
            value = self.expression(frame, node.iter)
            literal_sequence = isinstance(node.iter, (ast.List, ast.Tuple, ast.Set))
            empty = literal_sequence and not node.iter.elts
            guaranteed_entry = (
                literal_sequence
                and bool(node.iter.elts)
                and not any(isinstance(item, ast.Starred) for item in node.iter.elts)
            )
        if empty:
            return self.block(frame, node.orelse)
        base = {name: set(values) for name, values in frame.env.items()}
        if not isinstance(node, ast.While):
            self.assign(frame, node.target, value)
        alive = self.block(frame, node.body)
        reason = frame.exit_reason
        body = {name: set(values) for name, values in frame.env.items()}
        if guaranteed_entry and not alive:
            if reason in {"return", "raise"}:
                return False
            if reason == "break":
                frame.exit_reason = None
                return True  # A break skips the loop's else.
        states = [base]
        if alive or reason in {"break", "continue"}:
            states.append(body)
        self.merge_states(frame, states)
        frame.exit_reason = None
        can_break = _loop_breaks(node.body)
        if truth is True:
            if not can_break:
                frame.exit_reason = "nonterminating"
                return False
            return True  # while True can fall through only by breaking, never via else.
        if can_break:
            return self.branches(frame, [node.orelse, []])
        return self.block(frame, node.orelse)

    def exception_block(self, frame: _Frame, node: ast.Try) -> bool:
        self.diagnostic("EXCEPTION_FLOW_PARTIAL", frame, node)
        frame.uncertain_flow = True
        base = {name: set(values) for name, values in frame.env.items()}
        returns_before = len(frame.returns)
        states, alive_states, exits = [], [], []
        body_alive = self.block(frame, node.body)
        if body_alive:
            body_alive = self.block(frame, node.orelse)
        states.append(frame.env)
        if body_alive:
            alive_states.append(frame.env)
        else:
            exits.append(frame.exit_reason)
        for handler in node.handlers:
            frame.env = {name: set(values) for name, values in base.items()}
            if self.block(frame, handler.body):
                alive_states.append(frame.env)
            else:
                exits.append(frame.exit_reason)
            states.append(frame.env)
        self.merge_states(frame, states)
        returns_before_finally = len(frame.returns)
        final_alive = self.block(frame, node.finalbody)
        final_exit = frame.exit_reason
        if not final_alive:
            if final_exit in {"return", "raise", "nonterminating"}:
                # A guaranteed terminating finally replaces pending returns.
                frame.returns = (
                    frame.returns[:returns_before] + frame.returns[returns_before_finally:]
                )
            return False
        if not alive_states:
            frame.exit_reason = exits[0] if len(set(exits)) == 1 else "mixed"
            return False
        frame.exit_reason = None
        return True

    def unknown_call(
        self, frame: _Frame, node: ast.Call, result: str, values: list[str], code: str
    ):
        self.diagnostic(code, frame, node)
        if isinstance(node.func, ast.Name) and node.func.id in frame.env:
            values = [*values, self.expression(frame, node.func)]
        for value in values:
            self.edge(
                value, result, "TRANSFORM", resolution="unknown", basis="unknown_call_may_flow"
            )
        self.clear_fields(frame, node)

    def call(self, frame: _Frame, node: ast.Call) -> str:
        result = self.node(frame, node, "call_result", "call")
        arguments = [
            self.expression(frame, arg.value if isinstance(arg, ast.Starred) else arg)
            for arg in node.args
        ]
        keywords = [
            (keyword.arg, self.expression(frame, keyword.value)) for keyword in node.keywords
        ]
        java = frame.symbol.get("language") == "java"
        line, column = (
            getattr(node.func, "_java_relation_line", _position(node.func)[0]),
            getattr(node.func, "_java_relation_column", _position(node.func)[1]),
        )
        candidates = [
            relation
            for relation in frame.relations
            if relation.get("kind") in {"CALL", "INSTANTIATION"}
            and relation.get("line") == line
            and relation.get("column", 1) == column
        ]
        targets = {
            r["target_symbol_id"]
            for r in candidates
            if r.get("resolution") == "resolved" and r.get("target_symbol_id")
        }
        target_symbol, target_function = None, None
        if (
            java
            and len(targets) == 1
            and all(relation.get("resolution") == "resolved" for relation in candidates)
        ):
            target_symbol = self.symbol(next(iter(targets)))
            target_function = self.function(target_symbol)
        java_static = java and getattr(target_function, "_java_static", False)
        qualifier = node.func.value if isinstance(node.func, ast.Attribute) else None
        type_qualifier = False
        if java_static and qualifier is not None:
            # A matching declared owner with no local/field binding is a type
            # qualifier. Objects may also legally qualify Java static calls.
            parts, current = [], qualifier
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
                name = ".".join(reversed(parts))
                owner = target_symbol["qualname"].rsplit(".", 1)[0]
                fields = getattr(frame.function, "_java_fields", ())
                type_qualifier = (
                    current.id not in frame.env
                    and current.id not in fields
                    and (name == owner or name.endswith("." + owner))
                )
        receiver_value = (
            self.expression(frame, qualifier)
            if qualifier is not None and not type_qualifier
            else None
        )
        receiver = None if java_static else receiver_value
        values = [
            *arguments,
            *[value for _, value in keywords],
            *([receiver] if receiver else []),
        ]
        unsupported = getattr(node, "_java_unsupported_call", None)
        if unsupported:
            self.unknown_call(frame, node, result, values, unsupported)
            return result
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            k is None for k, _ in keywords
        ):
            self.unknown_call(frame, node, result, values, "EXPANDED_ARGUMENTS_PARTIAL")
            return result
        if len(targets) != 1 or any(r.get("resolution") != "resolved" for r in candidates):
            self.unknown_call(frame, node, result, values, "CALL_TARGET_UNKNOWN_OR_AMBIGUOUS")
            return result
        identifier = next(iter(targets))
        target_symbol = target_symbol or self.symbol(identifier)
        if java and not java_static:
            # A lexical target does not prove JVM dispatch. Keep an unknown
            # alternative even when the nominal method returns a constant.
            self.diagnostic("JAVA_DISPATCH_PARTIAL", frame, node)
            frame.uncertain_flow = True
            for value in values:
                self.edge(
                    value,
                    result,
                    "TRANSFORM",
                    resolution="unknown",
                    basis="java_dynamic_dispatch_may_flow",
                )
        if (
            isinstance(node.func, ast.Name)
            and target_symbol.get("scope") == frame.symbol["qualname"]
        ):
            definitions = [self.nodes[value] for value in frame.env.get(node.func.id, ())]
            bound = [
                value
                for value in definitions
                if value.get("callable_qualname") == target_symbol["qualname"]
                and value["line"] == target_symbol["start_line"]
            ]
            if len(bound) != len(definitions) or not bound:
                self.unknown_call(frame, node, result, values, "LOCAL_CALLABLE_NOT_BOUND")
                return result
            if any(value.get("conditional_definition") for value in bound):
                frame.uncertain_flow = True
                self.diagnostic("LOCAL_CALLABLE_CONDITIONAL_PARTIAL", frame, node)
        override = self.overrides.get((frame.context, line, column))
        if override:
            callee = self.frames[override]
        elif identifier in frame.stack:
            self.unknown_call(frame, node, result, values, "RECURSIVE_CALL_PARTIAL")
            return result
        elif frame.depth >= self.max_depth:
            self.unknown_call(frame, node, result, values, "CALL_DEPTH_PARTIAL")
            return result
        else:
            context = f"{frame.context}/call:{line}:{column}"
            callee = self.frame(identifier, context, frame.depth + 1, frame.stack)
        if callee is None:
            self.unknown_call(frame, node, result, values, "CALLEE_SOURCE_UNSUPPORTED")
            return result
        if callee.function.decorator_list and any(
            not (isinstance(d, ast.Name) and d.id == "staticmethod")
            for d in callee.function.decorator_list
        ):
            self.unknown_call(frame, node, result, values, "DECORATED_CALLABLE_PARTIAL")
            return result
        if isinstance(callee.function, ast.AsyncFunctionDef):
            self.unknown_call(frame, node, result, values, "ASYNC_SEMANTICS_PARTIAL")
            return result
        if callee.generator:
            self.unknown_call(frame, node, result, values, "ASYNC_OR_GENERATOR_PARTIAL")
            return result
        if not self.bind(frame, callee, node, arguments, keywords, receiver):
            self.unknown_call(frame, node, result, values, "ARGUMENT_BINDING_PARTIAL")
            return result
        for returned in callee.returns:
            self.edge(
                returned,
                result,
                "RETURN_TO_CALLSITE",
                resolution=self.resolution(frame),
                basis="resolved_static_callsite",
            )
        if receiver:
            self.diagnostic("CALL_SIDE_EFFECTS_PARTIAL", frame, node)
        self.clear_fields(frame, node)
        return result

    def bind(self, caller, callee, call, positional_values, keyword_values, receiver) -> bool:
        args = callee.function.args
        positional = [*args.posonlyargs, *args.args]
        values = list(positional_values)
        # Only an attribute call with a known method receiver consumes the implicit first arg.
        java = callee.symbol.get("language") == "java"
        if java and callee.receiver:
            if receiver is None and not isinstance(call.func, ast.Attribute) and caller.receiver:
                receiver = next(iter(caller.env.get(caller.receiver, ())), None)
            if receiver is None:
                return False
            for receiver_node in callee.env.get(callee.receiver, ()):
                self.edge(
                    receiver,
                    receiver_node,
                    "RECEIVER_BIND",
                    resolution="unknown",
                    basis="java_receiver_binding",
                )
        elif callee.receiver and isinstance(call.func, ast.Attribute):
            if receiver is None:
                return False
            values.insert(0, receiver)
        bindings: dict[str, str] = {}
        if len(values) > len(positional) and args.vararg is None:
            return False
        for parameter, value in zip(positional, values, strict=False):
            bindings[parameter.arg] = value
        extras = values[len(positional) :]
        for name, value in keyword_values:
            if name in bindings or name in {arg.arg for arg in args.posonlyargs}:
                return False
            if name not in callee.parameters or name in {
                getattr(args.vararg, "arg", None),
                getattr(args.kwarg, "arg", None),
            }:
                if args.kwarg is None:
                    return False
                # Exact keys are known, but dictionary alias/lookup semantics are outside this MVP.
                self.diagnostic("VARIADIC_PARAMETERS_PARTIAL", caller, call)
                continue
            bindings[name] = value
        default_start = len(positional) - len(args.defaults)
        defaults = {
            parameter.arg: default
            for parameter, default in zip(positional[default_start:], args.defaults, strict=True)
        }
        defaults.update(
            {
                parameter.arg: default
                for parameter, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                if default is not None
            }
        )
        required = [*positional, *args.kwonlyargs]
        for parameter in required:
            if parameter.arg not in bindings:
                default = defaults.get(parameter.arg)
                if default is None:
                    return False
                if not isinstance(default, ast.Constant):
                    self.diagnostic("DEFAULT_DEFINITION_SCOPE_PARTIAL", caller, call)
                    return False
                bindings[parameter.arg] = self.node(
                    callee, default, "literal", type(default.value).__name__
                )
        for name, value in bindings.items():
            self.edge(
                value,
                callee.parameters[name],
                "ARG_TO_PARAM",
                resolution=self.resolution(caller),
                basis="resolved_static_callsite",
            )
        if args.vararg:
            for value in extras:
                self.edge(
                    value,
                    callee.parameters[args.vararg.arg],
                    "ARG_TO_PARAM",
                    resolution="possible",
                    basis="variadic_aggregate",
                )
            self.diagnostic("VARIADIC_PARAMETERS_PARTIAL", caller, call)
        if args.kwarg:
            self.diagnostic("VARIADIC_PARAMETERS_PARTIAL", caller, call)
        return True

    def expand_incoming(self, frame: _Frame, depth: int = 0) -> None:
        if depth >= self.max_depth:
            return
        self.check()
        relations = self.load_relations(_symbol_id(frame.symbol), direction="incoming")
        for relation in relations:
            self.check()
            caller_id = relation.get("source_symbol_id")
            if (
                relation.get("kind") not in {"CALL", "INSTANTIATION"}
                or relation.get("resolution") != "resolved"
                or not caller_id
            ):
                continue
            if caller_id == _symbol_id(frame.symbol):
                self.diagnostic("RECURSIVE_CALL_PARTIAL", frame, frame.function)
                continue
            line, column = relation["line"], relation.get("column", 1)
            key = frame.context, caller_id, line * 1_000_000 + column
            if key in self.visited_incoming:
                continue
            self.visited_incoming.add(key)
            context = f"{frame.context}/caller:{caller_id}:{line}:{column}"
            self.overrides[context, line, column] = frame.context
            # Caller traversal reserves the remaining interprocedural depth.
            # Incoming traversal is not the caller's execution stack. Keeping the
            # callee there would mislabel other calls on that line as recursion.
            caller = self.frame(caller_id, context, depth + 1, ())
            if caller:
                self.expand_incoming(caller, depth + 1)

    def select(self, selector: dict, *, context: str | None = None) -> list[str]:
        identifier = selector.get("symbol_id")
        if not isinstance(identifier, str):
            raise ValueError("data-flow selector requires a resolved symbol_id")
        value = selector.get("value")
        if not isinstance(value, dict):
            raise ValueError("data-flow selector requires value")
        kind = value.get("kind")
        if kind not in {"parameter", "return", "occurrence"}:
            raise ValueError("value kind must be parameter, return, or occurrence")
        if kind == "parameter" and not isinstance(value.get("name"), str):
            raise ValueError("parameter selector requires name")
        if kind == "occurrence" and not all(
            type(value.get(key)) is int and value[key] >= 1 for key in ("line", "column")
        ):
            raise ValueError("occurrence selector requires positive line and UTF-8 column")
        for key in ("line", "column"):
            if key in value and (type(value[key]) is not int or value[key] < 1):
                raise ValueError("value location must be positive")
        matches = []
        for node in self.nodes.values():
            if node["symbol_id"] != identifier or (
                context is not None and node["context"] != context
            ):
                continue
            if kind == "parameter" and (
                node["kind"] != "parameter" or node["name"] != value["name"]
            ):
                continue
            if kind == "return" and node["kind"] != "return":
                continue
            if "line" in value and node["line"] != value["line"]:
                continue
            if "column" in value and node["column"] != value["column"]:
                continue
            if kind == "occurrence" and node["kind"] not in {
                "read",
                "definition",
                "field_read",
                "field_write",
                "call_result",
                "expression",
                "literal",
            }:
                continue
            matches.append(node["id"])
        return matches

    def walk(self, starts: list[str], *, backward: bool, max_nodes: int, max_edges: int):
        nodes = dict.fromkeys(starts)
        edges, edge_keys = [], set()
        queue = deque(starts)
        while queue:
            if not self.output_time_available():
                break
            identifier = queue.popleft()
            for edge in (self.incoming if backward else self.outgoing).get(identifier, []):
                target = edge["source"] if backward else edge["target"]
                key = edge["source"], edge["target"], edge["kind"]
                if key in edge_keys:
                    continue
                if len(edges) >= max_edges:
                    if "max_edges" not in self.truncated_reasons:
                        self.truncated_reasons.append("max_edges")
                    continue
                if target not in nodes:
                    if len(nodes) >= max_nodes:
                        if "max_nodes" not in self.truncated_reasons:
                            self.truncated_reasons.append("max_nodes")
                        continue
                    nodes[target] = None
                    queue.append(target)
                edge_keys.add(key)
                edges.append(edge)
        return list(nodes), edges

    def output_time_available(self) -> bool:
        if time.monotonic() < self.deadline:
            return True
        if "max_ms" not in self.truncated_reasons:
            self.truncated_reasons.append("max_ms")
        return False

    def path(self, source: str, targets: set[str], max_nodes: int, max_edges: int):
        queue = deque([source])
        previous: dict[str, tuple[str, dict] | None] = {source: None}
        found = source if source in targets else None
        while queue and found is None:
            if not self.output_time_available():
                return [], []
            value = queue.popleft()
            for edge in self.outgoing.get(value, []):
                target = edge["target"]
                if target in previous:
                    continue
                previous[target] = (value, edge)
                if target in targets:
                    found = target
                    break
                queue.append(target)
        if found is None:
            return [], []
        nodes, edges = [found], []
        while previous[found] is not None:
            parent, edge = previous[found]
            edges.append(edge)
            nodes.append(parent)
            found = parent
        nodes.reverse()
        edges.reverse()
        if len(nodes) > max_nodes or len(edges) > max_edges:
            self.truncated_reasons.append("path_output_budget")
            return [], []
        return nodes, edges


def analyze_dataflow(
    *,
    mode: str,
    selector: dict,
    target: dict | None = None,
    load_symbol: Callable[[str], dict],
    load_source: Callable[[str], str],
    load_relations: Callable[..., Iterable[dict]],
    load_ast: Callable[[str, str, Callable[[], None]], ast.Module] | None = None,
    max_depth: int = 1,
    max_nodes: int = 50,
    max_edges: int = 100,
    max_ms: int = 3000,
) -> dict[str, Any]:
    """Analyze selected values using guarded, per-request callbacks.

    ``load_relations(symbol_id, direction=...)`` supplies actual CALL/INSTANTIATION
    locations and targets; no name-based guessing is used. ``selector`` and
    ``target`` require ``symbol_id`` plus ``value`` with a parameter name, a
    return statement location, or an exact AST occurrence (1-based UTF-8 column).
    A missing/ambiguous selection is returned as candidates, never silently chosen.
    Output traversal is bounded separately from a fixed request-local work cap.
    """
    if mode not in _MODES:
        raise ValueError("unsupported data-flow mode")
    for value, minimum, maximum, name in (
        (max_depth, 0, 5, "max_depth"),
        (max_nodes, 1, 200, "max_nodes"),
        (max_edges, 1, 500, "max_edges"),
        (max_ms, 1, 30_000, "max_ms"),
    ):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"invalid {name}")
    if mode == "data_path" and target is None:
        raise ValueError("data_path requires target")
    identifier = selector.get("symbol_id")
    if not isinstance(identifier, str):
        raise ValueError("data-flow selector requires a resolved symbol_id")
    analysis = _Analysis(load_symbol, load_source, load_relations, max_depth, max_ms, load_ast)
    frame = None
    try:
        frame = analysis.frame(identifier, "root", 0, ())
        if frame is not None:
            analysis.expand_incoming(frame)
    except _WorkLimit:
        pass
    except DataflowBudgetExceeded:
        analysis.truncated_reasons.append("time_budget")
    except (RecursionError, MemoryError):
        analysis.diagnostic("ANALYZER_RESOURCE_LIMIT")
        analysis.truncated_reasons.append("analysis_work_budget")
    roots = analysis.select(selector, context="root")
    result: dict[str, Any] = {
        "mode": mode,
        "nodes": [],
        "edges": [],
        "root": roots[0] if len(roots) == 1 else None,
        "analysis_status": "partial"
        if analysis.diagnostics or analysis.truncated_reasons
        else "complete",
        "coverage": {
            "language": analysis.symbols.get(identifier, {}).get("language", "python"),
            "semantics": "static_may_flow",
            "runtime_trace": False,
            "alias_analysis": "same-method receiver fields only",
            "cross_call_depth": max_depth,
            "depth_semantics": "maximum call distance from the selected method",
        },
        "diagnostics": analysis.diagnostics,
        "truncated": bool(analysis.truncated_reasons),
        "truncated_reasons": analysis.truncated_reasons,
    }
    if len(roots) != 1:
        result["analysis_status"] = "selection_ambiguous" if roots else "selection_not_found"
        if not roots and analysis.truncated_reasons:
            result["analysis_status"] = "partial"
            result["selection_status"] = "selection_not_computed"
        result["candidates"] = [analysis.nodes[identifier] for identifier in roots[:max_nodes]]
        result["candidate_total"] = len(roots)
        result["candidates_truncated"] = len(roots) > max_nodes
        return analysis.describe_result(result, identifier)
    if mode == "data_path":
        targets = analysis.select(target)
        if not targets:
            result["analysis_status"] = "target_not_found"
            return analysis.describe_result(result, identifier)
        # Multiple contexts for one exact target occurrence are expected for different calls.
        # Multiple source occurrences, however, must be narrowed explicitly.
        locations = {
            (analysis.nodes[n]["line"], analysis.nodes[n]["column"], analysis.nodes[n]["kind"])
            for n in targets
        }
        if len(locations) != 1:
            result["analysis_status"] = "selection_ambiguous"
            result["candidates"] = [analysis.nodes[n] for n in targets[:max_nodes]]
            return analysis.describe_result(result, identifier)
        node_ids, edges = analysis.path(roots[0], set(targets), max_nodes, max_edges)
        result["target"] = node_ids[-1] if node_ids else None
        result["paths"] = [node_ids] if node_ids else []
        result["path_found"] = bool(node_ids)
        result["path_resolution"] = (
            "unknown"
            if any(edge["resolution"] == "unknown" for edge in edges)
            else "possible"
            if any(edge["resolution"] == "possible" for edge in edges)
            else "static"
            if node_ids
            else None
        )
        if not node_ids:
            result["no_path_claim"] = (
                "no path found in the analyzed static slice; runtime reachability is unknown"
            )
    elif mode == "combined":
        backward, back_edges = analysis.walk(
            roots, backward=True, max_nodes=max_nodes, max_edges=max_edges
        )
        forward, forward_edges = analysis.walk(
            roots, backward=False, max_nodes=max_nodes, max_edges=max_edges
        )
        all_ids = list(dict.fromkeys([*backward, *forward]))
        node_ids = all_ids[:max_nodes]
        if len(all_ids) > max_nodes:
            analysis.truncated_reasons.append("max_nodes")
        selected = set(node_ids)
        edges, keys = [], set()
        for edge in [*back_edges, *forward_edges]:
            key = edge["source"], edge["target"], edge["kind"]
            if edge["source"] in selected and edge["target"] in selected and key not in keys:
                keys.add(key)
                edges.append(edge)
        if len(edges) > max_edges:
            analysis.truncated_reasons.append("max_edges")
            edges = edges[:max_edges]
    else:
        node_ids, edges = analysis.walk(
            roots,
            backward=mode == "data_origin",
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    result["nodes"] = [analysis.nodes[node] for node in node_ids]
    result["edges"] = edges
    result["truncated_reasons"] = list(dict.fromkeys(analysis.truncated_reasons))
    result["truncated"] = bool(result["truncated_reasons"])
    if result["truncated"]:
        result["analysis_status"] = "partial"
    return analysis.describe_result(result, identifier)
