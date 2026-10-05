import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest

import code_context.project_registry as registry_module
from code_context.project_registry import ProjectRegistry, RegistryError
from code_context.scanner import Scanner
from code_context.source_access import SourceAccess, SourceError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def project(workspace, relative, marker=".git", body=None):
    root = workspace / relative
    root.mkdir(parents=True, exist_ok=True)
    if marker == ".git":
        (root / marker).mkdir(exist_ok=True)
    elif marker is not None:
        (root / marker).write_text("marker metadata only\n", encoding="utf-8")
    if body is not None:
        (root / "main.py").write_text(body, encoding="utf-8")
    return root


def rows(registry):
    return {p["relative_root"]: p for p in registry.list_projects(enabled_only=False)["projects"]}


def state_file(data):
    return data / "projects.json"


def save_test_state(data, state):
    state_file(data).write_text(json.dumps(state), encoding="utf-8")
    state_file(data).chmod(0o600)


def test_discovers_a_through_g_as_pending_from_metadata_without_source_reads(
    workspace, monkeypatch
):
    markers = [
        ".git",
        "pyproject.toml",
        "pom.xml",
        "settings.gradle",
        "settings.gradle.kts",
        "build.gradle",
        "build.gradle.kts",
    ]
    for name, marker in zip("ABCDEFG", markers, strict=True):
        project(workspace, name, marker, body=f"ONLY_{name}_BODY\n")
    registry = ProjectRegistry(workspace)

    def no_body(*args, **kwargs):
        pytest.fail("project discovery must not read source or marker bodies")

    monkeypatch.setattr(Scanner, "_read_text", no_body)
    result = registry.discover()

    assert not result["partial"] and result["reason"] is None
    assert result["directories"] == 8
    assert {p["relative_root"] for p in result["candidates"]} == set("ABCDEFG")
    assert all(p["status"] == "pending" and not p["enabled"] for p in result["candidates"])
    assert all(p["project_id"].startswith("p_") for p in result["candidates"])
    assert registry.list_projects() == {"projects": []}
    assert registry.authorized_sources() == {}
    assert str(workspace) not in json.dumps(result)
    assert "ONLY_A_BODY" not in json.dumps(result)
    first = rows(registry)["A"]["project_id"]
    registry.set_enabled(first, True)
    assert isinstance(registry.source(first), SourceAccess)
    assert registry.names() == {first: "A"}
    assert [p["project_id"] for p in registry.list_projects()["projects"]] == [first]


def test_git_marker_file_is_supported_without_reading_its_target(workspace):
    root = project(workspace, "worktree", marker=None)
    (root / ".git").write_text("gitdir: /unrelated/private/location\n")
    registry = ProjectRegistry(workspace)
    result = registry.discover()
    assert [p["relative_root"] for p in result["candidates"]] == ["worktree"]
    assert "/unrelated/private/location" not in json.dumps(result)


def test_maven_modules_python_packages_and_src_do_not_become_projects(workspace):
    project(workspace, "java", "pom.xml")
    project(workspace, "java/module-one", "pom.xml")
    project(workspace, "java/module-two", "build.gradle")
    project(workspace, "python", "pyproject.toml")
    project(workspace, "python/src/package", "pyproject.toml")
    registry = ProjectRegistry(workspace)
    assert {p["relative_root"] for p in registry.discover()["candidates"]} == {"java", "python"}
    child = registry.register("java/module-one", enabled=True)
    parent = rows(registry)["java"]["project_id"]
    registry.set_enabled(parent, True)
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        registry.source(parent).read("module-one/pom.xml")
    assert registry.source(child).read("pom.xml").content == "marker metadata only\n"


