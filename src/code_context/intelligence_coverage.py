"""Bounded explanations of the facts available in one validated source state.

Coverage describes extraction, not complete name binding or runtime behavior.
It is selected from the same publication as a query; source is never read here.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

_MAX_EXAMPLES = 12
_MAX_DIAGNOSTICS = 5
_MAX_SELECTED_PATHS = 2
_SOURCE_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".vue": "vue",
    ".rs": "rust",
}


def count_index_diagnostics(counts, diagnostics):
    """Accumulate affected-file counts, once per code per extracted file."""
    codes = {
        item.get("code")
        for item in diagnostics[:20]
        if isinstance(item, dict)
        and isinstance(item.get("code"), str)
        and 0 < len(item["code"]) <= 100
    }
    for code in sorted(codes):
        counts[code] = counts.get(code, 0) + 1


def public_index_stats(stats):
    """Do not advertise internal dependency declarations as unbounded metadata."""
    result = {key: value for key, value in stats.items() if key != "dependency_metadata"}
    metadata = stats.get("dependency_metadata")
    if isinstance(metadata, dict):
        result["dependency_manifest_summary"] = {
            "manifest_count": len(metadata.get("manifests", [])),
            "partial": bool(metadata.get("partial", True)),
            "diagnostics": {
                item["code"]: True
                for item in metadata.get("diagnostics", [])[:20]
                if isinstance(item, dict) and isinstance(item.get("code"), str)
            },
        }
    return result


def bound_index_coverage(coverage, max_chars, *, compact=False):
    """Fit internal-query metadata to an outer read's independent budget."""
    if compact and max_chars < 1600:
        files = coverage.get("files", {})
        return {
            "status": coverage["status"],
            "ready": files.get("structure_ready", 0),
            "limited": files.get("structure_limited", 0),
            "details_omitted": True,
        }
    budget = min(3500, max(300, max_chars // 4))
    result = dict(coverage)
    result["details"] = list(result.get("details", []))
    if budget < 800:
        result = {
            key: value
            for key, value in result.items()
            if key in {"status", "files", "limitations", "static_only", "inventory_complete"}
        }
        result["details_omitted"] = True
        return result
    while result["details"] and len(json.dumps(result, ensure_ascii=False)) > budget:
        result["details"].pop()
        result["details_omitted"] = True
    for key in ("guidance", "files_by_language", "supported_languages"):
        if len(json.dumps(result, ensure_ascii=False)) <= budget:
            break
        result.pop(key, None)
        result["details_omitted"] = True
    return result


def _diagnostics(payload):
    items = json.loads(payload).get("items", [])
    result = []
    for item in items[:_MAX_DIAGNOSTICS]:
        # Exception messages and source excerpts never belong in coverage metadata.
        code = item.get("code")
        if not isinstance(code, str) or not code or len(code) > 100:
            continue
        diagnostic = {"code": code}
        for name in ("line", "column"):
            if type(item.get(name)) is int and item[name] > 0:
                diagnostic[name] = item[name]
        result.append(diagnostic)
    return result


def _selected_paths(parameters, result):
    paths = []
    for selected in (parameters, parameters.get("selector"), parameters.get("target"), result):
        if isinstance(selected, dict) and isinstance(selected.get("path"), str):
            path = selected["path"]
            if path not in paths:
                paths.append(path)
    for key in ("symbol", "target_symbol"):
        selected = result.get(key)
        if isinstance(selected, dict) and isinstance(selected.get("path"), str):
            if selected["path"] not in paths:
                paths.append(selected["path"])
    return paths[:_MAX_SELECTED_PATHS]


def _file_detail(row):
    status = row["status"] or "not_analyzed"
    diagnostics = _diagnostics(row["diagnostics"]) if row["diagnostics"] else []
    detected = _SOURCE_LANGUAGES.get(PurePosixPath(row["path"]).suffix.lower())
    if row["status"] is None:
        if detected in {"python", "java"}:
            status = "not_indexed"
            diagnostics = [{"code": "PROJECT_INDEX_BUDGET_EXCEEDED"}]
        elif detected is not None:
            status = "unsupported"
    if status == "unsupported" and not diagnostics:
        diagnostics = [{"code": "LANGUAGE_NOT_SUPPORTED" if detected else "NOT_STRUCTURE_SOURCE"}]
    if status == "ready":
        capability = "structure_extracted"
    elif status == "partial":
        capability = "partial_structure"
    elif status in {"unsupported", "not_analyzed", "not_indexed"}:
        capability = "structure_unavailable"
    else:
        capability = "structure_limited"
    return {
        "path": row["path"],
        "language": detected
        if row["language"] in {None, "unsupported"} and detected
        else row["language"] or "unsupported",
        "status": status,
        "capability": capability,
        "diagnostics": diagnostics,
        "source_reading": "excluded"
        if any(item["code"] == "SOURCE_TEXT_EXCLUDED" for item in diagnostics)
        else "independent_of_index; source policy still applies",
    }


def index_coverage(
    db,
    project_id,
    revision,
    stats,
    *,
    parameters=None,
    result=None,
    inventory=None,
    max_chars=20_000,
):
    """Explain partial success without changing query names or parameters.

    Counts cover the known source inventory. File details are explicit samples,
    with selected paths first, so clipping a response cannot imply completeness.
    """
    parameters, result = parameters or {}, result or {}
    statuses = stats.get("files_by_status", {})
    languages = stats.get("files_by_language", {})
    indexed = sum(count for language, count in languages.items() if language != "unsupported")
    ready = statuses.get("ready", 0)
    limited = sum(
        count for status, count in statuses.items() if status not in {"ready", "unsupported"}
    )
    retained = stats.get("file_count")
    if retained is None:
        retained = sum(languages.values())
    discovered = len(inventory["files"]) if inventory is not None else retained
    omitted = max(0, discovered - retained)
    unindexed = max(0, retained - indexed)
    skipped = inventory.get("skipped", {}) if inventory is not None else {}
    discovery_partial = (
        inventory.get("partial", False)
        if inventory is not None
        else stats.get("discovery_partial", False)
    )
    reasons = {
        status: count
        for status, count in statuses.items()
        if status not in {"ready", "unsupported"} and count
    }
    if omitted:
        reasons["metadata_budget"] = omitted
    if discovery_partial:
        reasons["discovery_incomplete"] = 1
    if skipped:
        reasons["source_discovery_excluded"] = len(skipped)
    root_diagnostics = stats.get("python_source_roots_diagnostics", [])
    if root_diagnostics:
        reasons["source_root_configuration"] = len(root_diagnostics)
    if stats.get("partial", True) and not reasons:
        reasons["unspecified_index_limit"] = 1
    coverage = {
        "status": "partial" if stats.get("partial", True) else "ready",
        "files": {
            "known": discovered,
            "structure_ready": ready,
            "structure_limited": limited,
            "not_analyzed": unindexed,
            "metadata_omitted": omitted,
        },
        "limitations": reasons,
        "static_only": True,
        "inventory_complete": not discovery_partial and not skipped,
    }
    # Leave most of the response budget to the actual answer and its evidence.
    budget = min(3500, max(300, max_chars // 4))
    if budget < 800:
        return bound_index_coverage(coverage, max_chars)
    coverage.update(
        supported_languages=stats.get("supported_languages", []),
        files_by_language=languages,
        details_are_samples=True,
        details=[],
        limitation_codes=stats.get("index_diagnostics", {}),
        guidance="Use file_context for one path or read_code for authorized source; "
        "missing static results do not prove absence.",
    )
    paths = _selected_paths(parameters, result)
    for selected in (parameters, parameters.get("selector"), parameters.get("target")):
        if not isinstance(selected, dict) or not isinstance(selected.get("symbol_id"), str):
            continue
        row = db.execute(
            "SELECT path FROM ci_symbols WHERE project_id=? AND revision=? AND symbol_id=?",
            (project_id, revision, selected["symbol_id"]),
        ).fetchone()
        if row is not None and row["path"] not in paths:
            paths.append(row["path"])
    paths = paths[:_MAX_SELECTED_PATHS]
    for path in paths:
        row = db.execute(
            "SELECT f.path,c.language,c.status,c.diagnostics FROM files f "
            "LEFT JOIN ci_files c ON c.project_id=f.project_id AND c.revision=f.revision "
            "AND c.path=f.path WHERE f.project_id=? AND f.revision=? AND f.path=?",
            (project_id, revision, path),
        ).fetchone()
        if row is not None:
            coverage["details"].append(_file_detail(row))
        elif inventory is not None and any(item["path"] == path for item in inventory["files"]):
            coverage["details"].append(
                {
                    "path": path,
                    "status": "metadata_omitted",
                    "capability": "structure_unavailable",
                    "diagnostics": [{"code": "PROJECT_METADATA_BUDGET_EXCEEDED"}],
                    "source_reading": "independent_of_index; source policy still applies",
                }
            )
    limited_rows = db.execute(
        "SELECT path,language,status,diagnostics FROM ci_files "
        "WHERE project_id=? AND revision=? AND status NOT IN ('ready','unsupported') "
        "ORDER BY path LIMIT ?",
        (project_id, revision, 6),
    ).fetchall()
    unindexed_rows = db.execute(
        "SELECT f.path,c.language,c.status,c.diagnostics FROM files f "
        "LEFT JOIN ci_files c ON c.project_id=f.project_id AND c.revision=f.revision "
        "AND c.path=f.path WHERE f.project_id=? AND f.revision=? "
        "AND (c.status='unsupported' OR c.path IS NULL) ORDER BY f.path LIMIT 2",
        (project_id, revision),
    ).fetchall()
    ready_rows = db.execute(
        "SELECT path,language,status,diagnostics FROM ci_files "
        "WHERE project_id=? AND revision=? AND status='ready' ORDER BY path LIMIT 2",
        (project_id, revision),
    ).fetchall()
    # Keep examples of success, limitations and unavailable analysis near the
    # front so a small response cannot be filled only by one large category.
    rows = [
        group[index]
        for index in range(6)
        for group in (ready_rows, limited_rows, unindexed_rows)
        if index < len(group)
    ]
    for row in rows:
        if len(coverage["details"]) >= _MAX_EXAMPLES:
            break
        if row["path"] not in {item["path"] for item in coverage["details"]}:
            coverage["details"].append(_file_detail(row))
    for path, reason in list(skipped.items())[:3]:
        if len(coverage["details"]) >= _MAX_EXAMPLES:
            break
        coverage["details"].append(
            {
                "path": path,
                "status": "discovery_excluded",
                "capability": "structure_unavailable",
                "diagnostics": [{"code": reason}],
                "source_reading": "excluded by source discovery policy",
            }
        )
    return bound_index_coverage(coverage, max_chars)
