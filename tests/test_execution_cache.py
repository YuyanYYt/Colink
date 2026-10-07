"""Cache ownership/LRU contracts plus separate real native quota checks.

Fake APFS tests verify policy and filesystem operations in private fixtures.
The explicitly marked native tests check actual macOS volumes independently.
"""

import errno
import hashlib
import os
import plistlib
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import code_context.execution_cache as implementation
from code_context.execution_cache import GLOBAL_LIMIT, PROJECT_LIMIT, ExecutionCache
from code_context.local_control import private_directory
from code_context.source_access import SourceError

KEY_A, KEY_B, KEY_C = "a" * 64, "b" * 64, "c" * 64
INPUT_A, INPUT_B = "d" * 64, "e" * 64


class FakeAPFS:
    def __init__(self):
        self.volumes = {}
        self.project_budgets = {}
        self.global_budget = GLOBAL_LIMIT
        self.calls = []
        self.overrides = {}
        self.container_uuid = str(uuid.uuid4()).upper()

    def used(self, volume):
        mount = self.volumes[volume].get("mount")
        return (
            sum(
                p.lstat().st_size
                for p in Path(mount).rglob("*")
                if p.is_file() and not p.is_symlink()
            )
            if mount
            else 0
        )

    def run(self, argv):
        self.calls.append(argv)
        if argv[1:3] == ["apfs", "addVolume"]:
            name = argv[5]
            device = "disk999s" + str(len(self.volumes) + 2)
            unique = str(uuid.uuid4()).upper()
            self.volumes[unique] = {"name": name, "device": device, "mount": None}
            return ("Disk from APFS operation: " + device).encode()
        if argv[1] == "mount":
            volume = self.resolve(argv[-1])
            self.volumes[volume]["mount"] = argv[argv.index("-mountPoint") + 1]
            return b""
        if argv[1:3] == ["apfs", "list"]:
            volumes = [
                {
                    "DeviceIdentifier": "disk999s1",
                    "APFSVolumeUUID": "BASE",
                    "Name": "CoLink-cache",
                    "CapacityQuota": 0,
                    "CapacityInUse": 0,
                }
            ]
            for unique, entry in self.volumes.items():
                used = self.used(unique)
                budget = self.project_budgets.get(entry["name"], PROJECT_LIMIT)
                volume = {
                    "DeviceIdentifier": entry["device"],
                    "APFSVolumeUUID": unique,
                    "Name": entry["name"],
                    "CapacityQuota": PROJECT_LIMIT,
                    "CapacityInUse": PROJECT_LIMIT - budget + used,
                }
                volume.update(self.overrides.get("volume", {}))
                volumes.append(volume)
            result = {
                "ContainerReference": "disk999",
                "APFSContainerUUID": self.container_uuid,
                "CapacityCeiling": GLOBAL_LIMIT,
                "CapacityFree": max(
                    0, self.global_budget - sum(self.used(v) for v in self.volumes)
                ),
                "Volumes": volumes,
            }
            result.update(self.overrides.get("container", {}))
            return plistlib.dumps({"Containers": [result]})
        if argv[1] == "info":
            unique = self.resolve(argv[-1])
            entry = self.volumes[unique]
            info = {
                "VolumeUUID": unique,
                "MountPoint": entry["mount"] or "",
                "APFSContainerReference": "disk999",
                "FilesystemType": "apfs",
                "WritableVolume": True,
            }
            info.update(self.overrides.get("info", {}))
            return plistlib.dumps(info)
        raise AssertionError("unexpected fake native operation")

    def resolve(self, value):
        if value in self.volumes:
            return value
        return next(unique for unique, entry in self.volumes.items() if entry["device"] == value)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    native = FakeAPFS()

    class Disk:
        def __init__(self, root, name, *, size):
            self.state = private_directory(root)
            self.image = root / (name + ".sparsebundle")
            self.image.mkdir(mode=0o700)
            self.mount = root / (name + "-mount")
            self.mount.mkdir(mode=0o700)
            self.access = private_directory(self.mount)
            self.container, self.device, self.volume = "disk999", "disk998", "disk999s1"
            self.closed = False

        def verify(self):
            return self.mount

        def close(self):
            self.closed = True

    monkeypatch.setattr(implementation, "BoundedDisk", Disk)
    monkeypatch.setattr(implementation, "_run", native.run)
    monkeypatch.setattr(
        implementation.os.path,
        "ismount",
        lambda path: any(entry["mount"] == str(path) for entry in native.volumes.values()),
    )
    now = [1.0]
    coordinator = ExecutionCache(tmp_path / "cache", clock=lambda: now[0])
    yield SimpleNamespace(cache=coordinator, native=native, root=tmp_path, now=now)
    # Tests deliberately retain every artifact; this only releases simulated
    # in-memory references and a simulated mount, never removes fixture data.
    coordinator.references.clear()
    coordinator.close()


