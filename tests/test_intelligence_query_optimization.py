"""Golden compatibility for bounded graph queries and frontier-only fact reads.

The legacy algorithms below are frozen from build 25 so comparison exercises
ordering, call-site evidence and budget semantics independently of the optimized
implementation. Fixtures use both actual fact schemas and actual live compression.
"""

import json
import random
import sqlite3
from collections import defaultdict, deque
from dataclasses import asdict

import pytest

from code_context import intelligence_queries as queries
from code_context.intelligence_index import SCHEMA
from code_context.intelligence_models import CLASS_KINDS, Relation, Symbol, symbol_id
from code_context.intelligence_queries import _symbol, decode_relation
from code_context.live_index import _SCHEMA, _relation_payload
from code_context.policy import validate_path


def legacy_symbol_graph(
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


def legacy_file_edges(db, project_id: str, revision: int) -> dict[tuple[str, str], dict]:
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


def legacy_file_dependencies(
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
    all_edges = legacy_file_edges(db, project_id, revision)
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


def fixture_db(*, live: bool = True, size: int = 45, seed: int = 17):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    if live:
        for statement in _SCHEMA:
            db.execute(statement)
    else:
        db.execute(_SCHEMA[1])
        db.executescript(SCHEMA)
    symbols = [Symbol(f"pkg/f{i:04}.py", "f", "f", "class", 1, 20) for i in range(size)]
    for symbol in symbols:
        db.execute(
            "INSERT INTO files(project_id,source_id,revision,path,size,fingerprint) "
            "VALUES('sample','source',1,?,0,'fixture')",
            (symbol.path,),
        )
        fields = [
            "project_id",
            "revision",
            "symbol_id",
            "path",
            "name",
            "qualname",
            "kind",
            "start_line",
            "end_line",
            "data",
        ]
        args = [
            "sample",
            1,
            symbol.id,
            symbol.path,
            "f",
            "f",
            symbol.kind,
            1,
            20,
            json.dumps(asdict(symbol) | {"symbol_id": symbol.id}),
        ]
        if live:
            fields.append("source_id")
            args.append("source")
        db.execute(
            f"INSERT INTO ci_symbols({','.join(fields)}) VALUES({','.join('?' for _ in args)})",
            args,
        )
    rng = random.Random(seed)
    rows = []

    def add(
        i,
        target,
        *,
        kind="CALL",
        line=3,
        column=1,
        type_only=False,
        resolution="resolved",
        source_id=None,
    ):
        source = symbols[i]
        destination = symbols[target] if target is not None else None
        relation = Relation(
            source_path=source.path,
            kind=kind,
            name=f"f{target}",
            line=line,
            column=column,
            source_symbol_id=source_id or source.id,
            source_qualname="f",
            target_path=destination.path if destination else None,
            target_symbol_id=destination.id if destination else None,
            target_qualname="f" if destination else None,
            resolution=resolution,
            evidence=f"call site {line}:{column}",
            type_only=type_only,
        )
        rows.append(relation)
        return relation

    for i in range(size):
        add(i, (i + 1) % size, line=2)
        add(i, (i + 2) % size, line=4)
        add(i, i, line=1)  # Self call must not duplicate in a bidirectional read.
        add(i, i, kind="IMPORT", line=1)  # Same-file IMPORT remains in file graphs.
        add(i, (i + 1) % size, kind="TYPE_REFERENCE", type_only=True, line=2)
        add(i, (i + 3) % size, kind="TYPE_REFERENCE", type_only=True, line=2)
        add(i, (i + 4) % size, kind="CONTAINS", line=3)
        add(i, (i + 5) % size, kind="INHERITANCE", line=3)
        add(i, None, line=5, resolution="unresolved")
        # Same ordering keys, distinct call sites; raw payload dedup cannot be
        # replaced by endpoint or location-key dedup.
        for column in (2, 3, 4, 5, 6, 7):
            add(i, (i + 1) % size, line=2, column=column)
        for _ in range(3):
            add(i, rng.randrange(size), line=rng.randrange(1, 6), column=rng.randrange(20, 40))
    rows += rows[:4]  # Exact duplicate facts use the original raw-payload identity.
    for relation in rows:
        fields = [
            "project_id",
            "revision",
            "source_path",
            "source_symbol_id",
            "target_path",
            "target_symbol_id",
            "kind",
            "resolution",
            "line",
            "data",
        ]
        args = [
            "sample",
            1,
            relation.source_path,
            relation.source_symbol_id,
            relation.target_path,
            relation.target_symbol_id,
            relation.kind,
            relation.resolution,
            relation.line,
            _relation_payload(relation.to_dict()) if live else json.dumps(relation.to_dict()),
        ]
        if live:
            fields.append("source_id")
            args.append("source")
        db.execute(
            f"INSERT INTO ci_relations({','.join(fields)}) VALUES({','.join('?' for _ in args)})",
            args,
        )
    db.commit()
    return db, symbols


@pytest.fixture(params=[False, True], ids=["mirror-json", "live-compressed"])
def graph_db(request):
    db, symbols = fixture_db(live=request.param)
    try:
        yield db, symbols
    finally:
        db.close()


@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
@pytest.mark.parametrize("depth", [1, 3, 5])
@pytest.mark.parametrize("graph", ["call", "class"])
@pytest.mark.parametrize("bounds", [(1, 1), (5, 8), (25, 50), (200, 500)])
def test_symbol_frontier_matches_legacy_order_evidence_and_caps(
    graph_db, direction, depth, graph, bounds
):
    db, symbols = graph_db
    args = dict(
        symbol_id=symbols[0].id,
        graph=graph,
        direction=direction,
        depth=depth,
        max_nodes=bounds[0],
        max_edges=bounds[1],
    )
    assert queries.symbol_graph(db, "sample", 1, **args) == legacy_symbol_graph(
        db, "sample", 1, **args
    )


@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
@pytest.mark.parametrize("depth", [1, 3, 5])
@pytest.mark.parametrize("include_type_only", [False, True])
@pytest.mark.parametrize("bounds", [(1, 1), (5, 8), (25, 50), (200, 500)])
def test_file_frontier_matches_legacy_chains_order_and_aggregation(
    graph_db, direction, depth, include_type_only, bounds
):
    db, symbols = graph_db
    args = dict(
        path=symbols[0].path,
        direction=direction,
        depth=depth,
        include_type_only=include_type_only,
        max_nodes=bounds[0],
        max_edges=bounds[1],
    )
    assert queries.file_dependencies(db, "sample", 1, **args) == legacy_file_dependencies(
        db, "sample", 1, **args
    )


def test_frontiers_respect_a_small_sqlite_variable_limit(graph_db):
    db, symbols = graph_db
    db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 20)
    for direction in ("outgoing", "incoming", "both"):
        args = dict(
            symbol_id=symbols[0].id,
            graph="class",
            direction=direction,
            depth=5,
            max_nodes=200,
            max_edges=500,
        )
        assert queries.symbol_graph(db, "sample", 1, **args) == legacy_symbol_graph(
            db, "sample", 1, **args
        )
        args = dict(
            path=symbols[0].path, direction=direction, depth=5, max_nodes=200, max_edges=500
        )
        assert queries.file_dependencies(db, "sample", 1, **args) == legacy_file_dependencies(
            db, "sample", 1, **args
        )


def test_file_query_only_decodes_frontier_and_global_architecture_stays_complete(monkeypatch):
    db, symbols = fixture_db(size=500)
    calls = []
    original = queries.decode_relation

    def count(payload):
        result = original(payload)
        calls.append(result)
        return result

    monkeypatch.setattr(queries, "decode_relation", count)
    try:
        queries.file_dependencies(db, "sample", 1, path=symbols[0].path)
        assert 5 < len(calls) < 25
        assert all(r["source_path"] == symbols[0].path for r in calls)
        calls.clear()
        global_edges = queries._file_edges(db, "sample", 1)
        assert len(calls) > 5000
        assert {symbols[0].path, symbols[-1].path} <= {edge[0] for edge in global_edges}
    finally:
        db.close()


def test_layer_queries_batch_symbol_loads_and_use_direction_indexes():
    db, symbols = fixture_db(size=150)
    statements = []
    db.set_trace_callback(statements.append)
    args = dict(
        symbol_id=symbols[0].id,
        graph="call",
        direction="both",
        depth=3,
        max_nodes=200,
        max_edges=500,
    )
    try:
        expected = legacy_symbol_graph(db, "sample", 1, **args)
        legacy_count = len(statements)
        statements.clear()
        assert queries.symbol_graph(db, "sample", 1, **args) == expected
        assert len(statements) < legacy_count / 4
        frontier_statements = [statement for statement in statements if "ROW_NUMBER()" in statement]
        assert len(frontier_statements) == 4  # Eight-endpoint chunks after the root's direct read.
        plan = db.execute("EXPLAIN QUERY PLAN " + frontier_statements[0]).fetchall()
        details = "\n".join(row["detail"] for row in plan)
        assert "ci_relation_source" in details
        assert "ci_relation_target" in details
        assert "source_symbol_id=?" in details
        assert "target_symbol_id=?" in details
        assert "INTEGER PRIMARY KEY (rowid=?)" in details
        assert "r.data" not in frontier_statements[0].split("SELECT ranked.identifier", 1)[0]
    finally:
        db.close()


def test_missing_endpoints_and_payload_identity(graph_db):
    db, symbols = graph_db
    db.execute("DELETE FROM ci_symbols WHERE symbol_id=?", (symbols[1].id,))
    missing = symbol_id("missing.py", "f", 1)
    db.execute(
        "UPDATE ci_relations SET target_symbol_id=? WHERE target_symbol_id=?",
        (missing, symbols[2].id),
    )
    args = dict(
        symbol_id=symbols[0].id, graph="call", direction="both", depth=3, max_nodes=10, max_edges=30
    )
    assert queries.symbol_graph(db, "sample", 1, **args) == legacy_symbol_graph(
        db, "sample", 1, **args
    )


def test_each_frontier_node_has_its_own_row_limit_despite_high_fanout():
    db, symbols = fixture_db(size=650)
    db.execute("DELETE FROM ci_relations")

    def add(source, target, line):
        relation = Relation(
            source_path=symbols[source].path,
            source_symbol_id=symbols[source].id,
            kind="CALL",
            name="f",
            line=line,
            target_path=symbols[target].path,
            target_symbol_id=symbols[target].id,
            resolution="resolved",
            evidence="high fanout fixture",
        )
        db.execute(
            "INSERT INTO ci_relations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sample",
                "source",
                1,
                relation.source_path,
                relation.source_symbol_id,
                relation.target_path,
                relation.target_symbol_id,
                relation.kind,
                relation.resolution,
                relation.line,
                _relation_payload(relation.to_dict()),
            ),
        )

    add(0, 1, 2)
    add(0, 2, 3)
    for target in range(3, 650):
        add(1, target, target)
    add(2, 1, 2)
    args = dict(symbol_id=symbols[0].id, graph="call", depth=2, max_nodes=3, max_edges=3)
    try:
        result = queries.symbol_graph(db, "sample", 1, **args)
        assert result == legacy_symbol_graph(db, "sample", 1, **args)
        assert len(result["edges"]) == 3
        assert result["edges"][-1]["source_symbol_id"] == symbols[2].id
        assert result["truncated"]  # The rejected fanout still reports its limit.
    finally:
        db.close()


