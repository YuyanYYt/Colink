"""Journal recovery policy in new private fixtures; native proof is separate."""

import copy
import json
import os
import plistlib
import sys
import uuid
from types import SimpleNamespace

import pytest

from code_context.execution_disk_recovery import NativeDiskRecovery, TaskDiskRecovery, disk_identity
from code_context.execution_sandbox import BoundedDisk
from code_context.local_control import private_directory, read_state, write_state
from code_context.source_access import SourceAccess


class FakeNative(NativeDiskRecovery):
    """The real native validation runs over explicitly simulated APFS reports."""

    def __init__(self, image, record):
        self.attached = False
        self.calls = []
        self.entry = {
            "image-path": str(image),
            "owner-uid": os.geteuid(),
            "image-encrypted": False,
            "blockcount": record["size"] // 512,
            "blocksize": 512,
            "system-entities": [
                {"content-hint": "GUID_partition_scheme", "dev-entry": record["device"]},
                {"content-hint": "Apple_APFS_Container", "dev-entry": record["container"]},
                {"dev-entry": record["volume"], "mount-point": record["mount"]},
            ],
        }
        self.volume = {
            "VolumeUUID": record["volume_uuid"],
            "MountPoint": record["mount"],
            "APFSContainerReference": record["container"],
            "FilesystemType": "apfs",
            "WritableVolume": True,
        }
        self.container = {
            "ContainerReference": record["container"],
            "APFSContainerUUID": record["container_uuid"],
            "CapacityCeiling": record["size"],
            "Volumes": [
                {
                    "DeviceIdentifier": record["volume"],
                    "APFSVolumeUUID": record["volume_uuid"],
                }
            ],
        }

    def _plist(self, argv):
        self.calls.append(argv)
        if argv == ["/usr/bin/hdiutil", "info", "-plist"]:
            return {"images": [copy.deepcopy(self.entry)] if self.attached else []}
        if argv[1:3] == ["info", "-plist"]:
            return copy.deepcopy(self.volume)
        if argv[1:4] == ["apfs", "list", "-plist"]:
            return {"Containers": [copy.deepcopy(self.container)]}
        raise AssertionError("unexpected native inspection")

    def _run(self, argv):
        assert argv[1] == "eject" and len(argv) == 3
        self.calls.append(argv)
        self.attached = False
        return b""


@pytest.fixture
def parts(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "original.py").write_text("original input\n")
    source = SourceAccess(project)
    root = tmp_path / "jobs"
    state = private_directory(root)
    job_id = "job-" + uuid.uuid4().hex
    image = root / (job_id + ".sparsebundle")
    (image / "bands").mkdir(parents=True, mode=0o700)
    image.chmod(0o700)
    (image / "bands" / "0").write_bytes(b"fixture derived band")
    mount = root / (job_id + "-mount")
    mount.mkdir(mode=0o700)
    record = {
        "version": 1,
        "job_id": job_id,
        "project_id": "project",
        "source_id": source.source_id,
        "image_basename": image.name,
        "root_identity": disk_identity(root.stat()),
        "image_identity": disk_identity(image.stat()),
        "mount": str(mount),
        "mount_underlying": disk_identity(mount.stat()),
        "mounted_identity": disk_identity(mount.stat()),
        "device": "/dev/disk999",
        "container": "/dev/disk998",
        "volume": "/dev/disk998s1",
        "container_uuid": str(uuid.uuid4()).upper(),
        "volume_uuid": str(uuid.uuid4()).upper(),
        "size": 64 * 1024**2,
        "cleanup_verified": True,
        "updated_at": 100_000.0,
        "state": "detached",
    }
    jobs = {
        job_id: {
            "job_id": job_id,
            "project_id": "project",
            "source_id": source.source_id,
            "state": "exited",
            "snapshot": {"cleanup_verified": True},
        }
    }
    sources, now = {"project": source}, [100_000.0]
    native = FakeNative(image, record)
    recovery = TaskDiskRecovery(
        root, sources.__getitem__, jobs.get, native=native, clock=lambda: now[0]
    )
    write_state(state, job_id + ".disk.json", record)
    return SimpleNamespace(
        root=root,
        state=state,
        source=source,
        sources=sources,
        image=image,
        mount=mount,
        record=record,
        jobs=jobs,
        now=now,
        native=native,
        recovery=recovery,
        tmp=tmp_path,
    )