def acquire(parts, project="first", key=KEY_A, reserve=1):
    return parts.cache.acquire(project, key, reserve_bytes=reserve)


def release(parts, project="first", key=KEY_A):
    parts.cache.release(project, key)


def workspace(parts, name):
    path = parts.root / name
    path.mkdir(mode=0o700)
    return path


def node_modules(root):
    modules = root / "node_modules"
    (modules / "lib").mkdir(parents=True)
    (modules / "lib" / "run.js").write_text("console.log('fixture')\n")
    (modules / ".bin").mkdir()
    (modules / ".bin" / "run").symlink_to("../lib/run.js")
    return modules


def test_same_key_reuses_downloads_and_venv_without_new_volume(cache):
    path = acquire(cache)
    (path / "venv").mkdir()
    (path / "venv" / "fixture.py").write_text("reusable environment\n")
    inode = path.stat().st_ino
    release(cache)
    cache.now[0] = 4
    reused = acquire(cache)
    assert reused.stat().st_ino == inode
    assert (reused / "venv" / "fixture.py").read_text() == "reusable environment\n"
    assert len(cache.native.volumes) == 1
    project = next(iter(cache.cache.keys))
    assert cache.cache.keys[project][KEY_A]["last_used"] == 4


def test_reference_count_prevents_detach_until_all_released(cache):
    acquire(cache)
    acquire(cache)
    assert cache.cache.status()["active_leases"] == 2
    release(cache)
    with pytest.raises(SourceError, match="CACHE_IN_USE"):
        cache.cache.close()
    release(cache)
    cache.cache.close()
    assert cache.cache.disk is None


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_changed_key_identity_is_refused_and_replacement_preserved(cache, replacement):
    path = acquire(cache)
    release(cache)
    retained = path.with_name("retained-key")
    path.rename(retained)
    if replacement == "symlink":
        path.symlink_to(retained, target_is_directory=True)
    else:
        path.mkdir(mode=0o700)
        (path / "unknown.txt").write_text("preserve\n")
    with pytest.raises(SourceError, match="CACHE_IDENTITY_CHANGED"):
        acquire(cache)
    assert retained.is_dir()
    if replacement == "directory":
        assert (path / "unknown.txt").read_text() == "preserve\n"


def test_unknown_key_is_preserved_and_not_adopted(cache):
    path = acquire(cache)
    release(cache)
    unknown = path.parent / KEY_B
    unknown.mkdir(mode=0o700)
    (unknown / "unknown.txt").write_text("preserve\n")
    with pytest.raises(SourceError, match="CACHE_UNOWNED_KEY"):
        acquire(cache, key=KEY_B)
    assert (unknown / "unknown.txt").read_text() == "preserve\n"


@pytest.mark.parametrize(
    "scope,field,value",
    [
        ("volume", "CapacityQuota", 0),
        ("volume", "CapacityQuota", 3 * 1024**3),
        ("info", "VolumeUUID", "OTHER"),
        ("info", "APFSContainerReference", "disk-other"),
        ("info", "WritableVolume", False),
        ("info", "FilesystemType", "external"),
        ("container", "CapacityCeiling", 5 * 1024**3),
        ("container", "APFSContainerUUID", "OTHER"),
    ],
)
def test_quota_container_and_volume_identity_are_revalidated(cache, scope, field, value):
    path = acquire(cache)
    release(cache)
    cache.native.overrides[scope] = {field: value}
    with pytest.raises(SourceError, match="CACHE_QUOTA_UNVERIFIED"):
        acquire(cache)
    assert path.is_dir() and not cache.cache.references


