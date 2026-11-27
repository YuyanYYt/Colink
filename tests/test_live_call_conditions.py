"""Exercise guarded call sites through the public read-only live analysis tool."""

import asyncio
import json
from textwrap import dedent

import pytest

from code_context.fact_payloads import FactCacheError
from code_context.intelligence_queries import decode_relation
from code_context.language_frontends import parse_source
from code_context.live import LiveQueries
from code_context.project_index import ProjectIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess

JAVA = dedent("""\
    class Choose {
        static int left() { return 1; }
        static int right() { return 2; }
        static int split(boolean value) {
            if (value) { return left(); } else { return right(); }
        }
        static int guard(boolean value) {
            if (value) { return left(); }
            return right();
        }
        static int nested(boolean force, boolean cached) {
            if (!force) { synchronized(Choose.class) { if (cached) { return left(); } } }
            return right();
        }
        static int first(boolean flag) {
            if (flag) { return second(flag); }
            return 0;
        }
        static int second(boolean differentName) {
            if (!differentName) { return right(); } return 0;
        }
        static int reassigned(boolean flag) {
            if (flag) { flag = false; return second(flag); } return 0;
        }
        static int otherParameter(boolean flag, boolean actual) {
            if (flag) { return second(actual); } return 0;
        }
        static int twoSites(boolean flag) {
            if (flag) { return second(flag); } return second(flag);
        }
        static int overloaded(boolean flag) { return right(); }
        static int overloaded(int flag) { return right(); }
        static int ambiguous(boolean flag) {
            if (flag) { return overloaded(flag); } return 0;
        }
    }
""")
PYTHON = dedent("""\
    def left(): return 1
    def right(): return 2
    def split(value):
        if value: return left()
        else: return right()
    def guard(value):
        if value: return left()
        return right()
    def first(value):
        if value: return second(value)
    def second(flag):
        if not flag: return right()
    def outer(value):
        if value: return
        def inner(): return right()
        return inner()
""")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("guards")
    source = workspace / "source"
    source.mkdir()
    (source / "Choose.java").write_text(JAVA)
    (source / "choice.py").write_text(PYTHON)
    backend = LiveQueries(
        {"choice": SourceAccess(source)},
        index_factory=lambda: ProjectIndexService(workspace / "index"),
    )
    try:
        yield build_mcp(backend)
    finally:
        backend.close()


def query(live, file, source, target, **limits):
    prefix = "Choose." if file == "Choose.java" else ""
    response = asyncio.run(
        live.call_tool(
            "analyze_code",
            {
                "project_id": "choice",
                "selector": {"path": file, "qualname": prefix + source},
                "target": {"path": file, "qualname": prefix + target},
                "max_depth": 4,
                "max_chars": 25000,
                **limits,
            },
        )
    )
    assert not response.is_error
    return response.structured_content


@pytest.mark.parametrize(
    "file,source,branch_line,exit_line", [("Choose.java", JAVA, 5, 8), ("choice.py", PYTHON, 4, 7)]
)
def test_explicit_branch_and_early_exit_evidence_are_live_only(
    live, file, source, branch_line, exit_line
):
    for name, target, kind, line, outcome in [
        ("split", "left", "branch", branch_line, True),
        ("split", "right", "branch", branch_line, False),
        ("guard", "right", "fallthrough", exit_line, False),
    ]:
        result = query(live, file, name, target)
        assert result["path_found"]
        (edge,) = result["edges"]
        assert edge["conditional"]
        (guard,) = edge["call_control"]["guards"]
        assert (guard["kind"], guard["line"], guard["outcome"]) == (kind, line, outcome)
        assert guard["column"] >= 1
        assert result["path_semantics"]["runtime_reachability"] == "unknown"
    # The historical extractor still stores the same ordinary/bare call facts.
    legacy = parse_source(file, source)
    old = [
        r
        for r in legacy.references
        if r.kind == "CALL" and "guard" in r.source_qualname and r.name == "right"
    ]
    assert len(old) == 1 and not old[0].conditional and old[0].call_control is None
    assert all("call_control" not in r for r in legacy.to_dict()["references"])


def test_nested_early_exit_has_no_invented_required_outcome(live):
    result = query(live, "Choose.java", "nested", "right")
    (edge,) = result["edges"]
    assert edge["conditional"]
    assert edge["call_control"]["guards"] == [{"kind": "prior_exit", "line": 12, "column": 12}]
    inner = query(live, "choice.py", "outer.inner", "right")
    assert "call_control" not in inner["edges"][0]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("first", "conflict_in_all_returned_chains"),
        ("reassigned", "unverified"),
        ("otherParameter", "unverified"),
        ("twoSites", "unverified"),
    ],
)
def test_forwarded_static_boolean_proof_needs_same_stable_slot_and_call_site(live, name, expected):
    result = query(live, "Choose.java", name, "right")
    assert result["path_found"] and result["shortest_depth"] == 2
    assert result["path_semantics"] == {
        "kind": "static_may_call",
        "runtime_reachability": "unknown",
        "guard_consistency": expected,
    }
    if name == "twoSites":
        assert len(result["edges"]) == 3


def test_ambiguous_overloads_and_python_truthiness_remain_unverified(live):
    result = query(live, "Choose.java", "ambiguous", "right")
    assert not result["path_found"]
    assert result["path_semantics"]["guard_consistency"] == "unverified"
    assert any(edge["resolution"] == "ambiguous" for edge in result["unresolved"])
    result = query(live, "choice.py", "first", "right")
    assert result["path_found"] and result["path_semantics"]["guard_consistency"] == "unverified"
    for edge in result["edges"]:
        assert "guards" in edge["call_control"]
    malformed = {
        **result["edges"][0],
        "call_control": {
            "guards": [{"line": 1, "column": 2, "kind": "prior_exit", "text": "unexpected"}]
        },
    }
    with pytest.raises(FactCacheError):
        decode_relation(json.dumps(malformed))


def test_clipped_shortest_path_does_not_retain_unsupported_conflict(live):
    result = query(live, "Choose.java", "first", "right", max_chars=3000)
    assert result["truncated"] and result["path_found"]
    assert {field["field"] for field in result["clipped_fields"]} & {"edges", "paths"}
    assert result["path_semantics"]["guard_consistency"] == "unverified"
