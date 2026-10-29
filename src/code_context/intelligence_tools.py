"""Read-only MCP surface for on-demand Python and Java structure."""

import json
import time
from contextlib import nullcontext
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic.json_schema import SkipJsonSchema

from code_context.analysis_diff import bound_diff_result
from code_context.analysis_response import AnalysisRequestError, AnalysisResponseBudgetExceeded
from code_context.analysis_routing import AnalysisMode, resolve_analysis_mode
from code_context.intelligence_analysis import (
    AnalysisSelector,
    StructureInclude,
    bound_analysis_result,
)
from code_context.intelligence_queries import bound_result
from code_context.live_dataflow import FlowSelector
from code_context.mirror_intelligence_tools import register_mirror_code_tools
from code_context.read_code import execute_read_code
from code_context.storage import MirrorError

CODE_TOOL_NAMES = frozenset(
    {
        "symbol_search",
        "read_symbol",
        "find_references",
        "get_call_graph",
        "get_class_graph",
        "get_file_dependencies",
        "get_project_architecture",
        "get_external_dependencies",
        "get_impact_analysis",
    }
)
ADVANCED_CODE_TOOL_NAMES = frozenset({"analyze_code"})
LIVE_CODE_TOOL_NAMES = frozenset(
    {
        "find_references",
        "get_project_architecture",
        "get_external_dependencies",
        "get_impact_analysis",
    }
)