def save(parts, **changes):
    parts.record.update(changes)
    write_state(parts.state, parts.record["job_id"] + ".disk.json", parts.record)


def saved(parts):
    return read_state(parts.state, parts.record["job_id"] + ".disk.json")


def blocked(parts, reason):
    report = parts.recovery.recover()
    assert report["state"] == "blocked"
    assert any(reason in row["reason"] for row in report["blocked"])
    assert (parts.source.root / "original.py").read_text() == "original input\n"
    return report


def test_detached_owned_disk_is_reclaimed_once_and_receipt_is_retained(parts):
    report = parts.recovery.recover()
    assert report["state"] == "ready"
    assert report["reclaimed"] == [parts.record["job_id"]]
    assert not parts.image.exists() and not parts.mount.exists()
    assert saved(parts)["state"] == "retired"
    assert parts.recovery.recover()["reclaimed"] == []
    assert (parts.source.root / "original.py").read_text() == "original input\n"


def test_attached_disk_eject_requires_full_native_ownership_proof(parts):
    save(parts, state="attached")
    parts.native.attached = True
    report = parts.recovery.recover()
    assert report["state"] == "ready"
    assert [c for c in parts.native.calls if c[1] == "eject"] == [
        ["/usr/sbin/diskutil", "eject", parts.record["device"]]
    ]
    assert not parts.image.exists()


@pytest.mark.parametrize("state", ["queued", "starting", "running", "stopping"])
def test_live_job_reference_preserves_disk_and_cannot_eject(parts, state):
    parts.native.attached = True
    parts.jobs[parts.record["job_id"]]["state"] = state
    blocked(parts, "STILL_REFERENCED")
    assert parts.image.exists() and parts.mount.exists()
    assert parts.native.calls == []


def test_interrupted_started_job_has_no_cleanup_proof_and_is_retained(parts):
    save(parts, cleanup_verified=False)
    parts.jobs[parts.record["job_id"]].update(
        state="interrupted", snapshot={"cleanup_verified": False}
    )
    blocked(parts, "CLEANUP_UNVERIFIED")
    assert parts.image.exists() and parts.native.calls == []


def interrupted(parts):
    save(parts, cleanup_verified=False)
    parts.jobs[parts.record["job_id"]].update(
        state="interrupted", snapshot={"cleanup_verified": False}
    )


def scope_proof(parts):
    return {
        "job_id": parts.record["job_id"],
        "resource_id": 1234,
        "cleanup_verified": True,
        "launchd_retired": True,
    }


def test_interrupted_job_retires_only_after_bound_independent_scope_proof(parts):
    interrupted(parts)
    calls = []

    def proof(record, job):
        calls.append((copy.deepcopy(record), copy.deepcopy(job)))
        assert record["cleanup_verified"] is False and job["state"] == "interrupted"
        return scope_proof(parts)

    parts.recovery.cleanup_proof = proof
    parts.native.attached = True
    save(parts, state="attached")
    report = parts.recovery.recover()
    assert report["state"] == "ready" and len(calls) == 1
    assert report["cleanup_proofs"] == {parts.record["job_id"]: scope_proof(parts)}
    assert saved(parts)["cleanup_origin"] == "verified_retired_resource_scope"
    assert saved(parts)["recovery_resource_id"] == 1234
    assert not parts.image.exists() and not parts.mount.exists()
    # A crash between disk retirement and ledger update can replay the receipt.
    assert parts.recovery.recover()["cleanup_proofs"] == report["cleanup_proofs"]


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": "job-" + "f" * 32},
        {"resource_id": True},
        {"resource_id": 1},
        {"resource_id": 2**64},
        {"cleanup_verified": 1},
        {"launchd_retired": False},
        {"extra": "do not echo fixture values"},
    ],
)
def test_scope_proof_with_changed_binding_or_inexact_types_keeps_disk(parts, change):
    interrupted(parts)
    parts.recovery.cleanup_proof = lambda *args: {**scope_proof(parts), **change}
    report = blocked(parts, "CLEANUP_UNVERIFIED")
    assert "fixture values" not in json.dumps(report)
    assert parts.image.exists() and parts.native.calls == []
    assert saved(parts)["cleanup_verified"] is False


