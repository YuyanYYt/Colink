import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Barrier, Event

import pytest

from code_context.read_context import ContextError, ReadContext, ReadContexts

FIRST_HASH = "a" * 64
SECOND_HASH = "b" * 64


class Clock:
    def __init__(self, now: float = 0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_opaque_handles_are_unique_and_contexts_contain_only_metadata():
    clock = Clock(10)
    contexts = ReadContexts(clock=clock)
    handles = [contexts.create("project", "source") for _ in range(10)]

    assert len(set(handles)) == 10
    assert all(handle.startswith("live_") for handle in handles)
    assert all("project" not in handle and "source" not in handle for handle in handles)
    context = contexts.get("project", "source", handles[0])
    assert context == ReadContext("project", "source", {}, 10)
    assert not hasattr(context, "content")
    assert not hasattr(context, "__dict__")


def test_get_returns_detached_copies_of_metadata():
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
    saved = contexts.get("project", "source", handle)
    modified = contexts.get("project", "source", handle)
    modified.files["main.py"] = SECOND_HASH
    modified.files["extra.py"] = SECOND_HASH
    contexts.observe("project", "source", handle, "other.py", SECOND_HASH)

    assert saved.files == {"main.py": FIRST_HASH}
    assert contexts.get("project", "source", handle).files == {
        "main.py": FIRST_HASH,
        "other.py": SECOND_HASH,
    }
    with pytest.raises(FrozenInstanceError):
        modified.source_id = "different"


@pytest.mark.parametrize("handle", [None, "current", "previous", "latest", "42", 42, "ctx_old", ""])
def test_selectors_and_historical_modes_never_resolve_to_a_live_context(handle):
    contexts = ReadContexts(max_contexts=1)
    active = contexts.create("project", "source")

    with pytest.raises(ContextError):
        contexts.get("project", "source", handle)
    with pytest.raises(ContextError):
        contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
    with pytest.raises(ContextError):
        contexts.validate(
            "project", "source", handle, lambda path: pytest.fail("unexpected reader")
        )
    assert contexts.get("project", "source", active).files == {}
    with pytest.raises(ContextError, match="capacity"):
        contexts.create("other", "source")


@pytest.mark.parametrize("project,source", [("other", "source"), ("project", "other")])
def test_context_binding_is_checked_before_any_reader_or_metadata_change(project, source):
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "main.py", FIRST_HASH)

    with pytest.raises(ContextError, match="binding"):
        contexts.get(project, source, handle)
    with pytest.raises(ContextError, match="binding"):
        contexts.observe(project, source, handle, "main.py", SECOND_HASH)
    with pytest.raises(ContextError, match="binding"):
        contexts.validate(project, source, handle, lambda path: pytest.fail("unexpected reader"))
    assert contexts.get("project", "source", handle).files == {"main.py": FIRST_HASH}


def test_unknown_handles_are_rejected_without_creating_a_context():
    contexts = ReadContexts(max_contexts=1)
    for operation in (
        lambda: contexts.get("project", "source", "live_unknown"),
        lambda: contexts.observe("project", "source", "live_unknown", "main.py", FIRST_HASH),
        lambda: contexts.validate("project", "source", "live_unknown", lambda path: FIRST_HASH),
    ):
        with pytest.raises(ContextError, match="unknown"):
            operation()
    assert contexts.get("project", "source", contexts.create("project", "source")).files == {}


def test_observe_is_idempotent_and_conflicting_hash_invalidates_without_overwriting():
    contexts = ReadContexts(max_contexts=1, max_files=1)
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
    contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
    saved = contexts.get("project", "source", handle)

    with pytest.raises(ContextError, match="changed"):
        contexts.observe("project", "source", handle, "main.py", SECOND_HASH)
    assert saved.files == {"main.py": FIRST_HASH}
    with pytest.raises(ContextError, match="invalidated"):
        contexts.get("project", "source", handle)
    with pytest.raises(ContextError):
        contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
    with pytest.raises(ContextError):
        contexts.validate("project", "source", handle, lambda path: FIRST_HASH)
    fresh = contexts.create("project", "source")
    assert fresh != handle
    contexts.observe("project", "source", fresh, "main.py", SECOND_HASH)


def test_validation_reads_all_observed_hashes_and_does_not_add_or_replace_metadata():
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    hashes = {"main.py": FIRST_HASH, "源码/入口.py": SECOND_HASH}
    for path, sha256 in hashes.items():
        contexts.observe("project", "source", handle, path, sha256)
    read = []

    def reader(path):
        read.append(path)
        return hashes.get(path)

    contexts.validate("project", "source", handle, reader)
    assert read == list(hashes)
    assert contexts.get("project", "source", handle).files == hashes


