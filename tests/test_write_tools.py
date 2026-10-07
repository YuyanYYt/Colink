import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.live import LiveQueries
from code_context.recovery_store import RecoveryStore
from code_context.server import READ_TOOL_NAMES, build_mcp
from code_context.source_access import SourceAccess
from code_context.write_coordinator import WriteCoordinator
from code_context.write_tools import WRITE_TOOL_NAMES


@pytest.fixture
def parts(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("VALUE = 1\n")
    backend = LiveQueries({"sample": SourceAccess(root)})
    store = RecoveryStore(tmp_path / "recovery")
    c = WriteCoordinator(
        store,
        backend.source,
        control_alive=lambda: True,
        on_change=backend.contexts.invalidate_project,
    )
    backend.write_coordinator = c
    yield root, c, build_mcp(backend, project_scope="sample")
    c.close()
    backend.close()
    store.close()


def test_original_fifteen_and_seven_task_tools_without_undo_or_local_grant(parts):
    _, c, mcp = parts

    async def inspect():
        tools = {t.name: t for t in await mcp.list_tools()}
        assert set(tools) == READ_TOOL_NAMES | WRITE_TOOL_NAMES
        assert len(READ_TOOL_NAMES) == 15 and len(tools) == 24
        assert all(tools[n].annotations.read_only_hint for n in READ_TOOL_NAMES)
        assert tools["write_task_status"].annotations.read_only_hint
        assert tools["move_path_status"].annotations.read_only_hint
        for name in WRITE_TOOL_NAMES - {"write_task_status", "move_path_status"}:
            assert not tools[name].annotations.read_only_hint
            assert tools[name].annotations.idempotent_hint
        assert "rollback_write_task" not in tools
        assert not hasattr(c, "rollback_write_task")
        assert "expected_context" in tools["apply_edit"].description
        assert "NO_TASK_BASELINE" in tools["get_diff"].description
        assert not c.status()["write_enabled"]

    asyncio.run(inspect())


def test_mcp_real_edit_read_diff_finish_and_removed_undo_using_fresh_context(parts):
    root, c, mcp = parts

    async def run():
        async def call(name, **arguments):
            result = await mcp.call_tool(name, {"project_id": "sample", **arguments})
            assert not result.is_error
            return result.structured_content

        c.enable(["sample"])
        state = await call("write_task_status")
        task = (
            await call(
                "begin_write_task",
                request_id=state["next_task_request_id"],
                paths=["a.py", "new.py"],
            )
        )["task_id"]
        read = await call("read_file", path="a.py")
        parameters = {
            "task_id": task,
            "request_id": "edit_0001",
            "path": "a.py",
            "expected_sha256": read["sha256"],
            "edit": {"kind": "replace_fragment", "old_text": "1", "new_text": "2"},
        }
        first = await call("apply_edit", **parameters)
        assert (root / "a.py").read_text() == "VALUE = 2\n"
        assert await call("apply_edit", **parameters) == first
        await call(
            "create_file", task_id=task, request_id="create_001", path="new.py", content="B = 2\n"
        )
        patch = await call("get_diff", path="a.py", detail="patch")
        assert "-VALUE = 1" in patch["changes"][0]["patch"]
        assert "+VALUE = 2" in patch["changes"][0]["patch"]
        await call("finish_write_task", task_id=task, request_id="finish_001")
        with pytest.raises(ToolError, match="Unknown tool"):
            await mcp.call_tool(
                "rollback_write_task",
                {"project_id": "sample", "task_id": task, "request_id": "rollback_001"},
            )
        assert (root / "a.py").read_text() == "VALUE = 2\n"
        assert (root / "new.py").read_text() == "B = 2\n"
        assert (await call("get_diff"))["summary"]["files_changed"] == 2
        c.disable()
        assert not (await call("write_task_status"))["write_enabled"]

    asyncio.run(run())


@pytest.mark.parametrize("bad", ["unknown", "wrong_type", "nested_unknown", "scope"])
def test_write_validation_never_echoes_supplied_source_or_expands_scope(parts, bad):
    root, c, mcp = parts
    c.enable(["sample"])
    task = c.begin_write_task("sample", c.status()["next_task_request_id"])["task_id"]
    marker = "synthetic_private_source_do_not_echo"
    args = {
        "project_id": "sample",
        "task_id": task,
        "request_id": "create_001",
        "path": "new.py",
        "content": "ordinary\n",
    }
    name = "create_file"
    if bad == "unknown":
        args[marker] = marker
    elif bad == "wrong_type":
        args["path"] = {"source": marker}
    elif bad == "nested_unknown":
        name = "apply_edit"
        args.pop("content")
        args.update(
            path="a.py",
            expected_sha256=c.source_provider("sample").read("a.py").sha256,
            edit={"kind": "replace_fragment", "old_text": "1", "new_text": "2", marker: marker},
        )
    else:
        args["project_id"] = "another"

    async def run():
        try:
            result = await mcp.call_tool(name, args)
            assert result.is_error
            message = str(result)
        except ToolError as error:
            message = str(error)
        assert marker not in message

    asyncio.run(run())
    assert (root / "a.py").read_text() == "VALUE = 1\n" and not (root / "new.py").exists()


def test_default_off_is_a_backend_rejection_not_just_an_annotation(parts):
    root, c, mcp = parts

    async def run():
        with pytest.raises(ToolError, match="WRITE_DISABLED"):
            await mcp.call_tool(
                "begin_write_task",
                {"project_id": "sample", "request_id": c.status()["next_task_request_id"]},
            )

    asyncio.run(run())
    assert not c.store.query("SELECT * FROM tasks")
    assert (root / "a.py").read_text() == "VALUE = 1\n"
