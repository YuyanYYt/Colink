"""Choose a bounded daily analysis from explicit source selectors.

Legacy modes remain server-side strategies. A missing value is never inferred
from a method's parameters, and two files are not treated as a call path.
"""

from typing import Literal

from code_context.intelligence_analysis import AnalysisSelector, StructureMode
from code_context.live_dataflow import FlowSelector

AnalysisMode = (
    StructureMode | Literal["data_origin", "data_impact", "data_path", "combined", "diff"]
)


def _symbol(selected: AnalysisSelector) -> bool:
    return selected.symbol_id is not None or selected.qualname is not None


def resolve_analysis_mode(
    selector: AnalysisSelector | FlowSelector | None,
    target: AnalysisSelector | FlowSelector | None = None,
    mode: AnalysisMode | None = None,
) -> AnalysisMode:
    """Preserve explicit compatibility requests; infer only well-formed intent."""
    if mode is not None:
        return mode
    if selector is None:
        raise ValueError("analysis requires a file, exact symbol or explicit value selector")
    if isinstance(selector, FlowSelector):
        if not _symbol(selector):
            raise ValueError("value analysis requires an exact symbol and value selector")
        if target is None:
            return "combined"
        if not isinstance(target, FlowSelector) or not _symbol(target):
            raise ValueError("value paths require exact symbols and values at both endpoints")
        return "data_path"
    if target is not None:
        if isinstance(target, FlowSelector):
            raise ValueError("value paths require exact symbols and values at both endpoints")
        if not _symbol(selector) or not _symbol(target):
            raise ValueError("call paths require exact symbols at both endpoints")
        return "call_path"
    return "symbol_context" if _symbol(selector) else "file_context"
