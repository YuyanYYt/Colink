"""User-facing regressions from real Colink code-review workflows."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.server import build_mcp
from code_context.storage import MirrorError, MirrorStore


def commit(store, number, files):
    return store.apply(
        "sample",
        SyncBatch(
            request_id=f"workflow-{number}",
            base_revision=number - 1,
            mode="full" if number == 1 else "delta",
            changes=[
                FileChange(op="upsert", path=path, content=text, sha256=content_hash(text))
                for path, text in files.items()
            ],
        ),
    )


def test_initial_diff_does_not_export_the_repository(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    commit(store, 1, {"a.py": "initial_source_marker = 123\n"})
    result = store.get_recent_diff("sample")
    assert result["baseline"] is None
    assert result["reason"] == "NO_PREVIOUS_SNAPSHOT"
    assert result["current_file_count"] == 1
    assert result["changes_available"] is False
    assert result["changes"] == []
    assert "initial_source_marker" not in repr(result)


def test_missing_previous_is_distinct_from_expired_and_missing_project(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    commit(store, 1, {"a.py": "value = 1\n"})
    with pytest.raises(MirrorError, match="NO_PREVIOUS_SNAPSHOT"):
        store.resolve_snapshot("sample", "previous")
    with pytest.raises(MirrorError, match="PROJECT_NOT_FOUND"):
        store.resolve_snapshot("missing")
    handle = store.resolve_snapshot("sample")[1]
    commit(store, 2, {"a.py": "value = 2\n"})
    commit(store, 3, {"a.py": "value = 3\n"})
    with pytest.raises(MirrorError, match="SNAPSHOT_EXPIRED") as error:
        store.resolve_snapshot("sample", handle)
    assert handle not in str(error.value)


def test_diff_summary_then_explicit_patch_use_the_same_comparison(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    commit(store, 1, {"a.py": "value = 1\n", "b.py": "unchanged = True\n"})
    commit(store, 2, {"a.py": "value = 2\nextra = True\n", "c.py": "new = True\n"})
    overview = store.get_recent_diff("sample", limit=1)
    assert overview["summary"]["files_changed"] == 2
    assert overview["has_more"] and overview["next_offset"] == 1
    assert overview["changes"][0]["insertions"] == 2
    assert overview["changes"][0]["deletions"] == 1
    assert "diff" not in overview["changes"][0]
    patch = store.get_recent_diff(
        "sample", snapshot=overview["snapshot"], path="a.py", detail="patch"
    )
    assert "+value = 2" in patch["changes"][0]["diff"]
    assert "-value = 1" in patch["changes"][0]["diff"]
    second = store.get_recent_diff("sample", snapshot=overview["snapshot"], offset=1, limit=1)
    assert second["changes"][0]["path"] == "c.py"
    assert second["next_offset"] is None
    commit(store, 3, {"a.py": "value = 3\n"})
    with pytest.raises(MirrorError, match="COMPARISON_BASELINE_UNAVAILABLE"):
        store.get_recent_diff("sample", snapshot=overview["snapshot"], detail="patch")


def test_empty_baseline_requires_an_explicit_request_and_patch_budget_is_bounded(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    commit(store, 1, {"a.py": "initial_marker\n" * 3000})
    result = store.get_recent_diff("sample", baseline="empty", detail="patch", max_chars=1000)
    assert result["baseline"] == "empty"
    assert result["changes_available"]
    assert result["truncated"]
    assert len(result["changes"][0]["diff"]) <= 1000


def test_mcp_overview_is_compact_and_read_pagination_is_explicit(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    commit(store, 1, {"src/a.py": "value = 1\n" * 205})

    async def check():
        mcp = build_mcp(store)
        overview = (
            await mcp.call_tool("repo_overview", {"project_id": "sample"})
        ).structured_content
        assert "sha256" not in overview["files"][0]
        detailed = (
            await mcp.call_tool("repo_overview", {"project_id": "sample", "include_hashes": True})
        ).structured_content
        assert len(detailed["files"][0]["sha256"]) == 64
        result = (
            await mcp.call_tool(
                "read_file",
                {"project_id": "sample", "path": "src/a.py", "snapshot": overview["snapshot"]},
            )
        ).structured_content
        assert result["next_start_line"] == 201
        with pytest.raises(ToolError, match="NO_PREVIOUS_SNAPSHOT"):
            await mcp.call_tool("repo_overview", {"project_id": "sample", "snapshot": "previous"})

    asyncio.run(check())


@pytest.mark.parametrize(
    "path", ["service-account.json", "service_account-prod.json", ".SSH/config"]
)
def test_known_sensitive_file_names_are_rejected_even_without_a_recognized_token(path):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FileChange(
            op="upsert", path=path, content="placeholder", sha256=content_hash("placeholder")
        )


def test_status_is_read_only_and_available_when_source_reads_fail(tmp_path):
    store = MirrorStore(tmp_path / "mirror.sqlite3")
    commit(store, 1, {"a.py": "value = 1\n"})
    not_ready = False

    def before_read():
        if not_ready:
            raise MirrorError("SOURCE_NOT_READY: local source is not ready")

    mcp = build_mcp(
        store,
        project_scope="sample",
        project_names={"sample": "示例项目"},
        before_read=before_read,
        status_provider=lambda: {
            "state": "failed" if not_ready else "ready",
            "last_sync_at": "2026-10-05T00:00:00Z",
            "last_seen": "2026-10-05T00:00:01Z",
            "root": "private-root-must-not-be-forwarded",
        },
    )

    async def check():
        nonlocal not_ready
        projects = (await mcp.call_tool("list_projects", {})).structured_content
        assert projects["projects"][0]["display_name"] == "示例项目"
        not_ready = True
        manifest = store.manifest("sample")
        status = (await mcp.call_tool("connection_status", {})).structured_content
        assert status["server_reachable"] and status["live_sync_monitored"]
        assert status["source_status"] == "failed"
        assert status["tunnel_status"] == "not_observable_from_mcp"
        assert "private-root" not in repr(status)
        assert ".env.*" in status["filters"]["excluded_file_patterns"]
        assert store.manifest("sample") == manifest
        with pytest.raises(ToolError, match="SOURCE_NOT_READY"):
            await mcp.call_tool("repo_overview", {"project_id": "sample"})
        with pytest.raises(ToolError, match="outside.*scope"):
            await mcp.call_tool("repo_overview", {"project_id": "other"})

    asyncio.run(check())


def test_mirror_only_status_never_claims_live_synchronization(tmp_path):
    mcp = build_mcp(MirrorStore(tmp_path / "mirror.sqlite3"))

    async def check():
        status = (await mcp.call_tool("connection_status", {})).structured_content
        assert status["source_status"] == "mirror_only"
        assert not status["live_sync_monitored"]
        assert status["last_sync_at"] is None
        assert status["project_count"] == 0

    asyncio.run(check())


def test_sensitive_json_exclusion_cannot_be_overridden_by_ignore_negation(tmp_path):
    from code_context.scanner import Scanner

    source = tmp_path / "source"
    source.mkdir()
    (source / "service-account.json").write_text('{"client_secret": "placeholder"}')
    (source / ".gitignore").write_text("!service-account.json\n")
    result = Scanner(source).scan()
    assert "service-account.json" not in result.files


def test_status_tracks_the_actual_local_watcher_lifecycle_without_starting_it(tmp_path):
    from code_context.local import LocalMirror

    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text("value = 1\n")
    with LocalMirror(root, "sample", tmp_path / "data") as source:
        assert source.mcp_status()["state"] == "starting"
        assert source.worker is None
        source.start()
        source.ensure_ready()
        mcp = build_mcp(
            source.store, "sample", source.ensure_ready, status_provider=source.mcp_status
        )

        async def check():
            status = (await mcp.call_tool("connection_status", {})).structured_content
            assert status["source_status"] in {"ready", "syncing"}
            assert status["last_sync_at"] and status["last_seen"]
            assert status["live_sync_monitored"]
            source.close()
            stopped = (await mcp.call_tool("connection_status", {})).structured_content
            assert stopped["source_status"] == "stopped"
            with pytest.raises(ToolError, match="SOURCE_NOT_READY"):
                await mcp.call_tool("repo_overview", {"project_id": "sample"})

        asyncio.run(check())
