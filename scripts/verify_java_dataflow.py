"""Small Java value-flow acceptance through the real Live and MCP paths.

Creates a new private fixture/index directory and retains every input/receipt.
This is targeted functional acceptance, not a regression suite or Java runtime
execution. Use --prepare-only while an engine integration is still in progress.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from code_context.live import LiveQueries
from code_context.live_index import LiveIndexService
from code_context.server import build_mcp
from code_context.source_access import SourceAccess

REPO = Path(__file__).resolve().parents[1]
PROJECT = "java-functional"
FLOW = "src/main/java/demo/Flow.java"
HELPERS = "src/main/java/demo/Helpers.java"
INSTANCE = "src/main/java/demo/InstanceBox.java"
FIXTURES = {
    FLOW: """package demo;
public final class Flow {
    public static int copy(int value) {
        int current = value;
        return current;
    }
    public static int via(int value) {
        /* 中文 */ return Helpers.echo(value);
    }
    public static int branch(int value, boolean choose) {
        int current = value;
        if (choose) {
            current = value + 1;
        } else {
            current = 0;
        }
        return current;
    }
    public static int unknown(int value) {
        return UnknownLibrary.convert(value);
    }
    public static int overwrite(int oldValue, int newValue) {
        int current = oldValue;
        current = newValue;
        return current;
    }
    public static int viaInstance(InstanceBox box, int value) {
        return box.echo(value);
    }
}
""",
    HELPERS: """package demo;
public final class Helpers {
    public static int echo(int value) {
        return value;
    }
}
""",
    INSTANCE: """package demo;
