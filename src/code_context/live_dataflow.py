"""Request-local Python/Java flow over one guarded Live fact publication."""

from __future__ import annotations

import ast
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from code_context.analysis_context import attach_context, structure_evidence
from code_context.analysis_response import AnalysisRequestError
from code_context.dataflow_diagnostics import annotate_dataflow_result
from code_context.intelligence_analysis import (
    AnalysisBudgetExceeded,
    AnalysisSelector,
    apply_analysis_bounds,
    bound_analysis_result,
    select_symbol,
)
from code_context.intelligence_dataflow import DataflowBudgetExceeded, analyze_dataflow
from code_context.intelligence_queries import _symbol, decode_relation
from code_context.source_access import SourceError

DATAFLOW_MODES = frozenset({"data_origin", "data_impact", "data_path", "combined"})
_RELATION_LIMIT = 5000


class FlowValueSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["parameter", "return", "occurrence"]
    name: str | None = Field(default=None, min_length=1, max_length=200)
    line: int | None = Field(default=None, ge=1, le=1_000_000)
    column: int | None = Field(default=None, ge=1, le=4_194_304)

    @model_validator(mode="after")
    def valid_value(self):
        if self.kind == "parameter":
            if self.name is None or not self.name.replace("$", "_").isidentifier():
                raise ValueError("select a parameter identifier")
            if self.line is not None or self.column is not None:
                raise ValueError("parameter selection uses its name within one method")
        elif self.name is not None:
            raise ValueError("source occurrence selection uses line and column")
        if self.kind == "occurrence" and (self.line is None or self.column is None):
            raise ValueError("select a positive source line and UTF-8 byte column")
        if self.column is not None and self.line is None:
            raise ValueError("column requires a source line")
        return self


class FlowSelector(AnalysisSelector):
    value: FlowValueSelector


class FlowSources:
    """Detach facts under the adapter lock and read/parse saved source outside it."""

    def __init__(self, facts, project_id, revision, source, observe):
        self.facts, self.project_id, self.revision = facts, project_id, revision
        self.source, self.observe = source, observe
        self.sources = {}
        self.relations_partial = False
        self.ast_cache = getattr(facts.service, "dataflow_ast_cache", None)

    def load_symbol(self, identifier):
        try:
            return _symbol(self.facts, self.project_id, self.revision, identifier)
        except AnalysisBudgetExceeded:
            raise DataflowBudgetExceeded from None

    def load_source(self, path):
        try:
            self.facts._budget()
            if path in self.sources:
                return self.sources[path]
            indexed = self.facts.execute(
                "SELECT sha256, fingerprint FROM files "
                "WHERE project_id=? AND revision=? AND path=?",
                (self.project_id, self.revision, path),
            ).fetchone()
            document = self.source.read(path)
            if (
                indexed is None
                or document.sha256 != indexed["sha256"]
                or tuple(document.version) != tuple(json.loads(indexed["fingerprint"]))
            ):
                raise SourceError("LIVE_INDEX_CHANGED: data-flow source changed; restart analysis")
            self.observe(document.path, document.sha256)
            self.facts._budget()
            self.sources[path] = document.content
            return document.content
        except AnalysisBudgetExceeded:
            raise DataflowBudgetExceeded from None

    def load_relations(self, identifier, *, direction):
        if direction not in {"incoming", "outgoing"}:
            raise ValueError("invalid data-flow relation direction")
        column, index = (
            ("source_symbol_id", "ci_relation_source")
            if direction == "outgoing"
            else ("target_symbol_id", "ci_relation_target")
        )
        try:
            rows = self.facts.execute(
                f"SELECT data FROM ci_relations INDEXED BY {index} "
                f"WHERE project_id=? AND revision=? AND {column}=? "
                "AND kind IN ('CALL','INSTANTIATION') ORDER BY source_path,line,kind LIMIT ?",
                (self.project_id, self.revision, identifier, _RELATION_LIMIT + 1),
            ).fetchall()
            self.relations_partial |= len(rows) > _RELATION_LIMIT
            result = []
            for row in rows[:_RELATION_LIMIT]:
                self.facts._budget()
                result.append(decode_relation(row["data"]))
            return result
        except AnalysisBudgetExceeded:
            raise DataflowBudgetExceeded from None

    def load_ast(self, path, text, check):
        # Reuse can only follow this request's real, indexed-source read. Cache
        # hits never substitute for its SHA, fingerprint or final live guard.
        if path not in self.sources or self.sources[path] != text:
            raise ValueError("dataflow AST requires current validated source")
        if self.ast_cache is None:
            return ast.parse(text, filename="<CoLink Python data flow>")
        return self.ast_cache.parse(
            project_id=self.project_id,
            source_id=self.source.source_id,
            source_identity=self.source.stable_id,
            path=path,
            text=text,
            check=check,
        )


