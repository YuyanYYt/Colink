"""Repeatable loopback demo. Every run retains its sample files and databases."""

import asyncio
import json
import secrets
import socket
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from code_context.client import RetryableSyncError, SyncClient
from code_context.local import read_local_mirror_status
from code_context.server import create_app
from code_context.storage import MirrorStore


async def inspect_mcp(url: str, token: str, project_id: str) -> dict:
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                discovered = await session.list_tools()
                overview = await session.call_tool("repo_overview", {"project_id": project_id})
                snapshot = overview.structured_content["snapshot"]
                current = await session.call_tool(
                    "read_file",
                    {"project_id": project_id, "path": "main.py", "snapshot": snapshot},
                )
                previous = await session.call_tool(
                    "read_file",
                    {"project_id": project_id, "path": "main.py", "snapshot": "previous"},
                )
                found = await session.call_tool(
                    "search_code",
                    {"project_id": project_id, "query": "build_claims", "snapshot": snapshot},
                )
                diff = await session.call_tool(
                    "get_diff",
                    {"project_id": project_id, "snapshot": snapshot},
                )
                responses = [overview, current, previous, found, diff]
                if any(response.is_error for response in responses):
                    raise RuntimeError("MCP tool returned an error during demonstration")
                old_text = previous.structured_content["content"]
                new_text = current.structured_content["content"]
                assert "version = 1" in old_text and "version = 2" in new_text
                assert found.structured_content["matches"]
                assert diff.structured_content["changes"]
                assert all("revision" not in response.structured_content for response in responses)
                return {
                    "tools": [tool.name for tool in discovered.tools],
                    "read_only": all(t.annotations.read_only_hint for t in discovered.tools),
                    "snapshot_isolation": True,
                    "text_search": True,
                    "snapshot_diff": True,
                }