def test_scope_proof_failure_is_preserved_without_native_disk_operations(parts):
    interrupted(parts)

    def proof(*args):
        raise ValueError("unknown or still active")

    parts.recovery.cleanup_proof = proof
    blocked(parts, "CLEANUP_UNVERIFIED")
    assert parts.image.exists() and parts.native.calls == []


def test_job_binding_change_during_scope_proof_is_not_committed(parts):
    interrupted(parts)

    def proof(*args):
        parts.jobs[parts.record["job_id"]] = {
            **parts.jobs[parts.record["job_id"]],
            "state": "running",
        }
        return scope_proof(parts)

    parts.recovery.cleanup_proof = proof
    blocked(parts, "JOB_CHANGED")
    assert saved(parts)["cleanup_verified"] is False and parts.image.exists()


def test_disk_record_change_during_scope_proof_is_not_overwritten(parts):
    interrupted(parts)

    def proof(*args):
        save(parts, updated_at=parts.now[0] - 1)
        return scope_proof(parts)

    parts.recovery.cleanup_proof = proof
    blocked(parts, "RECORD_CHANGED")
    assert saved(parts)["updated_at"] == parts.now[0] - 1
    assert saved(parts)["cleanup_verified"] is False and parts.image.exists()


def test_non_interrupted_job_never_uses_restart_scope_proof(parts):
    save(parts, cleanup_verified=False)
    parts.recovery.cleanup_proof = lambda *args: pytest.fail("restart proof used for ended job")
    blocked(parts, "CLEANUP_UNVERIFIED")
    assert parts.image.exists()


def test_durable_never_started_proof_survives_interrupted_ledger(parts):
    parts.jobs[parts.record["job_id"]].update(
        state="interrupted", snapshot={"cleanup_verified": False}
    )
    assert parts.recovery.recover()["state"] == "ready"
    assert not parts.image.exists()


def test_inconsistent_completed_job_cleanup_is_not_inferred_from_exit(parts):
    parts.jobs[parts.record["job_id"]]["snapshot"] = {"cleanup_verified": False, "exit_code": 0}
    blocked(parts, "CLEANUP_UNVERIFIED")
    assert parts.image.exists()


@pytest.mark.parametrize("change", ["source", "job_source", "missing_job"])
def test_source_or_job_binding_change_prevents_retirement(parts, change):
    if change == "source":
        replacement = parts.tmp / "replacement"
        replacement.mkdir()
        parts.sources["project"] = SourceAccess(replacement)
    elif change == "job_source":
        parts.jobs[parts.record["job_id"]]["source_id"] = "f" * 64
    else:
        parts.jobs.clear()
    blocked(parts, "SOURCE_CHANGED" if change == "source" else "JOB_UNVERIFIED")
    assert parts.image.exists()


def test_unknown_image_is_preserved_and_blocks_new_capacity(parts):
    unknown = parts.root / "legacy.sparsebundle"
    unknown.mkdir()
    (unknown / "unknown-data").write_text("keep\n")
    report = parts.recovery.recover()
    assert report["state"] == "blocked" and report["unknown"] == 1
    assert (unknown / "unknown-data").read_text() == "keep\n"
    assert parts.record["job_id"] in report["reclaimed"]


