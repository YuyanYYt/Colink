"""First-run desktop setup: private local files, no network and no source reads."""

import json
import os
from pathlib import Path

from code_context.client import SyncError
from code_context.tunnel import _KEY, _TUNNEL_ID, prepare_profile


def configure_desktop(workspace: Path, payload: str) -> dict:
    """Accept credentials via stdin only; never overwrite an existing setup."""
    try:
        values = json.loads(payload)
        if not isinstance(values, dict) or set(values) != {"tunnel_id", "api_key"}:
            raise ValueError
        tunnel_id, key = values["tunnel_id"], values["api_key"]
        if not isinstance(tunnel_id, str) or _TUNNEL_ID.fullmatch(tunnel_id) is None:
            raise ValueError
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ValueError
    except (TypeError, ValueError, RecursionError) as exc:
        raise SyncError("invalid connection settings; check the tunnel ID and runtime key") from exc

    workspace = workspace.expanduser().resolve()
    root = workspace / "examples/sample_project"
    env_file = workspace / ".env.local"
    profile_file = workspace / ".code-context/tunnel/profile.yaml"
    # lexists also rejects dangling symlinks. Existing credentials/configurations
    # belong to the user and require a separate, explicit update workflow.
    if os.path.lexists(env_file) or os.path.lexists(profile_file):
        raise SyncError("connection settings already exist; no files were overwritten")
    if not root.is_dir() or root.is_symlink():
        raise SyncError("sample project is missing; reinstall the application first")
    prepare_profile(
        root, "sample", workspace / ".code-context/local-sample", tunnel_id, profile_file
    )
    try:
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("OPENAI_API_KEY=" + key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        # Keep the partial profile for diagnosis; do not silently delete a user
        # configuration or claim success. No connection has been started.
        raise SyncError(
            "setup is incomplete; check private file permissions before retrying"
        ) from exc
    return {"configured": True, "remote_connection_started": False, "source_collected": False}
