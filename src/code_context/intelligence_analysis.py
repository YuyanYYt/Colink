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

from code_context.analysis_response import AnalysisResponseBudgetExceeded
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
        raise ValueError(
            "invalid analysis selector; use path, symbol_id or path + qualname"
        ) from None


def select_symbol(db, project_id, revision, selector, *, offset=0, limit=20):
    """Return one exact symbol or a bounded ambiguity result for caller presentation."""
    selected = _selector(selector)
    if selected.symbol_id is not None:
        return _symbol(db, project_id, revision, selected.symbol_id), None
    if selected.qualname is None:
        raise ValueError("symbol selection requires symbol_id or path + qualname")
    args = (project_id, revision, selected.path, selected.qualname)
    total = db.execute(
        "SELECT COUNT(*) FROM ci_symbols WHERE project_id=? AND revision=? "
        "AND path=? AND qualname=?",
        args,
    ).fetchone()[0]
    if total == 0:
        raise ValueError("SYMBOL_NOT_FOUND: select a definition in this code context")
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
        raise ValueError("file_context requires exactly a path selector")
    if (
        db.execute(
            "SELECT 1 FROM files WHERE project_id=? AND revision=? AND path=?",
            (project_id, revision, path),
        ).fetchone()
        is None
    ):
        raise ValueError("file is not in this code context")
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
    reasons = []
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
            if len(edges) + len(unresolved) >= max_edges:
                truncated = True
                reasons.append("graph_budget")
                break
            next_id = relation["target_symbol_id"]
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
    for graph in _graphs(result):
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
        if "unresolved" in graph:
            kept = []
            for edge in graph["unresolved"]:
                if add_edge(edge, []):
                    kept.append(edge)
                else:
                    _reason(result, "graph_budget")
            graph["unresolved"] = kept
    return result


def bound_analysis_result(result: dict, max_chars: int) -> dict:
    """Clip whole facts, including composite graph sections, preserving references."""
    if type(max_chars) is not int or not 1000 <= max_chars <= 50_000:
        raise ValueError("max_chars must be between 1000 and 50000")
    result.setdefault("truncated_reason", [])
    result.setdefault("analysis_status", "ready")
    result.setdefault("truncated", False)
    while len(json.dumps(result, ensure_ascii=False)) > max_chars:
        lists = []

        def collect(value, lists=lists):
            if not isinstance(value, dict):
                return
            for key, item in value.items():
                if (
                    isinstance(item, list)
                    and item
                    and (key not in {"symbols", "candidates"} or len(item) > 1)
                    and key
                    not in {
                        "nodes",
                        "owners",
                        "truncated_reason",
                        "frontier_symbol_ids",
                    }
                ):
                    lists.append((value, key, item))
                elif isinstance(item, dict):
                    collect(item)

        collect(result)
        if not lists:
            raise AnalysisResponseBudgetExceeded
        container, key, values = max(
            lists, key=lambda item: len(json.dumps(item[2], ensure_ascii=False))
        )
        # Drop enough complete facts in one pass. Re-encoding a large graph once
        # for every discarded edge would turn clipping into quadratic work.
        excess = len(json.dumps(result, ensure_ascii=False)) - max_chars + 96
        removed = 0
        # A page must retain one selectable fact. Other sections and their
        # endpoints may be the real source of excess; exhausting the symbol
        # page before pruning those sections misdiagnoses a valid query.
        minimum = 1 if key in {"symbols", "candidates"} else 0
        while len(values) > minimum and removed < excess:
            removed += len(json.dumps(values.pop(), ensure_ascii=False)) + 2
        _reason(result, "max_chars")
        if key in {"symbols", "candidates"}:
            container["has_more"] = container["offset"] + len(values) < container["total"]
            container["next_offset"] = (
                container["offset"] + len(values)
                if container["has_more"] and not container.get("value_selection_candidates")
                else None
            )
        if isinstance(result.get("symbols"), list) and isinstance(result.get("ownership"), list):
            visible = {s["symbol_id"] for s in result["symbols"]}
            result["ownership"] = [
                e for e in result["ownership"] if e["target_symbol_id"] in visible
            ]
            owner_ids = {e["source_symbol_id"] for e in result["ownership"]} - visible
            result["owners"] = [s for s in result.get("owners", []) if s["symbol_id"] in owner_ids]
        for graph in _graphs(result):
            used = {graph.get("root", graph.get("path")), graph.get("target")}
            for edge in graph["edges"]:
                used.update(_endpoints(edge))
            graph["nodes"] = [n for n in graph["nodes"] if _node_id(n) in used]
            node_ids = {_node_id(n) for n in graph["nodes"]}
            pairs = {_endpoints(e) for e in graph["edges"]}
            if "paths" in graph:
                previous_count = len(graph["paths"])
                graph["paths"] = [
                    p
                    for p in graph["paths"]
                    if set(p) <= node_ids
                    and all(pair in pairs for pair in zip(p, p[1:], strict=False))
                ]
                if len(graph["paths"]) != previous_count:
                    graph["paths_truncated"] = True
            if "chains" in graph:
                graph["chains"] = [c for c in graph["chains"] if set(c) <= node_ids]
            graph["truncated"] = True
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
        raise ValueError("unsupported structure analysis mode")
    if direction not in {"outgoing", "incoming", "both"}:
        raise ValueError("invalid analysis direction")
    if type(include_type_only) is not bool:
        raise ValueError("include_type_only must be a boolean")
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
            raise ValueError(f"invalid {name} bounds")
    selected = _selector(selector)
    if target is not None and mode != "call_path":
        raise ValueError("target is only supported by call_path")
    included = ["ownership"] if include is None else include
    if (
        not isinstance(included, list)
        or any(item not in StructureInclude.__args__ for item in included)
        or len(included) != len(set(included))
    ):
        raise ValueError("include must contain unique ownership, calls or dependencies values")
    if include is not None and mode not in {"file_context", "symbol_context"}:
        raise ValueError("include is only supported by context views")
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
            raise ValueError("file_dependencies requires exactly a path selector")
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
        if mode == "call_path":
            if target is None:
                raise ValueError("call_path requires a target selector")
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