@pytest.mark.parametrize("kind", ["replaced_directory", "symlink"])
def test_changed_image_identity_or_alias_is_preserved(parts, kind):
    retained = parts.tmp / "retained-image"
    parts.image.rename(retained)
    if kind == "symlink":
        parts.image.symlink_to(retained, target_is_directory=True)
    else:
        parts.image.mkdir()
        (parts.image / "replacement").write_text("keep\n")
    blocked(parts, "IMAGE_CHANGED")
    assert retained.exists()
    assert parts.image.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_image_tree_cannot_traverse_or_remove_original_paths(parts, kind):
    target = parts.image / "bands" / "untrusted"
    original = parts.source.root / "original.py"
    if kind == "symlink":
        target.symlink_to(original)
    else:
        os.link(original, target)
    blocked(parts, "RETIREMENT_SCOPE")
    assert parts.image.exists() and (parts.image / "bands" / "0").exists()


def test_nonempty_underlying_mount_preserves_uncertain_data_and_image(parts):
    (parts.mount / "foreign-data").write_text("keep\n")
    blocked(parts, "MOUNT_NOT_EMPTY")
    assert parts.image.exists()
    assert (parts.mount / "foreign-data").read_text() == "keep\n"


def test_mount_scope_cannot_be_changed_to_user_source(parts):
    save(
        parts,
        mount=str(parts.source.root),
        mount_underlying=disk_identity(parts.source.root.stat()),
    )
    blocked(parts, "RECORD_UNVERIFIED")
    assert parts.image.exists()


@pytest.mark.parametrize(
    "field", ["owner", "volume_uuid", "container_uuid", "capacity", "device", "mounted_inode"]
)
def test_native_attachment_changes_prevent_any_eject(parts, field):
    save(parts, state="attached")
    parts.native.attached = True
    if field == "owner":
        parts.native.entry["owner-uid"] = os.geteuid() + 1
    elif field == "volume_uuid":
        parts.native.volume["VolumeUUID"] = str(uuid.uuid4())
    elif field == "container_uuid":
        parts.native.container["APFSContainerUUID"] = str(uuid.uuid4())
    elif field == "capacity":
        parts.native.container["CapacityCeiling"] = parts.record["size"] + 1
    elif field == "device":
        parts.native.entry["system-entities"][0]["dev-entry"] = "/dev/disk997"
    else:
        save(
            parts,
            mounted_identity={
                **parts.record["mounted_identity"],
                "ino": parts.record["mounted_identity"]["ino"] + 1,
            },
        )
    blocked(parts, "CHANGED")
    assert parts.image.exists() and parts.native.attached
    assert not any(c[1] == "eject" for c in parts.native.calls)


def test_incomplete_attach_intent_with_existing_native_mount_fails_closed(parts):
    save(parts, state="creating", volume_uuid=None)
    parts.native.attached = True
    blocked(parts, "ATTACHMENT_UNVERIFIED")
    assert parts.image.exists() and parts.native.attached


def test_native_attachment_through_an_alias_is_preserved_and_never_ejected(parts):
    alias = parts.tmp / "alias.sparsebundle"
    alias.symlink_to(parts.image, target_is_directory=True)
    parts.native.attached = True
    parts.native.entry["image-path"] = str(alias)
    blocked(parts, "ATTACHMENT_ALIAS")
    assert parts.image.exists() and parts.native.attached
    assert not any(c[1] == "eject" for c in parts.native.calls)


def test_image_creation_without_confirmed_inode_is_not_adopted(parts):
    save(parts, state="creating", image_identity=None)
    blocked(parts, "IMAGE_CHANGED")
    assert parts.image.exists()


def test_retiring_journal_finishes_after_crash_after_image_removal(parts, monkeypatch):
    def crash(_):
        raise RuntimeError("fixture crash")

    monkeypatch.setattr(parts.recovery, "_after_image_removed", crash)
    with pytest.raises(RuntimeError, match="fixture crash"):
        parts.recovery.recover()
    assert not parts.image.exists() and saved(parts)["state"] == "retiring"
    restarted = TaskDiskRecovery(
        parts.root,
        parts.sources.__getitem__,
        parts.jobs.get,
        native=parts.native,
        clock=lambda: parts.now[0],
    )
    assert restarted.recover()["state"] == "ready"
    assert saved(parts)["state"] == "retired" and not parts.mount.exists()


