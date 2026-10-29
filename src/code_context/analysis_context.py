"""Optional structural evidence inside an analysis's existing fact publication."""

import time


def structure_evidence(
    facts,
    project_id,
    revision,
    *,
    selector,
    include,
    direction,
    max_depth,
    max_nodes,
    max_edges,
    max_chars,
    deadline,
    include_type_only=True,
):
    """Share source identity, facts and the remaining deadline; never open a new context."""
    from code_context.intelligence_analysis import analyze_structure

    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms < 100 or max_chars < 1500:
        return {
            "analysis_status": "not_computed",
            "truncated": True,
            "truncated_reason": ["time_budget" if remaining_ms < 100 else "max_chars"],
            "requested_sections": include,
        }
    selected = {key: value for key, value in selector.items() if key != "value"}
    return analyze_structure(
        facts,
        project_id,
        revision,
        mode="symbol_context"
        if selected.get("symbol_id") is not None or selected.get("qualname") is not None
        else "file_context",
        selector=selected,
        include=include,
        direction=direction,
        max_depth=max(1, max_depth),
        max_nodes=max_nodes,
        max_edges=max_edges,
        max_chars=max_chars,
        max_ms=min(10000, remaining_ms),
        include_type_only=include_type_only,
    )


def attach_context(result, context):
    """Expose optional evidence without replacing the primary mode or its conclusions."""
    result["context"] = context
    if context.get("analysis_status") != "ready":
        result["analysis_status"] = "partial"
    if context.get("truncated"):
        result["truncated"] = True
    result["truncated_reason"] = list(
        dict.fromkeys([*result.get("truncated_reason", []), *context.get("truncated_reason", [])])
    )
    return result