def test_project_pressure_evicts_only_oldest_inactive_entry(cache):
    old = acquire(cache, key=KEY_A)
    (old / "data").write_bytes(b"a" * 20)
    release(cache, key=KEY_A)
    cache.now[0] = 2
    active = acquire(cache, key=KEY_B)
    (active / "data").write_bytes(b"b" * 20)
    name = next(iter(cache.native.volumes.values()))["name"]
    cache.native.project_budgets[name] = 50
    latest = acquire(cache, key=KEY_C, reserve=15)
    assert not old.exists() and active.exists() and latest.exists()
    assert (active / "data").read_bytes() == b"b" * 20
    assert cache.cache.status()["evicted_keys"] == 1


def test_global_pressure_can_reclaim_other_project_inactive_cache(cache):
    old = acquire(cache, "second", KEY_A)
    (old / "data").write_bytes(b"a" * 20)
    release(cache, "second", KEY_A)
    active = acquire(cache, "first", KEY_B)
    (active / "data").write_bytes(b"b" * 20)
    cache.native.global_budget = 50
    latest = acquire(cache, "first", KEY_C, reserve=15)
    assert not old.exists() and active.exists() and latest.exists()


def test_active_working_set_exhaustion_does_not_delete_or_expand(cache):
    path = acquire(cache)
    (path / "data").write_bytes(b"a" * 20)
    cache.native.global_budget = 25
    with pytest.raises(SourceError, match="CACHE_CAPACITY_EXHAUSTED"):
        acquire(cache, key=KEY_B, reserve=10)
    assert (path / "data").read_bytes() == b"a" * 20
    assert cache.cache.status()["active_leases"] == 1
    assert cache.cache.status()["global_limit_bytes"] == GLOBAL_LIMIT


def test_lru_unlinks_relative_symlink_without_touching_target(cache):
    old = acquire(cache)
    external = cache.root / "external-fixture.txt"
    external.write_text("preserve external\n")
    (old / "alias").symlink_to(external)
    release(cache)
    project = hashlib.sha256(b"first").hexdigest()[:24]
    cache.cache._evict(project, KEY_A)
    assert not old.exists() and external.read_text() == "preserve external\n"


def test_node_modules_capture_restore_preserves_safe_relative_bin_links(cache):
    acquire(cache)
    original = workspace(cache, "installed")
    node_modules(original)
    captured = cache.cache.capture_workspace("first", KEY_A, original, input_digest=INPUT_A)
    assert captured["artifacts"] == ["node_modules"]
    fresh = workspace(cache, "next-job")
    restored = cache.cache.restore_workspace("first", KEY_A, fresh, input_digest=INPUT_B)
    assert restored["artifacts"] == ["node_modules"]
    assert (fresh / "node_modules" / ".bin" / "run").is_symlink()
    assert os.readlink(fresh / "node_modules" / ".bin" / "run") == "../lib/run.js"
    assert (fresh / "node_modules" / ".bin" / "run").read_text() == "console.log('fixture')\n"
    (fresh / "node_modules" / "lib" / "run.js").write_text("workspace isolated\n")
    assert (original / "node_modules" / "lib" / "run.js").read_text() == "console.log('fixture')\n"


def test_maven_target_only_restores_exact_source_input_digest(cache):
    acquire(cache)
    original = workspace(cache, "compiled")
    (original / "target").mkdir()
    (original / "target" / "app.class").write_bytes(b"compiled fixture")
    cache.cache.capture_workspace("first", KEY_A, original, input_digest=INPUT_A)
    changed = workspace(cache, "changed-source")
    assert (
        cache.cache.restore_workspace("first", KEY_A, changed, input_digest=INPUT_B)["state"]
        == "empty"
    )
    assert not (changed / "target").exists()
    unchanged = workspace(cache, "same-source")
    assert cache.cache.restore_workspace("first", KEY_A, unchanged, input_digest=INPUT_A)[
        "artifacts"
    ] == ["target"]


