"""Native local write gate. Synthetic source, not a web/enterprise benchmark."""

import json
import time

from code_context.live import LiveQueries
from code_context.recovery_store import RecoveryStore
from code_context.source_access import SourceAccess
from code_context.write_coordinator import WriteCoordinator


def test_three_hundred_native_small_edits_have_bounded_bodies_and_real_diff(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    original = "VALUE = 0\n#" + "x" * (100 * 1024 - 12) + "\n"
    (root / "a.py").write_text(original)
    source = SourceAccess(root)
    store = RecoveryStore(tmp_path / "recovery")
    backend = LiveQueries({"sample": source})
    c = WriteCoordinator(
        store,
        backend.source,
        control_alive=lambda: True,
        on_change=backend.contexts.invalidate_project,
    )
    backend.write_coordinator = c
    started = time.monotonic()
    try:
        c.enable(["sample"])
        task = c.begin_write_task("sample", c.status()["next_task_request_id"], paths=["a.py"])[
            "task_id"
        ]
        sha = source.read("a.py").sha256
        for index in range(300):
            result = c.apply_edit(
                "sample",
                task,
                f"edit_{index:04d}",
                "a.py",
                sha,
                {
                    "kind": "replace_fragment",
                    "old_text": f"VALUE = {index}\n",
                    "new_text": f"VALUE = {index + 1}\n",
                },
            )
            sha = result["sha256"]
        elapsed = time.monotonic() - started
        before_usage = store.usage()
        assert before_usage["object_count"] == 1  # T0 only, not 300 full bodies.
        assert before_usage["resident_bytes"] < 4 * 1024 * 1024
        current = backend.get_recent_diff("sample", path="a.py", detail="patch")
        assert current["summary"]["files_changed"] == 1
        assert "-VALUE = 0" in current["changes"][0]["patch"]
        assert "+VALUE = 300" in current["changes"][0]["patch"]
        c.finish_write_task("sample", task, "finish_001")
        c.rollback_write_task("sample", task, "rollback_001")
        assert (root / "a.py").read_text() == original
        assert backend.get_recent_diff("sample")["summary"]["files_changed"] == 0
        after_usage = store.usage()
        assert after_usage["object_count"] == 2  # One task start + one undo/latest body.
        assert after_usage["resident_bytes"] < 4 * 1024 * 1024
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {
                    "synthetic": True,
                    "web_verified": False,
                    "operations": 300,
                    "edit_elapsed_seconds": elapsed,
                    "source_bytes": len(original.encode()),
                    "before_rollback": before_usage,
                    "after_rollback": after_usage,
                },
                indent=2,
            )
        )
    finally:
        c.close()
        backend.close()
        store.close()
