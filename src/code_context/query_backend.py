"""Source-query boundary shared by immutable mirrors and live local access.

The MCP surface owns schemas, annotations and presentation. Backends own source
selection and consistency. A live selection must never pretend to be a retained
mirror revision. The legacy adapter deliberately does not change mirror logic.
"""

from typing import Any, Protocol


class QueryBackend(Protocol):
    source_mode: str

    def list_projects(self) -> dict: ...

    def resolve_snapshot(self, project_id: str, snapshot: str | None = None) -> tuple[Any, str]: ...

    def repo_overview(self, project_id: str, selection: Any, offset: int, limit: int) -> dict: ...

    def read_file(
        self,
        project_id: str,
        path: str,
        selection: Any,
        start_line: int,
        end_line: int | None,
        max_chars: int,
        char_offset: int,
    ) -> dict: ...

    def search_code(self, project_id: str, query: str, selection: Any, limit: int) -> dict: ...

    def get_recent_diff(
        self, project_id: str, snapshot: str | None, path: str | None, **parameters: Any
    ) -> dict: ...

    def code_query(
        self, project_id: str, snapshot: str | None, operation: str, **parameters: Any
    ) -> dict: ...


class MirrorQueryBackend:
    """Keep the existing mirror API/transactions intact behind a query-only facade."""

    source_mode = "mirror"

    def __init__(self, store):
        self.store = store

    def list_projects(self):
        return self.store.list_projects()

    def resolve_snapshot(self, project_id, snapshot=None):
        return self.store.resolve_snapshot(project_id, snapshot)

    def repo_overview(self, *args, **kwargs):
        return self.store.repo_overview(*args, **kwargs)

    def read_file(self, *args, **kwargs):
        return self.store.read_file(*args, **kwargs)

    def search_code(self, *args, **kwargs):
        return self.store.search_code(*args, **kwargs)

    def get_recent_diff(self, *args, **kwargs):
        return self.store.get_recent_diff(*args, **kwargs)

    def code_query(self, *args, **kwargs):
        return self.store.code_query(*args, **kwargs)