def test_nested_git_is_independent_and_excluded_from_enabled_parent_even_while_disabled(workspace):
    project(workspace, "parent", body="PARENT_BODY\n")
    project(workspace, "parent/child", body="CHILD_BODY\n")
    registry = ProjectRegistry(workspace)
    registry.discover()
    found = rows(registry)
    assert set(found) == {"parent", "parent/child"}
    parent, child = found["parent"]["project_id"], found["parent/child"]["project_id"]
    registry.set_enabled(parent, True)
    source = registry.authorized_sources()[parent]
    assert source.read("main.py").content == "PARENT_BODY\n"
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        source.read("child/main.py")
    assert not any(f["path"].startswith("child/") for f in source.manifest()["files"])
    with pytest.raises(RegistryError, match="PROJECT_NOT_AUTHORIZED"):
        registry.source(child)
    registry.set_enabled(child, True)
    assert registry.source(child).read("main.py").content == "CHILD_BODY\n"
    registry.set_enabled(child, False)
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        source.read("child/main.py")


def test_manual_registration_overrides_boundaries_and_updates_retained_accessors(workspace):
    project(workspace, "parent", body="PARENT_BODY\n")
    project(workspace, "parent/plain", marker=None, body="PLAIN_BODY\n")
    registry = ProjectRegistry(workspace)
    parent = registry.register("parent", enabled=True)
    source = registry.source(parent)
    assert source.read("plain/main.py").content == "PLAIN_BODY\n"
    child = registry.register("parent/plain", display_name="Unmarked", aliases=("manual",))
    assert rows(registry)["parent/plain"]["status"] == "pending"
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        source.read("plain/main.py")
    registry.set_enabled(child, True)
    assert registry.resolve_name("manual") == child
    assert registry.source(child).read("main.py").content == "PLAIN_BODY\n"
    held = registry.source(child)
    registry.set_enabled(child, False)
    with pytest.raises(RegistryError, match="PROJECT_NOT_AUTHORIZED"):
        held.read("main.py")


def test_workspace_itself_can_be_a_manual_project_with_nested_isolation(workspace):
    project(workspace, "child", body="CHILD_BODY\n")
    registry = ProjectRegistry(workspace)
    root = registry.register(display_name="Workspace root", enabled=True)
    child = registry.register("child", enabled=False)
    assert rows(registry)[""]["relative_root"] == ""
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        registry.source(root).read("child/main.py")
    with pytest.raises(RegistryError, match="PROJECT_NOT_AUTHORIZED"):
        registry.source(child)


def test_new_candidates_do_not_inherit_current_authorization_or_reset_existing_names(workspace):
    project(workspace, "A")
    registry = ProjectRegistry(workspace)
    first = registry.register("A", display_name="Alpha", aliases=("primary",), enabled=True)
    project(workspace, "B")
    registry.discover()
    found = rows(registry)
    assert found["A"]["project_id"] == first
    assert found["A"]["display_name"] == "Alpha" and found["A"]["aliases"] == ["primary"]
    assert found["A"]["enabled"]
    assert not found["B"]["enabled"]
    assert set(registry.authorized_sources()) == {first}


def test_stable_ids_bind_roots_and_source_identity_not_names(workspace):
    project(workspace, "A")
    project(workspace, "B")
    registry = ProjectRegistry(workspace, max_projects=2)
    first = registry.register("A", display_name="Before", enabled=True)
    assert registry.register("A", display_name="After", aliases=("alias",), enabled=True) == first
    assert ProjectRegistry(workspace).register("A") == first
    assert registry.register("B", display_name="After", enabled=True) != first
    assert len(rows(registry)) == 2


def test_same_names_and_alias_collisions_require_unique_exact_selection(workspace):
    project(workspace, "one")
    project(workspace, "two")
    project(workspace, "three")
    registry = ProjectRegistry(workspace)
    one = registry.register("one", display_name="Same", aliases=("first", "shared"), enabled=True)
    two = registry.register("two", display_name="Same", aliases=("second", "shared"), enabled=True)
    three = registry.register("three", display_name="Hidden", aliases=("first",))
    assert registry.resolve_name("first") == one
    assert registry.resolve_name("second") == two
    for name in ("Same", "shared"):
        with pytest.raises(RegistryError, match="AMBIGUOUS_PROJECT") as error:
            registry.resolve_name(name)
        assert set(error.value.candidates) == {one, two}
        assert three not in error.value.candidates
    registry.set_enabled(two, False)
    assert registry.resolve_name("Same") == one


