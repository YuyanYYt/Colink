"""Bounded SELECT-only queries over a pinned code-intelligence snapshot."""

import json
import sqlite3
from collections import defaultdict, deque
from itertools import islice

from code_context.fact_payloads import FactCacheError, decode_fact
from code_context.intelligence_dependencies import DependencyClassifier
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
        if language not in {"python", "java", "typescript", "javascript"}:
            raise ValueError("unsupported structure language filter")
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


def _query_chunks(db, values, *, reserved: int = 2, width: int = 1):
    """Keep queries within SQLite's variable limit, including older mirror runtimes."""
    getlimit = getattr(db, "getlimit", None)
    limit = getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) if getlimit else 999
    size = min(256, (limit - reserved) // width)
    if size < 1:
        raise ValueError("SQLite variable limit is too small for an intelligence query")
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _symbol_payloads(db, project_id: str, revision: int, identifiers: list[str]) -> dict:
    found = {}
    for chunk in _query_chunks(db, identifiers):
        marks = ",".join("?" for _ in chunk)
        rows = db.execute(
            "SELECT symbol_id, data FROM ci_symbols WHERE project_id=? AND revision=? "
            f"AND symbol_id IN ({marks})",
            [project_id, revision, *chunk],
        )
        found.update((row["symbol_id"], row["data"]) for row in rows)
    return found


def _symbol_frontier(db, project_id, revision, frontier, kinds, direction, row_limit, *, live):
    """Read each BFS layer with an independent row limit for every frontier node.

    Both directions use their existing endpoint indexes. A self edge occurs in
    only the outgoing branch, while separate call sites remain separate rows.
    Rowid preserves the existing insertion order when source/line/kind tie.
    """
    result = defaultdict(list)
    kind_marks = ",".join("?" for _ in kinds)
    if not live:
        # The immutable mirror predates partial endpoint indexes. Its broad
        # OR scan orders equal source/line/kind keys by the chosen index, which
        # affects historical pagination/budgets. Keep that read unchanged.
        columns = []
        if direction in {"outgoing", "both"}:
            columns.append("source_symbol_id=?")
        if direction in {"incoming", "both"}:
            columns.append("target_symbol_id=?")
        for identifier in frontier:
            result[identifier] = db.execute(
                "SELECT data,source_symbol_id,target_symbol_id FROM ci_relations "
                f"WHERE project_id=? AND revision=? AND kind IN ({kind_marks}) "
                f"AND ({' OR '.join(columns)}) ORDER BY source_path,line,kind LIMIT ?",
                [project_id, revision, *kinds, *([identifier] * len(columns)), row_limit],
            ).fetchall()
        return result
    # Limit each indexed direction before ranking locations/rowids. Ranking
    # every high-fanout match would still allocate a large temporary graph and
    # sort rows which cannot fit in an endpoint's result. The first K rows of
    # a merged order must occur in the first K rows of either direction.
    columns = "r.source_path, r.line, r.kind"
    reserved = (3 + len(kinds)) * (2 if direction == "both" else 1) + 1
    for chunk in _query_chunks(db, frontier, reserved=reserved, width=2):
        if len(chunk) == 1 and direction != "both":
            column, index = (
                ("source_symbol_id", "ci_relation_source")
                if direction == "outgoing"
                else ("target_symbol_id", "ci_relation_target")
            )
            result[chunk[0]] = db.execute(
                "SELECT data,source_symbol_id,target_symbol_id FROM ci_relations "
                f"INDEXED BY {index} WHERE project_id=? AND revision=? "
                f"AND kind IN ({kind_marks}) AND {column}=? AND {column} IS NOT NULL "
                "ORDER BY source_path,line,kind,rowid LIMIT ?",
                [project_id, revision, *kinds, chunk[0], row_limit],
            ).fetchall()
            continue
        values = ",".join("(?,?)" for _ in chunk)
        args = [item for ordinal, identifier in enumerate(chunk) for item in (identifier, ordinal)]
        branches = []
        single = len(chunk) == 1
        for column, index in (
            ("source_symbol_id", "ci_relation_source"),
            ("target_symbol_id", "ci_relation_target"),
        ):
            if column == "source_symbol_id" and direction == "incoming":
                continue
            if column == "target_symbol_id" and direction == "outgoing":
                continue
            self_filter = (
                " AND (candidate.source_symbol_id IS NULL "
                "OR candidate.source_symbol_id<>f.identifier)"
                if direction == "both" and column == "target_symbol_id"
                else ""
            )
            candidate = (
                f"FROM frontier f CROSS JOIN ci_relations candidate INDEXED BY {index} "
                f"WHERE candidate.{column}=f.identifier "
                "AND candidate.project_id=? AND candidate.revision=? "
                f"AND candidate.{column} IS NOT NULL "
                f"AND candidate.kind IN ({kind_marks}){self_filter} "
                "ORDER BY candidate.source_path,candidate.line,candidate.kind,candidate.rowid "
                "LIMIT ?"
            )
            if single:
                # A single endpoint already has its own limit. Avoid ranking
                # and refetching its bounded rows, particularly for a large
                # number of calls from one method in a small source project.
                branches.append(
                    "SELECT * FROM (SELECT f.identifier,candidate.data,"
                    "candidate.source_symbol_id,candidate.target_symbol_id,"
                    "candidate.source_path,candidate.line,candidate.kind,"
                    f"candidate.rowid AS fact_order {candidate})"
                )
            else:
                # Candidate locations remain outside the outer query's row
                # scope; correlate their indexed reads with this frontier.
                candidate = candidate.replace("FROM frontier f CROSS JOIN ", "FROM ")
                branches.append(
                    f"SELECT f.identifier, f.ordinal, r.rowid AS fact_order, {columns} "
                    "FROM frontier f CROSS JOIN ci_relations r WHERE r.rowid IN "
                    f"(SELECT candidate.rowid {candidate})"
                )
            args.extend([project_id, revision, *kinds, row_limit])
        # A frontier-wide LIMIT would silently omit later nodes' neighbors.
        if single and len(branches) == 1:
            sql = f"WITH frontier(identifier,ordinal) AS (VALUES {values}) {branches[0]}"
        elif single:
            sql = (
                f"WITH frontier(identifier,ordinal) AS (VALUES {values}) "
                "SELECT identifier,data,source_symbol_id,target_symbol_id "
                f"FROM ({' UNION ALL '.join(branches)}) "
                "ORDER BY source_path,line,kind,fact_order LIMIT ?"
            )
        else:
            sql = (
                f"WITH frontier(identifier,ordinal) AS (VALUES {values}), "
                f"matches AS ({' UNION ALL '.join(branches)}), ranked AS "
                "(SELECT *, ROW_NUMBER() OVER (PARTITION BY ordinal "
                "ORDER BY source_path,line,kind,fact_order) AS position FROM matches) "
                "SELECT ranked.identifier,r.data,r.source_symbol_id,r.target_symbol_id "
                "FROM ranked CROSS JOIN ci_relations r "
                "WHERE ranked.position<=? AND r.rowid=ranked.fact_order "
                "ORDER BY ranked.ordinal,ranked.position"
            )
        if not single or len(branches) > 1:
            args.append(row_limit)
        for row in db.execute(sql, args):
            result[row["identifier"]].append(row)
    return result


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
    frontier, nodes, edges, seen = [symbol_id], {symbol_id: root}, [], set()
    mode = getattr(db, "source_mode", None)
    live = (
        mode == "live"
        if mode is not None
        else db.execute(
            "SELECT 1 FROM pragma_table_info('ci_relations') WHERE name='source_id'"
        ).fetchone()
        is not None
    )
    truncated = False
    for _level in range(depth):
        if not frontier:
            break
        next_frontier = []
        # Bound a high-fanout window to at most 8 * 2 * (max_edges + 1)
        # candidate locations, and stop before querying later full-budget groups.
        for start in range(0, len(frontier), 8):
            group = frontier[start : start + 8]
            adjacency = _symbol_frontier(
                db, project_id, revision, group, sorted(kinds), direction, max_edges + 1, live=live
            )
            # Load small groups of endpoint payloads; do not deserialize a
            # whole project or decode relations past a node's limit.
            entries = ((current, row) for current in group for row in adjacency[current])
            stopped = set()
            while block := list(islice(entries, 128)):
                identifiers = {
                    row[column]
                    for current, row in block
                    for column in ("source_symbol_id", "target_symbol_id")
                    if row[column] is not None
                    and row[column] not in nodes
                    and current not in stopped
                }
                payloads = _symbol_payloads(db, project_id, revision, sorted(identifiers))
                for current, row in block:
                    if current in stopped:
                        continue
                    relation = decode_relation(row["data"])
                    key = row["data"]
                    if key in seen:
                        continue
                    seen.add(key)
                    if len(edges) >= max_edges:
                        truncated = True
                        stopped.add(current)
                        break
                    endpoints = {relation["source_symbol_id"], relation["target_symbol_id"]} - {
                        None
                    }
                    added = []
                    for endpoint in sorted(endpoints - nodes.keys()):
                        # Legacy payloads remain authoritative if a scalar
                        # column disagrees; source guards handle corrupt caches.
                        if endpoint not in identifiers and endpoint not in payloads:
                            payloads.update(_symbol_payloads(db, project_id, revision, [endpoint]))
                        if endpoint in payloads:
                            added.append((endpoint, json.loads(payloads[endpoint])))
                    if len(nodes) + len(added) > max_nodes:
                        truncated = True
                        continue
                    for endpoint, item in added:
                        nodes[endpoint] = item
                        next_frontier.append(endpoint)
                    edges.append(relation)
                if len(edges) >= max_edges and truncated:
                    break
            # Once the edge budget is exhausted and its truncation is known,
            # no later node can change the result. Source pre/post guards still
            # validate every participating file outside this query function.
            if len(edges) >= max_edges and truncated:
                break
        frontier = next_frontier
        if len(edges) >= max_edges and truncated:
            break
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


def _aggregate_file_edges(rows) -> dict[tuple[str, str], dict]:
    edges = {}
    for row in rows:
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


def _file_edges(db, project_id: str, revision: int) -> dict[tuple[str, str], dict]:
    """The full graph is still required by architecture/SCC queries."""
    return _aggregate_file_edges(
        db.execute(
            "SELECT data FROM ci_relations WHERE project_id=? AND revision=? "
            "AND target_path IS NOT NULL AND (source_path<>target_path OR kind='IMPORT') "
            "AND resolution='resolved' "
            "AND kind<>'CONTAINS' ORDER BY source_path, target_path, line",
            (project_id, revision),
        )
    )


def _file_frontier(db, project_id, revision, frontier, direction):
    """Aggregate only complete file pairs incident to this bounded BFS layer."""
    result = {}
    branches_count = 2 if direction == "both" else 1
    for chunk in _query_chunks(db, frontier, reserved=2 * branches_count):
        values = ",".join("(?)" for _ in chunk)
        args = list(chunk)
        branches = []
        for column in ("source_path", "target_path"):
            if column == "source_path" and direction == "incoming":
                continue
            if column == "target_path" and direction == "outgoing":
                continue
            # Only the source-path index is guaranteed in the legacy schema.
            # Incoming reads can use the optional target-path index when present.
            index = " INDEXED BY ci_relation_file" if column == "source_path" else ""
            duplicate_filter = (
                " AND NOT EXISTS (SELECT 1 FROM frontier s WHERE s.path=r.source_path)"
                if direction == "both" and column == "target_path"
                else ""
            )
            branches.append(
                "SELECT r.data, r.source_path, r.target_path, r.line, r.rowid AS fact_order "
                f"FROM ci_relations r{index} "
                f"WHERE r.{column} IN (SELECT path FROM frontier) "
                "AND r.project_id=? AND r.revision=? AND r.target_path IS NOT NULL "
                "AND (r.source_path<>r.target_path OR r.kind='IMPORT') "
                f"AND r.resolution='resolved' AND r.kind<>'CONTAINS'{duplicate_filter}"
            )
            args.extend([project_id, revision])
        rows = db.execute(
            f"WITH frontier(path) AS (VALUES {values}) "
            f"SELECT data FROM ({' UNION ALL '.join(branches)}) "
            "ORDER BY source_path,target_path,line,fact_order",
            args,
        )
        # A pair encountered in two chunks is complete in either chunk, so
        # overwrite rather than add its evidence counts twice.
        result.update(_aggregate_file_edges(rows))
    return {key: result[key] for key in sorted(result)}


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
    nodes, selected, frontier = {path: {"path": path, "distance": 0}}, {}, [path]
    chains, truncated = [], False
    parents = {}
    for _level in range(depth):
        if not frontier:
            break
        all_edges = _file_frontier(db, project_id, revision, frontier, direction)
        adjacency = defaultdict(list)
        for key, edge in all_edges.items():
            if edge["type_only"] and not include_type_only:
                continue
            if direction in {"outgoing", "both"}:
                adjacency[key[0]].append((key[1], key))
            if direction in {"incoming", "both"}:
                adjacency[key[1]].append((key[0], key))
        next_frontier = []
        for current in frontier:
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
                    next_frontier.append(target)
                    chain = [target]
                    while chain[-1] in parents:
                        chain.append(parents[chain[-1]])
                    chains.append(list(reversed(chain)))
        frontier = next_frontier
    return {
        "path": path,
        "direction": direction,
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": list(selected.values()),
        "chains": chains,
        "truncated": truncated,
        "coverage": "resolved static edges in authorized indexed source only",
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
        "AND language IN ('python', 'java', 'typescript', 'javascript') ORDER BY path",
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
    if getattr(db, "source_mode", None) == "live":
        row = db.execute(
            "SELECT stats FROM li_projects WHERE project_id=?", (project_id,)
        ).fetchone()
    else:
        row = db.execute(
            "SELECT stats FROM ci_snapshots WHERE project_id=? AND revision=?",
            (project_id, revision),
        ).fetchone()
    stats = json.loads(row["stats"]) if row is not None else {}
    metadata = stats.get("dependency_metadata", {})
    metadata_diagnostics = [
        *metadata.get("diagnostics", []),
        *(
            {**diagnostic, "path": manifest["path"]}
            for manifest in metadata.get("manifests", [])
            for diagnostic in manifest.get("diagnostics", [])
        ),
    ]
    if not metadata:
        metadata_diagnostics.append({"code": "DEPENDENCY_METADATA_UNAVAILABLE"})
    files = list(
        db.execute(
            "SELECT path, language, module FROM ci_files WHERE project_id=? AND revision=?",
            (project_id, revision),
        )
    )
    languages = {item["path"]: item["language"] for item in files}
    classifier = DependencyClassifier(files, metadata, tuple(stats.get("python_source_roots", ())))
    local_imports_omitted = db.execute(
        "SELECT COUNT(*) FROM ci_relations WHERE project_id=? AND revision=? "
        "AND kind='IMPORT' AND resolution='resolved' AND target_path IS NOT NULL",
        (project_id, revision),
    ).fetchone()[0]
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
        language = languages.get(relation["source_path"], "unknown")
        classification = classifier.classify(relation, language)
        group = groups.setdefault(
            (language, name, classification["dependency_kind"]),
            {
                "module": name,
                "language": language,
                "classification": relation["resolution"],
                **classification,
                "used_by": [],
                "usage_count": 0,
            },
        )
        for evidence in classification["dependency_evidence"]:
            if (
                evidence not in group["dependency_evidence"]
                and len(group["dependency_evidence"]) < 4
            ):
                group["dependency_evidence"].append(evidence)
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
        "local_imports_omitted": local_imports_omitted,
        "local_dependency_query": "file_dependencies",
        "dependency_metadata_partial": metadata.get("partial", True),
        "dependency_manifest_count": len(metadata.get("manifests", [])),
        "dependency_metadata_diagnostics": metadata_diagnostics[:5],
        "dependency_metadata_diagnostics_omitted": max(0, len(metadata_diagnostics) - 5),
        "coverage": "missing imports only; dependency_kind is static provenance, "
        "not installation, version compatibility or symbol availability. Resolved local "
        "imports are omitted; use file_dependencies. Unknown imports can be excluded "
        "local modules, undeclared libraries or typos. No installed packages, "
        "third-party source or dependency resolver were inspected",
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
