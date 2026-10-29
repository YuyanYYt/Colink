"""Bounded structural views over existing facts, with no additional index tables.

The caller owns project authorization, index readiness and source pre/post checks.
This module only selects facts from that validated state. Results describe static
evidence; a missing edge or path never proves the absence of runtime behavior.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from code_context.analysis_context import attach_context, structure_evidence
from code_context.analysis_response import AnalysisRequestError, AnalysisResponseBudgetExceeded
from code_context.intelligence_models import CLASS_KINDS, is_traversable_call
from code_context.intelligence_queries import (
    _symbol,
    decode_relation,
    file_dependencies,
    symbol_graph,
)
from code_context.policy import validate_path

StructureMode = Literal[
    "file_context",
    "symbol_context",
    "callers",
    "callees",
    "call_graph",
    "call_path",
    "class_relations",
    "file_dependencies",
]
StructureInclude = Literal["ownership", "calls", "dependencies"]
STRUCTURE_MODES = frozenset(StructureMode.__args__)
_COVERAGE = "static authorized structure facts; unresolved or absent paths are not runtime proof"


class AnalysisBudgetExceeded(RuntimeError):
    """The guarded SQL adapter exhausted this request's analysis budget."""


class AnalysisSelector(BaseModel):
    """Select a file or one exact symbol; never guess among repeated definitions."""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    symbol_id: str | None = Field(default=None, pattern=r"^sym_[0-9a-f]{32}$")
    qualname: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_selection(self):
        if self.symbol_id is not None:
            if self.path is not None or self.qualname is not None:
                raise ValueError("symbol_id cannot be combined with path or qualname")
        elif self.path is None:
            raise ValueError("select a path or symbol_id")
        if self.path is not None:
            validate_path(self.path)
        if self.qualname is not None and any(ord(c) < 32 or ord(c) == 127 for c in self.qualname):
            raise ValueError("qualname must be printable")
        return self


def _selector(value) -> AnalysisSelector:
    if isinstance(value, AnalysisSelector):
        return value
    try:
        return AnalysisSelector.model_validate(value, strict=True)
    except ValidationError:
        raise AnalysisRequestError(
            "invalid analysis selector; use path, symbol_id or path + qualname"
        ) from None


def select_symbol(db, project_id, revision, selector, *, offset=0, limit=20):
    """Return one exact symbol or a bounded ambiguity result for caller presentation."""
    selected = _selector(selector)
    if selected.symbol_id is not None:
        try:
            return _symbol(db, project_id, revision, selected.symbol_id), None
        except ValueError:
            raise AnalysisRequestError(
                "SYMBOL_NOT_FOUND: select a definition in this context"
            ) from None
    if selected.qualname is None:
        raise AnalysisRequestError("symbol selection requires symbol_id or path + qualname")
    args = (project_id, revision, selected.path, selected.qualname)
    total = db.execute(
        "SELECT COUNT(*) FROM ci_symbols WHERE project_id=? AND revision=? "
        "AND path=? AND qualname=?",
        args,
    ).fetchone()[0]
    if total == 0:
        raise AnalysisRequestError("SYMBOL_NOT_FOUND: select a definition in this code context")
    rows = db.execute(
        "SELECT data FROM ci_symbols WHERE project_id=? AND revision=? AND path=? AND qualname=? "
        "ORDER BY start_line, symbol_id LIMIT ? OFFSET ?",
        (*args, 1 if total == 1 else limit, 0 if total == 1 else offset),
    ).fetchall()
    symbols = [json.loads(row["data"]) for row in rows]
    if total == 1:
        return symbols[0], None
    return None, {
        "analysis_status": "ambiguous",
        "candidates": symbols,
        "total": total,
        "offset": offset,
        "has_more": offset + len(symbols) < total,
        "next_offset": offset + len(symbols) if offset + len(symbols) < total else None,
        "selection_required": "choose one candidate symbol_id",
    }


def _symbols(db, project_id, revision, ids):
    found = {}
    ids = sorted(set(ids) - {None})
    # Leave room for project/revision even on SQLite with the historical 999 limit.
    for start in range(0, len(ids), 200):
        chunk = ids[start : start + 200]
        rows = db.execute(
            "SELECT symbol_id, data FROM ci_symbols WHERE project_id=? AND revision=? "
            f"AND symbol_id IN ({','.join('?' for _ in chunk)})",
            (project_id, revision, *chunk),
        ).fetchall()
        found.update((row["symbol_id"], json.loads(row["data"])) for row in rows)
    return found