def register_analysis_tool(mcp, store, authorize, display_name, annotations):
    """Register only against Live sources; legacy snapshot tools remain unchanged."""

    @mcp.tool(annotations=annotations, structured_output=True)
    def analyze_code(
        project_id: str,
        mode: SkipJsonSchema[AnalysisMode | None] = None,
        selector: AnalysisSelector | FlowSelector | None = None,
        snapshot: str | None = None,
        target: AnalysisSelector | FlowSelector | None = None,
        include: list[StructureInclude | Literal["source"]] | None = None,
        direction: Literal["outgoing", "incoming", "both"] = "both",
        max_depth: int = 1,
        max_nodes: int = 50,
        max_edges: int = 100,
        max_chars: int = 12_000,
        max_ms: int = 3000,
        offset: int = 0,
        limit: int = 20,
        include_type_only: bool = True,
        baseline: Literal["previous", "empty"] = "previous",
        detail: Literal["summary", "patch"] = "summary",
    ) -> dict[str, Any]:
        """Analyze saved source from one explicit selector; no mode choice is needed.
        Select {path} for file context or {symbol_id}/{path,qualname} for a symbol.
        Add value={kind:parameter,name}, {kind:return,line?,column?} or
        {kind:occurrence,line,column} for bounded value flow. With one value, direction
        incoming selects origins, outgoing selects impact, both selects combined context.
        A target exact symbol requests a directed call path; a target value requests
        a value path. Select values at both endpoints; missing values are never guessed.
        Repeated definitions return candidates rather than a guessed symbol. include may request
        ownership, calls and dependencies on any analysis: path/value views attach this evidence
        as context without changing the primary analysis. Include source for actual saved text.
        Symbol pages use next_offset; clipped graph sections have their own continuation guidance.
        Include source to receive bounded actual text with the same validated context and shared
        response/time budget. Source evidence needs max_chars >= 3000. Ambiguous definitions
        require selection; source is never picked from the first candidate.
        Java class.method may omit its parameter signature when unique; overloads return
        candidates. Unmatched symbols return selection_not_found with current-symbol search
        guidance; stale identifiers are never rebound to another definition.
        Absence of a static path is not proof of absence of runtime behavior. Budgeted results
        expose partial coverage and continuation, never execute source or change permissions.
        Value columns use one-based UTF-8 byte offsets. File/symbol-only requests do not
        start value flow. Values and contexts are local to one request; use saved positions
        to continue. Paths are directed and bounded to at most five hops.
        Loops, exceptions, recursion, dynamic calls and unknown side effects report partial
        coverage. Analysis never imports the project and is not a runtime execution trace.
        Existing explicit mode requests remain a validated compatibility interface.
        Task diff compares a retained write origin with current files; baseline/detail
        apply only to that compatibility request. NO_TASK_BASELINE means unavailable.
        Source reads, references and reverse file-dependency impact keep distinct tools.
        """
        authorize(project_id)
        try:
            mode = resolve_analysis_mode(selector, target, mode, direction)
            if mode == "diff":
                if target is not None or include is not None or isinstance(selector, FlowSelector):
                    raise AnalysisRequestError("diff accepts only an optional path selector")
                if selector is not None and (
                    selector.symbol_id is not None or selector.qualname is not None
                ):
                    raise AnalysisRequestError("diff accepts only an optional path selector")
                result = store.get_recent_diff(
                    project_id,
                    snapshot,
                    selector.path if selector is not None else None,
                    baseline=baseline,
                    detail=detail,
                    offset=offset,
                    limit=limit,
                    max_chars=max_chars,
                )
                return bound_diff_result(
                    {**result, "mode": mode, "display_name": display_name(project_id)}, max_chars
                )
            if selector is None:
                raise AnalysisRequestError("this analysis requires a selector")
            if baseline != "previous" or detail != "summary":
                raise AnalysisRequestError("baseline and detail are only supported by task diff")
            if include is not None and len(include) != len(set(include)):
                raise AnalysisRequestError("include values must be unique")
            with_source = include is not None and "source" in include
            if with_source and max_chars < 3000:
                raise AnalysisRequestError("source evidence requires max_chars of at least 3000")
            structural_include = (
                [item for item in include if item != "source"] if include is not None else None
            )
            if structural_include == [] and mode not in {"file_context", "symbol_context"}:
                structural_include = None
            source_budget = max(1000, (max_chars - 256) // 3) if with_source else 0
            analysis_budget = max_chars - source_budget - 256 if with_source else max_chars
            started = time.monotonic()
            options = {}
            if mode not in {"data_origin", "data_impact", "data_path", "combined"}:
                options["include_type_only"] = include_type_only
            context = (
                store.context_request(project_id, snapshot)
                if hasattr(store, "context_request")
                else nullcontext((snapshot, snapshot))
            )
            with context as (_, handle):
                result = store.code_query(
                    project_id,
                    handle,
                    "analyze_code",
                    mode=mode,
                    selector=selector.model_dump(exclude_none=True),
                    target=target.model_dump(exclude_none=True) if target is not None else None,
                    include=structural_include,
                    direction=direction,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                    max_chars=analysis_budget,
                    max_ms=max_ms,
                    offset=offset,
                    limit=limit,
                    **options,
                )
                result = bound_analysis_result(
                    {**result, "display_name": display_name(project_id)}, analysis_budget
                )
                if with_source:
                    remaining_ms = max_ms - int((time.monotonic() - started) * 1000)
                    if result.get("analysis_status") == "selection_not_found":
                        source = {"read_status": "selection_not_found"}
                    elif (
                        result.get("analysis_status") == "ambiguous"
                        and result.get("ambiguous_selector") != "target"
                    ):
                        source = {
                            "analysis_status": "ambiguous",
                            "selection_required": "choose one candidate symbol_id",
                        }
                    elif remaining_ms < 100:
                        source = {
                            "analysis_status": "partial",
                            "truncated_reason": ["max_ms"],
                        }
                    else:
                        selected = selector.model_dump(exclude_none=True)
                        if selected.get("symbol_id") is not None:
                            request = {"mode": "symbol", "symbol_id": selected["symbol_id"]}
                        elif selected.get("qualname") is not None:
                            request = {
                                "mode": "symbol",
                                "path": selected["path"],
                                "qualname": selected["qualname"],
                            }
                        else:
                            request = {"mode": "file", "path": selected["path"]}
                        source = execute_read_code(
                            store,
                            project_id,
                            request,
                            result.get("snapshot", handle),
                            max_chars=source_budget,
                            max_ms=remaining_ms,
                        )
                    result["source"] = source
                    if result.get("analysis_status") not in {"target_not_found", "ambiguous"} and (
                        source.get("analysis_status") == "partial"
                        or source.get("read_status") in {"partial", "index_not_ready"}
                        or source.get("content_truncated")
                    ):
                        result["analysis_status"] = "partial"
                        result["truncated"] = True
                        result.setdefault("truncated_reason", []).append("source_budget")
                    if len(json.dumps(result, ensure_ascii=False)) > max_chars:
                        raise AnalysisResponseBudgetExceeded
                    if hasattr(store, "resolve_snapshot"):
                        store.resolve_snapshot(project_id, result.get("snapshot", handle))
                return result
        except AnalysisResponseBudgetExceeded:
            raise ToolError(
                "ANALYSIS_RESPONSE_BUDGET_EXCEEDED: narrow the query or increase max_chars"
            ) from None
        except (MirrorError, ValueError) as exc:
            raise ToolError(str(exc)) from None


def register_code_tools(mcp, store, authorize, display_name, annotations):
    def query(project_id, snapshot, operation, max_chars, **parameters):
        authorize(project_id)
        try:
            context = (
                store.context_request(project_id, snapshot)
                if store.source_mode == "live" and hasattr(store, "context_request")
                else nullcontext((snapshot, snapshot))
            )
            with context as (_, handle):
                result = store.code_query(
                    project_id, handle, operation, max_chars=max_chars, **parameters
                )
                return bound_result({**result, "display_name": display_name(project_id)}, max_chars)
        except MirrorError as exc:
            raise ToolError(str(exc)) from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    query_result = query
    if store.source_mode != "live":
        register_mirror_code_tools(mcp, query, annotations)

    @mcp.tool(annotations=annotations, structured_output=True)
    def find_references(
        project_id: str,
        symbol_id: str,
        snapshot: str | None = None,
        offset: int = 0,
        limit: int = 50,
        include_imports: bool = True,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Find statically bound uses/imports/calls/types of a selected definition. Not a
        substring search: shadowed same-name variables are excluded. Dynamic/ambiguous uses may
        be absent, so absence is not proof of no runtime references. Read relevant source lines.
        """
        return query(
            project_id,
            snapshot,
            "find_references",
            max_chars,
            symbol_id=symbol_id,
            offset=offset,
            limit=limit,
            include_imports=include_imports,
        )

    @mcp.tool(annotations=annotations, structured_output=True)
    def get_project_architecture(
        project_id: str,
        snapshot: str | None = None,
        path_prefix: str | None = None,
        max_nodes: int = 100,
        max_edges: int = 200,
        include_type_only: bool = False,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Get bounded dependency-first file layers and cycle groups, not a giant UML
        dump. Layer zero has no resolved local dependencies outside its cycle group;
        cycles are condensed for ordering.
        These are static structural facts, not inferred business architecture. Narrow by path.
        """
        return query(
            project_id,
            snapshot,
            "project_architecture",
            max_chars,
            path_prefix=path_prefix,
            max_nodes=max_nodes,
            max_edges=max_edges,
            include_type_only=include_type_only,
        )

    @mcp.tool(annotations=annotations, structured_output=True)
    def get_external_dependencies(
        project_id: str,
        snapshot: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 20,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """List imported modules absent from the mirror and the files using them.
        Candidates may be external libraries, excluded local modules or typos; classification
        is explicit. Does not inspect installed packages/versions, fetch code, or run a resolver.
        """
        return query_result(
            project_id,
            snapshot,
            "external_dependencies",
            max_chars,
            query=query,
            offset=offset,
            limit=limit,
        )

    @mcp.tool(annotations=annotations, structured_output=True)
    def get_impact_analysis(
        project_id: str,
        path: str,
        snapshot: str | None = None,
        symbol_id: str | None = None,
        depth: int = 3,
        max_nodes: int = 50,
        max_edges: int = 100,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Identify potential reverse dependency impact before planning a change. May
        narrow direct evidence to a symbol; transitive scope remains a conservative file-level
        superset. Not a guarantee of affected runtime behavior or a substitute for tests.
        Read-only: no edits, commands, backups or Git writes.
        """
        return query(
            project_id,
            snapshot,
            "impact_analysis",
            max_chars,
            path=path,
            symbol_id=symbol_id,
            depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
