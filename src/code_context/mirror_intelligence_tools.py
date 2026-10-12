"""Legacy symbol and graph tool schemas for historical Mirror connections only."""

from typing import Any, Literal


def register_mirror_code_tools(mcp, query, annotations):
    query_result = query

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