@pytest.mark.parametrize("unsafe", ["absolute", "parent", "directory_alias", "hardlink"])
def test_artifact_capture_refuses_external_links_without_reading_them(cache, unsafe):
    acquire(cache)
    original = workspace(cache, "untrusted-artifact")
    modules = node_modules(original)
    external = cache.root / "external-fixture.txt"
    external.write_text("preserve external\n")
    if unsafe == "absolute":
        (modules / "alias").symlink_to(external)
    elif unsafe == "parent":
        (modules / "alias").symlink_to("../../external-fixture.txt")
    elif unsafe == "directory_alias":
        modules.rename(original / "retained-modules")
        modules.symlink_to(original / "retained-modules", target_is_directory=True)
    else:
        os.link(external, modules / "alias")
    with pytest.raises(SourceError, match="CACHE_ARTIFACT_UNSAFE"):
        cache.cache.capture_workspace("first", KEY_A, original, input_digest=INPUT_A)
    assert external.read_text() == "preserve external\n"
    assert cache.cache.keys[next(iter(cache.cache.keys))][KEY_A].get("artifacts", {}) == {}


def test_capture_does_not_replace_artifact_while_another_lease_is_active(cache):
    acquire(cache)
    original = workspace(cache, "first-artifact")
    node_modules(original)
    cache.cache.capture_workspace("first", KEY_A, original)
    acquire(cache)
    (original / "node_modules" / "lib" / "run.js").write_text("replacement\n")
    result = cache.cache.capture_workspace("first", KEY_A, original)
    assert result["active_retained"] == ["node_modules"]
    fresh = workspace(cache, "retained-artifact")
    cache.cache.restore_workspace("first", KEY_A, fresh)
    assert (fresh / "node_modules" / "lib" / "run.js").read_text() == "console.log('fixture')\n"


def test_restore_preserves_existing_workspace_destination(cache):
    acquire(cache)
    original = workspace(cache, "captured")
    node_modules(original)
    cache.cache.capture_workspace("first", KEY_A, original)
    fresh = workspace(cache, "already-installed")
    node_modules(fresh)
    (fresh / "node_modules" / "lib" / "run.js").write_text("preserve existing\n")
    with pytest.raises(SourceError, match="CACHE_ARTIFACT_DESTINATION_EXISTS"):
        cache.cache.restore_workspace("first", KEY_A, fresh)
    assert (fresh / "node_modules" / "lib" / "run.js").read_text() == "preserve existing\n"


def test_artifact_operations_require_active_key_lease(cache):
    path = acquire(cache)
    release(cache)
    fresh = workspace(cache, "workspace")
    for action in (cache.cache.capture_workspace, cache.cache.restore_workspace):
        with pytest.raises(SourceError, match="CACHE_LEASE_REQUIRED"):
            action("first", KEY_A, fresh)
    assert path.exists()


def test_interrupted_eviction_records_intent_and_retries_only_its_known_key(cache, monkeypatch):
    path = acquire(cache)
    (path / "data").write_text("private derived cache\n")
    release(cache)
    project = hashlib.sha256(b"first").hexdigest()[:24]
    erase = cache.cache._erase_contents

    def interrupted(fd):
        raise RuntimeError("injected eviction interruption")

    monkeypatch.setattr(cache.cache, "_erase_contents", interrupted)
    with pytest.raises(RuntimeError, match="eviction interruption"):
        cache.cache._evict(project, KEY_A)
    assert cache.cache.keys[project][KEY_A]["evicting"]
    assert (path / "data").read_text() == "private derived cache\n"
    monkeypatch.setattr(cache.cache, "_erase_contents", erase)
    cache.cache._evict(project, KEY_A)
    assert not path.exists() and KEY_A not in cache.cache.keys[project]


def test_interrupted_copy_preserves_previous_artifact_and_does_not_publish_partial(
    cache, monkeypatch
):
    acquire(cache)
    original = workspace(cache, "installed-before-failure")
    node_modules(original)
    cache.cache.capture_workspace("first", KEY_A, original)
    copy = cache.cache._copy_tree

    def interrupted(source, destination):
        raise SourceError("CACHE_ARTIFACT_COPY_FAILED: injected bounded disk failure")

    monkeypatch.setattr(cache.cache, "_copy_tree", interrupted)
    with pytest.raises(SourceError, match="COPY_FAILED"):
        cache.cache.capture_workspace("first", KEY_A, original)
    monkeypatch.setattr(cache.cache, "_copy_tree", copy)
    restored = workspace(cache, "restored-after-failure")
    cache.cache.restore_workspace("first", KEY_A, restored)
    assert (restored / "node_modules" / "lib" / "run.js").read_text() == "console.log('fixture')\n"


