"""Input export limits and bindings; native isolation is verified separately."""

import os

import pytest

import code_context.execution_sandbox as implementation
from code_context.execution_sandbox import export_input
from code_context.local_control import write_state
from code_context.source_access import SourceAccess, SourceError


def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    return root, SourceAccess(root)


def test_input_modes_and_empty_directories_invalidate_existing_plan(tmp_path):
    root, access = source(tmp_path)
    script = root / "run.py"
    script.write_text("print('fixture')\n")
    script.chmod(0o644)
    original = export_input(access, None)
    script.chmod(0o755)
    executable = export_input(access, None)
    assert original["sha256"] != executable["sha256"]
    assert original["files"]["run.py"]["sha256"] == executable["files"]["run.py"]["sha256"]
    (root / "empty").mkdir(mode=0o700)
    with_directory = export_input(access, None)
    assert with_directory["sha256"] != executable["sha256"]
    assert "empty" in with_directory["directories"] and "empty" not in with_directory["files"]
    (root / "empty").chmod(0o755)
    assert export_input(access, None)["sha256"] != with_directory["sha256"]


def test_protected_control_material_is_excluded_even_inside_selected_root(tmp_path):
    root, access = source(tmp_path)
    private = root / "private-control"
    private.mkdir()
    (private / "state.txt").write_text("private fixture state\n")
    (root / "public.py").write_text("VALUE=1\n")
    result = export_input(access, tmp_path / "export", protected_paths=[private])
    assert set(result["files"]) == {"public.py"}
    assert not (tmp_path / "export/private-control").exists()
    assert (private / "state.txt").read_text() == "private fixture state\n"


def test_empty_directory_tree_cannot_bypass_entry_capacity(tmp_path):
    root, access = source(tmp_path)
    for number in range(5):
        (root / f"empty{number}").mkdir()
    with pytest.raises(SourceError, match="EXECUTION_INPUT_LIMIT"):
        export_input(access, None, max_entries=4)
    assert len(list(root.iterdir())) == 5


def test_input_export_deadline_is_independent_of_child_execution(tmp_path, monkeypatch):
    root, access = source(tmp_path)
    (root / "a.py").write_text("VALUE=1\n")
    values = iter([0.0, 16.0])
    monkeypatch.setattr(implementation.time, "monotonic", lambda: next(values, 16.0))
    with pytest.raises(SourceError, match="EXECUTION_INPUT_LIMIT"):
        export_input(access, None)
    assert (root / "a.py").read_text() == "VALUE=1\n"


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_source_link_inputs_rejected_before_following_external_body(tmp_path, link):
    root, access = source(tmp_path)
    external = tmp_path / "external.py"
    external.write_text("EXTERNAL=1\n")
    if link == "symbolic":
        (root / "unsafe.py").symlink_to(external)
    else:
        os.link(external, root / "unsafe.py")
    with pytest.raises(SourceError, match="UNSAFE_EXECUTION_INPUT"):
        export_input(access, None)
    assert external.read_text() == "EXTERNAL=1\n"


def helper_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(implementation, "toolchains", lambda: {})
    parent = tmp_path / "proxy-parent"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(implementation, "PROXY_PARENT", parent)
    sandbox = implementation.NativeSandbox(tmp_path / "helper", [])
    proxy = parent / "colink-srt-fixture"
    proxy.mkdir(mode=0o700)
    info = proxy.stat()
    job = "job-" + "a" * 32
    write_state(
        sandbox.state,
        job + ".json",
        {
            "proxyTmp": str(proxy),
            "proxyIdentity": {"dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid},
        },
    )
    return sandbox, proxy, job


def test_verified_cleanup_retires_own_helper_and_proof_without_other_inputs(tmp_path, monkeypatch):
    sandbox, proxy, job = helper_fixture(tmp_path, monkeypatch)
    write_state(sandbox.state, job + ".json.database-proof", {"authentication": True})
    (proxy / "fixture.sock-state").write_text("own ephemeral proxy state")
    external = tmp_path / "external.txt"
    external.write_text("KEEP")
    sandbox.cleanup_job(job, verified=True)
    assert not proxy.exists()
    assert list(sandbox.state.root.iterdir()) == []
    assert external.read_text() == "KEEP"
    sandbox.cleanup_job(job, verified=True)


def test_unverified_cleanup_preserves_all_referenced_material(tmp_path, monkeypatch):
    sandbox, proxy, job = helper_fixture(tmp_path, monkeypatch)
    with pytest.raises(SourceError, match="CLEANUP_UNVERIFIED"):
        sandbox.cleanup_job(job, verified=False)
    assert proxy.is_dir() and (sandbox.state.root / (job + ".json")).is_file()


def test_proxy_replacement_link_cannot_redirect_cleanup(tmp_path, monkeypatch):
    sandbox, proxy, job = helper_fixture(tmp_path, monkeypatch)
    original = proxy.with_name("retained-original")
    proxy.rename(original)
    external = tmp_path / "external"
    external.mkdir()
    (external / "canary").write_text("KEEP")
    proxy.symlink_to(external, target_is_directory=True)
    with pytest.raises(SourceError, match="PROXY_CHANGED"):
        sandbox.cleanup_job(job, verified=True)
    assert (external / "canary").read_text() == "KEEP"
    assert original.is_dir() and (sandbox.state.root / (job + ".json")).exists()


def test_proxy_link_child_is_preserved_on_rejected_cleanup(tmp_path, monkeypatch):
    sandbox, proxy, job = helper_fixture(tmp_path, monkeypatch)
    external = tmp_path / "external.txt"
    external.write_text("KEEP")
    (proxy / "link").symlink_to(external)
    with pytest.raises(SourceError, match="PROXY_CHANGED"):
        sandbox.cleanup_job(job, verified=True)
    assert external.read_text() == "KEEP" and (proxy / "link").is_symlink()


def test_helper_capacity_refuses_before_native_preparation_and_keeps_receipts(
    tmp_path, monkeypatch
):
    sandbox, _, _ = helper_fixture(tmp_path, monkeypatch)
    for number in range(implementation.MAX_HELPER_FILES - 4):
        write_state(sandbox.state, f"fixture-{number}.json", {})
    monkeypatch.setattr(sandbox, "available", lambda: True)
    with pytest.raises(SourceError, match="METADATA_CAPACITY"):
        sandbox.prepare("job-" + "b" * 32, None, None, [])
    assert len(list(sandbox.state.root.iterdir())) == implementation.MAX_HELPER_FILES - 3


def test_helper_byte_capacity_includes_atomic_replacement_reserve(tmp_path, monkeypatch):
    sandbox, proxy, job = helper_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(implementation, "MAX_HELPER_BYTES", 4 * 65536)
    with pytest.raises(SourceError, match="METADATA_CAPACITY"):
        sandbox._check_metadata_budget()
    assert proxy.is_dir() and (sandbox.state.root / (job + ".json")).exists()