def test_retired_image_replacement_is_never_removed_twice(parts):
    parts.recovery.recover()
    parts.image.mkdir()
    (parts.image / "new-content").write_text("keep\n")
    blocked(parts, "IMAGE_CHANGED")
    assert (parts.image / "new-content").read_text() == "keep\n"


def test_expired_retired_metadata_is_removed_only_without_retained_job(parts):
    parts.recovery.recover()
    parts.now[0] += 86400
    assert parts.recovery.recover()["expired_records"] == []
    parts.jobs.clear()
    report = parts.recovery.recover()
    assert report["expired_records"] == [parts.record["job_id"]]
    assert not (parts.root / (parts.record["job_id"] + ".disk.json")).exists()


def test_runtime_metadata_expiry_does_not_run_recovery_of_attached_active_jobs(parts):
    parts.native.attached = True
    save(parts, state="attached", cleanup_verified=False)
    parts.jobs[parts.record["job_id"]]["state"] = "running"
    parts.now[0] += 86401
    report = parts.recovery.expire_retired_records(job_ids=[parts.record["job_id"]])
    assert report == {"expired_records": [], "blocked": []}
    assert parts.image.exists() and parts.native.calls == []


def test_runtime_metadata_expiry_keeps_live_receipt_and_then_expires_exact_id(parts):
    parts.recovery.recover()
    parts.now[0] += 86401
    assert parts.recovery.expire_retired_records()["expired_records"] == []
    parts.jobs.clear()
    unknown = parts.root / "unknown.json"
    unknown.write_text("keep unrelated evidence")
    report = parts.recovery.expire_retired_records(job_ids=[parts.record["job_id"]])
    assert report == {"expired_records": [parts.record["job_id"]], "blocked": []}
    assert unknown.read_text() == "keep unrelated evidence"


def test_retired_receipt_with_reappearing_mount_is_preserved(parts):
    parts.recovery.recover()
    parts.now[0] += 86401
    parts.jobs.clear()
    parts.mount.mkdir()
    report = parts.recovery.expire_retired_records(job_ids=[parts.record["job_id"]])
    assert report["blocked"] == [parts.record["job_id"]]
    assert saved(parts)["state"] == "retired" and parts.mount.exists()


def test_retired_receipt_for_changed_project_identity_is_preserved(parts):
    parts.recovery.recover()
    parts.now[0] += 86401
    parts.jobs.clear()
    parts.sources.clear()
    report = parts.recovery.expire_retired_records(job_ids=[parts.record["job_id"]])
    assert report["blocked"] == [parts.record["job_id"]]
    assert saved(parts)["state"] == "retired"


def test_started_scope_must_expire_before_its_disk_receipt_disappears(parts):
    parts.recovery.recover()
    parts.now[0] += 86401
    parts.jobs.clear()
    calls = []

    def expire(record):
        assert record["state"] == "retired" and not parts.image.exists()
        assert parts.jobs.get(record["job_id"]) is None
        calls.append(record["job_id"])
        return {**scope_proof(parts), "expired": True}

    parts.recovery.scope_expire = expire
    report = parts.recovery.expire_retired_records(job_ids=[parts.record["job_id"]])
    assert report["expired_records"] == calls == [parts.record["job_id"]]
    assert not (parts.root / (parts.record["job_id"] + ".disk.json")).exists()


def test_failed_scope_expiry_retains_detached_metadata_without_disabling_recovery(parts):
    parts.recovery.recover()
    parts.now[0] += 86401
    parts.jobs.clear()

    def expire(*args):
        raise ValueError("old boot or unknown scope; never print this")

    parts.recovery.scope_expire = expire
    report = parts.recovery.expire_retired_records(job_ids=[parts.record["job_id"]])
    assert report["blocked"] == [parts.record["job_id"]]
    assert saved(parts)["state"] == "retired"
    recovery = parts.recovery.recover()
    assert recovery["state"] == "ready" and not recovery["blocked"]
    assert recovery["retained_expired_records"] == [parts.record["job_id"]]


