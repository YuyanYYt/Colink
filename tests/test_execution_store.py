"""Private synthetic execution receipts; never open a connected project's state."""

import hashlib
import os
import sqlite3

import pytest

from code_context.execution_store import ExecutionStore, ExecutionStoreError

DIGEST = hashlib.sha256(b"synthetic request").hexdigest()


def _plan(store, number=1, **extra):
    return store.create_plan(
        f"plan-{number}",
        "project",
        "source",
        "epoch",
        f"plan-request-{number}",
        DIGEST,
        **extra,
    )


def _job(store, number=1, **extra):
    return store.reserve_job(
        f"job-{number}",
        f"plan-{number}",
        "project",
        "source",
        "epoch",
        f"start-request-{number}",
        DIGEST,
        **extra,
    )


def test_replay_scope_binding_final_snapshot_and_private_database(tmp_path):
    root = tmp_path / "state"
    with ExecutionStore(root) as store:
        plan = _plan(store, metadata={"title": "测试"})
        assert _plan(store)["plan_id"] == plan["plan_id"]
        job, replayed = _job(store)
        assert job["state"] == "queued" and not replayed
        assert _job(store)[1]
        assert store.mark_started("job-1", {"pid": 42})["state"] == "running"
        snapshot = {"state": "exited", "exit_code": 7, "observed_tree_stopped": True}
        done = store.complete_job("job-1", snapshot, metadata={"verified": False})
        assert done["snapshot"] == snapshot and done["metadata"] == {"verified": False}
        assert _job(store) == (done, True)
        assert store.complete_job("job-1", {"state": "failed"}) == done
        assert store.list_jobs("project", "source", "epoch") == [done]
        with pytest.raises(ExecutionStoreError, match="EXECUTION_JOB_NOT_STARTABLE"):
            store.mark_started("job-1")
        with pytest.raises(ExecutionStoreError, match="EXECUTION_PLAN_UNAVAILABLE"):
            store.reserve_job(
                "another", "plan-1", "project", "source", "epoch", "another-request", DIGEST
            )
        with pytest.raises(ExecutionStoreError, match="EXECUTION_REQUEST_CONFLICT"):
            store.get_receipt("project", "another-source", "epoch", "start-request-1", DIGEST)
        with pytest.raises(ExecutionStoreError, match="EXECUTION_REQUEST_CONFLICT"):
            store.get_receipt("project", "source", "another-epoch", "start-request-1", DIGEST)
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.db.execute("PRAGMA max_page_count").fetchone()[0] == (
            store.database_budget_bytes // 4096
        )
        assert store.db.execute("PRAGMA cache_spill").fetchone()[0] == 0
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root / "execution.sqlite3").stat().st_mode & 0o777 == 0o600
    with ExecutionStore(root) as store:
        assert store.get_job("job-1")["snapshot"] == snapshot
        assert _job(store)[1]


def test_reopen_marks_queued_and_running_interrupted_without_restart(tmp_path):
    root = tmp_path / "state"
    with ExecutionStore(root) as store:
        _plan(store)
        _job(store)
        _plan(store, 2)
        _job(store, 2)
        store.mark_started("job-2", {"pid": 123})
    with ExecutionStore(root) as store:
        for number in (1, 2):
            job, replayed = _job(store, number)
            assert replayed and job["state"] == "interrupted"
            assert job["snapshot"]["cleanup_verified"] is False
            assert job["snapshot"]["reason"] == "runtime_restarted"
        assert store.interrupt_unfinished() == 0


def test_expired_receipt_tombstone_and_expired_plan_never_start(tmp_path):
    now = [1000.0]
    with ExecutionStore(tmp_path / "state", clock=lambda: now[0], receipt_seconds=10) as store:
        _plan(store, expires_at=1002)
        now[0] = 1003
        assert store.get_plan("plan-1")["expired"]
        with pytest.raises(ExecutionStoreError, match="EXECUTION_PLAN_UNAVAILABLE"):
            _job(store)
        now[0] = 1011
        assert store.get_receipt("project", "source", "epoch", "plan-request-1", DIGEST)["expired"]
        with pytest.raises(ExecutionStoreError, match="EXECUTION_REQUEST_EXPIRED"):
            _plan(store)
        assert store.usage()["receipts"] == 1


