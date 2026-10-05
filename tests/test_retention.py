import hashlib
import sqlite3

import pytest

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.storage import MirrorError, MirrorStore, RevisionConflict


def update(store, number, content=None, project="sample"):
    text = content if content is not None else f"value = {number}\n"
    batch = SyncBatch(
        request_id=f"change-{number}",
        base_revision=number - 1,
        mode="full" if number == 1 else "delta",
        changes=[FileChange(op="upsert", path="main.py", content=text, sha256=content_hash(text))],
    )
    return batch, store.apply(project, batch)


def counts(store):
    with store.read_connection() as db:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("snapshots", "files", "blobs", "requests")
        }


def test_many_small_edits_keep_only_two_states_and_bounded_disk(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    for number in range(1, 301):
        # A single-line change to a ~100 KiB file, not an artificially tiny body.
        update(store, number, f"value = {number}\n#" + "x" * (100 * 1024))
    assert counts(store) == {"snapshots": 2, "files": 2, "blobs": 2, "requests": 2}
    with store.read_connection() as db:
        assert db.execute("SELECT SUM(size) FROM blobs").fetchone()[0] < 201 * 1024
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.database.stat().st_size < 1024 * 1024
    assert store.manifest("sample")["revision"] == 300
    assert "value = 299" in store.read_file("sample", "main.py", 299)["content"]
    with pytest.raises(MirrorError):
        store.read_file("sample", "main.py", 298)


def test_current_previous_and_handles_never_silently_switch_states(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    update(store, 1)
    first, handle = store.resolve_snapshot("sample")
    assert first == 1 and handle.startswith("ctx_")
    update(store, 2)
    assert store.resolve_snapshot("sample", "previous") == (1, handle)
    assert store.resolve_snapshot("sample", handle) == (1, handle)
    update(store, 3)
    assert store.resolve_snapshot("sample", "previous")[0] == 2
    with pytest.raises(MirrorError, match="expired") as error:
        store.resolve_snapshot("sample", handle)
    assert handle not in str(error.value)


def test_pruning_is_project_scoped_and_preserves_shared_content(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    update(store, 1, project="first")
    update(store, 1, project="other")
    _, other_handle = store.resolve_snapshot("other")
    update(store, 2, project="first")
    update(store, 3, project="first")
    assert counts(store) == {"snapshots": 3, "files": 3, "blobs": 3, "requests": 3}
    assert store.read_file("other", "main.py")["content"] == "value = 1\n"
    with pytest.raises(MirrorError, match="unavailable"):
        store.resolve_snapshot("first", other_handle)


def test_retained_retries_are_idempotent_and_older_retries_cannot_reapply(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    first, _ = update(store, 1)
    second, _ = update(store, 2)
    third, _ = update(store, 3)
    assert store.apply("sample", second)["replayed"]
    assert store.apply("sample", third)["replayed"]
    with pytest.raises(RevisionConflict):
        store.apply("sample", first)
    assert store.manifest("sample")["revision"] == 3
    assert counts(store)["requests"] == 2


def test_failed_commit_cannot_prune_either_retained_state(tmp_path, monkeypatch):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    update(store, 1)
    update(store, 2)
    before = counts(store)
    original = store._prune_history

    def failed_prune(db):
        original(db)
        raise sqlite3.OperationalError("simulated failure after pruning")

    monkeypatch.setattr(store, "_prune_history", failed_prune)
    with pytest.raises(sqlite3.OperationalError):
        update(store, 3)
    assert counts(store) == before
    assert store.manifest("sample")["revision"] == 2
    assert store.read_file("sample", "main.py", 1)["content"] == "value = 1\n"


def test_reader_transaction_stays_consistent_while_writer_prunes(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    update(store, 1)
    update(store, 2)
    with store.read_connection() as reader:
        assert store._revision(reader, "sample", 1) == 1
        update(store, 3)
        content = reader.execute(
            "SELECT b.content FROM files f JOIN blobs b USING(sha256) "
            "WHERE f.project_id='sample' AND f.revision=1"
        ).fetchone()[0]
        assert content == "value = 1\n"
    with pytest.raises(MirrorError):
        store.read_file("sample", "main.py", 1)


def test_recent_diff_resolves_and_reads_in_one_transaction(tmp_path, monkeypatch):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    update(store, 1)
    update(store, 2)
    original = store._resolve_snapshot

    def interleaved(db, project_id, snapshot):
        result = original(db, project_id, snapshot)
        update(store, 3)
        return result

    monkeypatch.setattr(store, "_resolve_snapshot", interleaved)
    result = store.get_recent_diff("sample", detail="patch")
    assert result["baseline"] == "previous"
    assert "-value = 1" in result["changes"][0]["diff"]
    assert "+value = 2" in result["changes"][0]["diff"]
    assert "value = 3" not in result["changes"][0]["diff"]
    assert "from_revision" not in result and "to_revision" not in result


def test_diff_distinguishes_expired_target_from_unavailable_baseline(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    update(store, 1)
    first = store.resolve_snapshot("sample")[1]
    update(store, 2)
    second = store.resolve_snapshot("sample")[1]
    update(store, 3)
    with pytest.raises(MirrorError, match="baseline is unavailable"):
        store.get_recent_diff("sample", second)
    with pytest.raises(MirrorError, match="expired"):
        store.get_recent_diff("sample", first)


def legacy_database(database, number=20):
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE projects(project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            CREATE TABLE snapshots(project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(project_id, revision));
            CREATE TABLE blobs(sha256 TEXT PRIMARY KEY, content TEXT NOT NULL,
                size INTEGER NOT NULL);
            CREATE TABLE files(project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                path TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES blobs(sha256),
                PRIMARY KEY(project_id, revision, path),
                FOREIGN KEY(project_id, revision) REFERENCES snapshots(project_id, revision));
            CREATE TABLE requests(project_id TEXT NOT NULL, request_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(project_id, request_id));
            PRAGMA user_version=1;
            """
        )
        for revision in range(1, number + 1):
            text = f"value = {revision}\n#" + "x" * (100 * 1024)
            change = FileChange(
                op="upsert", path="main.py", content=text, sha256=content_hash(text)
            )
            batch = SyncBatch(
                request_id=f"change-{revision}",
                base_revision=revision - 1,
                mode="full" if revision == 1 else "delta",
                changes=[change],
            )
            db.execute("INSERT INTO blobs VALUES(?, ?, ?)", (change.sha256, text, len(text)))
            db.execute("INSERT INTO snapshots VALUES('sample', ?, '2026-10-05')", (revision,))
            db.execute(
                "INSERT INTO files VALUES('sample', ?, 'main.py', ?)", (revision, change.sha256)
            )
            db.execute(
                "INSERT INTO requests VALUES('sample', ?, ?, ?)",
                (
                    batch.request_id,
                    hashlib.sha256(batch.model_dump_json().encode()).hexdigest(),
                    revision,
                ),
            )
        db.execute("INSERT INTO projects VALUES('sample', ?)", (number,))


def test_legacy_migration_keeps_two_backfills_handles_and_reclaims_disk(tmp_path):
    database = tmp_path / "mirror.sqlite3"
    legacy_database(database)
    before = database.stat().st_size
    store = MirrorStore(database)
    assert counts(store) == {"snapshots": 2, "files": 2, "blobs": 2, "requests": 2}
    current, handle = store.resolve_snapshot("sample")
    assert current == 20 and handle.startswith("ctx_")
    assert store.resolve_snapshot("sample", "previous")[0] == 19
    assert database.stat().st_size < before // 2
    restarted = MirrorStore(database)
    assert restarted.resolve_snapshot("sample") == (current, handle)
    with restarted.read_connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_interrupted_migration_retries_compaction_without_replacing_handles(tmp_path, monkeypatch):
    database = tmp_path / "mirror.sqlite3"
    legacy_database(database)
    original = MirrorStore.compact

    def interrupted(self):
        raise sqlite3.OperationalError("simulated interruption before compaction")

    monkeypatch.setattr(MirrorStore, "compact", interrupted)
    with pytest.raises(sqlite3.OperationalError):
        MirrorStore(database)
    with sqlite3.connect(database) as db:
        handles = db.execute("SELECT snapshot FROM snapshots ORDER BY revision").fetchall()
        assert db.execute("SELECT value FROM maintenance").fetchone()[0] == 1
    before = database.stat().st_size
    monkeypatch.setattr(MirrorStore, "compact", original)
    store = MirrorStore(database)
    with store.read_connection() as db:
        assert db.execute("SELECT snapshot FROM snapshots ORDER BY revision").fetchall()
        assert [
            tuple(row) for row in db.execute("SELECT snapshot FROM snapshots ORDER BY revision")
        ] == handles
        assert db.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 0
        assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert database.stat().st_size < before // 2
