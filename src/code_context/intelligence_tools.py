"""Read-only MCP surface for on-demand Python and Java structure."""

from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from code_context.intelligence_queries import bound_result
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


def register_code_tools(mcp, store, authorize, display_name, annotations):
    def query(project_id, snapshot, operation, max_chars, **parameters):
        authorize(project_id)
        try:
            result = store.code_query(
                project_id, snapshot, operation, max_chars=max_chars, **parameters
            )
        except MirrorError as exc:
            raise ToolError(str(exc)) from None
        try:
            return bound_result({**result, "display_name": display_name(project_id)}, max_chars)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=annotations, structured_output=True)
    def symbol_search(
        project_id: str,
        query: str,
        snapshot: str | None = None,
        exact: bool = False,
        kind: str | None = None,
        path_prefix: str | None = None,
        language: Literal["python", "java"] | None = None,
        offset: int = 0,
        limit: int = 20,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Find Python/Java definitions by name, kind, path and language. Use before reading
        unknown symbols; returns internal symbol_id plus exact source ranges, not same-name text
        matches. Read-only static index, not a compiler. Reuse repo_overview's snapshot handle;
        keep handles/IDs out of ordinary answers. If index_partial, fall back to search_code.
        """
        return query_result(
            project_id,
            snapshot,
            "symbol_search",
            max_chars,
            query=query,
            exact=exact,
            kind=kind,
            path_prefix=path_prefix,
            language=language,
            offset=offset,
            limit=limit,
        )

    # Avoid a collision between the parameter named query and the shared helper.
    query_result = query

    @mcp.tool(annotations=annotations, structured_output=True)
    def read_symbol(
        project_id: str,
        symbol_id: str,
        snapshot: str | None = None,
        line_offset: int = 0,
        max_lines: int = 200,
        char_offset: int = 0,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Read actual source text for a selected Python/Java symbol, not an AST substitute.
        Use symbol_search's ID in the same snapshot; narrow default 200 lines, maximum 1000.
        Pagination is relative to the definition. Truncated text is not the complete definition.
        """
        return query(
            project_id,
            snapshot,
            "read_symbol",
            max_chars,
            symbol_id=symbol_id,
            line_offset=line_offset,
            max_lines=max_lines,
            char_offset=char_offset,
        )

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
    def get_call_graph(
        project_id: str,
        symbol_id: str,
        snapshot: str | None = None,
        direction: Literal["outgoing", "incoming", "both"] = "outgoing",
        depth: int = 1,
        max_nodes: int = 50,
        max_edges: int = 100,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Ask for callees, callers or both, expanding only the needed symbol. Includes
        construction and explicit unresolved evidence. Defaults one hop; maximum five. Declared
        types are possible dispatch targets, not proof of runtime targets/complete call paths.
        """
        return query(
            project_id,
            snapshot,
            "symbol_graph",
            max_chars,
            symbol_id=symbol_id,
            graph="call",
            direction=direction,
            depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    @mcp.tool(annotations=annotations, structured_output=True)
    def get_class_graph(
        project_id: str,
        symbol_id: str,
        snapshot: str | None = None,
        direction: Literal["outgoing", "incoming", "both"] = "both",
        depth: int = 1,
        max_nodes: int = 50,
        max_edges: int = 100,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Query class/interface inheritance, implementation, members, type use and
        instantiation on demand. Incoming inheritance finds subclasses; contains edges find
        members. Dynamic Python base classes and Java overloads/dispatch may remain unresolved.
        """
        return query(
            project_id,
            snapshot,
            "symbol_graph",
            max_chars,
            symbol_id=symbol_id,
            graph="class",
            direction=direction,
            depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    @mcp.tool(annotations=annotations, structured_output=True)
    def get_file_dependencies(
        project_id: str,
        path: str,
        snapshot: str | None = None,
        direction: Literal["outgoing", "incoming", "both"] = "outgoing",
        depth: int = 1,
        max_nodes: int = 50,
        max_edges: int = 100,
        include_type_only: bool = True,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Query file/module dependencies or reverse dependents with line-level reasons
        (imports, types, calls). Chains expand at most five hops with cycle-safe traversal.
        Edges are consumer -> dependency. Does not include third-party source or guessed imports.
        """
        return query(
            project_id,
            snapshot,
            "file_dependencies",
            max_chars,
            path=path,
            direction=direction,
            depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            include_type_only=include_type_only,
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