public final class InstanceBox {
    public int echo(int value) {
        int current = value;
        return current;
    }
}
""",
}
METHODS = {
    "copy": (FLOW, "copy"),
    "via": (FLOW, "via"),
    "branch": (FLOW, "branch"),
    "unknown": (FLOW, "unknown"),
    "overwrite": (FLOW, "overwrite"),
    "via_instance": (FLOW, "viaInstance"),
    "helper": (HELPERS, "echo"),
    "instance_echo": (INSTANCE, "echo"),
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def flow_graph(response):
    return response.get("dataflow", response)


def flow_diagnostics(response):
    return flow_graph(response).get("diagnostics", [])


def assert_java_available(response):
    graph = flow_graph(response)
    require(graph.get("analysis_status") in {"ready", "partial"}, "Java value selection failed")
    coverage = graph.get("coverage", {})
    require(
        coverage.get("requested_language", coverage.get("language")) == "java",
        "response did not identify the requested Java language",
    )
    require(
        coverage.get("runtime_trace") is False, "static analysis must not claim a runtime trace"
    )
    require(
        not any(item.get("code") == "PYTHON_ONLY" for item in flow_diagnostics(response)),
        "Java still returned PYTHON_ONLY",
    )
    return graph


def assert_path(response, *, expected=True):
    graph = assert_java_available(response)
    require(graph.get("path_found") is expected, f"expected path_found={expected}")
    if expected:
        require(graph.get("paths"), "path_found did not include an actual value path")
    else:
        require(
            graph.get("conclusion", {}).get("absence_proven") is False,
            "a missing static path must not claim runtime absence",
        )
    return graph


def assert_partial(response):
    graph = assert_java_available(response)
    require(graph.get("analysis_status") == "partial", "uncertainty was not marked partial")
    diagnostics = graph.get("diagnostics", [])
    require(diagnostics, "partial uncertainty did not retain diagnostics")
    require(
        all(item.get("code") and item.get("category") for item in diagnostics),
        "uncertainty diagnostics were not categorized",
    )
    require(
        graph.get("diagnostic_summary", {}).get("by_category"),
        "partial uncertainty did not retain its category summary",
    )
    return graph


def find_symbol(backend, snapshot, path, name, *, language="java"):
    response = backend.code_query(
        PROJECT,
        snapshot,
        "symbol_search",
        query=name,
        exact=True,
        language=language,
        path_prefix=path,
        limit=20,
    )
    candidates = [item for item in response.get("symbols", []) if item["path"] == path]
    require(len(candidates) == 1, f"expected one actual definition for {path}:{name}")
    return candidates[0], response


async def verify(args, output, receipt):
    source_root = output / "source"
    source_root.mkdir(mode=0o700)
    fixtures = dict(FIXTURES)
    if args.python_smoke:
        fixtures["smoke.py"] = "def echo(value):\n    return value\n"
    for relative, content in fixtures.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        data = path.read_bytes()
        receipt["fixtures"].append(
            {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        )
    backend = LiveQueries({PROJECT: SourceAccess(source_root)})
    service = LiveIndexService(output / "index", wait_seconds=10)
    backend.index_service = service
    try:
        overview = backend.repo_overview(PROJECT, None)
        snapshot = overview["snapshot"]
        receipt["repo_overview"] = overview
        symbols = {}
        for key, (path, name) in METHODS.items():
            symbol, response = find_symbol(backend, snapshot, path, name)
            symbols[key] = symbol
            receipt["symbol_searches"].append({"key": key, "response": response})
        if args.python_smoke:
            symbol, response = find_symbol(backend, snapshot, "smoke.py", "echo", language="python")
            symbols["python_smoke"] = symbol
            receipt["symbol_searches"].append({"key": "python_smoke", "response": response})
        receipt["symbols"] = symbols
        receipt["structural_call_preflight"] = [
            {
                "method": key,
                "response": backend.code_query(
                    PROJECT,
                    snapshot,
                    "analyze_code",
                    mode="callees",
                    selector={"symbol_id": symbols[key]["symbol_id"]},
                    max_depth=1,
                    max_nodes=20,
                    max_edges=20,
                    max_chars=12000,
                ),
            }
            for key in ("via", "via_instance")
        ]
        mcp = build_mcp(backend, project_names={PROJECT: "Java functional fixtures"})
        tool = next(tool for tool in await mcp.list_tools() if tool.name == "analyze_code")
        receipt["analyze_schema"] = tool.input_schema
        require(
            all(
                mode in json.dumps(tool.input_schema)
                for mode in ("data_origin", "data_impact", "data_path", "combined")
            ),
            "existing analyze_code schema is missing a required mode",
        )
        if args.prepare_only:
            receipt.update(phase="prepared", functional_results_executed=False)
            return True

        def selector(method, kind="parameter", name="value"):
            value = {"kind": kind}
            if kind == "parameter":
                value["name"] = name
            return {"symbol_id": symbols[method]["symbol_id"], "value": value}

        async def invoke(case, mode, selected, target=None):
            arguments = {
                "project_id": PROJECT,
                "snapshot": snapshot,
                "mode": mode,
                "selector": selected,
                "max_depth": 2,
                "max_nodes": 150,
                "max_edges": 250,
                "max_chars": 30000,
                "max_ms": 3000,
            }
            if target is not None:
                arguments["target"] = target
            call = {"arguments": arguments}
            case["calls"].append(call)
            started = time.perf_counter()
            try:
                response = await mcp.call_tool("analyze_code", arguments)
            except Exception as error:
                call["error"] = {"type": type(error).__name__, "message": str(error)[:1000]}
                raise
            finally:
                call["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            call["mcp_result"] = response.model_dump(mode="json", by_alias=True)
            structured = response.structured_content
            require(isinstance(structured, dict), "MCP did not return structured content")
            call["response"] = structured
            require(
                len(json.dumps(structured, ensure_ascii=False)) <= arguments["max_chars"],
                "MCP response exceeded its complete JSON character budget",
            )
            return structured

        async def run_case(identifier, expected, operation):
            case = {"id": identifier, "expected": expected, "calls": []}
            receipt["cases"].append(case)
            try:
                await operation(case)
                case["passed"] = True
            except Exception as error:
                case.update(
                    passed=False,
                    failure={"type": type(error).__name__, "message": str(error)[:1000]},
                )

        async def local_path(case):
            response = await invoke(case, "data_path", selector("copy"), selector("copy", "return"))
            graph = assert_path(response)
            kinds = {item["kind"] for item in graph["edges"]}
            require({"ASSIGN", "RETURN_FLOW"} <= kinds, "local assignment/return evidence missing")

        async def local_origin(case):
            graph = assert_java_available(
                await invoke(case, "data_origin", selector("copy", "return"))
            )
            require(
                any(
                    node["kind"] == "parameter" and node["name"] == "value"
                    for node in graph["nodes"]
                ),
                "return origin did not reach its actual Java parameter",
            )

        async def local_impact(case):
            graph = assert_java_available(await invoke(case, "data_impact", selector("copy")))
            require(
                any(node["kind"] == "return" for node in graph["nodes"]),
                "parameter did not reach return",
            )

        async def cross_file(case):
            graph = assert_path(
                await invoke(case, "data_path", selector("via"), selector("via", "return"))
            )
            kinds = {item["kind"] for item in graph["edges"]}
            require(
                {"ARG_TO_PARAM", "RETURN_TO_CALLSITE"} <= kinds, "cross-file call links missing"
            )
            require(
                any(node["path"] == HELPERS for node in graph["nodes"]),
                "helper source was not traversed",
            )

        async def combined(case):
            response = await invoke(case, "combined", selector("via"))
            graph = assert_java_available(response)
            require(graph["nodes"] and graph["edges"], "combined has no value evidence")
            require(
                response.get("structure", {}).get("symbol", {}).get("symbol_id")
                == symbols["via"]["symbol_id"],
                "combined lost exact method structure",
            )

        async def uncertainty(case):
            for method in ("branch", "unknown"):
                response = await invoke(case, "data_impact", selector(method))
                assert_partial(response)

        async def overwritten_assignment(case):
            target = selector("overwrite", "return")
            assert_path(
                await invoke(case, "data_path", selector("overwrite", name="oldValue"), target),
                expected=False,
            )
            assert_path(
                await invoke(case, "data_path", selector("overwrite", name="newValue"), target)
            )

        async def instance_first_parameter(case):
            direct = await invoke(
                case, "data_path", selector("instance_echo"), selector("instance_echo", "return")
            )
            assert_path(direct)
            impact = assert_java_available(
                await invoke(case, "data_impact", selector("via_instance"))
            )
            impact_nodes = {node["id"]: node for node in impact.get("nodes", [])}
            impact_links = [
                edge
                for edge in impact.get("edges", [])
                if edge["kind"] == "ARG_TO_PARAM"
                and impact_nodes.get(edge["target"], {}).get("symbol_id")
                == symbols["instance_echo"]["symbol_id"]
            ]
            require(
                impact_links,
                "instance impact slice lacks argument-to-parameter binding for the first value",
            )
            require(
                all(impact_nodes[edge["target"]]["name"] == "value" for edge in impact_links),
                "instance impact bound the first argument to a receiver instead of value",
            )
            response = await invoke(
                case, "data_path", selector("via_instance"), selector("via_instance", "return")
            )
            graph = assert_java_available(response)
            nodes = {node["id"]: node for node in graph.get("nodes", [])}
            links = [
                edge
                for edge in graph.get("edges", [])
                if edge["kind"] == "ARG_TO_PARAM"
                and nodes.get(edge["target"], {}).get("symbol_id")
                == symbols["instance_echo"]["symbol_id"]
            ]
            require(
                all(nodes[edge["target"]]["name"] == "value" for edge in links),
                "Java first parameter was bound as a receiver",
            )
            if graph.get("path_found") and graph.get("path_resolution") == "static":
                require(links, "instance call path lacks its explicit argument-to-parameter edge")
                case["instance_call_scope"] = "resolved_static_candidate_path"
            else:
                assert_partial(response)
                codes = [item["code"] for item in flow_diagnostics(response)]
                require(
                    any(
                        code == "JAVA_DISPATCH_PARTIAL"
                        or "DYNAMIC" in code
                        or "UNKNOWN_CALL" in code
                        or "UNRESOLVED_CALL" in code
                        for code in codes
                    ),
                    "missing instance path was not explained as unresolved dispatch",
                )
                case["instance_call_scope"] = "dispatch_partial; value_argument_binding_verified"

        scenarios = (
            (
                "local_data_path",
                "parameter -> local -> return has assignment and return edges",
                local_path,
            ),
            ("local_data_origin", "return origin includes its actual parameter", local_origin),
            ("local_data_impact", "parameter impact includes return", local_impact),
            (
                "cross_file_static_call",
                "resolved static helper links argument and return across files; UTF-8 columns",
                cross_file,
            ),
            (
                "combined_structure_and_flow",
                "existing combined mode retains exact method structure and value graph",
                combined,
            ),
            (
                "classified_uncertainty",
                "branch and unknown library call remain partial with categorized diagnostics",
                uncertainty,
            ),
            (
                "assignment_overwrite",
                "old input has no current return path; new input has one",
                overwritten_assignment,
            ),
            (
                "instance_first_parameter",
                "first Java instance parameter remains a value; unresolved dispatch is explicit",
                instance_first_parameter,
            ),
        )
        receipt.update(phase="functional", functional_results_executed=True)
        for identifier, expected, operation in scenarios:
            await run_case(identifier, expected, operation)
        if args.python_smoke:

            async def smoke(case):
                response = await invoke(
                    case, "data_path", selector("python_smoke"), selector("python_smoke", "return")
                )
                require(
                    response.get("path_found") is True,
                    "existing simple Python parameter return path failed",
                )

            await run_case(
                "optional_python_smoke",
                "one existing Python parameter -> return path remains available",
                smoke,
            )
        return all(case["passed"] for case in receipt["cases"])
    finally:
        receipt["index_status"] = service.status(PROJECT)
        backend.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="new .artifacts/java-dataflow-* directory"
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="write sources and locate definitions; do not run value modes",
    )
    parser.add_argument(
        "--python-smoke",
        action="store_true",
        help="also exercise one existing Python parameter-return example",
    )
    args = parser.parse_args()
    artifacts = REPO / ".artifacts"
    output = args.output.resolve()
    if (
        output.exists()
        or output.parent != artifacts.resolve()
        or not output.name.startswith("java-dataflow-")
    ):
        parser.error(
            "--output must be a new directory directly under .artifacts named java-dataflow-*"
        )
    artifacts.mkdir(exist_ok=True)
    output.mkdir(mode=0o700)
    receipt = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_kind": "small explicit Java fixtures, not a real-business or scale benchmark",
        "scope": "bounded static may-flow through existing LiveQueries and build_mcp.call_tool",
        "project_code_executed": False,
        "installed_application_changed": False,
        "regression_suite_run": False,
        "output": str(output),
        "python": sys.version,
        "fixtures": [],
        "symbol_searches": [],
        "cases": [],
    }
    passed = False
    try:
        passed = asyncio.run(verify(args, output, receipt))
    except Exception as error:
        receipt.update(
            phase="failed", failure={"type": type(error).__name__, "message": str(error)[:1000]}
        )
    receipt["passed"] = passed
    receipt["functional_cases_passed"] = sum(case.get("passed", False) for case in receipt["cases"])
    receipt["functional_cases_total"] = len(receipt["cases"])
    path = output / "receipt.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "phase": receipt["phase"],
                "passed": passed,
                "cases_passed": receipt["functional_cases_passed"],
                "cases_total": receipt["functional_cases_total"],
                "receipt": str(path),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
