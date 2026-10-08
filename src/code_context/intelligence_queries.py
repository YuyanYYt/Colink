"""Bounded SELECT-only queries over a pinned code-intelligence snapshot."""

import json
from collections import defaultdict, deque

from code_context.fact_payloads import FactCacheError, decode_fact
from code_context.intelligence_models import CLASS_KINDS, Relation, module_id
from code_context.policy import validate_path
from code_context.source_page import source_page


def decode_relation(payload: str | bytes) -> dict:
    """Decode legacy JSON or bounded, compressed live-index relation facts."""
    value = decode_fact(payload, prefix=b"CL1:", max_bytes=65_536)
    if value.keys() != Relation.__dataclass_fields__.keys():
        raise FactCacheError("invalid derived relation") from None
    return value


def _bounds(offset: int, limit: int, maximum: int = 100) -> None:
    if offset < 0 or not 1 <= limit <= maximum:
        raise ValueError("invalid pagination bounds")


def bound_result(result: dict, max_chars: int) -> dict:
    if not 1000 <= max_chars <= 50_000:
        raise ValueError("max_chars must be between 1000 and 50000")
    # Keep metadata and remove complete items. Never cut a graph fact in half.
    page = next((key for key in ("symbols", "references", "dependencies") if key in result), None)
    graph = "nodes" in result and "edges" in result
    arrays = [value for key, value in result.items() if isinstance(value, list) and key != "nodes"]
    while len(json.dumps(result, ensure_ascii=False)) > max_chars:
        candidates = [values for values in arrays if values]
        if not candidates:
            raise ValueError(
                "response metadata exceeds budget; narrow the query or increase max_chars"
            )
        else:
            max(candidates, key=lambda a: len(json.dumps(a, ensure_ascii=False))).pop()
        result["truncated"] = True
        if graph:
            # Keep a graph internally consistent after dropping edges to meet a byte budget.
            used = {result.get("root", result.get("path"))}
            for edge in result["edges"]:
                used.update(
                    [
                        edge.get("source_symbol_id", edge.get("source")),
                        edge.get("target_symbol_id", edge.get("target")),
                    ]
                )
            result["nodes"] = [
                node for node in result["nodes"] if node.get("symbol_id", node.get("path")) in used
            ]
        if page:
            result["has_more"] = result["offset"] + len(result[page]) < result["total"]
            result["next_offset"] = result["offset"] + len(result[page])
            if not result[page] and result["has_more"]:
                raise ValueError("one item exceeds response budget; choose a larger max_chars")
        if "content" in result and result.get("truncated"):
            result["content_truncated"] = True
    return result


def index_status(db, project_id: str, revision: int) -> dict:
    row = db.execute(
        "SELECT stats FROM ci_snapshots WHERE project_id=? AND revision=?", (project_id, revision)
    ).fetchone()
    if row is None:
        raise ValueError("INDEX_NOT_READY: restart the server to index retained source states")
    return json.loads(row["stats"])


def _symbol(db, project_id: str, revision: int, symbol_id: str) -> dict:
    if not symbol_id.startswith("sym_") or len(symbol_id) != 36:
        raise ValueError("invalid symbol identifier; use symbol_search")
    row = db.execute(
        "SELECT data FROM ci_symbols WHERE project_id=? AND revision=? AND symbol_id=?",
        (project_id, revision, symbol_id),
    ).fetchone()
    if row is None:
        raise ValueError("SYMBOL_NOT_FOUND: search the symbol again in this code context")
    return json.loads(row["data"])


def symbol_search(
    db,
    project_id: str,
    revision: int,
    *,
    query: str,
    exact: bool = False,
    kind: str | None = None,
    path_prefix: str | None = None,
    language: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    _bounds(offset, limit)
    if not query or len(query) > 200 or any(ord(c) < 32 for c in query):
        raise ValueError("symbol query must contain 1-200 printable characters")
    where, args = ["project_id=?", "revision=?"], [project_id, revision]
    if exact:
        where.append("name=?")
        args.append(query)
    else:
        where.append("name LIKE ? ESCAPE '\\'")
        args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if kind:
        where.append("kind=?")
        args.append(kind)
    else:
        where.append("kind<>'module'")
    if language:
        if language not in {"python", "java"}:
            raise ValueError("supported languages are python and java")
        where.append("json_extract(data, '$.language')=?")
        args.append(language)
    if path_prefix:
        validate_path(path_prefix.rstrip("/"))
        where.append("substr(path, 1, ?)=?")
        args.extend([len(path_prefix), path_prefix])
    clause = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM ci_symbols WHERE {clause}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT data FROM ci_symbols WHERE {clause} "
        "ORDER BY path, start_line, symbol_id LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return {
        "symbols": [json.loads(row["data"]) for row in rows],
        "total": total,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
    }