def _reason(result, reason):
    result["truncated"] = True
    if result.get("analysis_status") != "ambiguous":
        result["analysis_status"] = "partial"
    if reason not in result["truncated_reason"]:
        result["truncated_reason"].append(reason)


def _file_context(
    db,
    project_id,
    revision,
    selected,
    include,
    limit,
    offset,
    max_nodes,
    max_edges,
    max_depth,
    direction,
    deadline,
):
    path = selected.path
    if path is None or selected.symbol_id is not None or selected.qualname is not None:
        raise AnalysisRequestError("file_context requires exactly a path selector")
    if (
        db.execute(
            "SELECT 1 FROM files WHERE project_id=? AND revision=? AND path=?",
            (project_id, revision, path),
        ).fetchone()
        is None
    ):
        raise AnalysisRequestError("FILE_NOT_FOUND: select an indexed file in this context")
    total = db.execute(
        "SELECT COUNT(*) FROM ci_symbols WHERE project_id=? AND revision=? AND path=?",
        (project_id, revision, path),
    ).fetchone()[0]
    rows = db.execute(
        "SELECT data FROM ci_symbols WHERE project_id=? AND revision=? AND path=? "
        "ORDER BY start_line, symbol_id LIMIT ? OFFSET ?",
        (project_id, revision, path, min(limit, max_nodes), offset),
    ).fetchall()
    symbols = [json.loads(row["data"]) for row in rows]
    result = {
        "path": path,
        "symbols": symbols,
        "total": total,
        "offset": offset,
        "has_more": offset + len(symbols) < total,
        "next_offset": offset + len(symbols) if offset + len(symbols) < total else None,
    }
    if "ownership" in include:
        ids = {s["symbol_id"] for s in symbols}
        result["ownership"], owner_ids = [], set()
        if ids:
            rows = db.execute(
                "SELECT data FROM ci_relations WHERE project_id=? AND revision=? AND source_path=? "
                f"AND kind='CONTAINS' AND target_symbol_id IN ({','.join('?' for _ in ids)}) "
                "ORDER BY line, source_symbol_id, target_symbol_id LIMIT ?",
                (project_id, revision, path, *sorted(ids), max_edges + 1),
            ).fetchall()
            for row in rows[:max_edges]:
                relation = decode_relation(row["data"])
                result["ownership"].append(relation)
                owner_ids.add(relation["source_symbol_id"])
            result["owners"] = list(_symbols(db, project_id, revision, owner_ids - ids).values())
            if len(rows) > max_edges:
                result["truncated"] = True
    if "calls" in include and time.monotonic() < deadline:
        result["calls"] = _file_calls(
            db, project_id, revision, path, symbols, max_nodes, max_edges, deadline
        )
    if "dependencies" in include and time.monotonic() < deadline:
        result["dependencies"] = file_dependencies(
            db,
            project_id,
            revision,
            path=path,
            direction=direction,
            depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    return result


def _file_calls(db, project_id, revision, path, symbols, max_nodes, max_edges, deadline):
    nodes, edges, truncated = {}, [], False
    reasons = []
    # Preliminary section bounds also obey the shared request budget before return.
    # Calls cover the file, including top-level calls with their actual locations.
    rows = db.execute(
        "SELECT data FROM ci_relations WHERE project_id=? AND revision=? AND source_path=? "
        "AND kind IN ('CALL','INSTANTIATION') ORDER BY line, kind LIMIT ?",
        (project_id, revision, path, max_edges + 1),
    ).fetchall()
    relations = []
    for row in rows:
        if time.monotonic() >= deadline:
            truncated = True
            reasons.append("time_budget")
            break
        relations.append(decode_relation(row["data"]))
    found = _symbols(
        db,
        project_id,
        revision,
        {r[k] for r in relations for k in ("source_symbol_id", "target_symbol_id")},
    )
    for relation in relations:
        if time.monotonic() >= deadline:
            truncated = True
            reasons.append("time_budget")
            break
        added = {
            value
            for key in ("source_symbol_id", "target_symbol_id")
            if (value := relation[key]) in found and value not in nodes
        }
        if len(nodes) + len(added) > max_nodes or len(edges) >= max_edges:
            truncated = True
            reasons.append("graph_budget")
            continue
        nodes.update((value, found[value]) for value in sorted(added))
        edges.append(relation)
    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": truncated,
        "truncated_reason": sorted(set(reasons)),
        "coverage": _COVERAGE,
        "execution_context_partial": any(not is_traversable_call(edge) for edge in edges),
    }


