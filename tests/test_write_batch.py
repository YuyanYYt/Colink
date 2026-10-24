import asyncio
import copy
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import code_context.write_operations as operations
from code_context.live import LiveQueries
from code_context.recovery_store import RecoveryStore
from code_context.server import build_mcp
from code_context.source_access import SourceAccess
from code_context.write_coordinator import WriteCoordinator, WriteError


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("VALUE = 1\n")
    (root / "b.py").write_text("VALUE = 10\n")
    backend = LiveQueries({"sample": SourceAccess(root)})
    store = RecoveryStore(tmp_path / "recovery")
    c = WriteCoordinator(store, backend.source, control_alive=lambda: True)
    backend.write_coordinator = c
    state = SimpleNamespace(root=root, backend=backend, store=store, c=c)
    yield state
    state.c.close()
    state.backend.close()
    state.store.close()


def begin(parts, paths=None):
    parts.c.enable(["sample"])
    return parts.c.begin_write_task(
        "sample", parts.c.status()["next_task_request_id"], paths=paths
    )["task_id"]


def edit(parts, path="a.py", old="1", new="2"):
    return {
        "kind": "apply_edit",
        "path": path,
        "expected_sha256": parts.backend.source("sample").read(path).sha256,
        "edit": {"kind": "replace_fragment", "old_text": old, "new_text": new},
    }


def test_real_mcp_batch_has_one_call_and_replays_without_extra_mutations(parts):
    task = begin(parts, ["a.py", "new", "new/b.py"])
    items = [
        edit(parts),
        {"kind": "create_directory", "path": "new"},
        {"kind": "create_file", "path": "new/b.py", "content": "B = 2\n"},
    ]

    async def run():
        mcp = build_mcp(parts.backend, project_scope="sample")
        arguments = {
            "project_id": "sample",
            "task_id": task,
            "request_id": "batch_001",
            "operations": items,
        }
        result = (await mcp.call_tool("write_batch", arguments)).structured_content
        assert result["state"] == "completed" and result["completed_count"] == 3
        assert result["current_state_verified"] and not result["atomic"]
        inode = (parts.root / "a.py").stat().st_ino
        assert (await mcp.call_tool("write_batch", arguments)).structured_content == result
        assert (parts.root / "a.py").stat().st_ino == inode
        status = (
            await mcp.call_tool("write_task_status", {"project_id": "sample"})
        ).structured_content
        assert status["batches"][0]["completed_count"] == 3
        assert "items" not in status["batches"][0]
        changed = copy.deepcopy(arguments)
        changed["operations"][-1]["content"] = "B = 3\n"
        with pytest.raises(ToolError, match="REQUEST_ID_CONFLICT"):
            await mcp.call_tool("write_batch", changed)
        invalid = copy.deepcopy(arguments)
        invalid["operations"][-1]["synthetic_private_source_do_not_echo"] = (
            "synthetic_private_source_do_not_echo"
        )
        with pytest.raises(ToolError) as error:
            await mcp.call_tool("write_batch", invalid)
        assert "synthetic_private_source_do_not_echo" not in str(error.value)
        parts.c.disable()
        fresh = {**arguments, "request_id": "batch_002"}
        with pytest.raises(ToolError, match="WRITE_DISABLED"):
            await mcp.call_tool("write_batch", fresh)

    asyncio.run(run())
    assert (parts.root / "a.py").read_text() == "VALUE = 2\n"
    assert (parts.root / "new/b.py").read_text() == "B = 2\n"
    assert len(parts.store.query("SELECT * FROM operations")) == 3
    receipt = parts.store.query("SELECT metadata FROM write_batches")[0]["metadata"]
    assert "VALUE =" not in receipt and "B = 2" not in receipt and "old_text" not in receipt


@pytest.mark.parametrize("failure", ["hash", "text", "parent_order"])
def test_later_preflight_failure_changes_no_files_and_binds_the_full_request(parts, failure):
    task = begin(parts, ["a.py", "b.py", "new", "new/value.py"])
    first = edit(parts)
    later = edit(parts, "b.py", "10", "20")
    if failure == "hash":
        later["expected_sha256"] = "0" * 64
    elif failure == "text":
        later["edit"]["old_text"] = "missing original text"
    else:
        later = {"kind": "create_file", "path": "new/value.py", "content": "value = 1\n"}
    items = [first, later]
    if failure == "parent_order":
        items.append({"kind": "create_directory", "path": "new"})
    result = parts.c.write_batch("sample", task, "batch_001", items)
    assert result["state"] == "blocked" and result["completed_count"] == 0
    assert not parts.store.query("SELECT * FROM operations")
    assert (parts.root / "a.py").read_text() == "VALUE = 1\n"
    assert (parts.root / "b.py").read_text() == "VALUE = 10\n" and not (parts.root / "new").exists()
    changed = copy.deepcopy(items)
    changed[0]["edit"]["new_text"] = "3"
    with pytest.raises(WriteError, match="REQUEST_ID_CONFLICT"):
        parts.c.write_batch("sample", task, "batch_001", changed)