@pytest.mark.parametrize("phase", ["remove", "publish"])
def test_artifact_publish_interruption_recovers_same_owned_copy(cache, monkeypatch, phase):
    acquire(cache)
    original = workspace(cache, "first-publish")
    node_modules(original)
    cache.cache.capture_workspace("first", KEY_A, original)
    (original / "node_modules" / "lib" / "run.js").write_text("new derived artifact\n")

    def interrupted():
        raise RuntimeError("injected artifact-publish interruption")

    seam = "_after_artifact_remove" if phase == "remove" else "_after_artifact_publish"
    monkeypatch.setattr(cache.cache, seam, interrupted)
    with pytest.raises(RuntimeError, match="publish interruption"):
        cache.cache.capture_workspace("first", KEY_A, original)
    project = hashlib.sha256(b"first").hexdigest()[:24]
    assert cache.cache.keys[project][KEY_A]["artifact_replace"]
    fresh = workspace(cache, "after-interruption")
    restored = cache.cache.restore_workspace("first", KEY_A, fresh)
    assert restored["artifacts"] == ["node_modules"]
    assert (fresh / "node_modules" / "lib" / "run.js").read_text() == "new derived artifact\n"
    assert "artifact_replace" not in cache.cache.keys[project][KEY_A]


def test_capture_reclaims_inactive_key_before_attempting_artifact_copy(cache):
    old = acquire(cache)
    (old / "download.fixture").write_bytes(b"x" * (1024**2 + 128))
    release(cache)
    current = acquire(cache, key=KEY_B)
    name = next(iter(cache.native.volumes.values()))["name"]
    cache.native.project_budgets[name] = 2 * 1024**2
    original = workspace(cache, "artifact-needing-headroom")
    node_modules(original)
    result = cache.cache.capture_workspace("first", KEY_B, original)
    assert result["artifacts"] == ["node_modules"]
    assert not old.exists() and current.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="native APFS quota integration")
def test_native_two_gib_project_quota_and_restart_reuse(tmp_path):
    root = tmp_path / "native-cache"
    coordinator = ExecutionCache(root)
    path = coordinator.acquire("native-project", KEY_A)
    (path / "venv").mkdir()
    (path / "venv" / "fixture.py").write_text("retained environment\n")
    project = next(iter(coordinator.projects))
    container = coordinator._container()
    volume = next(
        v
        for v in container["Volumes"]
        if v["APFSVolumeUUID"] == coordinator.projects[project]["uuid"]
    )
    (tmp_path / "quota-proof.plist").write_bytes(plistlib.dumps(container))
    assert volume["CapacityQuota"] == 2 * 1024**3
    assert 0 < container["CapacityCeiling"] <= 4 * 1024**3
    coordinator.release("native-project", KEY_A)
    coordinator.close()
    restarted = ExecutionCache(root)
    try:
        reused = restarted.acquire("native-project", KEY_A)
        assert (reused / "venv" / "fixture.py").read_text() == "retained environment\n"
        restarted.release("native-project", KEY_A)
    finally:
        restarted.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="native APFS quota fill integration")
def test_native_small_fixture_quota_stops_writes_instead_of_expanding(tmp_path, monkeypatch):
    # A 64 MiB test quota demonstrates the same kernel mechanism without leaving
    # a multi-GiB test payload. The production 2 GiB setting is checked above.
    monkeypatch.setattr(implementation, "PROJECT_LIMIT", 64 * 1024**2)
    coordinator = ExecutionCache(tmp_path / "native-small-cache")
    path = coordinator.acquire("native-project", KEY_A, reserve_bytes=1)
    written = 0
    try:
        with (path / "quota-fill.fixture").open("wb", buffering=0) as stream:
            with pytest.raises(OSError) as error:
                for _ in range(100):
                    written += stream.write(b"x" * 1024**2)
                raise AssertionError("native quota did not stop bounded fill")
        assert error.value.errno in {errno.ENOSPC, errno.EDQUOT}
        assert 0 < written <= 64 * 1024**2
    finally:
        coordinator.release("native-project", KEY_A)
        coordinator.close()