def _ownership(db, project_id, revision, root, depth, max_nodes, max_edges, deadline):
    nodes, edges, queue, seen = (
        {root["symbol_id"]: root},
        [],
        deque([(root["symbol_id"], 0, "descendants"), (root["symbol_id"], 0, "ancestors")]),
        set(),
    )
    truncated = False
    reasons = []
    while queue:
        if time.monotonic() >= deadline:
            truncated = True
            reasons.append("time_budget")
            break
        current, level, branch = queue.popleft()
        if level >= depth:
            continue
        column = "source_symbol_id" if branch == "descendants" else "target_symbol_id"
        rows = db.execute(
            "SELECT data FROM ci_relations WHERE project_id=? AND revision=? "
            f"AND {column}=? AND kind='CONTAINS' ORDER BY line, target_symbol_id LIMIT ?",
            (project_id, revision, current, max_edges + 1),
        ).fetchall()
        for row in rows:
            if time.monotonic() >= deadline:
                truncated = True
                reasons.append("time_budget")
                break
            if row["data"] in seen:
                continue
            seen.add(row["data"])
            relation = decode_relation(row["data"])
            child = relation["target_symbol_id" if branch == "descendants" else "source_symbol_id"]
            found = _symbols(db, project_id, revision, [child]) if child not in nodes else {}
            if len(edges) >= max_edges or len(nodes) + len(found) > max_nodes:
                truncated = True
                reasons.append("graph_budget")
                continue
            nodes.update(found)
            if found:
                queue.append((child, level + 1, branch))
            edges.append(relation)
    return {
        "root": root["symbol_id"],
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": truncated,
        "truncated_reason": sorted(set(reasons)),
        "coverage": _COVERAGE,
    }


def _call_path(db, project_id, revision, root, target, max_depth, max_nodes, max_edges, deadline):
    root_id, target_id = root["symbol_id"], target["symbol_id"]
    nodes = {root_id: root}
    distances, parents, queue, edges, unresolved = {root_id: 0}, {}, deque([root_id]), [], []
    truncated, shortest = False, 0 if root_id == target_id else None
    reasons, not_traversed = [], []
    while queue:
        if time.monotonic() >= deadline:
            truncated = True
            reasons.append("time_budget")
            break
        current = queue.popleft()
        level = distances[current]
        if level >= max_depth or (shortest is not None and level >= shortest):
            continue
        rows = db.execute(
            "SELECT data FROM ci_relations WHERE project_id=? AND revision=? "
            "AND source_symbol_id=? "
            "AND kind IN ('CALL','INSTANTIATION') ORDER BY source_path, line, kind LIMIT ?",
            (project_id, revision, current, max_edges + 1),
        ).fetchall()
        relations = []
        for row in rows:
            if time.monotonic() >= deadline:
                truncated = True
                reasons.append("time_budget")
                break
            relations.append(decode_relation(row["data"]))
        found = _symbols(db, project_id, revision, [r["target_symbol_id"] for r in relations])
        for relation in relations:
            if len(edges) + len(unresolved) + len(not_traversed) >= max_edges:
                truncated = True
                reasons.append("graph_budget")
                break
            next_id = relation["target_symbol_id"]
            if not is_traversable_call(relation):
                not_traversed.append(
                    {
                        **relation,
                        "not_traversed_reason": "deferred_or_type_only_execution_not_proven",
                    }
                )
                continue
            if relation["resolution"] != "resolved" or next_id not in found:
                unresolved.append(relation)
                continue
            if next_id not in nodes:
                if len(nodes) >= max_nodes:
                    truncated = True
                    reasons.append("graph_budget")
                    continue
                nodes[next_id] = found[next_id]
                distances[next_id] = level + 1
                queue.append(next_id)
            edges.append(relation)
            if distances[next_id] == level + 1:
                parents.setdefault(next_id, set()).add(current)
            if next_id == target_id:
                shortest = level + 1
    # Expand only shortest parents. Use an explicit stack, bounded at ten paths.
    paths, pending = [], [[target_id]] if shortest is not None else []
    while pending and len(paths) < 10:
        path = pending.pop()
        if path[-1] == root_id:
            paths.append(list(reversed(path)))
        else:
            for parent in sorted(parents.get(path[-1], ()), reverse=True):
                pending.append([*path, parent])
    if pending:
        truncated = True
        reasons.append("max_paths")
    used_edges = {(a, b) for path in paths for a, b in zip(path, path[1:], strict=False)}
    path_edges = [e for e in edges if (e["source_symbol_id"], e["target_symbol_id"]) in used_edges]
    used_nodes = {node for path in paths for node in path} | {root_id, target_id}
    # Target is included only if the requested node budget can hold both endpoints.
    if target_id not in nodes and len(nodes) < max_nodes:
        nodes[target_id] = target
    return {
        "root": root_id,
        "target": target_id,
        "direction": "outgoing",
        "paths": paths,
        "shortest_depth": shortest,
        "nodes": [s for k, s in nodes.items() if k in used_nodes],
        "edges": path_edges,
        "unresolved": unresolved,
        "not_traversed": not_traversed,
        "execution_context_partial": bool(not_traversed),
        "path_found": bool(paths),
        "truncated": truncated,
        "truncated_reason": sorted(set(reasons)),
        "no_path_is_not_runtime_proof": True,
        "continuation": {"frontier_symbol_ids": list(nodes)[-20:]},
    }


