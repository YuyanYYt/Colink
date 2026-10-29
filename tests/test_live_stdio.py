"""The standalone Live command serves its advertised analysis over real stdio."""

import asyncio
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.parametrize("language", ["python", "java", "typescript", "javascript"])
def test_standalone_live_analysis_is_lazy_readonly_and_closes_its_index(tmp_path, language):
    root = tmp_path / "source"
    root.mkdir()
    if language == "python":
        files = {
            "helper.py": "def normalize(value): return value + 1\n",
            "main.py": "from helper import normalize\ndef run(value): return normalize(value)\n",
        }
        left = {"path": "main.py", "qualname": "run"}
        right = {"path": "helper.py", "qualname": "normalize"}
    elif language == "java":
        files = {
            "Helper.java": "class Helper { static int normalize(int value) { return value+1; } }\n",
            "Flow.java": (
                "class Flow { static int run(int value) { return Helper.normalize(value); } }\n"
            ),
        }
        left = {"path": "Flow.java", "qualname": "Flow.run(int)"}
        right = {"path": "Helper.java", "qualname": "Helper.normalize(int)"}
    else:
        suffix = "ts" if language == "typescript" else "js"
        annotation = ": number" if suffix == "ts" else ""
        files = {
            f"helper.{suffix}": (
                f"export function normalize(value{annotation}) {{ return value+1; }}\n"
            ),
            f"main.{suffix}": (
                f'import {{ normalize }} from "./helper.{suffix}";\n'
                f"export function run(value{annotation}) {{ return normalize(value); }}\n"
            ),
        }
        left = {"path": f"main.{suffix}", "qualname": "run"}
        right = {"path": f"helper.{suffix}", "qualname": "normalize"}
    for path, text in files.items():
        (root / path).write_text(text)
    data = tmp_path / "data"
    process = StdioServerParameters(
        command=sys.executable,
        args=[
            "-B",
            "-m",
            "code_context",
            "live",
            "--root",
            str(root),
            "--project",
            "sample",
            "--data-dir",
            str(data),
        ],
    )

    async def check():
        for attempt in range(2):
            async with stdio_client(process) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=15) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    assert len(tools) == 9 and "terminal_start" not in tools
                    assert tools["analyze_code"].annotations.read_only_hint
                    assert "mode" not in tools["analyze_code"].input_schema["properties"]
                    overview = await session.call_tool("repo_overview", {"project_id": "sample"})
                    assert not overview.is_error
                    if attempt == 0:
                        assert not data.exists()
                        assert not list((data / "indexes").glob("projects/*/live-index.sqlite3"))
                    result = await session.call_tool(
                        "analyze_code",
                        {
                            "project_id": "sample",
                            "snapshot": overview.structured_content["snapshot"],
                            "selector": left,
                            "target": right,
                            "include": ["source"],
                            "max_chars": 20000,
                        },
                    )
                    assert not result.is_error, result.content
                    payload = result.structured_content
                    assert payload["mode"] == "call_path" and payload["path_found"]
                    assert payload["source"]["path"] == left["path"]
                    assert "normalize" in payload["source"]["content"]
            # A second real process must acquire the closed ownership lock and
            # serve the same facts from the retained derived index.
            assert list((data / "indexes").glob("projects/*/live-index.sqlite3"))

    asyncio.run(check())