def _resolved(db, project_id, revision, selector, *, offset, limit):
    try:
        selected = FlowSelector.model_validate(selector, strict=True)
    except ValidationError:
        raise AnalysisRequestError(
            "data-flow selection requires an exact method and value location"
        ) from None
    symbol, ambiguity = select_symbol(
        db,
        project_id,
        revision,
        selected.model_dump(exclude={"value"}, exclude_none=True),
        offset=offset,
        limit=limit,
    )
    return symbol, ambiguity


def _compact_flow_output(result):
    """Keep limitation evidence usable when the minimum budget cannot hold a graph."""
    graph = result.get("dataflow", result)
    graph["nodes"], graph["edges"] = [], []
    graph["root"] = None
    if "target" in graph:
        graph["target"] = None
    if "paths" in graph:
        graph["paths"], graph["paths_truncated"] = [], True
    if not graph.get("candidates"):
        for key in (
            "candidates",
            "candidate_total",
            "candidates_truncated",
            "total",
            "offset",
            "has_more",
            "next_offset",
            "value_selection_candidates",
            "selection_required",
        ):
            graph.pop(key, None)
    graph["diagnostics"] = [
        {key: item[key] for key in ("code", "category")}
        for item in graph.get("diagnostics", [])[:1]
    ]
    graph["coverage"] = {
        key: value
        for key, value in graph["coverage"].items()
        if key in {"language", "requested_language", "availability", "runtime_trace"}
    }
    graph["truncated"] = True
    graph.setdefault("truncated_reason", []).append("max_chars")
    graph["truncated_reason"] = list(dict.fromkeys(graph["truncated_reason"]))
    if graph["analysis_status"] == "ready":
        graph["analysis_status"] = "partial"
    annotate_dataflow_result(graph)
    # Omitted evidence must not look like a disproven path or a completed trace.
    if graph["coverage"]["availability"] == "available":
        graph["conclusion"]["status"] = "output_evidence_omitted"
    graph["diagnostics"] = [
        {key: item[key] for key in ("code", "category")} for item in graph["diagnostics"]
    ]
    result.pop("continuation", None)
    if "dataflow" in result:
        result["structure"] = {
            "analysis_status": "not_computed"
            if result["structure"]["analysis_status"] == "not_computed"
            else "partial",
            "evidence_omitted": True,
        }
        result.pop("coverage", None)
        result["analysis_status"], result["truncated"] = "partial", True
        result["truncated_reason"] = list(dict.fromkeys([*result["truncated_reason"], "max_chars"]))
    return result


