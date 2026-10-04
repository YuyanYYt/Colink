import threading
import time
from pathlib import Path

import httpx
import pytest

from code_context.client import (
    RetryableSyncError,
    SyncClient,
    SyncError,
    normalize_server_url,
    read_local_status,
)
from code_context.models import SyncBatch
from code_context.storage import MirrorStore, RevisionConflict


@pytest.fixture
def source(tmp_path):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    (root / "a.py").write_text("value=1\n", encoding="utf-8")
    return root


def server_transport(store):
    def send(request):
        try:
            project = request.url.path.split("/")[3]
            result = store.apply(project, SyncBatch.model_validate_json(request.content))
            return httpx.Response(200, json=result)
        except RevisionConflict as exc:
            return httpx.Response(409, json={"revision": exc.current_revision})

    return httpx.MockTransport(send)


def test_disconnect_recovery_keeps_frozen_payload_and_catches_new_edits(source, tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    data = tmp_path / "client"

    def unavailable(request):
        raise httpx.ConnectError("unavailable", request=request)

    failed_http = httpx.Client(transport=httpx.MockTransport(unavailable))
    client = SyncClient(source, "sample", "http://127.0.0.1:8765", "s" * 32, data, failed_http)
    with pytest.raises(RetryableSyncError):
        client.sync_once()
    saved_id = client.state.pending().request_id
    assert client.state.pending().changes[0].content == "value=1\n"
    client.close()
    failed_http.close()
    (source / "a.py").write_text("value=2\n", encoding="utf-8")
    good_http = httpx.Client(transport=server_transport(store))
    recovered = SyncClient(source, "sample", "http://127.0.0.1:8765", "s" * 32, data, good_http)
    assert recovered.state.pending().request_id == saved_id
    assert recovered.sync_once()["revision"] == 2
    assert store.read_file("sample", "a.py", revision=1)["content"] == "value=1\n"
    assert store.read_file("sample", "a.py", revision=2)["content"] == "value=2\n"
    assert recovered.sync_once()["changed_files"] == 0
    assert store.manifest("sample")["revision"] == 2
    recovered.close()
    good_http.close()


def test_stale_client_cannot_overwrite_another_project_writer(source, tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    with httpx.Client(transport=server_transport(store)) as http:
        first = SyncClient(
            source, "sample", "http://127.0.0.1:8765", "s" * 32, tmp_path / "first", http
        )
        first.sync_once()
        first.close()
        second = SyncClient(
            source, "sample", "http://127.0.0.1:8765", "s" * 32, tmp_path / "second", http
        )
        with pytest.raises(SyncError, match="revision"):
            second.sync_once()
        assert store.manifest("sample")["revision"] == 1
        second.close()


def test_single_local_writer_lock(source, tmp_path):
    first = SyncClient(source, "sample", "http://127.0.0.1:8765", "s" * 32, tmp_path / "data")
    try:
        with pytest.raises(SyncError, match="another sync client"):
            SyncClient(source, "sample", "http://127.0.0.1:8765", "s" * 32, tmp_path / "data")
    finally:
        first.close()


def test_status_remains_readable_while_writer_is_running(source, tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    data = tmp_path / "client"
    with httpx.Client(transport=server_transport(store)) as http:
        client = SyncClient(source, "sample", "http://127.0.0.1:8765", "s" * 32, data, http)
        try:
            client.sync_once()
            result = read_local_status(source, "sample", "http://127.0.0.1:8765", data)
            assert result["revision"] == 1 and result["tracked_files"] == 1
            assert result["initialized"] and not result["pending"]
        finally:
            client.close()


def test_quoted_tilde_uses_same_expanded_state_and_lock(source, tmp_path, monkeypatch):
    original = Path.expanduser
    fake_home = tmp_path.resolve() / "user-directory"

    def expand(path):
        if path.parts and path.parts[0] == "~":
            return fake_home.joinpath(*path.parts[1:])
        return original(path)

    monkeypatch.setattr(Path, "expanduser", expand)
    client = SyncClient(
        source, "sample", "http://127.0.0.1:8765", "s" * 32, Path("~/.code-context")
    )
    try:
        assert client.state.database.is_relative_to(fake_home)
        assert Path(client._lock.name) == client.state.database.with_suffix(".lock")
    finally:
        client.close()


@pytest.mark.parametrize(
    "url", ["http://remote.example", "https://a:b@example.org", "https://x/api", "file:///x"]
)
def test_no_cleartext_remote_transport_or_embedded_credentials(url):
    with pytest.raises(SyncError):
        normalize_server_url(url)


def test_real_watchfiles_updates_and_deletes_without_restart(source, tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    http = httpx.Client(transport=server_transport(store))
    client = SyncClient(
        source, "sample", "http://127.0.0.1:8765", "s" * 32, tmp_path / "client", http
    )
    stopped, emitted, failures = threading.Event(), [], []

    def run():
        try:
            client.watch(emitted.append, stop_event=stopped, reconcile_seconds=60)
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()

    def await_content(path, content):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if failures:
                raise failures[0]
            try:
                if store.read_file("sample", path)["content"] == content:
                    return
            except ValueError:
                pass
            time.sleep(0.05)
        raise AssertionError(f"watch did not synchronize {path}")

    try:
        await_content("a.py", "value=1\n")
        # Wait for watcher initialization and its first reconciliation, then
        # require later updates through events rather than the 60-second sweep.
        time.sleep(1.2)
        (source / "a.py").write_text("value=2\n", encoding="utf-8")
        await_content("a.py", "value=2\n")
        (source / "新增.py").write_text("created=True\n", encoding="utf-8")
        await_content("新增.py", "created=True\n")
        (source / "a.py").unlink()
        deadline = time.monotonic() + 8
        while any(f["path"] == "a.py" for f in store.manifest("sample")["files"]):
            if time.monotonic() > deadline:
                raise AssertionError("deleted file remained in the remote snapshot")
            time.sleep(0.05)
        assert not failures
    finally:
        stopped.set()
        worker.join(timeout=5)
        client.close()
        http.close()
    assert not worker.is_alive()