def _graphs(result):
    if isinstance(result, dict):
        if isinstance(result.get("nodes"), list) and isinstance(result.get("edges"), list):
            yield result
        for value in list(result.values()):
            if isinstance(value, dict):
                yield from _graphs(value)


def _node_id(node):
    return node.get("id", node.get("symbol_id", node.get("path")))


def _endpoints(edge):
    return (
        edge.get("source", edge.get("source_symbol_id")),
        edge.get("target", edge.get("target_symbol_id")),
    )


def _mark_sections(result, sections, reason):
    seen = set()
    for section in (result, *sections):
        if id(section) not in seen:
            seen.add(id(section))
            section.setdefault("truncated_reason", [])
            _reason(section, reason)


def _clipped_field(result, parents, section, field, before, reason="max_chars"):
    _mark_sections(result, (*parents, section), reason)
    fields = section.setdefault("clipped_fields", [])
    previous = next((entry for entry in fields if entry["field"] == field), None)
    if previous is None:
        fields.append({"field": field, "before": before, "returned": len(section[field])})
    else:
        previous["returned"] = len(section[field])


def apply_analysis_bounds(result, max_nodes, max_edges):
    """Share output budgets across sections, deduplicating symbol and file identities."""
    result.setdefault("truncated", False)
    result.setdefault("truncated_reason", [])
    result.setdefault("analysis_status", "ready")
    nodes, edges = set(), set()

    def identity(node):
        if "id" in node:
            return ("value", node["id"])
        if "symbol_id" in node:
            return ("symbol", node["symbol_id"])
        return ("file", node["path"])

    def add_node(node):
        key = identity(node)
        if key not in nodes and len(nodes) >= max_nodes:
            return False
        nodes.add(key)
        return True

    def add_edge(edge, candidates):
        key = json.dumps(edge, sort_keys=True, ensure_ascii=False)
        added = {identity(node) for node in candidates} - nodes
        if len(nodes) + len(added) > max_nodes or (key not in edges and len(edges) >= max_edges):
            return False
        nodes.update(added)
        edges.add(key)
        return True

    if "symbol" in result:
        add_node(result["symbol"])
    if isinstance(result.get("symbols"), list):
        symbols = []
        for symbol in result["symbols"]:
            if add_node(symbol):
                symbols.append(symbol)
            else:
                _reason(result, "graph_budget")
        result["symbols"] = symbols
        result["has_more"] = result["offset"] + len(symbols) < result["total"]
        result["next_offset"] = result["offset"] + len(symbols) if result["has_more"] else None
        if isinstance(result.get("ownership"), list):
            owners = {node["symbol_id"]: node for node in result.get("owners", [])}
            visible = {node["symbol_id"]: node for node in symbols}
            available = {**owners, **visible}
            selected, owner_ids = [], set()
            for edge in result["ownership"]:
                endpoints = [edge["source_symbol_id"], edge["target_symbol_id"]]
                if edge["target_symbol_id"] in visible and add_edge(
                    edge, [available[key] for key in endpoints if key in available]
                ):
                    selected.append(edge)
                    owner_ids.add(edge["source_symbol_id"])
                else:
                    _reason(result, "graph_budget")
            result["ownership"] = selected
            result["owners"] = [node for key, node in owners.items() if key in owner_ids]
    for graph, parents, _ in _analysis_sections(result):
        if not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
            continue
        before_counts = {
            key: len(graph[key])
            for key in ("nodes", "edges", "paths", "chains", "unresolved", "not_traversed")
            if isinstance(graph.get(key), list)
        }
        available = {_node_id(node): node for node in graph["nodes"]}
        selected_nodes = {}
        for root in (graph.get("root", graph.get("path")), graph.get("target")):
            if root in available:
                if add_node(available[root]):
                    selected_nodes[root] = available[root]
                else:
                    _reason(result, "graph_budget")
        selected_edges = []
        for edge in graph["edges"]:
            endpoints = _endpoints(edge)
            candidates = [available[key] for key in endpoints if key in available]
            if add_edge(edge, candidates):
                selected_edges.append(edge)
                selected_nodes.update((identity(node)[1], node) for node in candidates)
            else:
                graph["truncated"] = True
                _reason(result, "graph_budget")
        graph["nodes"], graph["edges"] = list(selected_nodes.values()), selected_edges
        pairs = {_endpoints(edge) for edge in selected_edges}
        if "paths" in graph:
            retained = [
                path
                for path in graph["paths"]
                if all(pair in pairs for pair in zip(path, path[1:], strict=False))
            ]
            if len(retained) != len(graph["paths"]):
                graph["paths_truncated"] = True
            graph["paths"] = retained
        if "chains" in graph:
            graph["chains"] = [
                chain for chain in graph["chains"] if set(chain) <= selected_nodes.keys()
            ]
        for field in ("unresolved", "not_traversed"):
            if field not in graph:
                continue
            kept = []
            for edge in graph[field]:
                if add_edge(edge, []):
                    kept.append(edge)
                else:
                    _reason(result, "graph_budget")
            graph[field] = kept
        for key, before in before_counts.items():
            if len(graph[key]) != before:
                _clipped_field(result, parents, graph, key, before, "graph_budget")
    return result


