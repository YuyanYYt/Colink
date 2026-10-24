"""Fit task-origin diffs into the unified read response without changing cursors."""

import json


def bound_diff_result(result: dict, max_chars: int) -> dict:
    """Clip patch text only; retain complete change descriptions and scan pagination."""
    if type(max_chars) is not int or not 1000 <= max_chars <= 50_000:
        raise ValueError("max_chars must be between 1000 and 50000")
    result = {**result, "changes": [dict(change) for change in result["changes"]]}
    result["analysis_status"] = (
        "unavailable"
        if not result.get("changes_available")
        else "partial"
        if result.get("truncated")
        else "ready"
    )
    result["truncated_reason"] = ["diff_budget"] if result.get("truncated") else []
    if len(json.dumps(result, ensure_ascii=False)) <= max_chars:
        return result
    result.update(analysis_status="partial", truncated=True)
    result["truncated_reason"].append("max_chars")
    patches = []
    for change in result["changes"]:
        if "patch" in change:
            patches.append((change, change["patch"]))
            change["patch"] = ""
    # Descriptions and cursors are indivisible. In particular, empty-baseline
    # pagination may have skipped excluded files, so item count is not a cursor.
    remaining = max_chars - len(json.dumps(result, ensure_ascii=False))
    if remaining < 0:
        raise ValueError(
            "diff metadata exceeds response budget; narrow path/limit or raise max_chars"
        )
    for change, patch in patches:
        low, high = 0, len(patch)
        while low < high:
            middle = (low + high + 1) // 2
            cost = len(json.dumps(patch[:middle], ensure_ascii=False)) - 2
            if cost <= remaining:
                low = middle
            else:
                high = middle - 1
        change["patch"] = patch[:low]
        remaining -= len(json.dumps(change["patch"], ensure_ascii=False)) - 2
        if low < len(patch):
            change["patch_truncated"] = True
    return result