def test_keyword_matches_return_candidates_without_choosing_or_reading(workspace):
    project(workspace, "A", body="A_BODY\n")
    registry = ProjectRegistry(workspace)
    first = registry.register("A", display_name="Alpha service", enabled=True)
    with pytest.raises(RegistryError, match="PROJECT_NAME_NOT_FOUND") as error:
        registry.resolve_name("Alpha")
    assert error.value.candidates == (first,)
    assert registry.source(first).metrics["body_reads"] == 0
    with pytest.raises(RegistryError, match="PROJECT_NAME_NOT_FOUND") as error:
        registry.resolve_name("No match")
    assert error.value.candidates == ()


@pytest.mark.parametrize(
    "relative",
    [
        "../outside",
        "/absolute",
        "A/../B",
        "A//B",
        ".",
        "A\\B",
        "node_modules/A",
        ".artifacts/A",
        ".code-context/A",
        "vendor/A",
    ],
)
def test_outside_and_excluded_manual_roots_are_rejected_without_echoing_input(workspace, relative):
    registry = ProjectRegistry(workspace)
    with pytest.raises(RegistryError, match="INVALID_PROJECT_ROOT") as error:
        registry.register(relative)
    assert str(workspace) not in str(error.value)
    assert "outside" not in str(error.value)
    assert registry.list_projects(enabled_only=False) == {"projects": []}


@pytest.mark.parametrize(
    "name",
    [
        "node_modules",
        "target",
        "build",
        "dist",
        ".venv",
        ".git",
        ".artifacts",
        ".code-context",
        "vendor",
        ".gradle",
        ".cache",
    ],
)
def test_dependency_build_cache_and_application_directories_are_pruned(workspace, name):
    project(workspace, f"{name}/hidden")
    project(workspace, "A")
    registry = ProjectRegistry(workspace)
    result = registry.discover()
    # A workspace .git is itself a root marker; its contents remain pruned.
    expected = {"", "A"} if name == ".git" else {"A"}
    assert {p["relative_root"] for p in result["candidates"]} == expected
    assert not result["partial"]


def test_symlink_projects_markers_and_ancestors_are_never_followed(workspace, tmp_path):
    outside = project(tmp_path, "outside")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    (workspace / "ancestor").symlink_to(outside, target_is_directory=True)
    plain = project(workspace, "plain", marker=None)
    (plain / ".git").symlink_to(outside / ".git", target_is_directory=True)
    registry = ProjectRegistry(workspace)
    assert registry.discover()["candidates"] == []
    for relative in ("link", "ancestor/subdir"):
        with pytest.raises(RegistryError, match="PROJECT_SOURCE_UNAVAILABLE"):
            registry.register(relative)
    with pytest.raises(RegistryError, match="INVALID_WORKSPACE"):
        ProjectRegistry(workspace / "link")


@pytest.mark.parametrize("workspace_path", [Path("/"), Path.home(), Path.home().parent])
def test_system_root_and_home_are_not_accepted_as_workspaces(workspace_path):
    with pytest.raises(RegistryError, match="INVALID_WORKSPACE"):
        ProjectRegistry(workspace_path)


@pytest.mark.parametrize(
    "limits,reason",
    [
        ({"max_projects": 2}, "max_projects"),
        ({"max_directories": 2}, "max_directories"),
        ({"max_depth": 0}, "max_depth"),
    ],
)
def test_discovery_budget_returns_explicit_partial_results(workspace, limits, reason):
    for name in "ABCDEFG":
        project(workspace, name)
    result = ProjectRegistry(workspace, **limits).discover()
    assert result["partial"] and reason in result["reason"]
    assert result["directories"] <= limits.get("max_directories", 5000)
    assert len(result["candidates"]) <= limits.get("max_projects", 64)


