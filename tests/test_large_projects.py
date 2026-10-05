"""Larger read scope without unbounded responses or half-published source states."""

import json

import pytest
from pydantic import ValidationError

from code_context.client import LocalState, prepare_batch
from code_context.local import LocalMirror
from code_context.models import FileChange, SyncBatch, content_hash
from code_context.policy import MAX_FILE_BYTES, MAX_FILES, MAX_REQUEST_BYTES, MAX_TOTAL_BYTES
from code_context.scanner import Scanner
from code_context.storage import MirrorError, MirrorStore


def upsert(path, content):
    return FileChange(op="upsert", path=path, content=content, sha256=content_hash(content))


def test_limits_raised_without_removing_guards():
    assert MAX_FILE_BYTES == 4 * 1024 * 1024
    assert MAX_TOTAL_BYTES == 128 * 1024 * 1024
    assert MAX_FILES == 50_000
    assert MAX_REQUEST_BYTES == 256 * 1024 * 1024
    with pytest.raises(ValidationError, match="file exceeds"):
        upsert("large.txt", "x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValidationError):
        upsert(".env", "not shared")
    with pytest.raises(ValidationError):
        upsert("a.py", "private='sk-" + "x" * 35 + "'\n")


def test_scanner_and_local_mirror_accept_over_old_1mib_and_8mib_limits(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    text = "class Large:\n    pass\n# " + "x" * (1100 * 1024)
    for i in range(9):
        (root / f"large_{i}.py").write_text(text)
    scanned = Scanner(root).scan()
    assert len(scanned.files) == 9 and not scanned.skipped
    assert sum(len(f.content.encode()) for f in scanned.files.values()) > 8 * 1024 * 1024
    with LocalMirror(root, "large", tmp_path / "data") as mirror:
        result = mirror.sync_once(scanned)
        assert result["revision"] == 1 and result["changed_files"] == 9
        assert mirror.store.manifest("large")["file_count"] == 9
        stats = mirror.store.code_index_status("large", 1)
        assert not stats["partial"] and stats["parsed_files"] == 9
        hits = mirror.store.code_query("large", None, "symbol_search", query="Large", exact=True)
        assert len(hits["symbols"]) == 9
        result = mirror.store.read_file("large", "large_0.py")
        assert len(result["content"]) <= 20_000 and result["content_truncated"]
        assert result["next_start_line"] == 3 and result["next_char_offset"] > 0
        assert mirror.state.pending() is None


def test_more_than_old_10000_file_limit_in_one_atomic_state(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    changes = [upsert(f"files/f{i:05d}.txt", "small text\n") for i in range(10_005)]
    result = store.apply(
        "large", SyncBatch(request_id="initial", base_revision=0, mode="full", changes=changes)
    )
    assert result["revision"] == 1
    assert store.manifest("large")["file_count"] == 10_005
    with store.read_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_batch_over_old_20mib_limit_keeps_complete_state(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    text = "# " + "x" * (3 * 1024 * 1024)
    for i in range(7):
        (root / f"large_{i}.py").write_text(text)
    scanned = Scanner(root).scan()
    batch = prepare_batch(scanned, {}, 0)
    assert len(batch.model_dump_json().encode()) > 20 * 1024 * 1024
    state = LocalState(tmp_path / "state.db", {"test": True})
    state.enqueue(batch)
    assert len(state.pending().changes) == 7
    store = MirrorStore(tmp_path / "mirror.db")
    result = store.apply("large", state.pending())
    state.acknowledge(batch, result["revision"])
    assert state.pending() is None and len(state.hashes()) == 7
    assert store.manifest("large")["file_count"] == 7


def test_long_unicode_lines_resume_without_dropping_or_repeating_text(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    text = "begin\n" + '中🙂\\"' * 800 + "\nend\n"
    store.apply(
        "sample",
        SyncBatch(
            request_id="initial", base_revision=0, mode="full", changes=[upsert("long.txt", text)]
        ),
    )
    start, char, collected = 1, 0, []
    while True:
        page = store.read_file(
            "sample", "long.txt", start_line=start, char_offset=char, max_chars=1000
        )
        assert len(page["content"]) <= 1000
        collected.append(page["content"])
        if not page["has_more"]:
            break
        start, char = page["next_start_line"], page["next_char_offset"]
    assert "".join(collected) == text
    with pytest.raises(MirrorError):
        store.read_file("sample", "long.txt", start_line=1, char_offset=1000)


@pytest.mark.parametrize("body", ["x" * 8000, '中🙂\\\\\\"' * 1200])
def test_read_symbol_continuation_stops_at_definition(tmp_path, body):
    store = MirrorStore(tmp_path / "mirror.db")
    text = 'def long():\n    return "' + body + '"\n\ndef other(): pass\n'
    store.apply(
        "sample",
        SyncBatch(
            request_id="initial", base_revision=0, mode="full", changes=[upsert("long.py", text)]
        ),
    )
    symbol = store.code_query("sample", None, "symbol_search", query="long", exact=True)["symbols"][
        0
    ]
    offset, char, collected = 0, 0, []
    while True:
        page = store.code_query(
            "sample",
            None,
            "read_symbol",
            symbol_id=symbol["symbol_id"],
            line_offset=offset,
            char_offset=char,
            max_chars=3000,
        )
        assert len(json.dumps(page, ensure_ascii=False)) <= 3000
        collected.append(page["content"])
        if not page["has_more"]:
            break
        offset, char = page["next_line_offset"], page["next_char_offset"]
    assert "".join(collected) == "".join(text.splitlines(keepends=True)[:2])


def test_source_budget_error_preserves_previous_complete_state(tmp_path, monkeypatch):
    import code_context.storage as module

    store = MirrorStore(tmp_path / "mirror.db")
    store.apply(
        "sample",
        SyncBatch(
            request_id="first", base_revision=0, mode="full", changes=[upsert("a.txt", "one")]
        ),
    )
    handle = store.resolve_snapshot("sample")[1]
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(MirrorError):
        store.apply(
            "sample",
            SyncBatch(
                request_id="too-large",
                base_revision=1,
                mode="delta",
                changes=[upsert("b.txt", "more")],
            ),
        )
    assert store.resolve_snapshot("sample")[1] == handle
    assert store.manifest("sample")["file_count"] == 1