def read_symbol(
    db,
    project_id: str,
    revision: int,
    *,
    symbol_id: str,
    line_offset: int = 0,
    max_lines: int = 200,
    char_offset: int = 0,
    text_budget: int = 18_000,
    response_budget: int = 19_500,
) -> dict:
    if line_offset < 0 or not 1 <= max_lines <= 1000:
        raise ValueError("invalid symbol line bounds")
    symbol = _symbol(db, project_id, revision, symbol_id)
    row = db.execute(
        "SELECT b.content FROM files f JOIN blobs b USING(sha256) "
        "WHERE f.project_id=? AND f.revision=? AND f.path=?",
        (project_id, revision, symbol["path"]),
    ).fetchone()
    first = max(1, symbol["start_line"]) + line_offset
    end = min(symbol["end_line"], first + max_lines - 1)
    if first > max(1, symbol["end_line"]):
        raise ValueError("line_offset is outside the symbol")

    def result_for(budget):
        page = source_page(row["content"], first, end, budget, char_offset)
        has_more = page["content_truncated"] or page["end_line"] < symbol["end_line"]
        next_line = page["next_start_line"] if has_more else None
        return {
            "symbol": symbol,
            "path": symbol["path"],
            **page,
            "has_more": has_more,
            "next_start_line": next_line,
            "next_char_offset": page["next_char_offset"] if has_more else None,
            "next_line_offset": next_line - max(1, symbol["start_line"]) if next_line else None,
            "source_is_untrusted": True,
        }

    result = result_for(min(text_budget, 50_000))
    if len(json.dumps(result, ensure_ascii=False)) > response_budget:
        # Account for JSON escapes without ever chopping a page after computing its cursor.
        overhead = len(json.dumps({**result, "content": ""}, ensure_ascii=False))
        available = response_budget - overhead - 100
        low, high = 0, len(result["content"])
        while low < high:
            middle = (low + high + 1) // 2
            cost = len(json.dumps(result["content"][:middle], ensure_ascii=False)) - 2
            if cost <= available:
                low = middle
            else:
                high = middle - 1
        if not low:
            raise ValueError(
                "symbol metadata exceeds response budget; use a larger max_chars "
                "or read_file with the symbol's source range"
            )
        result = result_for(low)
    return result


def find_references(
    db,
    project_id: str,
    revision: int,
    *,
    symbol_id: str,
    offset: int = 0,
    limit: int = 50,
    include_imports: bool = True,
) -> dict:
    _bounds(offset, limit, 200)
    symbol = _symbol(db, project_id, revision, symbol_id)
    clause = "project_id=? AND revision=? AND target_symbol_id=? AND kind<>'CONTAINS'"
    args = [project_id, revision, symbol_id]
    if not include_imports:
        clause += " AND kind<>'IMPORT'"
    total = db.execute(f"SELECT COUNT(*) FROM ci_relations WHERE {clause}", args).fetchone()[0]
    rows = db.execute(
        f"SELECT data FROM ci_relations WHERE {clause} "
        "ORDER BY source_path, line, kind LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    return {
        "symbol": symbol,
        "references": [decode_relation(row["data"]) for row in rows],
        "total": total,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        "coverage": "statically bound references only; dynamic references may be missing",
    }


