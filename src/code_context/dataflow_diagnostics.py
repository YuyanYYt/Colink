"""Stable, source-free explanations of bounded static value-flow limitations.

The original diagnostic codes remain the detailed compatibility contract. Counts
describe all limitations observed during analysis, including details omitted by
the diagnostic/output budgets; they do not assert whole-project coverage.
"""

from collections import Counter

_GROUPS = {
    "language_support": (
        "Value-flow analysis is unavailable for this language or source form.",
        {"PYTHON_ONLY", "PYTHON_STUB_UNSUPPORTED"},
    ),
    "call_resolution": (
        "Call targets or argument bindings are uncertain; flow through these calls is unknown.",
        {
            "CALL_TARGET_UNKNOWN_OR_AMBIGUOUS",
            "LOCAL_CALLABLE_NOT_BOUND",
            "EXPANDED_ARGUMENTS_PARTIAL",
            "ARGUMENT_BINDING_PARTIAL",
            "VARIADIC_PARAMETERS_PARTIAL",
            "DECORATED_CALLABLE_PARTIAL",
        },
    ),
    "control_flow": (
        "Control flow is only partially modeled; possible or unknown paths require source review.",
        {
            "ASYNC_SEMANTICS_PARTIAL",
            "ASYNC_OR_GENERATOR_PARTIAL",
            "BRANCH_UNBOUND_VALUE_PARTIAL",
            "CONTEXT_MANAGER_PARTIAL",
            "EXCEPTION_FLOW_PARTIAL",
            "LOOP_FIXED_POINT_PARTIAL",
            "LOCAL_CALLABLE_CONDITIONAL_PARTIAL",
            "RECURSIVE_CALL_PARTIAL",
            "STATEMENT_PARTIAL",
        },
    ),
    "alias_side_effect": (
        "Aliasing, container selection or mutations may hide or add value flow.",
        {
            "GLOBAL_OR_NONLOCAL_PARTIAL",
            "UNKNOWN_FIELD",
            "ATTRIBUTE_ALIAS_PARTIAL",
            "SUBSCRIPT_SELECTION_PARTIAL",
            "DESTRUCTURING_PARTIAL",
            "MUTATION_ALIAS_PARTIAL",
            "RECEIVER_ALIAS_PARTIAL",
            "CALL_SIDE_EFFECTS_PARTIAL",
        },
    ),
    "resource_budget": (
        "An analysis or output budget limited coverage; omitted facts remain unknown.",
        {
            "SOURCE_PARSE_BUDGET",
            "AST_NODE_BUDGET",
            "RELATION_FACTS_PARTIAL",
            "ANALYZER_RESOURCE_LIMIT",
            "CALL_DEPTH_PARTIAL",
        },
    ),
    "source_context": (
        "Required source, binding or lexical-scope context is missing or unsupported.",
        {
            "CALLABLE_REQUIRED",
            "PYTHON_PARSE_ERROR",
            "SYMBOL_SOURCE_MISMATCH",
            "CALLEE_SOURCE_UNSUPPORTED",
            "EXTERNAL_OR_UNBOUND_NAME",
            "IMPORT_VALUE_PARTIAL",
            "DEFAULT_DEFINITION_SCOPE_PARTIAL",
            "NESTED_SCOPE_PARTIAL",
            "EXPRESSION_SCOPE_PARTIAL",
        },
    ),
}
_DETAILS = {
    code: {"category": category, "message": message}
    for category, (message, codes) in _GROUPS.items()
    for code in codes
}
_OTHER = {
    "category": "other_limitation",
    "message": "An unclassified analysis limitation requires conservative interpretation.",
}


def diagnostic_details(code: str) -> dict:
    """Return only fixed explanations; never interpolate source or exception text."""
    return _DETAILS.get(code, _OTHER).copy()


def annotate_dataflow_result(result: dict, *, diagnostic_counts: dict | None = None) -> dict:
    """Attach additive classification and an explicit non-runtime conclusion.

    Reapplying after Live output clipping preserves the original aggregate
    counts. Existing truncation reasons contribute resource-budget limitations
    to the summary; the original diagnostic codes and reason fields stay intact.
    """
    diagnostics = result.get("diagnostics", [])
    previous = result.get("diagnostic_summary")
    if diagnostic_counts is not None:
        counts = Counter(diagnostic_counts)
    elif previous is not None:
        counts = Counter(previous["by_category"])
        counts["resource_budget"] -= previous.get("budget_limits", 0)
    else:
        counts = Counter(diagnostic_details(item["code"])["category"] for item in diagnostics)
    reasons = list(
        dict.fromkeys([*result.get("truncated_reasons", []), *result.get("truncated_reason", [])])
    )
    counts["resource_budget"] += len(reasons)
    result["diagnostics"] = [{**item, **diagnostic_details(item["code"])} for item in diagnostics]
    counts = {key: value for key, value in sorted(counts.items()) if value > 0}
    result["diagnostic_summary"] = {
        "total": sum(counts.values()),
        "by_category": counts,
        "budget_limits": len(reasons),
        "scope": "analyzed_slice",
    }
    availability = result.get("coverage", {})
    availability = availability.get("availability") if isinstance(availability, dict) else None
    if result.get("selection_status") == "selection_not_computed":
        status = "selection_not_computed"
    elif availability not in {None, "available"}:
        status = "analysis_unavailable"
    elif result.get("analysis_status") in {"ambiguous", "selection_ambiguous"}:
        status = "selection_ambiguous"
    elif result.get("analysis_status") in {"selection_not_found", "target_not_found"}:
        status = "selection_not_resolved"
    elif result.get("path_found"):
        status = (
            "uncertain_path_found"
            if result.get("path_resolution") == "unknown"
            else "possible_static_path_found"
            if result.get("path_resolution") == "possible"
            else "static_path_found"
        )
    elif result.get("path_found") is False:
        status = "no_path_in_analyzed_slice"
    else:
        status = "partial_static_slice" if counts else "analyzed_static_slice"
    result["conclusion"] = {
        "status": status,
        "runtime_reachability": "unknown",
        "absence_proven": False,
    }
    return result