def analyze_live_dataflow(
    facts,
    project_id,
    revision,
    *,
    source,
    observe,
    mode,
    selector,
    target=None,
    include=None,
    direction="both",
    max_depth=1,
    max_nodes=50,
    max_edges=100,
    max_chars=12_000,
    max_ms=3000,
    offset=0,
    limit=20,
):
    """One static slice with strict selection, shared deadline and output budgets."""
    if mode not in DATAFLOW_MODES:
        raise AnalysisRequestError("unsupported data-flow mode")
    for name, value, low, high in (
        ("max_depth", max_depth, 0, 5),
        ("max_nodes", max_nodes, 1, 200),
        ("max_edges", max_edges, 1, 500),
        ("max_chars", max_chars, 1000, 50_000),
        ("max_ms", max_ms, 100, 10_000),
        ("limit", limit, 1, 100),
        ("offset", offset, 0, 1_000_000),
    ):
        if type(value) is not int or not low <= value <= high:
            raise AnalysisRequestError(f"invalid {name} bounds")
    if direction not in {"both", "incoming", "outgoing"}:
        raise AnalysisRequestError("invalid analysis direction")
    if include is not None and (
        not isinstance(include, list)
        or any(item not in {"ownership", "calls", "dependencies"} for item in include)
        or len(include) != len(set(include))
    ):
        raise AnalysisRequestError(
            "include must contain unique ownership, calls or dependencies values"
        )
    if target is not None and mode != "data_path":
        raise AnalysisRequestError("target is only supported by data_path")
    if mode == "data_path" and target is None:
        raise AnalysisRequestError("data_path requires a target value selector")
    try:
        symbol, ambiguity = _resolved(
            facts, project_id, revision, selector, offset=offset, limit=limit
        )
        if ambiguity is not None:
            return annotate_dataflow_result({"mode": mode, **ambiguity})
        resolved = {"symbol_id": symbol["symbol_id"], "value": selector["value"]}
        resolved_target = None
        if target is not None:
            end, ambiguity = _resolved(
                facts, project_id, revision, target, offset=offset, limit=limit
            )
            if ambiguity is not None:
                result = {"mode": mode, **ambiguity, "selection_scope": "target"}
                if ambiguity["analysis_status"] == "ambiguous":
                    result["ambiguous_selector"] = "target"
                else:
                    result["analysis_status"] = "target_not_found"
                return annotate_dataflow_result(result)
            resolved_target = {"symbol_id": end["symbol_id"], "value": target["value"]}
    except AnalysisBudgetExceeded:
        return annotate_dataflow_result(
            {
                "mode": mode,
                "analysis_status": "partial",
                "nodes": [],
                "edges": [],
                "truncated": True,
                "truncated_reason": ["time_budget"],
                "selection_status": "selection_not_computed",
            }
        )
    provider = FlowSources(facts, project_id, revision, source, observe)
    remaining_ms = max(1, min(max_ms, int((facts.deadline - time.monotonic()) * 1000)))
    flow = analyze_dataflow(
        mode=mode,
        selector=resolved,
        target=resolved_target,
        load_symbol=provider.load_symbol,
        load_source=provider.load_source,
        load_relations=provider.load_relations,
        load_ast=provider.load_ast,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        max_ms=remaining_ms,
    )
    if flow["analysis_status"] == "complete":
        flow["analysis_status"] = "ready"
    if flow["analysis_status"] == "selection_ambiguous":
        flow["analysis_status"] = "ambiguous"
    if "candidates" in flow:
        candidates = flow["candidates"]
        flow["total"] = flow.get("candidate_total", len(candidates))
        flow["offset"] = 0
        flow["has_more"] = len(candidates) < flow["total"]
        # Value candidates are source occurrences, not a stored cursor. Clients
        # narrow the returned line/column; symbol ambiguity uses standard paging.
        flow["next_offset"] = None
        flow["value_selection_candidates"] = True
        flow["selection_required"] = "select one exact saved source occurrence"
    flow["truncated_reason"] = flow.pop("truncated_reasons", [])
    if provider.relations_partial:
        flow["analysis_status"] = "partial"
        flow["truncated"] = True
        flow["truncated_reason"].append("relation_facts_budget")
    annotate_dataflow_result(flow)
    if mode == "combined" or include:
        structure = structure_evidence(
            facts,
            project_id,
            revision,
            selector={"symbol_id": symbol["symbol_id"]},
            include=["ownership", "calls", "dependencies"] if include is None else include,
            direction=direction,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_chars=max_chars,
            deadline=facts.deadline,
        )
    if mode == "combined":
        flow = {
            "mode": mode,
            "analysis_status": "ready"
            if structure["analysis_status"] == flow["analysis_status"] == "ready"
            else "partial",
            "structure": structure,
            "dataflow": flow,
            "truncated": structure.get("truncated", False) or flow.get("truncated", False),
            "truncated_reason": list(
                dict.fromkeys([*structure["truncated_reason"], *flow["truncated_reason"]])
            ),
            "coverage": "static Python/Java structure and bounded may-flow; not a runtime trace",
        }
    elif include:
        attach_context(flow, structure)
    flow["continuation"] = {"selector": selector, "value_ids_are_request_local": True}
    if max_chars < 1500:
        return bound_analysis_result(_compact_flow_output(flow), max_chars)
    bounded = apply_analysis_bounds(flow, max_nodes, max_edges)
    if mode != "combined":
        annotate_dataflow_result(bounded)
    bounded = bound_analysis_result(bounded, max_chars)
    if mode != "combined":
        annotate_dataflow_result(bounded)
        # Classification is bounded metadata; a changed count/conclusion must
        # still respect the caller's exact serialized output budget.
        bounded = bound_analysis_result(bounded, max_chars)
    return bounded