def test_capacity_refuses_new_plans_and_reserves_final_metadata(tmp_path):
    with ExecutionStore(tmp_path / "capacity", max_metadata_bytes=512 * 1024) as store:
        _plan(store)
        _job(store)
        with pytest.raises(ExecutionStoreError, match="EXECUTION_METADATA_CAPACITY"):
            _plan(store, 2, metadata={"body": "x" * 32000})
        done = store.complete_job(
            "job-1",
            {"state": "exited", "details": "x" * 62000},
            metadata={"details": "y" * 62000},
        )
        assert done["state"] == "exited"
        assert store.usage()["database_bytes"] <= store.database_budget_bytes
        assert store.get_plan("plan-1") is not None
        assert store.usage()["receipts"] == 2


def test_single_owner_unknown_schema_and_replaced_files_are_preserved(tmp_path):
    root = tmp_path / "state"
    with ExecutionStore(root) as store:
        with pytest.raises(ExecutionStoreError, match="EXECUTION_STORE_ALREADY_OPEN"):
            ExecutionStore(root)
        database = root / "execution.sqlite3"
        database.rename(root / "saved-original.sqlite3")
        with sqlite3.connect(database) as foreign:
            foreign.execute("CREATE TABLE unknown (value TEXT)")
        database.chmod(0o600)
        with pytest.raises(ExecutionStoreError, match="EXECUTION_STORE_REPLACED"):
            store.get_job("unissued")
    with pytest.raises(ExecutionStoreError, match="EXECUTION_SCHEMA_INVALID"):
        ExecutionStore(root)
    assert (root / "saved-original.sqlite3").exists()


def test_hardlinked_and_public_database_are_rejected(tmp_path):
    root = tmp_path / "state"
    with ExecutionStore(root):
        pass
    database = root / "execution.sqlite3"
    os.link(database, tmp_path / "linked")
    with pytest.raises(ExecutionStoreError, match="UNSAFE_EXECUTION_STATE"):
        ExecutionStore(root)
    other = tmp_path / "other"
    with ExecutionStore(other):
        pass
    (other / "execution.sqlite3").chmod(0o644)
    with pytest.raises(ExecutionStoreError, match="UNSAFE_EXECUTION_STATE"):
        ExecutionStore(other)


def test_invalid_metadata_is_content_free_and_does_not_issue_receipts(tmp_path):
    with ExecutionStore(tmp_path / "state") as store:
        for metadata in (
            {"secret": "sentinel" * 10000},
            {"invalid": float("nan")},
            [],
            False,
        ):
            with pytest.raises(ExecutionStoreError, match="EXECUTION_METADATA_LIMIT") as error:
                _plan(store, metadata=metadata)
            assert "sentinel" not in str(error.value)
        assert store.usage()["receipts"] == 0


def test_additional_job_receipt_is_scoped_atomic_and_never_restarts_expired_work(tmp_path):
    now = [1000.0]
    with ExecutionStore(tmp_path / "state", receipt_seconds=10, clock=lambda: now[0]) as store:
        _plan(store)
        _job(store)
        _plan(store, 2)
        _job(store, 2)
        done = store.complete_job("job-1", {"state": "exited", "exit_code": 0})
        receipt = store.record_job_receipt("job-1", "project", "source", "epoch", "reuse", DIGEST)
        assert receipt["kind"] == "job" and receipt["target_id"] == "job-1"
        assert (
            store.record_job_receipt("job-1", "project", "source", "epoch", "reuse", DIGEST)
            == receipt
        )
        assert store.reserve_job(
            "unissued", "plan-2", "project", "source", "epoch", "reuse", DIGEST
        ) == (done, True)
        count = store.usage()["receipts"]
        with pytest.raises(ExecutionStoreError, match="EXECUTION_REQUEST_CONFLICT"):
            store.record_job_receipt("job-2", "project", "source", "epoch", "reuse", DIGEST)
        with pytest.raises(ExecutionStoreError, match="EXECUTION_REQUEST_CONFLICT"):
            store.record_job_receipt("job-1", "project", "foreign", "epoch", "other", DIGEST)
        assert store.usage()["receipts"] == count
        now[0] += 11
        with pytest.raises(ExecutionStoreError, match="EXECUTION_REQUEST_EXPIRED"):
            store.record_job_receipt("job-1", "project", "source", "epoch", "reuse", DIGEST)
        with pytest.raises(ExecutionStoreError, match="EXECUTION_REQUEST_EXPIRED"):
            store.reserve_job("unissued", "plan-2", "project", "source", "epoch", "reuse", DIGEST)
        assert store.get_job("job-1") == done


