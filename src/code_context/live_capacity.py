"""Local Live capacity presets; independent of historical Mirror transport limits.

Budgets are ceilings, allocated on demand. They do not reserve RAM or prove that
the largest permitted repository can be analyzed on the current machine.
"""

from dataclasses import dataclass

MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_LIVE_FILES = 1_000_000
MAX_LIVE_DIRECTORIES = 500_000


@dataclass(frozen=True)
class LiveCapacity:
    name: str
    files: int
    directories: int
    source_bytes: int
    parse_bytes: int
    index_bytes: int
    facts: int
    database_bytes: int
    manifest_seconds: float = 30


LARGE_CAPACITY = LiveCapacity(
    "large", 250_000, 100_000, 512 * MIB, 256 * MIB, 256 * MIB, 2_000_000, 2 * GIB
)
EXTREME_CAPACITY = LiveCapacity(
    "extreme", 1_000_000, 500_000, 2 * GIB, GIB, GIB, 10_000_000, 8 * GIB, 60
)
CAPACITIES = {item.name: item for item in (LARGE_CAPACITY, EXTREME_CAPACITY)}


def capacity_usage(stats, limits, storage_bytes):
    """Bounded numeric telemetry; never sample source or estimate process RAM."""
    metrics = (
        ("source_bytes", "source_bytes", "source_bytes_per_project"),
        ("parse_bytes", "parse_payload_bytes", "parse_bytes_per_project"),
        ("index_bytes", "index_payload_bytes", "index_bytes_per_project"),
        ("parse_facts", "parse_fact_count", "facts_per_project"),
        ("index_facts", "fact_count", "facts_per_project"),
        ("files", "file_count", "files_per_project"),
        ("directories", "directory_count", "directories_per_project"),
    )
    result = {}
    for name, key, limit_key in metrics:
        used, limit = stats.get(key), limits.get(limit_key)
        result[name] = _usage(used, limit)
    result["database_bytes"] = _usage(storage_bytes, limits["database_page_ceiling_bytes"])
    return result


def _usage(used, limit):
    return {
        "used": used,
        "limit": limit,
        "percent": round(100 * used / limit, 6)
        if type(used) is int and type(limit) is int and limit > 0
        else None,
    }
