"""Producer-owned, bounded derived indexes committed with source snapshots.

Queries never call this module. The immutable source blobs remain authoritative;
ASTs are discarded after extraction and caches follow the two-state source window.
"""

import json
import sqlite3
import time
from pathlib import PurePosixPath

from code_context.intelligence_models import PARSER_VERSION, ParsedFile
from code_context.intelligence_resolver import Resolver

MAX_SNAPSHOT_PARSE_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_INDEX_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_FACTS = 100_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS ci_snapshots (
    project_id TEXT NOT NULL, revision INTEGER NOT NULL,
    parser_version TEXT NOT NULL, stats TEXT NOT NULL,
    PRIMARY KEY (project_id, revision),
    FOREIGN KEY (project_id, revision) REFERENCES snapshots(project_id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ci_parse_cache (
    project_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (project_id, path, sha256, parser_version),
    FOREIGN KEY (sha256) REFERENCES blobs(sha256) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ci_files (
    project_id TEXT NOT NULL, revision INTEGER NOT NULL, path TEXT NOT NULL,
    language TEXT NOT NULL, status TEXT NOT NULL, module TEXT NOT NULL, diagnostics TEXT NOT NULL,
    PRIMARY KEY (project_id, revision, path),
    FOREIGN KEY (project_id, revision) REFERENCES snapshots(project_id, revision) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ci_symbols (
    project_id TEXT NOT NULL, revision INTEGER NOT NULL, symbol_id TEXT NOT NULL,
    path TEXT NOT NULL, name TEXT NOT NULL, qualname TEXT NOT NULL, kind TEXT NOT NULL,
    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (project_id, revision, symbol_id),
    FOREIGN KEY (project_id, revision) REFERENCES snapshots(project_id, revision) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ci_symbol_name ON ci_symbols(project_id, revision, name);
CREATE INDEX IF NOT EXISTS ci_symbol_path ON ci_symbols(project_id, revision, path);
CREATE TABLE IF NOT EXISTS ci_relations (
    project_id TEXT NOT NULL, revision INTEGER NOT NULL,
    source_path TEXT NOT NULL, source_symbol_id TEXT,
    target_path TEXT, target_symbol_id TEXT, kind TEXT NOT NULL,
    resolution TEXT NOT NULL, line INTEGER NOT NULL, data TEXT NOT NULL,
    FOREIGN KEY (project_id, revision) REFERENCES snapshots(project_id, revision) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ci_relation_source
    ON ci_relations(project_id, revision, source_symbol_id);
CREATE INDEX IF NOT EXISTS ci_relation_target
    ON ci_relations(project_id, revision, target_symbol_id);
CREATE INDEX IF NOT EXISTS ci_relation_file ON ci_relations(project_id, revision, source_path);
"""


def encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def prune_index(db: sqlite3.Connection) -> None:
    db.execute(
        "DELETE FROM ci_parse_cache WHERE parser_version<>? OR NOT EXISTS "
        "(SELECT 1 FROM files f WHERE f.project_id=ci_parse_cache.project_id "
        "AND f.path=ci_parse_cache.path AND f.sha256=ci_parse_cache.sha256)",
        (PARSER_VERSION,),
    )


def parse_file(path: str, content: str) -> ParsedFile:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".py", ".pyi"}:
        from code_context.intelligence_python import parse_python

        return parse_python(path, content)
    if suffix == ".java":
        from code_context.intelligence_java import parse_java

        return parse_java(path, content)
    return ParsedFile(path=path, language="unsupported", status="unsupported")


def build_index(db: sqlite3.Connection, project_id: str, revision: int) -> None:
    """Extract changed hashes only; copy stable bindings, rebuild when topology changes.

    This runs inside the source producer's transaction. A parser/resource diagnostic
    limits derived intelligence, not ordinary source reading. Unexpected implementation
    or storage failures roll back both the source and index rather than mix states.
    """
    started = time.perf_counter()
    parsed_files: dict[str, ParsedFile] = {}
    parse_bytes = parsed_count = reused_count = 0
    limited_paths = set()
    for row in db.execute(
        "SELECT f.path, f.sha256, b.content FROM files f JOIN blobs b USING(sha256) "
        "WHERE f.project_id=? AND f.revision=? ORDER BY f.path",
        (project_id, revision),
    ):
        path, sha = row["path"], row["sha256"]
        cached = db.execute(
            "SELECT data FROM ci_parse_cache WHERE project_id=? AND path=? AND sha256=? "
            "AND parser_version=?",
            (project_id, path, sha, PARSER_VERSION),
        ).fetchone()
        if cached:
            data = cached["data"]
            parsed = ParsedFile.from_dict(json.loads(data))
            reused_count += int(parsed.language != "unsupported")
        else:
            parsed = parse_file(path, row["content"])
            parsed_count += int(parsed.language != "unsupported")
            data = encode(parsed.to_dict())
        size = len(data.encode("utf-8"))
        if parse_bytes + size > MAX_SNAPSHOT_PARSE_BYTES:
            parsed = ParsedFile(
                path=path,
                language=parsed.language,
                status="resource_limited",
                module=parsed.module,
                diagnostics=[{"code": "PROJECT_PARSE_BUDGET_EXCEEDED"}],
            )
            data = encode(parsed.to_dict())
            limited_paths.add(path)
        else:
            parse_bytes += size
            if not cached:
                db.execute(
                    "INSERT OR IGNORE INTO ci_parse_cache VALUES(?, ?, ?, ?, ?)",
                    (project_id, path, sha, PARSER_VERSION, data),
                )
        parsed_files[path] = parsed
    resolver = Resolver(parsed_files)
    prior_files = {}
    for row in db.execute(
        "SELECT c.path, c.data FROM files f JOIN ci_parse_cache c "
        "ON c.project_id=f.project_id AND c.path=f.path AND c.sha256=f.sha256 "
        "AND c.parser_version=? WHERE f.project_id=? AND f.revision=?",
        (PARSER_VERSION, project_id, revision - 1),
    ):
        prior_files[row["path"]] = ParsedFile.from_dict(json.loads(row["data"]))
    prior = db.execute(
        "SELECT stats, parser_version FROM ci_snapshots WHERE project_id=? AND revision=?",
        (project_id, revision - 1),
    ).fetchone()
    stable = (
        bool(prior)
        and prior["parser_version"] == PARSER_VERSION
        and not json.loads(prior["stats"]).get("partial")
        and not limited_paths
        and resolver.topology() == Resolver(prior_files).topology()
    )
    previous_hashes = dict(
        db.execute(
            "SELECT path, sha256 FROM files WHERE project_id=? AND revision=?",
            (project_id, revision - 1),
        ).fetchall()
    )
    current_hashes = dict(
        db.execute(
            "SELECT path, sha256 FROM files WHERE project_id=? AND revision=?",
            (project_id, revision),
        ).fetchall()
    )
    index_bytes = facts_count = resolved_count = copied_count = 0
    languages: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for path, parsed in sorted(parsed_files.items()):
        languages[parsed.language] = languages.get(parsed.language, 0) + 1
        symbol_rows = []
        relation_rows = []
        copied = stable and previous_hashes.get(path) == current_hashes[path]
        if copied:
            relations = [
                json.loads(row["data"])
                for row in db.execute(
                    "SELECT data FROM ci_relations WHERE project_id=? AND revision=? "
                    "AND source_path=? ORDER BY rowid",
                    (project_id, revision - 1, path),
                )
            ]
            copied_count += int(parsed.language != "unsupported")
        else:
            relations = [relation.to_dict() for relation in resolver.resolve_file(path)]
            resolved_count += int(parsed.language != "unsupported")
        for symbol in parsed.symbols:
            from dataclasses import asdict

            data = encode({**asdict(symbol), "symbol_id": symbol.id, "language": parsed.language})
            symbol_rows.append(
                (
                    project_id,
                    revision,
                    symbol.id,
                    path,
                    symbol.name,
                    symbol.qualname,
                    symbol.kind,
                    symbol.start_line,
                    symbol.end_line,
                    data,
                )
            )
        for relation in relations:
            data = encode(relation)
            relation_rows.append(
                (
                    project_id,
                    revision,
                    path,
                    relation["source_symbol_id"],
                    relation["target_path"],
                    relation["target_symbol_id"],
                    relation["kind"],
                    relation["resolution"],
                    relation["line"],
                    data,
                )
            )
        cost = sum(len(row[-1].encode("utf-8")) for row in [*symbol_rows, *relation_rows])
        count = len(symbol_rows) + len(relation_rows)
        if (
            index_bytes + cost > MAX_SNAPSHOT_INDEX_BYTES
            or facts_count + count > MAX_SNAPSHOT_FACTS
        ):
            symbol_rows, relation_rows = [], []
            parsed.status = "resource_limited"
            parsed.diagnostics = [{"code": "PROJECT_INDEX_BUDGET_EXCEEDED"}]
            limited_paths.add(path)
        else:
            index_bytes += cost
            facts_count += count
        statuses[parsed.status] = statuses.get(parsed.status, 0) + 1
        db.execute(
            "INSERT INTO ci_files VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                revision,
                path,
                parsed.language,
                parsed.status,
                parsed.module,
                encode({"items": parsed.diagnostics[:20]}),
            ),
        )
        db.executemany("INSERT INTO ci_symbols VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", symbol_rows)
        db.executemany(
            "INSERT INTO ci_relations VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", relation_rows
        )
    stats = {
        "supported_languages": ["python", "java"],
        "files_by_language": languages,
        "files_by_status": statuses,
        "partial": any(s not in {"ready", "unsupported"} for s in statuses),
        "parsed_files": parsed_count,
        "reused_parse_files": reused_count,
        "resolved_files": resolved_count,
        "reused_relation_files": copied_count,
        "parse_payload_bytes": parse_bytes,
        "index_payload_bytes": index_bytes,
        "fact_count": facts_count,
        "build_ms": round((time.perf_counter() - started) * 1000, 3),
        "limits": {
            "parse_payload_bytes_per_state": MAX_SNAPSHOT_PARSE_BYTES,
            "index_payload_bytes_per_state": MAX_SNAPSHOT_INDEX_BYTES,
            "facts_per_state": MAX_SNAPSHOT_FACTS,
            "retained_states": 2,
        },
        "analysis": "static; unresolved targets are not runtime facts",
    }
    db.execute(
        "INSERT INTO ci_snapshots VALUES(?, ?, ?, ?)",
        (project_id, revision, PARSER_VERSION, encode(stats)),
    )


def backfill_indexes(db: sqlite3.Connection) -> None:
    for row in db.execute(
        "SELECT project_id, revision FROM snapshots ORDER BY project_id, revision"
    ):
        indexed = db.execute(
            "SELECT parser_version FROM ci_snapshots WHERE project_id=? AND revision=?",
            (row["project_id"], row["revision"]),
        ).fetchone()
        if indexed and indexed["parser_version"] == PARSER_VERSION:
            continue
        for table in ("ci_relations", "ci_symbols", "ci_files", "ci_snapshots"):
            db.execute(
                f"DELETE FROM {table} WHERE project_id=? AND revision=?",
                (row["project_id"], row["revision"]),
            )
        build_index(db, row["project_id"], row["revision"])
    prune_index(db)