def test_completed_followup_snapshot_preserves_outcome_completion_and_receipt(tmp_path):
    root = tmp_path / "state"
    now = [1000.0]
    with ExecutionStore(root, clock=lambda: now[0]) as store:
        _plan(store)
        _job(store)
        with pytest.raises(ExecutionStoreError, match="EXECUTION_JOB_NOT_COMPLETED"):
            store.update_completed_snapshot("job-1", {"writeback_intent": "pending"})
        done = store.complete_job("job-1", {"state": "resource_limit", "reason": "rss_limit"})
        receipt = store.get_receipt("project", "source", "epoch", "start-request-1", DIGEST)
        now[0] += 10
        revised = {**done["snapshot"], "writeback_intent": "complete", "writeback": {"files": 2}}
        changed = store.update_completed_snapshot("job-1", revised)
        assert changed["state"] == done["state"]
        assert changed["completed"] == done["completed"]
        assert changed["updated"] == now[0] and changed["snapshot"] == revised
        assert store.complete_job("job-1", {"state": "failed"}) == changed
        assert store.get_receipt("project", "source", "epoch", "start-request-1", DIGEST) == receipt
        with pytest.raises(ExecutionStoreError, match="INVALID_EXECUTION_TERMINAL_STATE"):
            store.update_completed_snapshot("job-1", {"state": "exited"})
        with pytest.raises(ExecutionStoreError, match="EXECUTION_METADATA_LIMIT"):
            store.update_completed_snapshot("job-1", {"details": "x" * 70000})
        assert store.get_job("job-1")["snapshot"] == revised
    with ExecutionStore(root) as store:
        assert store.get_job("job-1")["snapshot"] == revised


def test_tiny_budget_rejects_launch_instead_of_losing_final_journal_reserve(tmp_path):
    with ExecutionStore(tmp_path / "tiny", max_metadata_bytes=256 * 1024) as store:
        _plan(store)
        with pytest.raises(ExecutionStoreError, match="EXECUTION_METADATA_CAPACITY"):
            _job(store)
        assert store.usage()["jobs"] == 0
        assert store.usage()["receipts"] == 1
        assert store.database_budget_bytes < 2 * 64 * 1024


def test_changed_sqlite_page_size_is_preserved_and_rejected_before_budget_use(tmp_path):
    root = tmp_path / "page-size"
    with ExecutionStore(root) as store:
        _plan(store)
    with sqlite3.connect(root / "execution.sqlite3") as foreign:
        foreign.execute("PRAGMA page_size=8192")
        foreign.execute("VACUUM")
        assert foreign.execute("PRAGMA page_size").fetchone()[0] == 8192
    with pytest.raises(ExecutionStoreError, match="EXECUTION_STORE_CORRUPT"):
        ExecutionStore(root)
    with sqlite3.connect(root / "execution.sqlite3") as foreign:
        assert foreign.execute("SELECT count(*) FROM plans").fetchone()[0] == 1
        assert foreign.execute("PRAGMA page_size").fetchone()[0] == 8192


def test_sqlite_growth_completion_and_gc_fit_combined_journal_budget(tmp_path, monkeypatch):
    now = [1000.0]
    with ExecutionStore(
        tmp_path / "physical", max_metadata_bytes=1024 * 1024, clock=lambda: now[0]
    ) as store:
        peaks = []
        original = store._check_auxiliary

        def track(parent):
            original(parent)
            peaks.append(
                sum(
                    os.stat(name, dir_fd=parent, follow_symlinks=False).st_size
                    for name in os.listdir(parent)
                )
            )

        monkeypatch.setattr(store, "_check_auxiliary", track)
        for number in (1, 2):
            _plan(store, number)
            _job(store, number)
        for number in (1, 2):
            store.complete_job(
                f"job-{number}",
                {
                    "state": "exited",
                    "cleanup_verified": True,
                    "workspace_retired": True,
                    "details": "x" * 62000,
                },
                metadata={"details": "y" * 62000},
            )
        now[0] += store.receipt_seconds + 1
        result = store.collect_expired(["job-1", "job-2"])
        assert result["jobs"] == 2 and result["plans"] == 2 and result["receipts"] == 4
        assert max(peaks) <= store.max_metadata_bytes
        assert store.usage()["database_bytes"] < 100 * 1024


def test_active_first_and_retention_window_preserve_running_and_unsafe_jobs(tmp_path):
    now = [1000.0]
    with ExecutionStore(tmp_path / "listing", clock=lambda: now[0]) as store:
        _plan(store)
        _job(store, service=True)
        store.mark_started("job-1")
        _plan(store, 2)
        _job(store, 2)
        store.complete_job("job-2", {"state": "stop_failed", "cleanup_verified": False})
        _plan(store, 3)
        _job(store, 3)
        store.complete_job("job-3", {"state": "exited", "cleanup_verified": True})
        now[0] += store.receipt_seconds + 1
        _plan(store, 4)
        _job(store, 4)
        store.complete_job("job-4", {"state": "exited", "cleanup_verified": True})
        rows = store.list_jobs(
            "project", limit=3, active_first=True, completed_since=now[0] - store.receipt_seconds
        )
        assert [row["job_id"] for row in rows] == ["job-1", "job-2", "job-4"]
        assert len(store.list_jobs("project")) == 4