def test_discovery_depth_limit_does_not_claim_to_have_found_deep_projects(workspace):
    project(workspace, "group/deep/A")
    result = ProjectRegistry(workspace, max_depth=1).discover()
    assert result["candidates"] == []
    assert result["partial"] and result["reason"] == "max_depth"


def test_discovery_time_budget_is_checked_without_reading_sources(workspace, monkeypatch):
    project(workspace, "A")
    ticks = iter([0.0, 4.0])
    monkeypatch.setattr(registry_module, "monotonic", lambda: next(ticks))
    result = ProjectRegistry(workspace, max_seconds=3).discover()
    assert result["partial"] and result["reason"] == "max_seconds"
    assert result["directories"] == 0 and result["candidates"] == []


def test_manual_registration_capacity_is_global_and_preserves_old_ids(workspace):
    for name in "ABC":
        project(workspace, name)
    registry = ProjectRegistry(workspace, max_projects=2)
    first = registry.register("A", enabled=True)
    second = registry.register("B", enabled=True)
    with pytest.raises(RegistryError, match="PROJECT_CAPACITY"):
        registry.register("C", enabled=True)
    assert set(registry.authorized_sources()) == {first, second}


def test_source_replacement_revokes_old_binding_and_new_discovery_stays_pending(
    workspace, tmp_path
):
    root = project(workspace, "A", body="OLD_BODY\n")
    registry = ProjectRegistry(workspace)
    old = registry.register("A", enabled=True)
    held = registry.source(old)
    root.rename(tmp_path / "moved-A")
    project(workspace, "A", body="NEW_BODY\n")
    with pytest.raises(RegistryError, match="PROJECT_SOURCE_CHANGED"):
        registry.source(old)
    with pytest.raises(SourceError):
        held.read("main.py")
    assert rows(registry)["A"]["status"] == "unavailable"
    result = registry.discover()
    new = result["candidates"][0]["project_id"]
    assert new != old and not result["candidates"][0]["enabled"]
    with pytest.raises(RegistryError, match="UNKNOWN_PROJECT"):
        registry.source(old)
    with pytest.raises(RegistryError, match="PROJECT_NOT_AUTHORIZED"):
        registry.source(new)
    registry.set_enabled(new, True)
    assert registry.source(new).read("main.py").content == "NEW_BODY\n"


def test_replacing_ancestor_does_not_adopt_a_moved_child_with_the_same_inode(workspace, tmp_path):
    child = project(workspace, "group/A", body="OLD_BODY\n")
    registry = ProjectRegistry(workspace)
    project_id = registry.register("group/A", enabled=True)
    before = child.stat().st_ino
    (workspace / "group").rename(tmp_path / "old-group")
    (workspace / "group").mkdir()
    (tmp_path / "old-group/A").rename(workspace / "group/A")
    assert (workspace / "group/A").stat().st_ino == before
    with pytest.raises(RegistryError, match="PROJECT_SOURCE_CHANGED"):
        registry.source(project_id)
    assert not rows(registry)["group/A"]["enabled"]


def test_missing_project_does_not_follow_a_new_location_or_reenable_when_restored(
    workspace, tmp_path
):
    root = project(workspace, "A", body="A_BODY\n")
    registry = ProjectRegistry(workspace)
    first = registry.register("A", enabled=True)
    root.rename(tmp_path / "moved")
    assert registry.authorized_sources() == {}
    assert rows(registry)["A"]["status"] == "unavailable"
    (tmp_path / "moved").rename(root)
    assert registry.authorized_sources() == {}
    assert rows(registry)["A"]["status"] == "pending"
    registry.set_enabled(first, True)
    assert registry.source(first).read("main.py").content == "A_BODY\n"