def symbol_graph(
    db,
    project_id: str,
    revision: int,
    *,
    symbol_id: str,
    graph: str,
    direction: str = "outgoing",
    depth: int = 1,
    max_nodes: int = 50,
    max_edges: int = 100,
) -> dict:
    if direction not in {"outgoing", "incoming", "both"}:
        raise ValueError("invalid graph direction")
    if not 1 <= depth <= 5 or not 1 <= max_nodes <= 200 or not 1 <= max_edges <= 500:
        raise ValueError("invalid graph bounds")
    root = _symbol(db, project_id, revision, symbol_id)
    if graph == "class" and root["kind"] not in CLASS_KINDS:
        raise ValueError("class graph requires a class/interface/enum/record symbol")
    kinds = (
        {"CALL", "INSTANTIATION"}
        if graph == "call"
        else {"INHERITANCE", "IMPLEMENTS", "CONTAINS", "TYPE_REFERENCE", "INSTANTIATION"}
    )
    placeholders = ",".join("?" for _ in kinds)
    queue, nodes, edges, seen = deque([(symbol_id, 0)]), {symbol_id: root}, [], set()
    truncated = False
    while queue:
        current, level = queue.popleft()
        if level >= depth:
            continue
        clauses = []
        if direction in {"outgoing", "both"}:
            clauses.append("source_symbol_id=?")
        if direction in {"incoming", "both"}:
            clauses.append("target_symbol_id=?")
        rows = db.execute(
            "SELECT data FROM ci_relations WHERE project_id=? AND revision=? "
            f"AND kind IN ({placeholders}) AND ({' OR '.join(clauses)}) "
            "ORDER BY source_path, line, kind LIMIT ?",
            [project_id, revision, *sorted(kinds), *([current] * len(clauses)), max_edges + 1],
        ).fetchall()
        for row in rows:
            relation = decode_relation(row["data"])
            key = row["data"]
            if key in seen:
                continue
            seen.add(key)
            if len(edges) >= max_edges:
                truncated = True
                break
            endpoints = {relation["source_symbol_id"], relation["target_symbol_id"]} - {None}
            added = []
            for endpoint in sorted(endpoints - nodes.keys()):
                found = db.execute(
                    "SELECT data FROM ci_symbols WHERE project_id=? AND revision=? AND symbol_id=?",
                    (project_id, revision, endpoint),
                ).fetchone()
                if found:
                    added.append((endpoint, json.loads(found["data"])))
            if len(nodes) + len(added) > max_nodes:
                truncated = True
                continue
            for endpoint, item in added:
                nodes[endpoint] = item
                queue.append((endpoint, level + 1))
            edges.append(relation)
    return {
        "graph": graph,
        "root": symbol_id,
        "direction": direction,
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": edges,
        "truncated": truncated,
        "coverage": "static candidates, not a complete runtime dispatch graph",
    }


def _file_edges(db, project_id: str, revision: int) -> dict[tuple[str, str], dict]:
    edges = {}
    for row in db.execute(
        "SELECT data FROM ci_relations WHERE project_id=? AND revision=? "
        "AND target_path IS NOT NULL AND (source_path<>target_path OR kind='IMPORT') "
        "AND resolution='resolved' "
        "AND kind<>'CONTAINS' ORDER BY source_path, target_path, line",
        (project_id, revision),
    ):
        relation = decode_relation(row["data"])
        key = relation["source_path"], relation["target_path"]
        if key not in edges:
            edges[key] = {
                "source": key[0],
                "target": key[1],
                "kinds": set(),
                "evidence": [],
                "evidence_count": 0,
                "type_only": True,
            }
        edge = edges[key]
        edge["kinds"].add(relation["kind"])
        edge["type_only"] &= relation["type_only"]
        edge["evidence_count"] += 1
        if len(edge["evidence"]) < 5:
            edge["evidence"].append(
                {
                    "kind": relation["kind"],
                    "line": relation["line"],
                    "name": relation["name"],
                    "basis": relation["evidence"],
                }
            )
    for edge in edges.values():
        edge["kinds"] = sorted(edge["kinds"])
    return edges