@pytest.mark.parametrize("first", ["single", "batch"])
def test_batch_and_single_operations_cannot_reuse_the_same_request_id(parts, first):
    task = begin(parts, ["new.py", "next.py"])
    item = {"kind": "create_file", "path": "new.py", "content": "value = 1\n"}
    if first == "single":
        parts.c.create_file("sample", task, "shared_001", "new.py", item["content"])
        with pytest.raises(WriteError, match="REQUEST_ID_CONFLICT"):
            parts.c.write_batch("sample", task, "shared_001", [item])
    else:
        assert parts.c.write_batch("sample", task, "shared_001", [item])["state"] == "completed"
        with pytest.raises(WriteError, match="REQUEST_ID_CONFLICT"):
            parts.c.create_file("sample", task, "shared_001", "next.py", "value = 2\n")
    assert not (parts.root / "next.py").exists()


@pytest.mark.parametrize("external_change", [False, True])
def test_revocation_stops_later_items_and_retry_verifies_completed_children(
    parts, monkeypatch, external_change
):
    task = begin(parts, ["a.py", "new.py"])
    items = [edit(parts), {"kind": "create_file", "path": "new.py", "content": "NEW = 1\n"}]
    apply = parts.c.operations.apply_edit
    calls = []

    def revoke(*args):
        result = apply(*args)
        calls.append(args[2])
        parts.c.disable()
        return result

    monkeypatch.setattr(parts.c.operations, "apply_edit", revoke)
    result = parts.c.write_batch("sample", task, "batch_001", items)
    assert result["state"] == "partial" and result["completed_count"] == 1
    assert result["error"] == "WRITE_DISABLED" and not (parts.root / "new.py").exists()
    inode = (parts.root / "a.py").stat().st_ino
    if external_change:
        (parts.root / "a.py").write_text("external = 3\n")
    parts.c.enable(["sample"])
    result = parts.c.write_batch("sample", task, "batch_001", items)
    assert len(calls) == 1 and (parts.root / "a.py").stat().st_ino == inode
    if external_change:
        assert result["state"] == "partial" and not result["current_state_verified"]
        assert result["error"] == "WRITE_BATCH_CONFLICT" and not (parts.root / "new.py").exists()
    else:
        assert result["state"] == "completed" and (parts.root / "new.py").read_text() == "NEW = 1\n"


def test_restart_requires_local_recovery_and_never_repeats_saved_children(parts, monkeypatch):
    task = begin(parts, ["a.py", "new.py"])
    items = [edit(parts), {"kind": "create_file", "path": "new.py", "content": "NEW = 1\n"}]
    native = operations.commit_file
    count = 0

    def interrupted(*args):
        nonlocal count
        count += 1
        result = native(*args)
        if count == 2:
            raise RuntimeError("simulated process exit after second native install")
        return result

    monkeypatch.setattr(operations, "commit_file", interrupted)
    result = parts.c.write_batch("sample", task, "batch_001", items)
    assert result["state"] == "partial" and result["requires_local_recovery"]
    inode = (parts.root / "a.py").stat().st_ino
    parts.c.close()
    parts.store.close()
    parts.backend.close()
    parts.backend = LiveQueries({"sample": SourceAccess(parts.root)})
    parts.store = RecoveryStore(parts.root.parent / "recovery")
    parts.c = WriteCoordinator(parts.store, parts.backend.source, control_alive=lambda: True)
    parts.backend.write_coordinator = parts.c
    assert not parts.c.status()["write_enabled"] and parts.c.status()["recovery_required"]
    result = parts.c.write_batch("sample", task, "batch_001", items)
    assert result["requires_local_recovery"] and count == 2
    with pytest.raises(WriteError, match="WRITE_RECOVERY_REQUIRED"):
        parts.c.enable(["sample"])
    parts.c.recover("sample")
    parts.c.enable(["sample"])
    result = parts.c.write_batch("sample", task, "batch_001", items)
    assert result["state"] == "completed" and result["current_state_verified"]
    assert count == 2 and (parts.root / "a.py").stat().st_ino == inode
    assert (parts.root / "new.py").stat().st_nlink == 1
    assert len(parts.store.query("SELECT * FROM operations")) == 2
    parts.c.finish_write_task("sample", task, "finish_001")
    next_task = parts.c.begin_write_task("sample", parts.c.status()["next_task_request_id"])[
        "task_id"
    ]
    parts.c.finish_write_task("sample", next_task, "finish_002")
    assert not parts.store.query("SELECT * FROM write_batches")
    assert parts.store.query("PRAGMA foreign_key_check") == []


def test_duplicate_paths_and_unsupported_operations_are_rejected_before_records(parts):
    task = begin(parts, ["new.py"])
    item = {"kind": "create_file", "path": "new.py", "content": "value = 1\n"}
    for items in ([item, item], [{"kind": "delete_file", "path": "new.py"}]):
        with pytest.raises(WriteError, match="INVALID_WRITE_BATCH"):
            parts.c.write_batch("sample", task, "batch_001", items)
    assert not (parts.root / "new.py").exists()
    assert not parts.store.query("SELECT * FROM write_batches")
