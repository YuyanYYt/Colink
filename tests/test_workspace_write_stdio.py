"""Real local stdio/Unix-control task lifecycle, not web or tunnel proof."""

import asyncio
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from code_context.local_control import control_request
from code_context.server import READ_TOOL_NAMES
from code_context.workspace import initialize_workspace
from code_context.write_tools import WRITE_TOOL_NAMES


def test_workspace_stdio_default_off_three_round_edits_original_diff_and_whole_undo():
    # The control socket must also fit when the CI checkout path is longer.
    base = Path(tempfile.mkdtemp(prefix="s-", dir=Path.cwd() / ".artifacts"))
    root, state = base / "r", base / ".code-context" / "live-v1"
    root.mkdir()
    paths = ["a.py", "b.py", "c.py"]
    origin = "def value():\n    return 1\n"
    for path in paths:
        (root / path).write_text(origin)
    pid = initialize_workspace(root, state, "synthetic sample")["project_id"]

    async def run():
        process = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                "-m",
                "code_context",
                "workspace",
                "--root",
                str(root),
                "--data-dir",
                str(state),
            ],
        )
        async with stdio_client(process) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=15) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert set(tools) == READ_TOOL_NAMES | WRITE_TOOL_NAMES

                async def call(name, **arguments):
                    result = await session.call_tool(name, {"project_id": pid, **arguments})
                    assert not result.is_error, (name, result.content)
                    return result.structured_content

                status = await call("write_task_status")
                assert not status["write_enabled"]
                denied = await session.call_tool(
                    "begin_write_task",
                    {
                        "project_id": pid,
                        "request_id": status["next_task_request_id"],
                        "paths": paths,
                    },
                )
                assert denied.is_error and "WRITE_DISABLED" in str(denied.content)
                assert all((root / path).read_text() == origin for path in paths)
                await asyncio.to_thread(
                    control_request, state, "enable_write", {"project_ids": [pid]}
                )
                status = await call("write_task_status")
                task = (
                    await call(
                        "begin_write_task",
                        request_id=status["next_task_request_id"],
                        paths=paths + ["new", "new/tool.py"],
                    )
                )["task_id"]
                for number, path in enumerate(paths):
                    current = await call("read_file", path=path)
                    arguments = {
                        "task_id": task,
                        "request_id": f"stdio_edit_{number:03}",
                        "path": path,
                        "expected_sha256": current["sha256"],
                        "edit": {
                            "kind": "replace_fragment",
                            "old_text": "return 1",
                            "new_text": "return 2",
                        },
                    }
                    changed = await call("apply_edit", **arguments)
                    assert changed["readback_verified"]
                    assert await call("apply_edit", **arguments) == changed
                    assert "return 2" in (await call("read_file", path=path))["content"]
                    stale = await session.call_tool(
                        "read_file",
                        {
                            "project_id": pid,
                            "path": path,
                            "snapshot": current["snapshot"],
                        },
                    )
                    assert stale.is_error
                await call("create_directory", task_id=task, request_id="stdio_dir_001", path="new")
                await call(
                    "create_file",
                    task_id=task,
                    request_id="stdio_file_001",
                    path="new/tool.py",
                    content="CREATED = True\n",
                )
                target = await call("read_file", path="b.py")
                deletion = {
                    "task_id": task,
                    "request_id": "stdio_delete_001",
                    "path": "b.py",
                    "expected_sha256": target["sha256"],
                }
                deleted = await call("delete_file", **deletion)
                assert deleted["state"] == "deleted" and deleted["readback_verified"]
                assert await call("delete_file", **deletion) == deleted
                assert not (root / "b.py").exists()
                diff = await call("get_diff")
                assert diff["summary"]["files_changed"] == 4
                assert diff["summary"]["deleted"] == 1
                patch = (await call("get_diff", path="a.py", detail="patch"))["changes"][0]["patch"]
                assert "-    return 1" in patch and "+    return 2" in patch
                await call("finish_write_task", task_id=task, request_id="stdio_finish_001")
                undone = await call(
                    "rollback_write_task", task_id=task, request_id="stdio_undo_001"
                )
                assert undone["state"] == "rolled_back"
                assert (await call("get_diff"))["summary"]["files_changed"] == 0
                assert all((root / path).read_text() == origin for path in paths)
                assert not (root / "new").exists()
                await asyncio.to_thread(control_request, state, "disable_write", {})
                assert not (await call("write_task_status"))["write_enabled"]
                assert not (state / "server" / "mirror.sqlite3").exists()

    asyncio.run(run())