def test_empty_context_validation_never_invokes_reader():
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    contexts.validate("project", "source", handle, lambda path: pytest.fail("unexpected reader"))


def test_contexts_for_the_same_binding_do_not_share_observations_or_switch_to_latest():
    contexts = ReadContexts()
    first = contexts.create("project", "source")
    second = contexts.create("project", "source")
    contexts.observe("project", "source", first, "main.py", FIRST_HASH)
    contexts.observe("project", "source", second, "main.py", SECOND_HASH)

    assert contexts.get("project", "source", first).files == {"main.py": FIRST_HASH}
    with pytest.raises(ContextError, match="validation failed"):
        contexts.validate("project", "source", first, lambda path: SECOND_HASH)
    assert contexts.get("project", "source", second).files == {"main.py": SECOND_HASH}
    contexts.validate("project", "source", second, lambda path: SECOND_HASH)


def test_validation_requires_a_callable_reader_without_changing_the_context():
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    with pytest.raises(ContextError, match="hash reader"):
        contexts.validate("project", "source", handle, None)
    assert contexts.get("project", "source", handle).files == {}


@pytest.mark.parametrize("actual", [SECOND_HASH, None, "untrusted source body", 7])
def test_changed_missing_or_non_hash_reader_result_invalidates_context(actual):
    contexts = ReadContexts(max_contexts=1, max_files=2)
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "first.py", FIRST_HASH)
    contexts.observe("project", "source", handle, "second.py", FIRST_HASH)
    saved = contexts.get("project", "source", handle)

    with pytest.raises(ContextError, match="changed or is unavailable") as error:
        contexts.validate(
            "project", "source", handle, lambda path: FIRST_HASH if path == "first.py" else actual
        )
    assert "second.py" not in str(error.value)
    assert "untrusted source body" not in str(error.value)
    assert saved.files == {"first.py": FIRST_HASH, "second.py": FIRST_HASH}
    with pytest.raises(ContextError, match="invalidated"):
        contexts.get("project", "source", handle)
    fresh = contexts.create("project", "source")
    contexts.observe("project", "source", fresh, "new.py", FIRST_HASH)


def test_reader_exceptions_are_sanitized_and_invalidate_context():
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "private-file.py", FIRST_HASH)

    def failed_reader(path):
        raise OSError("private-file.py SYNTHETIC_PRIVATE_DETAIL untrusted source body")

    with pytest.raises(ContextError, match="validation failed") as error:
        contexts.validate("project", "source", handle, failed_reader)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    displayed = "".join(traceback.format_exception_only(error.value))
    for private in ("private-file.py", "SYNTHETIC_PRIVATE_DETAIL", "untrusted source body", handle):
        assert private not in displayed
    with pytest.raises(ContextError, match="invalidated"):
        contexts.get("project", "source", handle)


def test_invalidation_removes_all_sources_for_only_the_requested_project():
    contexts = ReadContexts(max_contexts=3, max_files=3)
    first = contexts.create("project", "source-one")
    second = contexts.create("project", "source-two")
    other = contexts.create("other", "source-one")
    for project, source, handle in (
        ("project", "source-one", first),
        ("project", "source-two", second),
        ("other", "source-one", other),
    ):
        contexts.observe(project, source, handle, "main.py", FIRST_HASH)

    contexts.invalidate_project("project")
    contexts.invalidate_project("missing")
    for source, handle in (("source-one", first), ("source-two", second)):
        with pytest.raises(ContextError, match="invalidated"):
            contexts.get("project", source, handle)
    assert contexts.get("other", "source-one", other).files == {"main.py": FIRST_HASH}
    fresh = contexts.create("project", "source-three")
    contexts.observe("project", "source-three", fresh, "one.py", FIRST_HASH)
    contexts.observe("project", "source-three", fresh, "two.py", FIRST_HASH)


def test_clear_releases_all_capacity_and_old_handles_never_alias_new_contexts():
    contexts = ReadContexts(max_contexts=1, max_files=1)
    old = contexts.create("project", "source")
    contexts.observe("project", "source", old, "main.py", FIRST_HASH)
    contexts.clear()
    contexts.clear()
    fresh = contexts.create("project", "source")
    contexts.observe("project", "source", fresh, "main.py", SECOND_HASH)

    assert old != fresh
    with pytest.raises(ContextError, match="invalidated"):
        contexts.get("project", "source", old)
    assert contexts.get("project", "source", fresh).files == {"main.py": SECOND_HASH}