@pytest.mark.parametrize(
    "protected",
    ["running", "stop_failed", "cleanup_false", "workspace_active", "awaiting_start", "applying"],
)
def test_gc_preserves_active_unverified_or_pending_writeback_even_if_caller_supplies_id(
    tmp_path, protected
):
    now = [1000.0]
    with ExecutionStore(tmp_path / "protected", clock=lambda: now[0]) as store:
        _plan(store)
        _job(store)
        snapshot = {"state": "exited", "cleanup_verified": True, "workspace_retired": True}
        if protected == "running":
            store.mark_started("job-1")
        else:
            if protected == "stop_failed":
                snapshot["state"] = "stop_failed"
            elif protected == "cleanup_false":
                snapshot["cleanup_verified"] = False
            elif protected == "workspace_active":
                snapshot["workspace_retired"] = False
            else:
                snapshot["writeback"] = {"state": protected}
            store.complete_job("job-1", snapshot)
        now[0] += store.receipt_seconds + 1
        assert store.collect_expired(["job-1"])["jobs"] == 0
        assert store.get_job("job-1") is not None
        assert store.get_plan("plan-1") is not None
        assert store.usage()["receipts"] == 2


def test_gc_waits_for_latest_alias_receipt_and_old_plan_never_restarts(tmp_path):
    now = [1000.0]
    with ExecutionStore(tmp_path / "aliases", clock=lambda: now[0], receipt_seconds=10) as store:
        _plan(store, expires_at=1005)
        _job(store)
        done = store.complete_job(
            "job-1", {"state": "exited", "cleanup_verified": True, "workspace_retired": True}
        )
        now[0] += 8
        store.record_job_receipt("job-1", "project", "source", "epoch", "latest-alias", DIGEST)
        now[0] += 3
        assert store.get_receipt("project", "source", "epoch", "start-request-1", DIGEST)["expired"]
        assert store.collect_expired(["job-1"])["jobs"] == 0
        assert store.get_job("job-1") == done
        now[0] += 8
        assert store.collect_expired(["job-1"])["job_ids"] == ["job-1"]
        assert store.get_receipt("project", "source", "epoch", "start-request-1", DIGEST) is None
        with pytest.raises(ExecutionStoreError, match="EXECUTION_PLAN_UNAVAILABLE"):
            _job(store)
        fresh = store.create_plan(
            "fresh-plan", "project", "source", "epoch", "plan-request-1", DIGEST
        )
        started, duplicate = store.reserve_job(
            "fresh-job", fresh["plan_id"], "project", "source", "epoch", "start-request-1", DIGEST
        )
        assert started["job_id"] == "fresh-job" and not duplicate


def test_gc_removes_only_expired_unused_plans_and_can_repeat_without_growth(tmp_path):
    now = [1000.0]
    with ExecutionStore(tmp_path / "cycles", clock=lambda: now[0], receipt_seconds=10) as store:
        page_sizes = []
        for number in range(15):
            _plan(store, number, metadata={"body": "synthetic" * 7000}, expires_at=now[0] + 5)
            now[0] += 11
            assert store.get_plan(f"plan-{number}")["expired"]
            assert store.usage()["receipts"] == 1
            reclaimed = store.collect_expired()
            assert reclaimed["plans"] == reclaimed["receipts"] == 1
            assert store.collect_expired() == {"jobs": 0, "plans": 0, "receipts": 0, "job_ids": []}
            page_sizes.append(store.usage()["database_bytes"])
        assert max(page_sizes) <= 64 * 1024
        assert store.usage()["receipts"] == store.usage()["jobs"] == 0


def test_gc_job_not_supplied_remains_even_with_durable_retirement_proof(tmp_path):
    now = [1000.0]
    with ExecutionStore(tmp_path / "referenced", clock=lambda: now[0], receipt_seconds=10) as store:
        _plan(store, expires_at=now[0] + 5)
        _job(store)
        store.complete_job(
            "job-1", {"state": "exited", "cleanup_verified": True, "workspace_retired": True}
        )
        now[0] += 11
        assert store.expired_job_candidates() == ["job-1"]
        assert store.collect_expired()["jobs"] == 0
        assert store.get_job("job-1") is not None
        assert store.collect_expired(["job-1", "unknown"])["jobs"] == 1