def run_demo(output_dir: Path) -> dict:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    artifacts = (output_dir / run_id).expanduser().resolve()
    root = artifacts / "sample-project"
    root.mkdir(parents=True)
    old = "version = 1\n\ndef build_claims(title: str):\n    return [title]\n"
    new = old.replace("version = 1", "version = 2")
    (root / "main.py").write_text(old, encoding="utf-8")
    (root / ".env").write_text("DEMO_IGNORED_VALUE=placeholder\n", encoding="utf-8")
    store = MirrorStore(artifacts / "server" / "mirror.sqlite3")
    read_token, sync_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    app = create_app(store, read_token, sync_token)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    worker.start()
    client = None
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not worker.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("local demonstration server did not start")
            time.sleep(0.05)
        client = SyncClient(root, "demo", url, sync_token, artifacts / "client")
        first = client.sync_once()
        assert first["revision"] == 1 and ".env" in first["skipped"]
        assert client.sync_once()["changed_files"] == 0
        (root / "main.py").write_text(new, encoding="utf-8")

        # Simulate an accepted upload whose acknowledgement was lost. Retrying
        # must return its original revision, never apply the same batch twice.
        normal_http = client.http
        lost_ack = False

        def transport(request):
            nonlocal lost_ack
            response = normal_http.send(request)
            if request.method == "POST" and not lost_ack:
                lost_ack = True
                response.close()
                raise httpx.ReadTimeout("simulated lost acknowledgement", request=request)
            return response

        flaky_http = httpx.Client(transport=httpx.MockTransport(transport))
        client.http = flaky_http
        try:
            client.sync_once()
        except RetryableSyncError:
            pass
        else:
            raise AssertionError("lost acknowledgement was not simulated")
        assert client.state.pending() is not None
        assert store.manifest("demo")["revision"] == 2
        client.http = normal_http
        flaky_http.close()
        client.close()
        client = SyncClient(root, "demo", url, sync_token, artifacts / "client")
        recovered = client.sync_once()
        assert recovered["revision"] == 2 and client.state.pending() is None
        assert store.manifest("demo")["revision"] == 2
        result = asyncio.run(inspect_mcp(url + "/mcp", read_token, "demo"))
        result.update(
            {
                "status": "passed",
                "full_sync": True,
                "delta_sync": True,
                "crash_recovery": True,
                "idempotent_retry": True,
                "sensitive_file_excluded": True,
                "revision": 2,
                "artifacts": str(artifacts),
            }
        )
        (artifacts / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
    finally:
        if client is not None:
            client.close()
        server.should_exit = True
        worker.join(timeout=5)
        sock.close()


def run_local_demo(output_dir: Path) -> dict:
    """Exercise the actual continuously-updated stdio command and a full restart."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    artifacts = (output_dir / run_id).expanduser().resolve()
    root, data = artifacts / "sample-project", artifacts / "local-data"
    root.mkdir(parents=True)
    source = root / "main.py"
    source.write_text("version = 1\n", encoding="utf-8")
    (root / ".env").write_text("IGNORED_DEMO_VALUE=placeholder\n", encoding="utf-8")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-B",  # SDK default child env does not inherit the parent's bytecode flag.
            "-m",
            "code_context",
            "local",
            "--root",
            str(root),
            "--project",
            "sample",
            "--data-dir",
            str(data),
        ],
    )

    async def read_version(session, version):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = await session.call_tool(
                "read_file", {"project_id": "sample", "path": "main.py"}
            )
            if (
                not result.is_error
                and result.structured_content["content"] == f"version = {version}\n"
            ):
                assert "revision" not in result.structured_content
                return result.structured_content["snapshot"]
            await asyncio.sleep(0.05)
        raise RuntimeError("local demonstration did not observe the expected source version")

    async def verify(log):
        async with stdio_client(parameters, errlog=log) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                from code_context.server import READ_TOOL_NAMES

                assert {t.name for t in tools} == READ_TOOL_NAMES
                assert all(t.annotations.read_only_hint for t in tools)
                first = await read_version(session, 1)
                overview = await session.call_tool("repo_overview", {"project_id": "sample"})
                assert [f["path"] for f in overview.structured_content["files"]] == ["main.py"]
                source.write_text("version = 2\n", encoding="utf-8")
                second = await read_version(session, 2)
                old = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "main.py", "snapshot": first}
                )
                assert old.structured_content["content"] == "version = 1\n"
                diff = await session.call_tool(
                    "get_diff",
                    {"project_id": "sample", "snapshot": second},
                )
                assert not diff.is_error and diff.structured_content["changes"]
                outside = await session.call_tool("repo_overview", {"project_id": "other"})
                assert outside.is_error
                unsupported = await session.call_tool("execute_command", {"command": "unused"})
                assert unsupported.is_error
        assert not read_local_mirror_status(data)["running"]
        # The process is stopped: the next start must discover offline changes.
        source.write_text("version = 3\n", encoding="utf-8")
        async with stdio_client(parameters, errlog=log) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=10) as session:
                await session.initialize()
                third = await read_version(session, 3)
                assert len({first, second, third}) == 3
                old = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "main.py", "snapshot": second}
                )
                assert old.structured_content["content"] == "version = 2\n"
                expired = await session.call_tool(
                    "read_file", {"project_id": "sample", "path": "main.py", "snapshot": first}
                )
                assert expired.is_error
        return {
            "status": "passed",
            "transport": "stdio",
            "tools": [t.name for t in tools],
            "read_only": True,
            "live_update": True,
            "snapshot_isolation": True,
            "offline_edit_recovered_on_restart": True,
            "source_scope_restricted": True,
            "sensitive_file_excluded": True,
            "revisions": [1, 2, 3],  # CLI-only diagnostic, never an MCP tool result.
            "two_state_retention": True,
            "expired_context_rejected": True,
            "numbered_mcp_versions_removed": True,
            "chatgpt_web_verified": False,
            "artifacts": str(artifacts),
        }

    with (artifacts / "mcp-stderr.log").open("w", encoding="utf-8") as log:
        result = asyncio.run(verify(log))
    assert not read_local_mirror_status(data)["running"]
    (artifacts / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
