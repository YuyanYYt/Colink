"""Explicit task-scoped writes; local permission controls are never MCP tools."""

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from code_context.source_access import SourceError

WRITE_TOOL_NAMES = {
    "begin_write_task",
    "apply_edit",
    "create_file",
    "create_directory",
    "delete_file",
    "finish_write_task",
    "rollback_write_task",
    "write_task_status",
}


def register_write_tools(mcp, coordinator, authorize):
    write = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    )
    read = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    def call(project, fn):
        authorize(project)
        try:
            return fn()
        except SourceError as exc:
            raise ToolError(str(exc)) from None
        except Exception:
            raise ToolError(
                "WRITE_OPERATION_FAILED: preserve materials and inspect locally"
            ) from None

    @mcp.tool(annotations=write, structured_output=True)
    def begin_write_task(
        project_id: str, request_id: str, title: str = "", paths: list[str] | None = None
    ) -> dict[str, Any]:
        """Begin one explicit user-requested editing task. Requires the local app's default-off
        project grant. Obtain next_task_request_id from write_task_status; reuse it on retry.
        Prefer declaring the exact target files and all future parent directories for large
        projects. Keep one task across related A/B/C edits. No commands or directory expansion.
        """
        return call(
            project_id,
            lambda: coordinator.begin_write_task(project_id, request_id, title=title, paths=paths),
        )

    @mcp.tool(annotations=write, structured_output=True)
    def apply_edit(
        project_id: str,
        task_id: str,
        request_id: str,
        path: str,
        expected_sha256: str,
        edit: dict[str, Any],
    ) -> dict[str, Any]:
        """Precisely modify one declared UTF-8 source file after reading its current SHA256.
        edit is exactly one of: {kind:insert_lines,line:int,position:before|after,text:str,
        expected_context:str}, {kind:replace_lines,start_line:int,end_line:int,
        old_text:str,new_text:str},
        {kind:replace_fragment,old_text:str,new_text:str}. Ranges are 1-based inclusive;
        fragments must match uniquely. The server verifies actual current content, durably
        saves the task origin before writing, and returns verified new SHA256. Reuse the same
        stable request_id and arguments on retry; do not retry a conflict with guessed data.
        """
        return call(
            project_id,
            lambda: coordinator.apply_edit(
                project_id, task_id, request_id, path, expected_sha256, edit
            ),
        )

    @mcp.tool(annotations=write, structured_output=True)
    def create_file(
        project_id: str, task_id: str, request_id: str, path: str, content: str
    ) -> dict[str, Any]:
        """Create one declared source file without overwriting an existing object. Parent
        directories must already exist or be explicitly created in this task. UTF-8 only;
        local project permission, filters, bounded storage and durable ownership apply.
        """
        return call(
            project_id,
            lambda: coordinator.create_file(project_id, task_id, request_id, path, content),
        )

    @mcp.tool(annotations=write, structured_output=True)
    def create_directory(
        project_id: str, task_id: str, request_id: str, path: str
    ) -> dict[str, Any]:
        """Create one explicitly declared directory, non-recursively and without overwriting.
        Create each declared missing ancestor first. Never adopt existing or external objects.
        """
        return call(
            project_id, lambda: coordinator.create_directory(project_id, task_id, request_id, path)
        )

    @mcp.tool(annotations=write, structured_output=True)
    def delete_file(
        project_id: str, task_id: str, request_id: str, path: str, expected_sha256: str
    ) -> dict[str, Any]:
        """Delete one declared UTF-8 source file only on the user's explicit request.
        Read its current SHA256 first. Saves the task origin before verified removal;
        whole-task rollback restores original files while the bounded recovery point
        remains retained. No directory/recursive/system deletion, commands or forced
        overwrite. Reuse identical request_id and arguments only for the same retry.
        """
        return call(
            project_id,
            lambda: coordinator.delete_file(project_id, task_id, request_id, path, expected_sha256),
        )

    @mcp.tool(annotations=write, structured_output=True)
    def finish_write_task(project_id: str, task_id: str, request_id: str) -> dict[str, Any]:
        """Complete a related edit task after requested checks, retaining at most the latest
        completed recovery point for seven days. No command execution/test runner is provided.
        Readback proves content, not business behavior. External changes cause a conflict.
        """
        return call(
            project_id, lambda: coordinator.finish_write_task(project_id, task_id, request_id)
        )

    @mcp.tool(annotations=write, structured_output=True)
    def rollback_write_task(project_id: str, task_id: str, request_id: str) -> dict[str, Any]:
        """Undo the entire related A/B/C task, not just its last edit. Checks all participants
        before any restore; never overwrite external edits. Restores saved origins and removes
        only task-owned unchanged new files and empty directories. Interrupted progress stays
        protected for local recovery; this tool cannot bypass conflicts or enable permission.
        """
        return call(
            project_id, lambda: coordinator.rollback_write_task(project_id, task_id, request_id)
        )

    @mcp.tool(annotations=read, structured_output=True)
    def write_task_status(project_id: str) -> dict[str, Any]:
        """Inspect local permission, protected task and bounded recovery capacity without
        reading source or granting permission. Internal handles and hashes are not ordinary
        answer content. Only the local app may enable writing or recover an interrupted task.
        """

        def status():
            coordinator.source_provider(project_id).ensure_available()
            result = coordinator.status()
            result["write_enabled"] = (
                result["write_enabled"] and project_id in result["write_projects"]
            )
            result["write_projects"] = [p for p in result["write_projects"] if p == project_id]
            for key in ("active_task", "recent_task"):
                if result[key] and result[key]["project_id"] != project_id:
                    result[key] = None
            return {**result, "project_id": project_id}

        return call(project_id, status)