def test_never_started_disk_receipt_does_not_guess_or_touch_a_scope(parts):
    save(parts, execution_started=False)
    parts.recovery.recover()
    parts.now[0] += 86401
    parts.jobs.clear()
    parts.recovery.scope_expire = lambda *args: pytest.fail("scope API for never-started task")
    assert parts.recovery.expire_retired_records()["expired_records"] == [parts.record["job_id"]]


def test_runtime_expiry_finds_expired_record_after_unexpired_active_record(parts):
    parts.recovery.recover()
    parts.now[0] += 86401
    parts.jobs.clear()
    active_id = "job-" + "0" * 32
    active = {
        **parts.record,
        "job_id": active_id,
        "image_basename": active_id + ".sparsebundle",
        "state": "creating",
        "updated_at": parts.now[0],
        "image_identity": None,
        "mount": None,
    }
    write_state(parts.state, active_id + ".disk.json", active)
    report = parts.recovery.expire_retired_records(limit=1)
    assert report["expired_records"] == [parts.record["job_id"]]
    assert (parts.root / (active_id + ".disk.json")).exists()


def test_invalid_future_record_or_root_identity_never_retires_image(parts):
    save(parts, updated_at=parts.now[0] + 61)
    blocked(parts, "RECORD_UNVERIFIED")
    assert parts.image.exists()
    save(
        parts,
        updated_at=parts.now[0],
        root_identity={
            **parts.record["root_identity"],
            "ino": parts.record["root_identity"]["ino"] + 1,
        },
    )
    blocked(parts, "RECORD_UNVERIFIED")
    assert parts.image.exists()


@pytest.mark.parametrize("change", [{"unexpected": "fixture-only-secret"}, {"version": True}])
def test_unknown_record_fields_or_boolean_schema_version_are_preserved(parts, change):
    save(parts, **change)
    report = blocked(parts, "RECORD_UNVERIFIED")
    assert "fixture-only-secret" not in json.dumps(report)
    assert parts.image.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="real native APFS recovery")
def test_native_independent_small_task_disk_is_ejected_and_reclaimed_with_uuid_evidence(tmp_path):
    """64MiB disk proves native mapping/recovery, not a filled production 2GiB quota."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "original.py").write_text("original native fixture\n")
    source = SourceAccess(project)
    job_id = "job-" + uuid.uuid4().hex
    disk = BoundedDisk(
        tmp_path / "jobs",
        job_id,
        size=64 * 1024**2,
        project_id="project",
        source_id=source.source_id,
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    native = NativeDiskRecovery()
    record = read_state(disk.state, job_id + ".disk.json")
    (evidence / "disk-record-before.json").write_text(json.dumps(record, indent=2))
    entries = native.attachments(disk.image)
    (evidence / "native-attachment.plist").write_bytes(plistlib.dumps({"images": entries}))
    assert len(entries) == 1
    native.verify_attachment(record, entries[0])
    job = {
        "project_id": "project",
        "source_id": source.source_id,
        "state": "interrupted",
        "snapshot": {"cleanup_verified": False},
    }
    try:
        recovery = TaskDiskRecovery(disk.state.root, lambda _: source, lambda _: job)
        report = recovery.recover()
        (evidence / "recovery-report.json").write_text(json.dumps(report, indent=2))
        assert report["state"] == "ready" and report["reclaimed"] == [job_id]
        assert not disk.image.exists() and not disk.mount.exists()
        assert read_state(disk.state, job_id + ".disk.json")["state"] == "retired"
        disk.device = None
        assert (project / "original.py").read_text() == "original native fixture\n"
    finally:
        if disk.device and native.attachments(disk.image):
            # Release only this independent native fixture's live attachment.
            # Preserve its image and evidence when recovery did not pass.
            disk.close()
