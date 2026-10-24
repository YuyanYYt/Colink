"""Original immutable-snapshot source tools, registered only by Mirror backends."""

from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from code_context.storage import MirrorError


def register_mirror_source_tools(mcp, store, authorize, read_context, annotations):
    @mcp.tool(annotations=annotations, structured_output=True)
    def read_file(
        project_id: str,
        path: str,
        snapshot: str | None = None,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 20_000,
        char_offset: int = 0,
    ) -> dict[str, Any]:
        """Read necessary source ranges; default 200, max 1000 lines, 20000 text chars.
        Very long lines continue at next_start_line + next_char_offset; pass char_offset to
        resume without losing or repeating text. max_chars is 1000-50000. If the location is
        unknown, use search_code first. next_start_line indicates the next page. Omit snapshot for
        current code, use 'previous', or reuse repo_overview's opaque handle for consistent reads.
        Report code findings, not handles, hashes or synchronization metadata, unless requested.
        """
        authorize(project_id)
        return read_context(
            project_id,
            snapshot,
            lambda rev: store.read_file(
                project_id, path, rev, start_line, end_line, max_chars, char_offset
            ),
        )

    @mcp.tool(annotations=annotations, structured_output=True)
    def search_code(
        project_id: str,
        query: str,
        snapshot: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Locate literal, case-sensitive code text with paths and lines before targeted
        read_file calls when a symbol or source location is unknown. Omit snapshot for
        current code, use 'previous', or reuse an opaque handle. Keep handles out of the answer.
        """
        authorize(project_id)
        return read_context(
            project_id, snapshot, lambda rev: store.search_code(project_id, query, rev, limit)
        )

    @mcp.tool(annotations=annotations, structured_output=True)
    def get_diff(
        project_id: str,
        snapshot: str | None = None,
        path: str | None = None,
        baseline: Literal["previous", "empty"] = "previous",
        detail: Literal["summary", "patch"] = "summary",
        offset: int = 0,
        limit: int = 50,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        """Review the latest code changes against the preceding state; no numbered versions
        needed. Returns a paginated summary by default. For relevant recent changes, reuse
        snapshot and request detail='patch', preferably with path. No previous state returns
        NO_PREVIOUS_SNAPSHOT without source content; baseline='empty' must be explicit.
        Patch output is bounded by max_chars (1000-50000); truncated patches require narrower
        reads or a larger budget. Do not request whole-repository patches for general questions.
        """
        authorize(project_id)
        try:
            return store.get_recent_diff(
                project_id,
                snapshot,
                path,
                baseline=baseline,
                detail=detail,
                offset=offset,
                limit=limit,
                max_chars=max_chars,
            )
        except MirrorError as exc:
            raise ToolError(str(exc)) from None