def file_dependencies(
    db,
    project_id: str,
    revision: int,
    *,
    path: str,
    direction: str = "outgoing",
    depth: int = 1,
    max_nodes: int = 50,
    max_edges: int = 100,
    include_type_only: bool = True,
) -> dict:
    validate_path(path)
    if direction not in {"outgoing", "incoming", "both"}:
        raise ValueError("invalid dependency direction")
    if not 1 <= depth <= 5 or not 1 <= max_nodes <= 200 or not 1 <= max_edges <= 500:
        raise ValueError("invalid dependency bounds")
    if (
        db.execute(
            "SELECT 1 FROM files WHERE project_id=? AND revision=? AND path=?",
            (project_id, revision, path),
        ).fetchone()
        is None
    ):
        raise ValueError("file is not in this code context")
    all_edges = _file_edges(db, project_id, revision)
    adjacency = defaultdict(list)
    for key, edge in all_edges.items():
        if edge["type_only"] and not include_type_only:
            continue
        if direction in {"outgoing", "both"}:
            adjacency[key[0]].append((key[1], key))
        if direction in {"incoming", "both"}:
            adjacency[key[1]].append((key[0], key))
    nodes, selected, queue = {path: {"path": path, "distance": 0}}, {}, deque([path])
    chains, truncated = [], False
    parents = {}
    while queue:
        current = queue.popleft()
        if nodes[current]["distance"] >= depth:
            continue
        for target, key in adjacency[current]:
            if key in selected:
                continue
            if len(selected) >= max_edges or (target not in nodes and len(nodes) >= max_nodes):
                truncated = True
                continue
            selected[key] = all_edges[key]
            if target not in nodes:
                nodes[target] = {"path": target, "distance": nodes[current]["distance"] + 1}
                parents[target] = current
                queue.append(target)
                chain = [target]
                while chain[-1] in parents:
                    chain.append(parents[chain[-1]])
                chains.append(list(reversed(chain)))
    return {
        "path": path,
        "direction": direction,
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": list(selected.values()),
        "chains": chains,
        "truncated": truncated,
        "coverage": "resolved static edges in authorized Python/Java source only",
    }


def _components(nodes: set[str], adjacency: dict) -> list[list[str]]:
    """Iterative Kosaraju: no recursion limit on a long file-dependency chain."""
    reverse = defaultdict(list)
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].append(source)
    order, visited = [], set()
    for root in sorted(nodes):
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(adjacency.get(root, [])))]
        while stack:
            current, children = stack[-1]
            target = next(children, None)
            if target is None:
                order.append(current)
                stack.pop()
            elif target not in visited:
                visited.add(target)
                stack.append((target, iter(adjacency.get(target, []))))
    components, visited = [], set()
    for root in reversed(order):
        if root in visited:
            continue
        component, stack = [], [root]
        visited.add(root)
        while stack:
            current = stack.pop()
            component.append(current)
            for target in reverse[current]:
                if target not in visited:
                    visited.add(target)
                    stack.append(target)
        components.append(sorted(component))
    return components


def project_architecture(
    db,
    project_id: str,
    revision: int,
    *,
    path_prefix: str | None = None,
    max_nodes: int = 100,
    max_edges: int = 200,
    include_type_only: bool = False,
) -> dict:
    if not 1 <= max_nodes <= 500 or not 1 <= max_edges <= 1000:
        raise ValueError("invalid architecture bounds")
    if path_prefix:
        validate_path(path_prefix.rstrip("/"))
    rows = db.execute(
        "SELECT path FROM ci_files WHERE project_id=? AND revision=? "
        "AND language IN ('python', 'java') ORDER BY path",
        (project_id, revision),
    )
    nodes = {r["path"] for r in rows if not path_prefix or r["path"].startswith(path_prefix)}
    edges = _file_edges(db, project_id, revision)
    adjacency = defaultdict(list)
    for (source, target), edge in edges.items():
        if source in nodes and target in nodes and (include_type_only or not edge["type_only"]):
            adjacency[source].append(target)
    components = _components(nodes, adjacency)
    cycle_groups = [
        group for group in components if len(group) > 1 or group[0] in adjacency.get(group[0], [])
    ]
    membership = {node: i for i, group in enumerate(components) for node in group}
    cyclic_memberships = {membership[group[0]] for group in cycle_groups}
    dependencies, dependents = defaultdict(set), defaultdict(set)
    for source, targets in adjacency.items():
        for target in targets:
            a, b = membership[source], membership[target]
            if a != b:
                dependencies[a].add(b)
                dependents[b].add(a)
    counts = {i: len(dependencies[i]) for i in range(len(components))}
    queue = deque(i for i in counts if not counts[i])
    levels = {i: 0 for i in queue}
    while queue:
        current = queue.popleft()
        for parent in dependents[current]:
            levels[parent] = max(levels.get(parent, 0), levels[current] + 1)
            counts[parent] -= 1
            if not counts[parent]:
                queue.append(parent)
    selected = set(sorted(nodes)[:max_nodes])
    layers = [
        {
            "path": node,
            "layer": levels[membership[node]],
            "cycle_group": sorted(components[membership[node]])
            if membership[node] in cyclic_memberships
            else [],
        }
        for node in sorted(selected, key=lambda n: (levels[membership[n]], n))
    ]
    visible_edges = [
        edge
        for (s, t), edge in edges.items()
        if s in selected and t in selected and (include_type_only or not edge["type_only"])
    ]
    cycles = [group for group in cycle_groups if set(group) & selected]
    return {
        "files": layers,
        "edges": visible_edges[:max_edges],
        "cycles": cycles,
        "total_files": len(nodes),
        "total_cycles": len(cycle_groups),
        "truncated": len(nodes) > max_nodes or len(visible_edges) > max_edges,
        "layer_order": "dependency_first; layer 0 = no resolved local dependencies "
        "outside its cycle group",
        "coverage": "condensed static dependency graph; layers are not business semantics",
    }