def _analysis_sections(result, parents=(), name=None):
    """Visit response partitions, never the internals of an indivisible source fact."""
    yield result, parents, name
    protected = {
        "symbol",
        "selector",
        "target",
        "source",
        "coverage",
        "index_coverage",
        "continuation",
        "clipped_fields",
    }
    for key, child in list(result.items()):
        if isinstance(child, dict) and key not in protected:
            yield from _analysis_sections(child, (*parents, result), key)


def _prune_clipped_graph(result, parents, graph):
    """Remove orphan endpoints and path witnesses only from the graph that changed."""
    used = {graph.get("root", graph.get("path")), graph.get("target")}
    for edge in graph["edges"]:
        used.update(_endpoints(edge))
    before = graph["nodes"]
    graph["nodes"] = [node for node in before if _node_id(node) in used]
    removed = [node for node in before if _node_id(node) not in used]
    if removed:
        _clipped_field(result, parents, graph, "nodes", len(before))
    ids = {_node_id(node) for node in graph["nodes"]}
    pairs = {_endpoints(edge) for edge in graph["edges"]}
    for field in ("paths", "chains"):
        if field not in graph:
            continue
        old = graph[field]
        graph[field] = [
            path
            for path in old
            if set(path) <= ids
            and (
                field == "chains"
                or all(pair in pairs for pair in zip(path, path[1:], strict=False))
            )
        ]
        dropped = [path for path in old if path not in graph[field]]
        if dropped:
            removed.extend(dropped)
            _clipped_field(result, parents, graph, field, len(old))
            if field == "paths":
                graph["paths_truncated"] = True
                if graph.get("path_found") and not graph["paths"]:
                    graph["path_evidence_omitted"] = True
    return removed


