import json
import sqlite3
import time

import pytest

from code_context.cli import main
from code_context.client import SyncError
from code_context.local import LocalMirror, read_local_mirror_status
from code_context.scanner import ScanError
from code_context.storage import MirrorError, RevisionConflict


@pytest.fixture
def source(tmp_path):
    root = tmp_path.resolve() / "source"
    root.mkdir()
    (root / "main.py").write_text("version = 1\n", encoding="utf-8")
    return root


def test_local_full_delta_unchanged_and_history(source, tmp_path):
    with LocalMirror(source, "sample", tmp_path / "data") as mirror:
        assert mirror.sync_once()["revision"] == 1
        assert mirror.sync_once()["changed_files"] == 0
        (source / "main.py").write_text("version = 2\n", encoding="utf-8")
        assert mirror.sync_once()["revision"] == 2
        assert mirror.store.read_file("sample", "main.py", 1)["content"] == "version = 1\n"
        assert mirror.store.read_file("sample", "main.py", 2)["content"] == "version = 2\n"
        (source / "main.py").unlink()
        assert mirror.sync_once()["revision"] == 3
        assert mirror.store.manifest("sample")["file_count"] == 0


def test_local_restart_recovers_lost_ack_then_offline_edits(source, tmp_path, monkeypatch):
    data = tmp_path / "data"
    with LocalMirror(source, "sample", data) as mirror:
        original = mirror.state.acknowledge

        def lost_ack(*_):
            raise sqlite3.OperationalError("simulated lost local acknowledgement")

        monkeypatch.setattr(mirror.state, "acknowledge", lost_ack)
        with pytest.raises(sqlite3.OperationalError):
            mirror.sync_once()
        request = mirror.state.pending().request_id
        assert mirror.store.manifest("sample")["revision"] == 1
        monkeypatch.setattr(mirror.state, "acknowledge", original)
    (source / "main.py").write_text("version = 2\n", encoding="utf-8")
    with LocalMirror(source, "sample", data) as restarted:
        assert restarted.state.pending().request_id == request
        assert restarted.sync_once()["revision"] == 2
        assert restarted.state.pending() is None
        assert restarted.store.read_file("sample", "main.py", 1)["content"] == "version = 1\n"
        assert restarted.sync_once()["changed_files"] == 0


def test_local_writer_lock_and_source_binding(source, tmp_path):
    data = tmp_path / "data"
    other = tmp_path.resolve() / "other"
    other.mkdir()
    with LocalMirror(source, "sample", data) as mirror:
        mirror.sync_once()
        with pytest.raises(SyncError, match="another local mirror"):
            LocalMirror(source, "sample", data)
        status = read_local_mirror_status(data)
        assert status["running"] and status["revision"] == 1
    assert read_local_mirror_status(data)["status"] == "not_running"
    with pytest.raises(SyncError, match="different project root"):
        LocalMirror(other, "sample", data)
    with pytest.raises(SyncError, match="different project root"):
        LocalMirror(source, "other", data)


def test_local_scan_error_cannot_delete_prior_snapshot(source, tmp_path, monkeypatch):
    with LocalMirror(source, "sample", tmp_path / "data") as mirror:
        mirror.sync_once()

        def unavailable():
            raise ScanError("simulated unreadable directory")

        monkeypatch.setattr(mirror.scanner, "scan", unavailable)
        with pytest.raises(ScanError):
            mirror.sync_once()
        assert mirror.store.manifest("sample")["revision"] == 1
        assert mirror.store.manifest("sample")["file_count"] == 1


def test_local_cannot_overwrite_another_writer(source, tmp_path):
    from code_context.models import FileChange, SyncBatch, content_hash

    with LocalMirror(source, "sample", tmp_path / "data") as mirror:
        mirror.sync_once()
        mirror.store.apply(
            "sample",
            SyncBatch(
                request_id="outside_writer",
                base_revision=1,
                mode="delta",
                changes=[
                    FileChange(
                        op="upsert",
                        path="main.py",
                        content="external = True\n",
                        sha256=content_hash("external = True\n"),
                    )
                ],
            ),
        )
        (source / "main.py").write_text("version = 2\n", encoding="utf-8")
        with pytest.raises(RevisionConflict):
            mirror.sync_once()
        assert mirror.state.pending() is not None
        assert mirror.store.read_file("sample", "main.py")["content"] == "external = True\n"


def test_local_never_adopts_an_existing_unbound_mirror(source, tmp_path):
    data = tmp_path / "data"
    assert (
        main(["snapshot", "--root", str(source), "--project", "sample", "--data-dir", str(data)])
        == 0
    )
    with LocalMirror(source, "sample", data) as mirror:
        with pytest.raises(RevisionConflict):
            mirror.sync_once()
        assert mirror.store.manifest("sample")["revision"] == 1


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), 86401])
def test_invalid_reconciliation_interval(source, tmp_path, seconds):
    with pytest.raises(SyncError):
        LocalMirror(source, "sample", tmp_path / "data", seconds)


def test_local_status_does_not_create_files(tmp_path, capsys):
    data = tmp_path / "absent"
    assert main(["local-status", "--data-dir", str(data)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert not status["initialized"] and not status["running"]
    assert not data.exists()


def test_local_status_during_schema_initialization_is_read_only(tmp_path):
    import fcntl

    data = tmp_path / "state"
    data.mkdir()
    database = data / "local.sqlite3"
    sqlite3.connect(database).close()
    original = database.read_bytes()
    assert read_local_mirror_status(data)["status"] == "incomplete_state"
    with (data / "local.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = read_local_mirror_status(data)
        assert status["running"] and not status["initialized"]
        assert status["status"] == "initializing"
    assert database.read_bytes() == original


def test_real_local_watcher_and_safe_stderr(source, tmp_path, capsys):
    with LocalMirror(source, "sample", tmp_path / "data") as mirror:
        mirror.start()
        assert read_local_mirror_status(mirror.data_dir)["running"]
        time.sleep(1.2)
        (source / "main.py").write_text("version = 2\n", encoding="utf-8")
        deadline = time.monotonic() + 8
        while mirror.store.read_file("sample", "main.py")["content"] != "version = 2\n":
            mirror.ensure_ready()
            if time.monotonic() >= deadline:
                raise AssertionError("local watcher did not process the modification")
            time.sleep(0.05)
        mirror.ensure_ready()
        assert mirror.store.read_file("sample", "main.py", 1)["content"] == "version = 1\n"
        secret = "sk-" + "k" * 32
        mirror._watch_event({"status": "scan_retry", "reason": secret})
        output = capsys.readouterr()
        assert not output.out and secret not in output.err
        with pytest.raises(MirrorError, match="not ready"):
            mirror.ensure_ready()
    assert not mirror.worker.is_alive()
    assert not read_local_mirror_status(mirror.data_dir)["running"]


def test_worker_failure_blocks_reads_without_echoing_input(source, tmp_path, monkeypatch, capsys):
    import code_context.local as module

    secret = "sk-" + "z" * 32

    def failed(*_, **__):
        raise RuntimeError(secret)

    monkeypatch.setattr(module, "watch_source", failed)
    with LocalMirror(source, "sample", tmp_path / "data") as mirror:
        mirror.start()
        mirror.worker.join(timeout=5)
        with pytest.raises(MirrorError, match="not ready"):
            mirror.ensure_ready()
        assert read_local_mirror_status(mirror.data_dir)["status"] == "failed"
        assert secret not in capsys.readouterr().err
