"""Small immutable repository snapshots, stored transactionally in SQLite."""

import difflib
import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from code_context.intelligence_index import SCHEMA, backfill_indexes, build_index, prune_index
from code_context.intelligence_queries import QUERIES, bound_result, index_status
from code_context.models import SyncBatch, validate_project
from code_context.policy import MAX_FILES, MAX_TOTAL_BYTES, validate_path
from code_context.source_page import source_page


class MirrorError(ValueError):
    pass


class RevisionConflict(MirrorError):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__(f"revision conflict: remote revision is {current_revision}")


class MirrorStore:
    retained_snapshots = 2

    def __init__(self, database: Path):
        self.database = database.expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise MirrorError(f"unsupported database schema: {version}")
            if version == 0:
                db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, snapshot TEXT NOT NULL,
                    PRIMARY KEY (project_id, revision)
                );
                CREATE TABLE IF NOT EXISTS blobs (
                    sha256 TEXT PRIMARY KEY, content TEXT NOT NULL, size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES blobs(sha256),
                    PRIMARY KEY (project_id, revision, path),
                    FOREIGN KEY (project_id, revision) REFERENCES snapshots(project_id, revision)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    project_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY (project_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS files_sha256 ON files(sha256);
                CREATE TABLE IF NOT EXISTS maintenance (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
                """
            )
            # Optional derived tables leave the source schema/protocol at version 2/1.
            # Cascades also allow the older read-only binary to prune retained states.
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(snapshots)")}
            if "snapshot" not in columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN snapshot TEXT")
            self._prune_history(db)
            for row in db.execute(
                "SELECT project_id, revision FROM snapshots WHERE snapshot IS NULL"
            ):
                db.execute(
                    "UPDATE snapshots SET snapshot=? WHERE project_id=? AND revision=?",
                    (self._new_snapshot(), row["project_id"], row["revision"]),
                )
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS snapshot_handle ON snapshots(snapshot)")
            if version == 1 or db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                db.execute("INSERT OR REPLACE INTO maintenance VALUES('compaction_pending', 1)")
            compact_pending = db.execute(
                "SELECT 1 FROM maintenance WHERE key='compaction_pending'"
            ).fetchone()
            db.execute("PRAGMA user_version=2")
            backfill_indexes(db)
        if compact_pending:
            # One-time migration: actually shrink old databases, not just their row count.
            self.compact()

    @staticmethod
    def _new_snapshot() -> str:
        return "ctx_" + secrets.token_hex(16)

    @staticmethod
    def _prune_history(db: sqlite3.Connection) -> None:
        db.execute(
            "DELETE FROM files WHERE EXISTS (SELECT 1 FROM projects p "
            "WHERE p.project_id=files.project_id AND files.revision < p.revision-1)"
        )
        db.execute(
            "DELETE FROM snapshots WHERE EXISTS (SELECT 1 FROM projects p "
            "WHERE p.project_id=snapshots.project_id AND snapshots.revision < p.revision-1)"
        )
        db.execute(
            "DELETE FROM requests WHERE NOT EXISTS (SELECT 1 FROM snapshots s "
            "WHERE s.project_id=requests.project_id AND s.revision=requests.revision)"
        )
        db.execute(
            "DELETE FROM blobs WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.sha256=blobs.sha256)"
        )

    def compact(self) -> None:
        """Reclaim allocated free pages; used once when upgrading the historical store."""
        with self.connection() as db:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("VACUUM")
            checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0]:
                raise sqlite3.OperationalError(
                    "database compaction checkpoint is busy; retry startup"
                )
            db.execute("DELETE FROM maintenance WHERE key='compaction_pending'")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA journal_size_limit=4194304")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def read_connection(self):
        with self.connection() as db:
            # Pin all SELECTs in a read to one committed state while the writer prunes.
            db.execute("BEGIN")
            yield db

    def resolve_snapshot(self, project_id: str, snapshot: str | None = None) -> tuple[int, str]:
        """Resolve current/previous or an opaque handle; never fall back from an expired handle."""
        with self.read_connection() as db:
            return self._resolve_snapshot(db, project_id, snapshot)

    def code_query(
        self,
        project_id: str,
        snapshot: str | None,
        operation: str,
        *,
        max_chars: int = 20_000,
        **parameters,
    ) -> dict:
        """Only SELECTs; resolve context and all derived/source rows in one transaction."""
        if operation not in QUERIES:
            raise MirrorError("unknown code-intelligence query")
        try:
            with self.read_connection() as db:
                revision, handle = self._resolve_snapshot(db, project_id, snapshot)
                status = index_status(db, project_id, revision)
                if operation == "read_symbol":
                    parameters["text_budget"] = max(1000, max_chars - 2000)
                    parameters["response_budget"] = max_chars - 500
                result = QUERIES[operation](db, project_id, revision, **parameters)
                return bound_result(
                    {
                        **result,
                        "project_id": project_id,
                        "snapshot": handle,
                        "index_partial": status["partial"],
                    },
                    max_chars,
                )
        except ValueError as exc:
            raise MirrorError(str(exc)) from None

    def code_index_status(self, project_id: str, revision: int) -> dict:
        with self.read_connection() as db:
            self._revision(db, project_id, revision)
            return index_status(db, project_id, revision)

    @staticmethod
    def _resolve_snapshot(
        db: sqlite3.Connection, project_id: str, snapshot: str | None
    ) -> tuple[int, str]:
        validate_project(project_id)
        if snapshot is None or snapshot == "current":
            row = db.execute(
                "SELECT s.revision, s.snapshot FROM snapshots s JOIN projects p "
                "ON p.project_id=s.project_id AND p.revision=s.revision WHERE p.project_id=?",
                (project_id,),
            ).fetchone()
        elif snapshot == "previous":
            row = db.execute(
                "SELECT revision, snapshot FROM snapshots WHERE project_id=? "
                "ORDER BY revision DESC LIMIT 1 OFFSET 1",
                (project_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT revision, snapshot FROM snapshots WHERE project_id=? AND snapshot=?",
                (project_id, snapshot),
            ).fetchone()
        if row is None:
            if (
                db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone()
                is None
            ):
                raise MirrorError(
                    "PROJECT_NOT_FOUND: select an available project from list_projects"
                )
            if snapshot == "previous":
                raise MirrorError(
                    "NO_PREVIOUS_SNAPSHOT: no preceding code state exists; "
                    "use current code or wait for a source change"
                )
            raise MirrorError(
                "SNAPSHOT_EXPIRED: code context is unavailable or expired; "
                "restart the analysis from repo_overview"
            )
        return row["revision"], row["snapshot"]

    @staticmethod
    def _revision(db: sqlite3.Connection, project_id: str, revision: int | None) -> int:
        validate_project(project_id)
        if revision is None:
            row = db.execute(
                "SELECT revision FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT revision FROM snapshots WHERE project_id=? AND revision=?",
                (project_id, revision),
            ).fetchone()
        if row is None:
            raise MirrorError("project or snapshot not found")
        return row["revision"]

    def apply(self, project_id: str, batch: SyncBatch) -> dict:
        validate_project(project_id)
        payload_hash = hashlib.sha256(batch.model_dump_json().encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT payload_hash, revision FROM requests WHERE project_id=? AND request_id=?",
                (project_id, batch.request_id),
            ).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise MirrorError("request_id was already used for a different payload")
                return {
                    "project_id": project_id,
                    "revision": previous["revision"],
                    "replayed": True,
                }
            row = db.execute(
                "SELECT revision FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            current = row["revision"] if row else 0
            if batch.base_revision != current:
                raise RevisionConflict(current)
            if current == 0 and batch.mode != "full":
                raise MirrorError("the first synchronization must be a full snapshot")
            # A full message is a replacement. Only allow it on a new project in V1;
            # existing projects always use a checked delta, including policy removals.
            if current > 0 and batch.mode == "full":
                raise MirrorError("existing projects require delta synchronization")
            files = {
                row["path"]: row["sha256"]
                for row in db.execute(
                    "SELECT path, sha256 FROM files WHERE project_id=? AND revision=?",
                    (project_id, current),
                )
            }
            for change in batch.changes:
                if change.op == "delete":
                    files.pop(change.path, None)
                else:
                    size = len(change.content.encode("utf-8"))
                    db.execute(
                        "INSERT OR IGNORE INTO blobs(sha256, content, size) VALUES(?, ?, ?)",
                        (change.sha256, change.content, size),
                    )
                    files[change.path] = change.sha256
            if len(files) > MAX_FILES:
                raise MirrorError(f"snapshot exceeds {MAX_FILES} files")
            # Bound the stored view even when many individual deltas arrive.
            sizes = {
                r["sha256"]: r["size"]
                for r in db.execute(
                    "SELECT DISTINCT b.sha256, b.size FROM blobs b JOIN files f USING(sha256) "
                    "WHERE f.project_id=? AND f.revision=?",
                    (project_id, current),
                )
            }
            sizes.update(
                (change.sha256, len(change.content.encode("utf-8")))
                for change in batch.changes
                if change.op == "upsert"
            )
            if sum(sizes[sha] for sha in files.values()) > MAX_TOTAL_BYTES:
                raise MirrorError(f"snapshot exceeds {MAX_TOTAL_BYTES} bytes of source text")
            revision = current + 1
            db.execute(
                "INSERT INTO snapshots(project_id, revision, created_at, snapshot) "
                "VALUES(?, ?, ?, ?)",
                (project_id, revision, datetime.now(UTC).isoformat(), self._new_snapshot()),
            )
            db.executemany(
                "INSERT INTO files VALUES(?, ?, ?, ?)",
                [(project_id, revision, path, sha) for path, sha in sorted(files.items())],
            )
            db.execute(
                "INSERT INTO projects VALUES(?, ?) ON CONFLICT(project_id) "
                "DO UPDATE SET revision=excluded.revision",
                (project_id, revision),
            )
            db.execute(
                "INSERT INTO requests VALUES(?, ?, ?, ?)",
                (project_id, batch.request_id, payload_hash, revision),
            )
            build_index(db, project_id, revision)
            self._prune_history(db)
            prune_index(db)
            # Small incremental reclamation plus page reuse bounds disk growth without
            # a full VACUUM on every edit. Long readers can defer WAL checkpointing.
            db.execute("PRAGMA incremental_vacuum(64)").fetchall()
            return {"project_id": project_id, "revision": revision, "replayed": False}

    def list_projects(self) -> dict:
        with self.read_connection() as db:
            rows = db.execute(
                "SELECT p.project_id, p.revision, s.snapshot, s.created_at, "
                "COUNT(f.path) AS file_count "
                "FROM projects p JOIN snapshots s ON p.project_id=s.project_id "
                "AND p.revision=s.revision LEFT JOIN files f ON f.project_id=p.project_id "
                "AND f.revision=p.revision GROUP BY p.project_id ORDER BY p.project_id"
            ).fetchall()
        return {"projects": [dict(row) for row in rows]}

    def manifest(self, project_id: str, revision: int | None = None) -> dict:
        with self.read_connection() as db:
            revision = self._revision(db, project_id, revision)
            rows = db.execute(
                "SELECT f.path, f.sha256, b.size FROM files f JOIN blobs b USING(sha256) "
                "WHERE f.project_id=? AND f.revision=? ORDER BY f.path",
                (project_id, revision),
            ).fetchall()
            created = db.execute(
                "SELECT created_at FROM snapshots WHERE project_id=? AND revision=?",
                (project_id, revision),
            ).fetchone()[0]
        return {
            "project_id": project_id,
            "revision": revision,
            "created_at": created,
            "files": [dict(row) for row in rows],
            "file_count": len(rows),
        }

    def repo_overview(
        self, project_id: str, revision: int | None = None, offset: int = 0, limit: int = 200
    ) -> dict:
        if offset < 0 or not 1 <= limit <= 1000:
            raise MirrorError("offset must be nonnegative and limit must be 1-1000")
        with self.read_connection() as db:
            revision = self._revision(db, project_id, revision)
            total = db.execute(
                "SELECT COUNT(*) FROM files WHERE project_id=? AND revision=?",
                (project_id, revision),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT f.path, f.sha256, b.size FROM files f JOIN blobs b USING(sha256) "
                "WHERE f.project_id=? AND f.revision=? ORDER BY f.path LIMIT ? OFFSET ?",
                (project_id, revision, limit, offset),
            ).fetchall()
            created = db.execute(
                "SELECT created_at FROM snapshots WHERE project_id=? AND revision=?",
                (project_id, revision),
            ).fetchone()[0]
            return {
                "project_id": project_id,
                "revision": revision,
                "created_at": created,
                "files": [dict(row) for row in rows],
                "file_count": total,
                "offset": offset,
                "has_more": offset + len(rows) < total,
                "code_intelligence": index_status(db, project_id, revision),
            }

    def read_file(
        self,
        project_id: str,
        path: str,
        revision: int | None = None,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 20_000,
        char_offset: int = 0,
    ) -> dict:
        validate_path(path)
        if not 1000 <= max_chars <= 50_000:
            raise MirrorError("max_chars must be between 1000 and 50000")
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise MirrorError("invalid line range")
        if end_line is None:
            end_line = start_line + 199
        if end_line - start_line + 1 > 1000:
            raise MirrorError("read at most 1000 lines at a time")
        with self.read_connection() as db:
            revision = self._revision(db, project_id, revision)
            row = db.execute(
                "SELECT b.content, b.sha256, b.size FROM files f JOIN blobs b USING(sha256) "
                "WHERE f.project_id=? AND f.revision=? AND f.path=?",
                (project_id, revision, path),
            ).fetchone()
            if row is None:
                raise MirrorError("file not found in this snapshot")
        try:
            page = source_page(row["content"], start_line, end_line, max_chars, char_offset)
        except ValueError as exc:
            raise MirrorError(str(exc)) from None
        return {
            "project_id": project_id,
            "revision": revision,
            "path": path,
            "sha256": row["sha256"],
            "size": row["size"],
            **page,
        }

    def search_code(
        self,
        project_id: str,
        query: str,
        revision: int | None = None,
        limit: int = 50,
    ) -> dict:
        if not query or len(query) > 200 or not 1 <= limit <= 200:
            raise MirrorError("query must be 1-200 characters; limit must be 1-200")
        with self.read_connection() as db:
            revision = self._revision(db, project_id, revision)
            rows = db.execute(
                "SELECT f.path, b.content FROM files f JOIN blobs b USING(sha256) "
                "WHERE f.project_id=? AND f.revision=? ORDER BY f.path",
                (project_id, revision),
            ).fetchall()
        matches = []
        for row in rows:
            for number, line in enumerate(row["content"].splitlines(), start=1):
                if query in line:
                    if len(matches) == limit:
                        return {
                            "project_id": project_id,
                            "revision": revision,
                            "matches": matches,
                            "has_more": True,
                        }
                    column = line.index(query)
                    begin = max(0, column - 200)
                    matches.append(
                        {
                            "path": row["path"],
                            "line": number,
                            "text": line[begin : begin + 1000],
                            "column": column + 1,
                            "truncated": len(line) > 1000,
                        }
                    )
        return {
            "project_id": project_id,
            "revision": revision,
            "matches": matches,
            "has_more": False,
        }

    def get_diff(
        self,
        project_id: str,
        from_revision: int,
        to_revision: int | None = None,
        path: str | None = None,
    ) -> dict:
        if path is not None:
            validate_path(path)
        with self.read_connection() as db:
            if from_revision < 0:
                raise MirrorError("from_revision must be nonnegative")
            if from_revision:
                self._revision(db, project_id, from_revision)
            to_revision = self._revision(db, project_id, to_revision)

            before = self._load_source(db, project_id, from_revision)
            after = self._load_source(db, project_id, to_revision)
        return {
            "project_id": project_id,
            "from_revision": from_revision,
            "to_revision": to_revision,
            **self._format_diff(before, after, path),
        }

    def get_recent_diff(
        self,
        project_id: str,
        snapshot: str | None = None,
        path: str | None = None,
        *,
        baseline: str = "previous",
        detail: str = "summary",
        offset: int = 0,
        limit: int = 50,
        max_chars: int = 20_000,
    ) -> dict:
        """Resolve and load the comparison in one read transaction, without numbered labels."""
        if path is not None:
            validate_path(path)
        if baseline not in {"previous", "empty"} or detail not in {"summary", "patch"}:
            raise MirrorError("baseline must be previous/empty; detail must be summary/patch")
        if offset < 0 or not 1 <= limit <= 100 or not 1000 <= max_chars <= 50_000:
            raise MirrorError("offset must be nonnegative; limit 1-100; max_chars 1000-50000")
        with self.read_connection() as db:
            revision, handle = self._resolve_snapshot(db, project_id, snapshot)
            after = self._load_source(db, project_id, revision)
            if baseline == "previous" and revision == 1:
                return {
                    "project_id": project_id,
                    "snapshot": handle,
                    "baseline": None,
                    "reason": "NO_PREVIOUS_SNAPSHOT",
                    "current_file_count": len(after),
                    "changes_available": False,
                    "changes": [],
                    "truncated": False,
                    "has_more": False,
                    "next_offset": None,
                }
            if baseline == "previous":
                row = db.execute(
                    "SELECT 1 FROM snapshots WHERE project_id=? AND revision=?",
                    (project_id, revision - 1),
                ).fetchone()
                if row is None:
                    raise MirrorError(
                        "COMPARISON_BASELINE_UNAVAILABLE: comparison baseline is unavailable; "
                        "restart the comparison from repo_overview for current code"
                    )
            before = (
                self._load_source(db, project_id, revision - 1) if baseline == "previous" else {}
            )

        descriptions = []
        for name in sorted(before.keys() | after.keys()):
            if path is not None and name != path:
                continue
            old, new = before.get(name), after.get(name)
            if old and new and old[0] == new[0]:
                continue
            insertions = deletions = 0
            matcher = difflib.SequenceMatcher(
                a=(old[1] if old else "").splitlines(),
                b=(new[1] if new else "").splitlines(),
            )
            for operation, start_old, end_old, start_new, end_new in matcher.get_opcodes():
                if operation in {"replace", "delete"}:
                    deletions += end_old - start_old
                if operation in {"replace", "insert"}:
                    insertions += end_new - start_new
            descriptions.append(
                {
                    "path": name,
                    "op": "add" if old is None else "delete" if new is None else "modify",
                    "insertions": insertions,
                    "deletions": deletions,
                }
            )
        page = descriptions[offset : offset + limit]
        changes, truncated = page, False
        if detail == "patch":
            names = {item["path"] for item in page}
            patches = self._format_diff(
                {name: value for name, value in before.items() if name in names},
                {name: value for name, value in after.items() if name in names},
                path,
                max_chars,
            )
            stats = {item["path"]: item for item in page}
            changes = [{**stats[item["path"]], **item} for item in patches["changes"]]
            truncated = patches["truncated"]
        next_offset = offset + len(changes)
        has_more = next_offset < len(descriptions)
        return {
            "project_id": project_id,
            "snapshot": handle,
            "baseline": baseline,
            "detail": detail,
            "changes_available": True,
            "summary": {
                "files_changed": len(descriptions),
                "added": sum(item["op"] == "add" for item in descriptions),
                "deleted": sum(item["op"] == "delete" for item in descriptions),
                "modified": sum(item["op"] == "modify" for item in descriptions),
                "insertions": sum(item["insertions"] for item in descriptions),
                "deletions": sum(item["deletions"] for item in descriptions),
            },
            "changes": changes,
            "offset": offset,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "truncated": truncated,
        }

    @staticmethod
    def _load_source(db: sqlite3.Connection, project_id: str, revision: int) -> dict:
        return {
            r["path"]: (r["sha256"], r["content"])
            for r in db.execute(
                "SELECT f.path, b.sha256, b.content FROM files f "
                "JOIN blobs b USING(sha256) WHERE f.project_id=? AND f.revision=?",
                (project_id, revision),
            )
        }

    @staticmethod
    def _format_diff(before: dict, after: dict, path: str | None, max_chars: int = 50_000) -> dict:
        changes, remaining, truncated = [], max_chars, False
        for name in sorted(before.keys() | after.keys()):
            if path is not None and name != path:
                continue
            old, new = before.get(name), after.get(name)
            if old and new and old[0] == new[0]:
                continue
            if len(changes) >= 100 or remaining <= 0:
                truncated = True
                break
            diff = "".join(
                difflib.unified_diff(
                    (old[1] if old else "").splitlines(keepends=True),
                    (new[1] if new else "").splitlines(keepends=True),
                    fromfile=f"a/{name}",
                    tofile=f"b/{name}",
                )
            )
            clipped = len(diff) > remaining
            changes.append(
                {
                    "path": name,
                    "op": "add" if old is None else "delete" if new is None else "modify",
                    "diff": diff[:remaining],
                    "truncated": clipped,
                }
            )
            remaining -= min(len(diff), remaining)
            truncated = truncated or clipped
        return {
            "changes": changes,
            "truncated": truncated,
        }
