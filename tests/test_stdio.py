import asyncio
import sys

import pytest
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
                assert initialized.server_info.name == "CoLink"
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "list_projects",
                    "repo_overview",
                    "read_file",
                    "search_code",
                    "get_diff",
                    "connection_status",
                    "symbol_search",
                    "read_symbol",
                    "find_references",
                    "get_call_graph",
                    "get_class_graph",
                    "get_file_dependencies",
                    "get_project_architecture",
                    "get_external_dependencies",
                    "get_impact_analysis",
                }
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


@pytest.mark.parametrize("language", ["python", "java"])
def test_stdio_subprocess_serves_all_code_intelligence_queries(tmp_path, capsys, language):
    root = tmp_path.resolve() / "source"
    root.mkdir()
    if language == "python":
        files = {
            "models.py": "class Base:\n    @staticmethod\n    def clean(): return 42\n",
            "service.py": "from models import Base\nfrom typing import Literal\n"
            "class Child(Base):\n    def run(self): return Base.clean()\n",
        }
        base_path, child_path = "models.py", "service.py"
    else:
        files = {
            "Base.java": "package demo; public class Base {"
            " public static int clean() { return 42; } }\n",
            "Child.java": "package demo; import java.util.List; public class Child extends Base {"
            " public int run() { return Base.clean(); } }\n",
        }
        base_path, child_path = "Base.java", "Child.java"
    for path, text in files.items():
        (root / path).write_text(text, encoding="utf-8")
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
            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                await session.initialize()
                overview = await session.call_tool("repo_overview", {"project_id": "lesson"})
                context = {
                    "project_id": "lesson",
                    "snapshot": overview.structured_content["snapshot"],
                }

                async def query(name, **parameters):
                    result = await session.call_tool(name, {**context, **parameters})
                    assert not result.is_error, result.content
                    payload = result.structured_content
                    assert payload["snapshot"] == context["snapshot"]
                    assert "revision" not in payload
                    return payload

                run = (await query("symbol_search", query="run", exact=True))["symbols"][0]
                child = (await query("symbol_search", query="Child", exact=True))["symbols"][0]
                base = (await query("symbol_search", query="Base", exact=True))["symbols"][0]
                source = await query("read_symbol", symbol_id=run["symbol_id"])
                assert "Base.clean()" in source["content"]
                calls = await query("get_call_graph", symbol_id=run["symbol_id"])
                assert any(e["resolution"] == "resolved" for e in calls["edges"])
                classes = await query("get_class_graph", symbol_id=child["symbol_id"])
                assert any(e["kind"] == "INHERITANCE" for e in classes["edges"])
                refs = await query("find_references", symbol_id=base["symbol_id"])
                assert refs["references"]
                deps = await query("get_file_dependencies", path=child_path)
                assert any(e["target"] == base_path for e in deps["edges"])
                architecture = await query("get_project_architecture")
                assert architecture["total_files"] == 2
                external = await query("get_external_dependencies")
                assert external["dependencies"]
                impact = await query("get_impact_analysis", path=base_path)
                assert any(f["path"] == child_path for f in impact["affected_files"])

    asyncio.run(check())