def external_dependencies(
    db,
    project_id: str,
    revision: int,
    *,
    query: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    _bounds(offset, limit)
    if query is not None and len(query) > 200:
        raise ValueError("dependency query is too long")
    groups = {}
    for row in db.execute(
        "SELECT data FROM ci_relations WHERE project_id=? AND revision=? "
        "AND resolution IN ('external_or_unavailable', 'unavailable') "
        "AND kind='IMPORT' ORDER BY source_path, line",
        (project_id, revision),
    ):
        relation = decode_relation(row["data"])
        name = relation.get("target_module") or relation["name"]
        if query and query.casefold() not in name.casefold():
            continue
        group = groups.setdefault(
            name,
            {
                "module": name,
                "classification": relation["resolution"],
                "used_by": [],
                "usage_count": 0,
            },
        )
        group["usage_count"] += 1
        if len(group["used_by"]) < 10:
            group["used_by"].append({"path": relation["source_path"], "line": relation["line"]})
    items = [groups[key] for key in sorted(groups)]
    return {
        "dependencies": items[offset : offset + limit],
        "total": len(items),
        "offset": offset,
        "has_more": offset + limit < len(items),
        "next_offset": offset + limit if offset + limit < len(items) else None,
        "coverage": "missing imports may be external libraries, ignored local files or typos; "
        "no installed versions or third-party source were inspected",
    }


def impact_analysis(
    db,
    project_id: str,
    revision: int,
    *,
    path: str,
    symbol_id: str | None = None,
    depth: int = 3,
    max_nodes: int = 50,
    max_edges: int = 100,
) -> dict:
    result = file_dependencies(
        db,
        project_id,
        revision,
        path=path,
        direction="incoming",
        depth=depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    if symbol_id:
        symbol = _symbol(db, project_id, revision, symbol_id)
        if symbol["path"] != path:
            raise ValueError("symbol and path do not match in this code context")
        references = find_references(db, project_id, revision, symbol_id=symbol_id, limit=100)
        result["symbol"] = symbol
        result["direct_symbol_references"] = references["references"]
        result["truncated"] |= references["has_more"]
    else:
        row = db.execute(
            "SELECT data FROM ci_symbols WHERE project_id=? AND revision=? AND symbol_id=?",
            (project_id, revision, module_id(path)),
        ).fetchone()
        result["symbol"] = json.loads(row["data"]) if row else None
    result["affected_files"] = [node for node in result.pop("nodes") if node["path"] != path]
    result["interpretation"] = (
        "potential static impact, not a guarantee that tests/runtime "
        "will change; validate relevant source and tests before editing"
    )
    return result


QUERIES = {
    "symbol_search": symbol_search,
    "read_symbol": read_symbol,
    "find_references": find_references,
    "symbol_graph": symbol_graph,
    "file_dependencies": file_dependencies,
    "project_architecture": project_architecture,
    "external_dependencies": external_dependencies,
    "impact_analysis": impact_analysis,
}
