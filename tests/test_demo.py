import code_context.demo as demo
from code_context.demo import run_demo, run_local_demo


def test_complete_http_and_sdk_demo(tmp_path):
    result = run_demo(tmp_path / "demo")
    assert result["status"] == "passed"
    assert result["snapshot_isolation"] and result["idempotent_retry"]
    assert result["read_only"] and result["crash_recovery"]
    assert len(result["tools"]) == 6


def test_complete_local_stdio_and_restart_demo(tmp_path, monkeypatch):
    parameters = demo.StdioServerParameters

    def no_bytecode_child(**kwargs):
        assert kwargs["args"][:2] == ["-B", "-m"]
        return parameters(**kwargs)

    monkeypatch.setattr(demo, "StdioServerParameters", no_bytecode_child)
    result = run_local_demo(tmp_path / "demo")
    assert result["status"] == "passed" and result["live_update"]
    assert result["offline_edit_recovered_on_restart"] and result["snapshot_isolation"]
    assert result["source_scope_restricted"] and result["sensitive_file_excluded"]
    assert result["read_only"] and len(result["tools"]) == 6
    assert result["revisions"] == [1, 2, 3]
    assert result["two_state_retention"] and result["expired_context_rejected"]
    assert result["numbered_mcp_versions_removed"]
    assert not result["chatgpt_web_verified"]