@pytest.mark.parametrize("operation", ["get", "observe", "validate"])
def test_ttl_is_fixed_at_creation_and_expires_at_the_exact_boundary(operation):
    clock = Clock()
    contexts = ReadContexts(ttl_seconds=10, clock=clock)
    handle = contexts.create("project", "source")
    clock.now = 9
    contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
    contexts.validate("project", "source", handle, lambda path: FIRST_HASH)
    assert contexts.get("project", "source", handle).created_at == 0
    clock.now = 10

    with pytest.raises(ContextError, match="expired"):
        if operation == "get":
            contexts.get("project", "source", handle)
        elif operation == "observe":
            contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
        else:
            contexts.validate("project", "source", handle, lambda path: FIRST_HASH)
    clock.now = 0
    with pytest.raises(ContextError, match="unknown"):
        contexts.get("project", "source", handle)


def test_expiry_reclaims_context_and_file_capacity_across_projects():
    clock = Clock()
    contexts = ReadContexts(max_contexts=1, max_files=1, ttl_seconds=10, clock=clock)
    old = contexts.create("first", "source")
    contexts.observe("first", "source", old, "main.py", FIRST_HASH)
    clock.now = 10
    fresh = contexts.create("second", "source")
    contexts.observe("second", "source", fresh, "main.py", SECOND_HASH)

    with pytest.raises(ContextError, match="expired"):
        contexts.get("first", "source", old)
    assert contexts.get("second", "source", fresh).files == {"main.py": SECOND_HASH}


def test_observing_a_live_context_reclaims_expired_metadata_from_other_contexts():
    clock = Clock()
    contexts = ReadContexts(max_files=1, ttl_seconds=10, clock=clock)
    old = contexts.create("first", "source")
    contexts.observe("first", "source", old, "main.py", FIRST_HASH)
    clock.now = 5
    active = contexts.create("second", "source")
    clock.now = 10
    contexts.observe("second", "source", active, "main.py", SECOND_HASH)

    with pytest.raises(ContextError, match="expired"):
        contexts.get("first", "source", old)
    assert contexts.get("second", "source", active).files == {"main.py": SECOND_HASH}


def test_expiry_during_validation_cannot_return_success():
    clock = Clock()
    contexts = ReadContexts(ttl_seconds=10, clock=clock)
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "main.py", FIRST_HASH)

    def reader(path):
        clock.now = 10
        return FIRST_HASH

    with pytest.raises(ContextError, match="expired"):
        contexts.validate("project", "source", handle, reader)


def test_context_capacity_is_global_and_does_not_evict_valid_handles():
    contexts = ReadContexts(max_contexts=2)
    first = contexts.create("first", "source")
    second = contexts.create("second", "source")

    with pytest.raises(ContextError, match="capacity"):
        contexts.create("third", "source")
    assert contexts.get("first", "source", first).files == {}
    assert contexts.get("second", "source", second).files == {}


def test_file_capacity_is_global_counts_each_context_and_keeps_existing_metadata():
    contexts = ReadContexts(max_files=2)
    first = contexts.create("first", "source")
    second = contexts.create("second", "source")
    contexts.observe("first", "source", first, "main.py", FIRST_HASH)
    contexts.observe("second", "source", second, "main.py", FIRST_HASH)
    contexts.observe("first", "source", first, "main.py", FIRST_HASH)

    with pytest.raises(ContextError, match="file capacity"):
        contexts.observe("second", "source", second, "extra.py", SECOND_HASH)
    assert contexts.get("second", "source", second).files == {"main.py": FIRST_HASH}
    contexts.invalidate_project("first")
    contexts.observe("second", "source", second, "extra.py", SECOND_HASH)


@pytest.mark.parametrize("name", ["max_contexts", "max_files"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "1"])
def test_count_limits_must_be_positive_integers(name, value):
    with pytest.raises(ValueError, match="positive integer"):
        ReadContexts(**{name: value})


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "10"])
def test_ttl_must_be_finite_and_positive(value):
    with pytest.raises(ValueError, match="finite and positive"):
        ReadContexts(ttl_seconds=value)


def test_clock_must_be_callable():
    with pytest.raises(ValueError, match="callable"):
        ReadContexts(clock=None)


@pytest.mark.parametrize("project,source", [("", "source"), ("project", ""), (None, "source")])
def test_creation_requires_explicit_bindings(project, source):
    with pytest.raises(ContextError, match="binding"):
        ReadContexts().create(project, source)


@pytest.mark.parametrize(
    "path,sha256",
    [
        ("../private-file.py", FIRST_HASH),
        ("/private-file.py", FIRST_HASH),
        ("main.py\nuntrusted source body", FIRST_HASH),
        ("main.py", "untrusted source body"),
        ("main.py", "A" * 64),
        ("main.py", None),
        (None, FIRST_HASH),
    ],
)
def test_invalid_metadata_is_rejected_without_leaking_or_changing_context(path, sha256):
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    with pytest.raises(ContextError) as error:
        contexts.observe("project", "source", handle, path, sha256)
    assert "private-file.py" not in str(error.value)
    assert "untrusted source body" not in str(error.value)
    assert contexts.get("project", "source", handle).files == {}