def test_workspace_replacement_invalidates_registry_and_retained_sources(workspace, tmp_path):
    project(workspace, "A", body="OLD_BODY\n")
    registry = ProjectRegistry(workspace)
    first = registry.register("A", enabled=True)
    held = registry.source(first)
    workspace.rename(tmp_path / "old-workspace")
    workspace.mkdir()
    project(workspace, "A", body="NEW_BODY\n")
    with pytest.raises(RegistryError, match="WORKSPACE_CHANGED"):
        registry.discover()
    with pytest.raises(RegistryError, match="WORKSPACE_CHANGED"):
        held.read("main.py")


def test_only_asking_a_never_reads_b_bodies(workspace, monkeypatch):
    for name in "ABCDEFG":
        project(workspace, name, body=f"ONLY_{name}_BODY\n")
    registry = ProjectRegistry(workspace)
    registry.discover()
    found = rows(registry)
    for row in found.values():
        registry.set_enabled(row["project_id"], True)
    sources = registry.authorized_sources()
    read = []
    original = Scanner._read_text

    def recorded(scanner, parent, name, path):
        read.append((scanner.root, path))
        assert scanner.root == workspace / "A", "querying A must not read another project's body"
        return original(scanner, parent, name, path)

    monkeypatch.setattr(Scanner, "_read_text", recorded)
    assert registry.resolve_name("A") == found["A"]["project_id"]
    assert registry.source(found["A"]["project_id"]).read("main.py").content == "ONLY_A_BODY\n"
    assert read and all(root == workspace / "A" for root, _ in read)
    assert all(
        sources[row["project_id"]].metrics["body_reads"] == 0
        for name, row in found.items()
        if name != "A"
    )


def test_persistent_metadata_restores_stable_ids_names_authorizations_and_nested_isolation(
    workspace, tmp_path
):
    project(workspace, "A", body="PARENT_PRIVATE_BODY\n")
    project(workspace, "A/child", body="CHILD_PRIVATE_BODY\n")
    data = tmp_path / "state/nested"
    registry = ProjectRegistry(workspace, data_dir=data)
    parent = registry.register("A", display_name="Alpha", aliases=("first",), enabled=True)
    child = registry.register("A/child")
    saved = json.loads(state_file(data).read_text())
    assert saved["schema"] == 1 and saved["authorized_projects"] == [parent]
    assert saved["workspace"]["source_id"]
    assert all(p["source_id"] for p in saved["projects"])
    assert "PARENT_PRIVATE_BODY" not in state_file(data).read_text()
    assert "CHILD_PRIVATE_BODY" not in state_file(data).read_text()
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file(data).stat().st_mode) == 0o600

    reopened = ProjectRegistry(workspace, data_dir=data)
    assert reopened.resolve_name("first") == parent
    assert reopened.names() == {parent: "Alpha"}
    assert set(reopened.authorized_sources()) == {parent}
    assert rows(reopened)["A/child"]["project_id"] == child
    with pytest.raises(SourceError, match="PATH_EXCLUDED"):
        reopened.source(parent).read("child/main.py")


def test_persistence_rechecks_replaced_source_and_does_not_transfer_authorization(
    workspace, tmp_path
):
    root = project(workspace, "A")
    data = tmp_path / "state"
    registry = ProjectRegistry(workspace, data_dir=data)
    old = registry.register("A", enabled=True)
    root.rename(tmp_path / "old-A")
    project(workspace, "A")
    reopened = ProjectRegistry(workspace, data_dir=data)
    assert reopened.authorized_sources() == {}
    assert rows(reopened)["A"]["project_id"] == old
    assert not rows(reopened)["A"]["enabled"]
    assert json.loads(state_file(data).read_text())["authorized_projects"] == []
    with pytest.raises(RegistryError, match="PROJECT_SOURCE_CHANGED"):
        reopened.set_enabled(old, True)


