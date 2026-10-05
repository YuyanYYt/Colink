import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.server import build_mcp, create_app
from code_context.storage import MirrorStore

READ_TOKEN, SYNC_TOKEN = "r" * 32, "s" * 32


@pytest.fixture
def api(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    app = create_app(store, READ_TOKEN, SYNC_TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, store


def message(path="a.py", content="a=1\n"):
    return {
        "request_id": "initial",
        "base_revision": 0,
        "mode": "full",
        "changes": [
            {"op": "upsert", "path": path, "content": content, "sha256": content_hash(content)},
        ],
    }


def test_read_credentials_cannot_upload_and_health_is_public(api):
    client, store = api
    assert client.get("/health").status_code == 200
    assert client.get("/api/projects").status_code == 401
    assert (
        client.get("/api/projects", headers={"Authorization": f"Bearer {READ_TOKEN}"}).status_code
        == 200
    )
    response = client.post(
        "/api/projects/sample/sync",
        json=message(),
        headers={"Authorization": f"Bearer {READ_TOKEN}"},
    )
    assert response.status_code == 401
    assert store.list_projects()["projects"] == []
    response = client.post(
        "/api/projects/sample/sync",
        json=message(),
        headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 1


def test_rejected_secrets_not_echoed_to_client(api):
    client, store = api
    secret = "sk-" + "z" * 30
    response = client.post(
        "/api/projects/sample/sync",
        json=message(content=secret),
        headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert store.list_projects()["projects"] == []


@pytest.mark.parametrize("nested", [False, True])
def test_unknown_field_names_cannot_echo_secrets(api, nested):
    client, store = api
    secret = "sk-" + "q" * 32
    payload = message()
    if nested:
        payload["changes"][0][secret] = True
    else:
        payload[secret] = True
    response = client.post(
        "/api/projects/sample/sync", json=payload, headers={"Authorization": f"Bearer {SYNC_TOKEN}"}
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "unknown_field" in response.text
    assert store.list_projects()["projects"] == []


def test_deep_json_is_rejected_without_server_error(api):
    client, store = api
    response = client.post(
        "/api/projects/sample/sync",
        content="[" * 1200 + "0" + "]" * 1200,
        headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
    )
    assert response.status_code == 422
    assert store.list_projects()["projects"] == []


@pytest.mark.parametrize("body", [b"\xff", b'{"mode":"full","mode":"delta"}', b"[]", b"{}"])
def test_rejects_invalid_messages_without_writes(api, body):
    client, store = api
    response = client.post(
        "/api/projects/sample/sync", content=body, headers={"Authorization": f"Bearer {SYNC_TOKEN}"}
    )
    assert response.status_code == 422
    assert store.list_projects()["projects"] == []


def test_host_and_origin_validation(api):
    client, _ = api
    assert client.get("/health", headers={"Host": "untrusted.example"}).status_code == 400
    assert client.get("/health", headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/health", headers={"Origin": "http://localhost:8765"}).status_code == 200


def test_real_mcp_protocol_discovery_and_call(api):
    client, _ = api
    client.post(
        "/api/projects/sample/sync",
        json=message(),
        headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
    )
    headers = {
        "Authorization": f"Bearer {READ_TOKEN}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert initialized.status_code == 200
    tools = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        },
    ).json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
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
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    result = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "read_file",
                "arguments": {"project_id": "sample", "path": "a.py"},
            },
        },
    ).json()["result"]
    assert not result.get("isError", False)
    assert result["structuredContent"]["content"] == "a=1\n"
    assert "revision" not in result["structuredContent"]
    assert all("revision" not in t["inputSchema"]["properties"] for t in tools)


def test_payload_limit(api, monkeypatch):
    import code_context.server as module

    monkeypatch.setattr(module, "MAX_REQUEST_BYTES", 20)
    client, store = api
    response = client.post(
        "/api/projects/sample/sync",
        content=json.dumps(message()),
        headers={"Authorization": f"Bearer {SYNC_TOKEN}"},
    )
    assert response.status_code == 413
    assert store.list_projects()["projects"] == []


def test_all_mcp_tools_hide_numbered_versions_pin_context_and_reject_old_schemas(tmp_path):
    store = MirrorStore(tmp_path / "mirror.db")
    for revision in (1, 2):
        text = f"value = {revision}\n"
        store.apply(
            "sample",
            SyncBatch(
                request_id=f"update-{revision}",
                base_revision=revision - 1,
                mode="full" if revision == 1 else "delta",
                changes=[
                    FileChange(op="upsert", path="a.py", content=text, sha256=content_hash(text))
                ],
            ),
        )

    async def check():
        mcp = build_mcp(store, project_scope="sample")
        schemas = await mcp.list_tools()
        legacy_fields = {"revision", "from_revision", "to_revision"}
        assert all(not (legacy_fields & t.input_schema["properties"].keys()) for t in schemas)
        overview = await mcp.call_tool("repo_overview", {"project_id": "sample"})
        handle = overview.structured_content["snapshot"]
        calls = (
            ("list_projects", {}),
            ("repo_overview", {"project_id": "sample", "snapshot": handle}),
            ("read_file", {"project_id": "sample", "path": "a.py", "snapshot": handle}),
            ("search_code", {"project_id": "sample", "query": "value", "snapshot": handle}),
            ("get_diff", {"project_id": "sample", "snapshot": handle}),
        )
        for name, arguments in calls:
            result = await mcp.call_tool(name, arguments)
            assert not (legacy_fields & result.structured_content.keys())
            if name == "list_projects":
                assert all("revision" not in p for p in result.structured_content["projects"])
        with pytest.raises(ToolError, match="refresh"):
            await mcp.call_tool(
                "read_file", {"project_id": "sample", "path": "a.py", "revision": 1}
            )
        with pytest.raises(ToolError, match="refresh"):
            await mcp.call_tool("get_diff", {"project_id": "sample", "from_revision": 1})
        with pytest.raises(ToolError, match="scope"):
            await mcp.call_tool("repo_overview", {"project_id": "outside"})
        first = await mcp.call_tool(
            "repo_overview", {"project_id": "sample", "snapshot": "previous"}
        )
        first_handle = first.structured_content["snapshot"]
        store.apply(
            "sample",
            SyncBatch(
                request_id="update-3",
                base_revision=2,
                mode="delta",
                changes=[
                    FileChange(
                        op="upsert",
                        path="a.py",
                        content="value = 3\n",
                        sha256=content_hash("value = 3\n"),
                    )
                ],
            ),
        )
        old = await mcp.call_tool(
            "read_file", {"project_id": "sample", "path": "a.py", "snapshot": handle}
        )
        assert old.structured_content["content"] == "value = 2\n"
        with pytest.raises(ToolError, match="expired"):
            await mcp.call_tool(
                "read_file", {"project_id": "sample", "path": "a.py", "snapshot": first_handle}
            )

    asyncio.run(check())
