from pathlib import Path

from code_context.models import FileChange, SyncBatch, content_hash
from code_context.query_backend import MirrorQueryBackend
from code_context.storage import MirrorStore


def test_mirror_adapter_keeps_snapshot_and_source_semantics(tmp_path: Path):
    store = MirrorStore(tmp_path / "mirror.db")
    text = "class Demo:\n    pass\n"
    store.apply(
        "sample",
        SyncBatch(
            request_id="adapter",
            base_revision=0,
            mode="full",
            changes=[
                FileChange(op="upsert", path="demo.py", content=text, sha256=content_hash(text))
            ],
        ),
    )
    backend = MirrorQueryBackend(store)
    assert backend.source_mode == "mirror"
    assert backend.list_projects() == store.list_projects()
    state, handle = backend.resolve_snapshot("sample")
    assert (state, handle) == store.resolve_snapshot("sample")
    assert backend.repo_overview("sample", state, 0, 20) == store.repo_overview(
        "sample", state, 0, 20
    )
    assert backend.read_file("sample", "demo.py", state, 1, None, 20000, 0)["content"] == text
    assert backend.code_query("sample", handle, "symbol_search", query="Demo")["symbols"]
