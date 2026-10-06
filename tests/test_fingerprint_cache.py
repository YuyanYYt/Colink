from concurrent.futures import ThreadPoolExecutor

import pytest

from code_context.fingerprint_cache import (
    MAX_CHARGED_BYTES,
    MAX_ENTRIES,
    FingerprintCache,
    SourceFingerprints,
)

A, B = "a" * 64, "b" * 64
HASH, CHANGED = "1" * 64, "2" * 64
VERSION = (1, 2, 0o100644, 10, 20, 30)


def test_default_caps_and_source_view_are_metadata_only():
    cache = FingerprintCache()
    a, b = SourceFingerprints(cache, A), SourceFingerprints(cache, B)
    assert a.put("same.py", VERSION, HASH)
    assert b.put("same.py", VERSION, CHANGED)
    assert a.get("same.py") == (VERSION, HASH)
    assert b.get("same.py") == (VERSION, CHANGED)
    assert len(a) == len(b) == 1 and len(cache) == 2
    stats = cache.stats()
    assert stats["max_entries"] == MAX_ENTRIES == 50_000
    assert stats["max_charged_bytes"] == MAX_CHARGED_BYTES == 32 * 1024 * 1024
    stats["entries"] = -1
    assert cache.stats()["entries"] == 2
    with pytest.raises(TypeError):
        a.get("same.py")[0][0] = 0


def test_global_lru_hits_and_updates_across_sources():
    cache = FingerprintCache(max_entries=2)
    cache.put(A, "first.py", VERSION, HASH)
    cache.put(B, "second.py", VERSION, HASH)
    cache.get(A, "first.py")
    cache.put(A, "third.py", VERSION, HASH)
    assert cache.get(B, "second.py") is None
    assert cache.get(A, "first.py") == (VERSION, HASH)
    charge = cache.stats()["charged_bytes"]
    cache.put(A, "first.py", (1, 2, 0o100644, 11, 21, 31), CHANGED)
    assert cache.get(A, "first.py")[1] == CHANGED
    assert cache.stats()["charged_bytes"] == charge
    assert len(cache) == 2


def test_byte_budget_unicode_charge_and_oversized_skip():
    cache = FingerprintCache(max_entries=10, max_bytes=1400)
    cache.put(A, "a.py", VERSION, HASH)
    cache.put(B, "b.py", VERSION, HASH)
    cache.put(A, "c.py", VERSION, HASH)
    assert len(cache) == 2 and cache.get(A, "a.py") is None
    assert 0 < cache.stats()["charged_bytes"] <= 1400
    assert not cache.put(A, "界" * 300 + ".py", VERSION, HASH)
    assert len(cache) == 2  # Too-large metadata must not flush other sources.
    cache.clear()
    cache.put(A, "ascii.py", VERSION, HASH)
    ascii_charge = cache.stats()["charged_bytes"]
    cache.clear()
    cache.put(A, "汉字汉字汉.py", VERSION, HASH)
    assert cache.stats()["charged_bytes"] > ascii_charge


def test_revocation_only_drops_its_namespace_and_blocks_late_puts():
    cache = FingerprintCache()
    cache.retain_sources((A, B))
    a, b = SourceFingerprints(cache, A), SourceFingerprints(cache, B)
    a.put("same.py", VERSION, HASH)
    b.put("same.py", VERSION, CHANGED)
    cache.retain_sources((B,))
    assert len(a) == 0 and len(b) == 1
    assert b.get("same.py") == (VERSION, CHANGED)
    assert not a.put("late.py", VERSION, HASH)
    assert cache.stats()["binding_charged_bytes"] == 192
    cache.retain_sources(())
    assert len(cache) == cache.stats()["charged_bytes"] == 0


def test_bindings_are_charged_and_do_not_evict_unrelated_data_on_invalid_budget():
    cache = FingerprintCache(max_bytes=700)
    cache.put(A, "a.py", VERSION, HASH)
    with pytest.raises(ValueError, match="binding byte"):
        cache.retain_sources(tuple(f"{number:064x}" for number in range(4)))
    assert cache.get(A, "a.py") == (VERSION, HASH)
    cache.retain_sources((A,))
    assert len(cache) == 0  # Entry + binding cannot fit; both charged limits still hold.
    assert cache.stats()["charged_bytes"] == 192


@pytest.mark.parametrize(
    "options",
    [
        {"max_entries": 0},
        {"max_entries": True},
        {"max_entries": 50_001},
        {"max_bytes": 0},
        {"max_bytes": True},
        {"max_bytes": 32 * 1024 * 1024 + 1},
    ],
)
def test_invalid_budgets_rejected(options):
    with pytest.raises(ValueError):
        FingerprintCache(**options)


@pytest.mark.parametrize(
    "version,sha",
    [
        ("source body", HASH),
        ([1, 2, 3, 4, 5, 6], HASH),
        ((1, 2, 3, 4, 5, True), HASH),
        ((1, 2, 3, 4, 5, 1 << 200), HASH),
        (VERSION, "source body"),
    ],
)
def test_only_bounded_immutable_metadata_is_accepted(version, sha):
    cache = FingerprintCache()
    with pytest.raises(ValueError) as error:
        cache.put(A, "a.py", version, sha)
    assert "source body" not in str(error.value) and len(cache) == 0


def test_discard_source_and_clear_are_scoped():
    cache = FingerprintCache()
    cache.put(A, "a.py", VERSION, HASH)
    cache.put(A, "b.py", VERSION, HASH)
    cache.put(B, "a.py", VERSION, CHANGED)
    cache.discard(A, "a.py")
    assert cache.source_size(A) == 1
    cache.drop_source(A)
    assert cache.get(B, "a.py") == (VERSION, CHANGED)
    cache.clear()
    assert cache.stats()["charged_bytes"] == 0 and len(cache) == 0


def test_threaded_put_get_eviction_and_clear_stay_within_global_budgets():
    cache = FingerprintCache(max_entries=17, max_bytes=6000)

    def operate(number):
        source = A if number % 2 else B
        path = f"file_{number % 30}.py"
        cache.put(source, path, VERSION, HASH)
        value = cache.get(source, path)
        assert value is None or value == (VERSION, HASH)
        if number % 31 == 0:
            cache.clear()
        stats = cache.stats()
        assert 0 <= stats["entries"] <= 17
        assert 0 <= stats["charged_bytes"] <= 6000

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(operate, range(300)))
    assert cache.source_size(A) + cache.source_size(B) == len(cache)


def test_binding_iterator_runs_outside_cache_lock():
    cache = FingerprintCache()

    def bindings():
        assert not cache._lock.locked()
        cache.stats()  # Would deadlock if retain_sources held its non-reentrant lock.
        yield A

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(cache.retain_sources, bindings()).result(timeout=2)
    assert cache.stats()["active_sources"] == 1