def _clip_continuation(result, parents, section, name, field, max_chars, removed):
    """Suggest a narrower public request; symbol offsets never resume graph edges."""
    result.setdefault("next_request_reuses", ["project_id", "snapshot"])
    contexts = (section, *reversed(parents), result)
    selector = None
    for context in contexts:
        saved = context.get("continuation", {}).get("selector")
        symbol = context.get("symbol")
        if saved:
            selector = dict(saved)
            break
        if isinstance(symbol, dict) and symbol.get("symbol_id"):
            selector = {"symbol_id": symbol["symbol_id"]}
            break
        root_node = next(
            (node for node in context.get("nodes", []) if _node_id(node) == context.get("root")),
            None,
        )
        if root_node is not None and root_node.get("symbol_id") and not root_node.get("id"):
            selector = {"symbol_id": root_node["symbol_id"]}
            break
        if context.get("path"):
            selector = {"path": context["path"]}
            break
    if field == "ownership" and removed and removed.get("target_symbol_id"):
        selector = {"symbol_id": removed["target_symbol_id"]}
    continuation = section.setdefault("continuation", {})
    if field in {"symbols", "candidates"}:
        section["next_offset_scope"] = "symbols" if field == "symbols" else "candidates"
        if selector is not None and section.get("next_offset") is not None:
            continuation[field] = {
                "scope": section["next_offset_scope"],
                "next_request": {"selector": selector, "offset": section["next_offset"]},
            }
        return
    request = {"selector": selector, "max_chars": min(50_000, max_chars * 2)}
    partition = name if name in {"ownership", "calls", "dependencies"} else field
    if partition == "ownership" and not isinstance(section.get("nodes"), list):
        continuation = continuation.setdefault("ownership", {})
    if partition in {"ownership", "calls", "dependencies"}:
        request["include"] = [partition]
        if selector is not None and set(selector) == {"path"}:
            request["limit"] = 1
        for key in ("direction", "depth"):
            if key in section:
                request["max_depth" if key == "depth" else key] = section[key]
    if selector is None:
        # A bare graph can lack the original source selector. Never invent one
        # from request-local value IDs or send an undeclared compatibility mode.
        continuation["action"] = "narrow_source_selector"
        return
    if section.get("mode") in {"call_path", "data_path"}:
        target = next(
            (node for node in section.get("nodes", []) if _node_id(node) == section.get("target")),
            None,
        )
        if target is not None and target.get("symbol_id"):
            selected_target = {"symbol_id": target["symbol_id"]}
            if target.get("id") and target.get("line") and target.get("column"):
                selected_target["value"] = (
                    {"kind": "parameter", "name": target["name"]}
                    if target.get("kind") == "parameter"
                    else {
                        "kind": "return" if target.get("kind") == "return" else "occurrence",
                        "line": target["line"],
                        "column": target["column"],
                    }
                )
            request["target"] = selected_target
            coverage = section.get("coverage")
            request["max_depth"] = (
                coverage.get("cross_call_depth", 5) if isinstance(coverage, dict) else 5
            )
        else:
            continuation["action"] = "narrow_source_selector"
            return
    continuation["next_request"] = request


def bound_analysis_result(result: dict, max_chars: int) -> dict:
    """Clip whole facts, including composite graph sections, preserving references."""
    if type(max_chars) is not int or not 1000 <= max_chars <= 50_000:
        raise ValueError("max_chars must be between 1000 and 50000")
    result.setdefault("truncated_reason", [])
    result.setdefault("analysis_status", "ready")
    result.setdefault("truncated", False)
    minimums = {
        "symbols": 1,
        "candidates": 1,
        "ownership": 1,
        "edges": 1,
        "paths": 1,
        "chains": 1,
        "unresolved": 0,
        "not_traversed": 0,
        "diagnostics": 0,
    }
    sizes = {}

    def size(fact):
        identity = id(fact)
        if identity not in sizes:
            sizes[identity] = len(json.dumps(fact, ensure_ascii=False))
        return sizes[identity]

    while (encoded_size := len(json.dumps(result, ensure_ascii=False))) > max_chars:
        candidates = []
        for section, parents, name in _analysis_sections(result):
            for key, minimum in minimums.items():
                values = section.get(key)
                if isinstance(values, list) and len(values) > minimum:
                    candidates.append((section, parents, name, key, values, minimum))
        if not candidates:
            # Prefer one fact per requested graph, but the hard minimum is one
            # selectable symbol/candidate. Empty graph evidence remains explicit.
            for section, parents, name in _analysis_sections(result):
                for key in ("ownership", "edges", "paths", "chains"):
                    values = section.get(key)
                    if isinstance(values, list) and values:
                        candidates.append((section, parents, name, key, values, 0))
        if not candidates:
            for section, parents, name in _analysis_sections(result):
                nodes = section.get("nodes")
                if (
                    isinstance(nodes, list)
                    and nodes
                    and not section.get("edges")
                    and not section.get("paths")
                    and not section.get("chains")
                    and all("id" not in node for node in nodes)
                ):
                    # A selected symbol remains intact in its own fact. Its
                    # duplicate metadata in a now-empty graph may be omitted as
                    # a whole field, with the original/returned counts retained.
                    candidates.append((section, parents, name, "nodes", nodes, 0))
        if not candidates:
            raise AnalysisResponseBudgetExceeded
        section, parents, name, key, values, minimum = max(
            candidates, key=lambda item: sum(size(fact) + 2 for fact in item[4][item[5] :])
        )
        excess, removed_size, before = encoded_size - max_chars + 96, 0, len(values)
        removed = None
        while len(values) > minimum and removed_size < excess:
            removed = values.pop()
            removed_size += size(removed) + 2
            if key in {"edges", "unresolved"} and isinstance(section.get("nodes"), list):
                removed_size += sum(
                    size(fact) + 2 for fact in _prune_clipped_graph(result, parents, section)
                )
        _clipped_field(result, parents, section, key, before)
        if key == "paths":
            section["paths_truncated"] = True
            if section.get("path_found") and not values:
                section["path_evidence_omitted"] = True
        if key in {"symbols", "candidates"}:
            section["has_more"] = section["offset"] + len(values) < section["total"]
            section["next_offset"] = (
                section["offset"] + len(values)
                if section["has_more"] and not section.get("value_selection_candidates")
                else None
            )
        if key == "symbols" and isinstance(section.get("ownership"), list):
            visible = {symbol["symbol_id"] for symbol in values}
            ownership = section["ownership"]
            section["ownership"] = [
                edge for edge in ownership if edge["target_symbol_id"] in visible
            ]
            if len(ownership) != len(section["ownership"]):
                _clipped_field(result, parents, section, "ownership", len(ownership))
        if isinstance(section.get("ownership"), list) and isinstance(section.get("owners"), list):
            visible = {symbol["symbol_id"] for symbol in section.get("symbols", [])}
            owners = {edge["source_symbol_id"] for edge in section["ownership"]} - visible
            previous_owners = section["owners"]
            section["owners"] = [s for s in previous_owners if s["symbol_id"] in owners]
            if len(previous_owners) != len(section["owners"]):
                _clipped_field(result, parents, section, "owners", len(previous_owners))
        _clip_continuation(result, parents, section, name, key, max_chars, removed)
    return result