def test_live_adapter_can_supply_schema_hint_without_metadata_queries():
    db, symbols = fixture_db()

    class Adapter:
        source_mode = "live"

        def execute(self, sql, args=()):
            assert "pragma_table_info" not in sql
            return db.execute(sql, args)

        def getlimit(self, category):
            return db.getlimit(category)

    try:
        args = dict(symbol_id=symbols[0].id, graph="call", direction="both", depth=3)
        assert queries.symbol_graph(Adapter(), "sample", 1, **args) == legacy_symbol_graph(
            db, "sample", 1, **args
        )
    finally:
        db.close()


def test_exact_edge_budget_with_only_duplicate_later_edges_is_not_truncated():
    db, symbols = fixture_db(size=2)
    db.execute("DELETE FROM ci_relations")
    relation = Relation(
        source_path=symbols[0].path,
        source_symbol_id=symbols[0].id,
        target_path=symbols[1].path,
        target_symbol_id=symbols[1].id,
        kind="CALL",
        name="f",
        line=2,
        resolution="resolved",
    )
    row = (
        "sample",
        "source",
        1,
        relation.source_path,
        relation.source_symbol_id,
        relation.target_path,
        relation.target_symbol_id,
        relation.kind,
        relation.resolution,
        relation.line,
        _relation_payload(relation.to_dict()),
    )
    db.executemany("INSERT INTO ci_relations VALUES(?,?,?,?,?,?,?,?,?,?,?)", [row, row])
    args = dict(symbol_id=symbols[0].id, graph="call", direction="both", depth=5, max_edges=1)
    try:
        result = queries.symbol_graph(db, "sample", 1, **args)
        assert result == legacy_symbol_graph(db, "sample", 1, **args)
        assert len(result["edges"]) == 1
        assert not result["truncated"]
    finally:
        db.close()