@pytest.mark.parametrize("replacement", [False, True])
def test_persistent_state_rejects_other_or_replaced_workspace(workspace, tmp_path, replacement):
    data = tmp_path / "state"
    ProjectRegistry(workspace, data_dir=data)
    if replacement:
        workspace.rename(tmp_path / "original")
        workspace.mkdir()
        other = workspace
    else:
        other = tmp_path / "other"
        other.mkdir()
    before = state_file(data).read_bytes()
    with pytest.raises(RegistryError, match="REGISTRY_WORKSPACE_MISMATCH"):
        ProjectRegistry(other, data_dir=data)
    assert state_file(data).read_bytes() == before


@pytest.mark.parametrize("schema", [2, True, "1"])
def test_unknown_schema_is_rejected_without_overwriting_metadata(workspace, tmp_path, schema):
    data = tmp_path / "state"
    ProjectRegistry(workspace, data_dir=data)
    state = json.loads(state_file(data).read_text())
    state["schema"] = schema
    save_test_state(data, state)
    before = state_file(data).read_bytes()
    with pytest.raises(RegistryError, match="UNKNOWN_REGISTRY_SCHEMA"):
        ProjectRegistry(workspace, data_dir=data)
    assert state_file(data).read_bytes() == before


@pytest.mark.parametrize(
    "corruption", ["id", "authorized", "enabled", "root", "extra", "duplicate"]
)
def test_invalid_persisted_bindings_and_authorization_sets_are_rejected(
    workspace, tmp_path, corruption
):
    project(workspace, "A")
    data = tmp_path / "state"
    registry = ProjectRegistry(workspace, data_dir=data)
    registry.register("A", enabled=True)
    state = json.loads(state_file(data).read_text())
    if corruption == "id":
        state["projects"][0]["project_id"] = "p_forged"
    elif corruption == "authorized":
        state["authorized_projects"] = []
    elif corruption == "enabled":
        state["projects"][0]["enabled"] = "true"
    elif corruption == "root":
        state["projects"][0]["relative_root"] = "../private-source"
    elif corruption == "extra":
        state["projects"][0]["content"] = "SYNTHETIC_PRIVATE_BODY"
    else:
        state["projects"].append(dict(state["projects"][0]))
    save_test_state(data, state)
    with pytest.raises(RegistryError) as error:
        ProjectRegistry(workspace, data_dir=data)
    assert "private-source" not in str(error.value)
    assert "SYNTHETIC_PRIVATE_BODY" not in str(error.value)


def test_custom_state_directory_is_pruned_from_discovery_and_parent_reads(workspace):
    data = workspace / "local-state"
    registry = ProjectRegistry(workspace, data_dir=data)
    project(data, "hidden")
    parent = registry.register(enabled=True)
    assert {p["relative_root"] for p in registry.discover()["candidates"]} == {""}
    assert not any(
        f["path"].startswith("local-state/") for f in registry.source(parent).manifest()["files"]
    )
    with pytest.raises(RegistryError, match="INVALID_PROJECT_ROOT"):
        registry.register("local-state/hidden")


def test_state_directory_and_metadata_symlinks_and_public_permissions_are_rejected(
    workspace, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RegistryError, match="UNSAFE_REGISTRY_STATE"):
        ProjectRegistry(workspace, data_dir=linked / "child")
    assert not (outside / "child").exists()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(RegistryError, match="UNSAFE_REGISTRY_STATE"):
        ProjectRegistry(workspace, data_dir=public)
    data = tmp_path / "state"
    data.mkdir(mode=0o700)
    private = tmp_path / "private.json"
    private.write_text("SYNTHETIC_PRIVATE_BODY")
    state_file(data).symlink_to(private)
    with pytest.raises(RegistryError, match="UNSAFE_REGISTRY_STATE"):
        ProjectRegistry(workspace, data_dir=data)
    assert private.read_text() == "SYNTHETIC_PRIVATE_BODY"