def _analyze_structure(
    db,
    project_id: str,
    revision: int,
    *,
    mode: str,
    selector,
    target=None,
    include=None,
    direction="both",
    max_depth=1,
    max_nodes=50,
    max_edges=100,
    max_chars=12_000,
    max_ms=3000,
    offset=0,
    limit=20,
    include_type_only=True,
):
    """Compute one bounded view inside the caller's single validated read context."""
    if not isinstance(mode, str) or mode not in STRUCTURE_MODES:
        raise AnalysisRequestError("unsupported structure analysis mode")
    if direction not in {"outgoing", "incoming", "both"}:
        raise AnalysisRequestError("invalid analysis direction")
    if type(include_type_only) is not bool:
        raise AnalysisRequestError("include_type_only must be a boolean")
    for name, value, low, high in (
        ("max_depth", max_depth, 1, 5),
        ("max_nodes", max_nodes, 1, 200),
        ("max_edges", max_edges, 1, 500),
        ("max_chars", max_chars, 1000, 50_000),
        ("max_ms", max_ms, 100, 10_000),
        ("limit", limit, 1, 100),
        ("offset", offset, 0, 1_000_000),
    ):
        if type(value) is not int or not low <= value <= high:
            raise AnalysisRequestError(f"invalid {name} bounds")
    selected = _selector(selector)
    if target is not None and mode != "call_path":
        raise AnalysisRequestError("target is only supported by call_path")
    included = ["ownership"] if include is None else include
    if (
        not isinstance(included, list)
        or any(item not in StructureInclude.__args__ for item in included)
        or len(included) != len(set(included))
    ):
        raise AnalysisRequestError(
            "include must contain unique ownership, calls or dependencies values"
        )
    deadline = time.monotonic() + max_ms / 1000
    result = {
        "mode": mode,
        "analysis_status": "ready",
        "truncated": False,
        "truncated_reason": [],
        "coverage": _COVERAGE,
    }
    if mode == "file_context":
        result.update(
            _file_context(
                db,
                project_id,
                revision,
                selected,
                included,
                limit,
                offset,
                max_nodes,
                max_edges,
                max_depth,
                direction,
                deadline,
            )
        )
    elif mode == "file_dependencies":
        if selected.path is None or selected.symbol_id is not None or selected.qualname is not None:
            raise AnalysisRequestError("file_dependencies requires exactly a path selector")
        result.update(
            file_dependencies(
                db,
                project_id,
                revision,
                path=selected.path,
                direction=direction,
                depth=max_depth,
                max_nodes=max_nodes,
                max_edges=max_edges,
                include_type_only=include_type_only,
            )
        )
    else:
        root, ambiguity = select_symbol(
            db, project_id, revision, selected, offset=offset, limit=limit
        )
        if ambiguity is not None:
            result.update(ambiguity)
            return bound_analysis_result(result, max_chars)
        if mode == "class_relations" and root["kind"] not in CLASS_KINDS:
            raise AnalysisRequestError("class relations require a class or interface selector")
        if mode == "call_path":
            if target is None:
                raise AnalysisRequestError("call_path requires a target selector")
            end, ambiguity = select_symbol(
                db, project_id, revision, target, offset=offset, limit=limit
            )
            if ambiguity is not None:
                result.update(ambiguity, ambiguous_selector="target")
                return bound_analysis_result(result, max_chars)
            result.update(
                _call_path(
                    db, project_id, revision, root, end, max_depth, max_nodes, max_edges, deadline
                )
            )
        elif mode == "symbol_context":
            result["symbol"] = root
            if "ownership" in included:
                result["ownership"] = _ownership(
                    db, project_id, revision, root, max_depth, max_nodes, max_edges, deadline
                )
            if "calls" in included and time.monotonic() < deadline:
                result["calls"] = symbol_graph(
                    db,
                    project_id,
                    revision,
                    symbol_id=root["symbol_id"],
                    graph="call",
                    direction=direction,
                    depth=max_depth,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
            if "dependencies" in included and time.monotonic() < deadline:
                result["dependencies"] = file_dependencies(
                    db,
                    project_id,
                    revision,
                    path=root["path"],
                    direction=direction,
                    depth=max_depth,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
        else:
            result.update(
                symbol_graph(
                    db,
                    project_id,
                    revision,
                    symbol_id=root["symbol_id"],
                    graph="class" if mode == "class_relations" else "call",
                    direction={"callers": "incoming", "callees": "outgoing"}.get(mode, direction),
                    depth=max_depth,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
            )
    if include and mode not in {"file_context", "symbol_context"}:
        attach_context(
            result,
            structure_evidence(
                db,
                project_id,
                revision,
                selector=selected.model_dump(exclude_none=True),
                include=include,
                direction=direction,
                max_depth=max_depth,
                max_nodes=max_nodes,
                max_edges=max_edges,
                max_chars=max_chars,
                deadline=deadline,
                include_type_only=include_type_only,
            ),
        )
    graph_truncated = result.get("truncated")
    if graph_truncated and not result["truncated_reason"]:
        _reason(result, "graph_budget")
    if time.monotonic() >= deadline:
        _reason(result, "time_budget")
        if mode in {"file_context", "symbol_context"}:
            omitted = [section for section in included if section not in result]
            if omitted:
                result["omitted_sections"] = omitted
    for graph in _graphs(result):
        if graph.get("execution_context_partial"):
            result["analysis_status"] = "partial"
        if graph.get("truncated"):
            for reason in graph.get("truncated_reason") or ["graph_budget"]:
                _reason(result, reason)
    if "nodes" in result and "continuation" not in result:
        identity = "path" if mode == "file_dependencies" else "symbol_id"
        result["continuation"] = {
            "frontier_paths" if identity == "path" else "frontier_symbol_ids": [
                node[identity] for node in result["nodes"][-20:]
            ]
        }
    return bound_analysis_result(apply_analysis_bounds(result, max_nodes, max_edges), max_chars)


def analyze_structure(
    db,
    project_id: str,
    revision: int,
    *,
    mode: str,
    selector,
    target=None,
    include=None,
    direction="both",
    max_depth=1,
    max_nodes=50,
    max_edges=100,
    max_chars=12_000,
    max_ms=3000,
    offset=0,
    limit=20,
    include_type_only=True,
):
    """Query entry point; budget exhaustion discards unfinished composite results."""
    try:
        return _analyze_structure(
            db,
            project_id,
            revision,
            mode=mode,
            selector=selector,
            target=target,
            include=include,
            direction=direction,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_chars=max_chars,
            max_ms=max_ms,
            offset=offset,
            limit=limit,
            include_type_only=include_type_only,
        )
    except AnalysisBudgetExceeded:
        return bound_analysis_result(
            {
                "mode": mode,
                "analysis_status": "partial",
                "truncated": True,
                "truncated_reason": ["time_budget"],
                "coverage": _COVERAGE,
                "nodes": [],
                "edges": [],
                "continuation": {"selector": _selector(selector).model_dump(exclude_none=True)},
            },
            max_chars,
        )
