import asyncio
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from code_context.cli import main


def test_stdio_subprocess_reads_snapshot(tmp_path, capsys):
    root = tmp_path.resolve() / "source"
    root.mkdir()
    (root / "lesson.py").write_text("answer = 42\n", encoding="utf-8")
    data = tmp_path / "data"
    assert (
        main(["snapshot", "--root", str(root), "--project", "lesson", "--data-dir", str(data)]) == 0
    )
    capsys.readouterr()

    async def check():
        process = StdioServerParameters(
            command=sys.executable,
            args=["-m", "code_context", "mcp", "--data-dir", str(data)],
        )
        async with stdio_client(process) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=5) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "Colink"
                tools = await session.list_tools()
                assert len(tools.tools) == 5
                result = await session.call_tool(
                    "read_file",
                    {
                        "project_id": "lesson",
                        "path": "lesson.py",
                        "snapshot": "current",
                    },
                )
                assert not result.is_error
                assert result.structured_content["content"] == "answer = 42\n"
                rejected = await session.call_tool("execute_command", {"command": "unused"})
                assert rejected.is_error

    asyncio.run(check())