@pytest.mark.parametrize("unsafe", ["public", "hardlink", "fifo"])
def test_unsafe_state_files_are_rejected_without_blocking_or_modifying_them(
    workspace, tmp_path, unsafe
):
    data = tmp_path / "state"
    data.mkdir(mode=0o700)
    path = state_file(data)
    if unsafe == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text("SYNTHETIC_PRIVATE_BODY")
        path.chmod(0o644 if unsafe == "public" else 0o600)
        if unsafe == "hardlink":
            os.link(path, tmp_path / "linked.json")
    with pytest.raises(RegistryError, match="UNSAFE_REGISTRY_STATE"):
        ProjectRegistry(workspace, data_dir=data)


def test_replaced_state_directory_cannot_receive_new_authorizations(workspace, tmp_path):
    project(workspace, "A")
    data = tmp_path / "state"
    registry = ProjectRegistry(workspace, data_dir=data)
    data.rename(tmp_path / "old-state")
    data.mkdir(mode=0o700)
    with pytest.raises(RegistryError, match="REGISTRY_STORAGE_CHANGED"):
        registry.register("A", enabled=True)
    with pytest.raises(RegistryError, match="REGISTRY_STORAGE_CHANGED"):
        registry.list_projects(enabled_only=False)
    assert not state_file(data).exists()


def test_atomic_save_failure_preserves_prior_state_and_does_not_grant_in_memory(
    workspace, tmp_path, monkeypatch
):
    project(workspace, "A")
    data = tmp_path / "state"
    registry = ProjectRegistry(workspace, data_dir=data)
    first = registry.register("A")
    before = state_file(data).read_bytes()

    def failed_replace(*args, **kwargs):
        raise OSError("SYNTHETIC_PRIVATE_DETAIL")

    monkeypatch.setattr(registry_module.os, "replace", failed_replace)
    with pytest.raises(RegistryError, match="REGISTRY_SAVE_FAILED") as error:
        registry.set_enabled(first, True)
    assert "SYNTHETIC_PRIVATE_DETAIL" not in str(error.value)
    assert state_file(data).read_bytes() == before
    assert registry.authorized_sources() == {}
    assert list(data.glob("projects.json.tmp-*")), "failed save artifacts must be retained"


def test_stale_registry_writer_cannot_overwrite_more_recent_authorizations(workspace, tmp_path):
    project(workspace, "A")
    project(workspace, "B")
    data = tmp_path / "state"
    first = ProjectRegistry(workspace, data_dir=data)
    stale = ProjectRegistry(workspace, data_dir=data)
    first_id = first.register("A", enabled=True)
    with pytest.raises(RegistryError, match="REGISTRY_STORAGE_CHANGED"):
        stale.register("B", enabled=True)
    reopened = ProjectRegistry(workspace, data_dir=data)
    assert set(reopened.authorized_sources()) == {first_id}
    assert set(rows(reopened)) == {"A"}


def test_retained_sources_cannot_use_permissions_revoked_by_another_local_registry(
    workspace, tmp_path
):
    project(workspace, "A", body="A_BODY\n")
    data = tmp_path / "state"
    first = ProjectRegistry(workspace, data_dir=data)
    project_id = first.register("A", enabled=True)
    held = first.source(project_id)
    second = ProjectRegistry(workspace, data_dir=data)
    second.set_enabled(project_id, False)
    with pytest.raises(RegistryError, match="REGISTRY_STORAGE_CHANGED"):
        held.read("main.py")
    assert held.metrics["body_reads"] == 0
    with pytest.raises(RegistryError, match="REGISTRY_STORAGE_CHANGED"):
        first.authorized_sources()


def test_list_results_are_detached_from_internal_registration_metadata(workspace):
    project(workspace, "A")
    registry = ProjectRegistry(workspace)
    first = registry.register("A", display_name="Alpha", aliases=("first",), enabled=True)
    returned = registry.list_projects()["projects"][0]
    returned["aliases"].append("forged")
    returned["enabled"] = False
    assert registry.resolve_name("first") == first
    with pytest.raises(RegistryError, match="PROJECT_NAME_NOT_FOUND"):
        registry.resolve_name("forged")
    assert registry.names() == {first: "Alpha"}