def test_errors_never_echo_handles_or_project_and_source_bindings():
    contexts = ReadContexts()
    project, source = "private-project", "private-source"
    handle = contexts.create(project, source)
    operations = (
        lambda: contexts.get("different-project", source, handle),
        lambda: contexts.get(project, "different-source", handle),
        lambda: contexts.get(project, source, "live_private-unknown-handle"),
        lambda: contexts.get(project, source, "private-history-selector"),
    )
    for operation in operations:
        with pytest.raises(ContextError) as error:
            operation()
        for private in (
            project,
            source,
            handle,
            "different-project",
            "different-source",
            "live_private-unknown-handle",
            "private-history-selector",
        ):
            assert private not in str(error.value)


def test_concurrent_observations_are_complete_and_idempotent():
    contexts = ReadContexts(max_files=64)
    handle = contexts.create("project", "source")

    def observe(number):
        path = f"file-{number}.py"
        contexts.observe("project", "source", handle, path, FIRST_HASH)
        contexts.observe("project", "source", handle, path, FIRST_HASH)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(observe, range(64)))
    assert contexts.get("project", "source", handle).files == {
        f"file-{number}.py": FIRST_HASH for number in range(64)
    }
    contexts.validate("project", "source", handle, lambda path: FIRST_HASH)


def test_concurrent_conflicting_observations_invalidate_instead_of_overwriting():
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    ready = Barrier(2)

    def observe(sha256):
        ready.wait(timeout=5)
        try:
            contexts.observe("project", "source", handle, "main.py", sha256)
        except ContextError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(observe, [FIRST_HASH, SECOND_HASH]))
    assert sorted(outcomes) == [False, True]
    with pytest.raises(ContextError, match="invalidated"):
        contexts.get("project", "source", handle)


def test_concurrent_creation_cannot_exceed_global_context_capacity():
    contexts = ReadContexts(max_contexts=4)

    def create(number):
        project = f"project-{number}"
        try:
            return project, contexts.create(project, "source")
        except ContextError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(16)))
    created = [result for result in results if result is not None]
    assert len(created) == 4
    assert len({handle for _, handle in created}) == 4
    for project, handle in created:
        assert contexts.get(project, "source", handle).files == {}


def test_concurrent_observations_cannot_exceed_global_file_capacity():
    contexts = ReadContexts(max_files=8)
    handles = [contexts.create(project, "source") for project in ("first", "second")]

    def observe(number):
        index = number % 2
        project = ("first", "second")[index]
        try:
            contexts.observe(project, "source", handles[index], f"file-{number}.py", FIRST_HASH)
        except ContextError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(observe, range(64)))
    assert sum(results) == 8
    total_files = sum(
        len(contexts.get(project, "source", handle).files)
        for project, handle in zip(("first", "second"), handles, strict=True)
    )
    assert total_files == 8


@pytest.mark.parametrize("invalidate", ["project", "clear"])
def test_invalidation_during_reader_callback_prevents_validation_success(invalidate):
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "main.py", FIRST_HASH)
    reading, release = Event(), Event()

    def reader(path):
        reading.set()
        assert release.wait(timeout=5)
        return FIRST_HASH

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(contexts.validate, "project", "source", handle, reader)
        try:
            assert reading.wait(timeout=5)
            if invalidate == "project":
                contexts.invalidate_project("project")
            else:
                contexts.clear()
        finally:
            release.set()
        with pytest.raises(ContextError, match="invalidated"):
            result.result(timeout=5)


def test_concurrent_addition_requires_validation_of_the_complete_new_file_set():
    contexts = ReadContexts()
    handle = contexts.create("project", "source")
    contexts.observe("project", "source", handle, "first.py", FIRST_HASH)
    reading, release = Event(), Event()

    def reader(path):
        reading.set()
        assert release.wait(timeout=5)
        return FIRST_HASH

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(contexts.validate, "project", "source", handle, reader)
        try:
            assert reading.wait(timeout=5)
            contexts.observe("project", "source", handle, "second.py", SECOND_HASH)
        finally:
            release.set()
        with pytest.raises(ContextError, match="changed during validation"):
            result.result(timeout=5)
    hashes = {"first.py": FIRST_HASH, "second.py": SECOND_HASH}
    read = []

    def complete_reader(path):
        read.append(path)
        return hashes.get(path)

    contexts.validate("project", "source", handle, complete_reader)
    assert read == list(hashes)
