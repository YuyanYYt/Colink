"""Source positions for bounded, syntactic may-call conditions in the live index."""

MAX_GUARDS = 6
MAX_BOOLEAN_SLOTS = 16


def add_guard(guards, guard):
    if len(guards) < MAX_GUARDS:
        return (*guards, guard)
    if guards[-1]["kind"] == "additional_conditions":
        return guards
    # Bound both stored facts and tool output. Omitted conditions remain unknown.
    return (
        *guards[:-1],
        {"kind": "additional_conditions", "line": guard["line"], "column": guard["column"]},
    )


def valid_call_control(value):
    if not isinstance(value, dict) or not value or value.keys() - {"guards", "forwarded_booleans"}:
        return False
    guards = value.get("guards", [])
    forwarded = value.get("forwarded_booleans", [])
    if not isinstance(guards, list) or len(guards) > MAX_GUARDS:
        return False
    for guard in guards:
        if not isinstance(guard, dict) or not {"kind", "line", "column"} <= guard.keys():
            return False
        if any(type(guard[key]) is not int or guard[key] < 1 for key in ("line", "column")):
            return False
        kind = guard["kind"]
        if kind in ("branch", "fallthrough"):
            if type(guard.get("outcome")) is not bool:
                return False
            if "parameter" in guard or "value" in guard:
                if (
                    not _boolean_slot(guard.get("parameter"))
                    or type(guard.get("value")) is not bool
                ):
                    return False
            if guard.keys() - {"kind", "line", "column", "outcome", "parameter", "value"}:
                return False
        elif kind not in ("prior_exit", "additional_conditions") or len(guard) != 3:
            return False
    return (
        isinstance(forwarded, list)
        and len(forwarded) <= MAX_BOOLEAN_SLOTS
        and all(
            isinstance(pair, list) and len(pair) == 2 and all(map(_boolean_slot, pair))
            for pair in forwarded
        )
    )


def _boolean_slot(value):
    return type(value) is int and 0 <= value < MAX_BOOLEAN_SLOTS


def contradictory_call_guards(path, by_pair):
    """Check this particular shortest symbol path, never general runtime reachability."""
    required = {}
    for source, target in zip(path, path[1:], strict=False):
        sites = by_pair[source, target]
        if len(sites) != 1:
            # Sites between the same two symbols are alternatives. Comparing the
            # guards of different alternatives as if both executed is unsound.
            required = {}
            continue
        site = sites[0]
        flow = site.get("call_control") or {}
        for guard in flow.get("guards", ()):
            if "parameter" in guard:
                slot, value = guard["parameter"], guard["value"]
                if slot in required and value != required[slot]:
                    return True
                required[slot] = value
        if site["evidence"] != "lexical_definition" or site["source_path"] != site["target_path"]:
            required = {}
            continue
        required = {
            target_slot: required[source_slot]
            for source_slot, target_slot in flow.get("forwarded_booleans", ())
            if source_slot in required
        }
    return False