def test_concurrent_registration_enforces_one_global_project_budget(workspace):
    for name in "ABCDEFG":
        project(workspace, name)
    registry = ProjectRegistry(workspace, max_projects=3)

    def register(name):
        try:
            return registry.register(name, enabled=True)
        except RegistryError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(register, "ABCDEFG"))
    assert sum(result is not None for result in results) == 3
    assert len(registry.authorized_sources()) == 3


def test_callers_holding_source_lock_can_read_while_another_caller_resolves_source(
    workspace, monkeypatch
):
    project(workspace, "A", body="A_BODY\n")
    registry = ProjectRegistry(workspace)
    project_id = registry.register("A", enabled=True)
    source = registry.source(project_id)
    locked, resolving = Event(), Event()
    original = SourceAccess.ensure_available
    results, errors = [], []

    def signaled(access):
        if access is source and current_thread().name == "registry-getter":
            resolving.set()
        return original(access)

    monkeypatch.setattr(SourceAccess, "ensure_available", signaled)

    def locked_reader():
        try:
            with source.lock:
                locked.set()
                assert resolving.wait(timeout=5)
                results.append(source.read("main.py").content)
        except Exception as exc:
            errors.append(exc)

    def getter():
        try:
            assert locked.wait(timeout=5)
            registry.source(project_id)
        except Exception as exc:
            errors.append(exc)

    reader = Thread(target=locked_reader, daemon=True)
    resolving_thread = Thread(target=getter, name="registry-getter", daemon=True)
    reader.start()
    resolving_thread.start()
    reader.join(timeout=5)
    resolving_thread.join(timeout=5)
    assert not reader.is_alive() and not resolving_thread.is_alive()
    assert not errors
    assert results == ["A_BODY\n"]


@pytest.mark.parametrize("payload", ["{", '{"schema":1,"schema":1}', "[" * 2000])
def test_malformed_duplicate_or_deep_metadata_is_rejected_without_overwriting(
    workspace, tmp_path, payload
):
    data = tmp_path / "state"
    data.mkdir(mode=0o700)
    state_file(data).write_text(payload)
    state_file(data).chmod(0o600)
    with pytest.raises(RegistryError, match="INVALID_REGISTRY_METADATA"):
        ProjectRegistry(workspace, data_dir=data)
    assert state_file(data).read_text() == payload


def test_oversized_metadata_is_rejected_before_loading(workspace, tmp_path):
    data = tmp_path / "state"
    data.mkdir(mode=0o700)
    state_file(data).write_bytes(b" " * (1024 * 1024 + 1))
    state_file(data).chmod(0o600)
    with pytest.raises(RegistryError, match="REGISTRY_METADATA_LIMIT"):
        ProjectRegistry(workspace, data_dir=data)


def test_long_directory_names_get_bounded_display_names_and_survive_restart(workspace, tmp_path):
    name = "a" * 160
    project(workspace, name)
    data = tmp_path / "state"
    registry = ProjectRegistry(workspace, data_dir=data)
    candidate = registry.discover()["candidates"][0]
    assert candidate["display_name"] == name[:128]
    reopened = ProjectRegistry(workspace, data_dir=data)
    assert rows(reopened)[name]["project_id"] == candidate["project_id"]


@pytest.mark.parametrize(
    "options",
    [
        {"max_projects": 0},
        {"max_projects": True},
        {"max_directories": -1},
        {"max_depth": -1},
        {"max_depth": 1.5},
        {"max_seconds": 0},
        {"max_seconds": float("nan")},
        {"max_seconds": float("inf")},
    ],
)
def test_invalid_discovery_limits_are_rejected(workspace, options):
    with pytest.raises(ValueError):
        ProjectRegistry(workspace, **options)
