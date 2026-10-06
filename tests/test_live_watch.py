import threading
import time

from code_context.live_watch import WatchCoordinator
from code_context.source_access import SourceAccess


def projects(tmp_path):
    result = {}
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        (root / "module.py").write_text("def run(): pass\n")
        result[name] = SourceAccess(root)
    return result


def test_coalesces_only_owning_project_without_reading_bodies(tmp_path):
    sources = projects(tmp_path)
    changes = []
    watcher = WatchCoordinator(sources, changes.append)
    changes.clear()
    assert watcher.notify([(2, str(sources["b"].root / "module.py"))] * 100) == {"b"}
    assert changes == ["b"]
    assert all(source.metrics["body_reads"] == 0 for source in sources.values())
    assert watcher.status()["freshness_requires_validation"]


def test_nonrecursive_paths_prune_dependencies_and_report_quota(tmp_path):
    sources = projects(tmp_path)
    for source in sources.values():
        (source.root / "node_modules").mkdir()
        (source.root / "src").mkdir()
        (source.root / "src" / "nested").mkdir()
    watcher = WatchCoordinator(sources, lambda _: None, max_directories=4)
    watcher.reconcile()
    assert watcher.status()["directories"] <= 4
    assert watcher.status()["partial_projects"] == ["a", "b"]
    assert all(
        "node_modules" not in str(path)
        for item in watcher.metadata.values()
        for path in item["directories"]
    )
    assert watcher.notify([(1, str(sources["a"].root / "node_modules" / "x.js"))]) == set()


def test_missed_event_and_new_directory_compensated_with_metadata(tmp_path):
    sources = projects(tmp_path)
    changes = []
    watcher = WatchCoordinator(sources, changes.append)
    watcher.reconcile()
    changes.clear()
    (sources["a"].root / "new").mkdir()
    (sources["a"].root / "new" / "feature.py").write_text("updated = True\n")
    assert watcher.reconcile() == {"a"}
    assert changes == ["a"]
    assert sources["a"].root / "new" in watcher.metadata["a"]["directories"]
    assert all(source.metrics["body_reads"] == 0 for source in sources.values())


def test_overflow_coalesces_without_accumulating_paths(tmp_path):
    sources = projects(tmp_path)
    watcher = WatchCoordinator(sources, lambda _: None, max_events=2)
    watcher.notify([(2, str(sources["a"].root / "module.py"))] * 100)
    assert watcher.status()["event_overflows"] == 1
    assert watcher.status()["dirty_projects"] == ["a", "b"]
    assert len(watcher.refresh) == 2


def test_budget_smaller_than_project_count_is_explicitly_degraded(tmp_path):
    sources = projects(tmp_path)
    watcher = WatchCoordinator(sources, lambda _: None, max_directories=1)
    watcher.reconcile()
    assert watcher.status()["directories"] <= 1
    assert "b" in watcher.status()["partial_projects"]
    assert sources["b"].read("module.py").content == "def run(): pass\n"


def test_one_thread_uses_explicit_nonrecursive_paths_and_stops(tmp_path):
    sources = projects(tmp_path)
    observed = []
    ready = threading.Event()

    def fake_watch(*paths, **kwargs):
        observed.append((paths, kwargs["recursive"]))
        ready.set()
        while not kwargs["stop_event"].wait(0.01):
            yield set()

    watcher = WatchCoordinator(sources, lambda _: None, watch_function=fake_watch)
    watcher.start()
    assert ready.wait(2)
    watcher.close()
    assert not watcher.thread.is_alive()
    assert observed[0][1] is False
    assert watcher.status()["state"] == "stopped"


def test_real_watch_records_saved_sample_changes(tmp_path):
    sources = projects(tmp_path)
    changed = threading.Event()
    watcher = WatchCoordinator(sources, lambda _: changed.set(), reconcile_seconds=0.1)
    changed.clear()
    watcher.start()
    deadline = time.monotonic() + 2
    while watcher.status()["state"] == "stopped" and time.monotonic() < deadline:
        time.sleep(0.01)
    (sources["a"].root / "module.py").write_text("def changed(): pass\n")
    assert changed.wait(3)
    watcher.close()
    assert all(source.metrics["body_reads"] == 0 for source in sources.values())
