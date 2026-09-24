"""Transport-independent workspace and request-project resolution."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from lemoncrow.core.environment import bool_env
from lemoncrow.gateway.tools.errors import ToolArgumentError

# Request-scoped project isolation. One worker thread can bind a registered
# project (preferred) or a legacy path override for the duration of a tool call.
request_project: threading.local = threading.local()


def workspace_path(file_path: str) -> Path:
    """Resolve *file_path* against the active host workspace when relative."""
    path = Path(file_path)
    if path.is_absolute():
        return path
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    return Path(workspace) / path


def http_project_override_allowed() -> bool:
    """Whether the legacy arbitrary-path compatibility escape hatch is enabled."""
    return bool_env("LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE", False)


def project_override_root() -> Path:
    """Return the allowlisted root for legacy arbitrary-path compatibility."""
    root = (
        os.environ.get("CLAUDE_WORKSPACE_ROOT")
        or os.environ.get("LEMONCROW_WORKSPACE_ROOT")
        or os.environ.get("VSCODE_CWD")
        or os.getcwd()
    )
    return Path(root).expanduser().resolve()


def is_within_root(candidate: Path, root: Path) -> bool:
    """Return True iff *candidate* is *root* or nested under it."""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def set_request_project(reference: str | None) -> str | None:
    """Bind one registered project to this worker thread; return prior value."""
    prior = getattr(request_project, "value", None)
    resolved: str | None = None
    if isinstance(reference, str) and reference.strip():
        from lemoncrow.core.service.project_registry import resolve_registered_project

        registered = resolve_registered_project(reference)
        if registered is not None:
            resolved = str(registered.root)
        elif reference.strip().startswith("proj_"):
            raise ToolArgumentError(f"unknown registered project: {reference.strip()}")
        elif http_project_override_allowed():
            try:
                candidate = Path(reference).expanduser().resolve()
                if candidate.is_dir() and is_within_root(candidate, project_override_root()):
                    resolved = str(candidate)
            except OSError:
                resolved = None
    request_project.value = resolved
    return prior


def clear_request_project(prior: str | None) -> None:
    """Restore a previous value returned by :func:`set_request_project`."""
    request_project.value = prior


def extract_request_project(params: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Pull a project id (preferred) or legacy project path from a tool call."""
    meta = params.get("_meta")
    if isinstance(meta, dict):
        for key in ("lemoncrow-project-id", "projectId", "project_id", "workspaceId", "workspace_id"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for key in ("mcp-project-path", "projectPath", "project_path", "projectRoot", "project_root"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if isinstance(args, dict):
        project_id = args.pop("project_id", None)
        project_path = args.pop("project_path", None)
        if isinstance(project_id, str) and project_id.strip():
            return project_id
        if isinstance(project_path, str) and project_path.strip():
            return project_path
    return None


def next_session_cwd(current: str | None, name: str, args: Any) -> str | None:
    """Return the updated session cwd after observing one tool call."""
    if name != "bash" or not isinstance(args, dict):
        return current
    cwd = args.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    return current


def session_worktree_root(recorded_cwd: str | None, workspace_root: Path) -> Path | None:
    """Resolve a recorded cwd to a linked worktree belonging to *workspace_root*."""
    if not recorded_cwd:
        return None
    try:
        candidate = Path(recorded_cwd).expanduser().resolve()
        if not candidate.is_dir():
            return None
        root = workspace_root.resolve()
        worktrees_dir = (root / ".git" / "worktrees").resolve()
        for directory in (candidate, *candidate.parents):
            marker = directory / ".git"
            if marker.is_dir():
                return None
            if not marker.is_file():
                continue
            gitdir = marker.read_text(encoding="utf-8").strip()
            if not gitdir.startswith("gitdir:"):
                return None
            target = Path(gitdir.split(":", 1)[1].strip()).resolve()
            if not target.is_relative_to(worktrees_dir):
                return None
            return None if directory == root else directory
    except OSError:
        return None
    return None


def workspace_root() -> Path:
    """Return the request-bound project root or the active host workspace."""
    override = getattr(request_project, "value", None)
    if isinstance(override, str) and override:
        return Path(override)
    workspace = (
        os.environ.get("CLAUDE_WORKSPACE_ROOT")
        or os.environ.get("LEMONCROW_WORKSPACE_ROOT")
        or os.environ.get("VSCODE_CWD")
        or os.getcwd()
    )
    return Path(workspace)


__all__ = [
    "clear_request_project",
    "extract_request_project",
    "http_project_override_allowed",
    "is_within_root",
    "next_session_cwd",
    "project_override_root",
    "request_project",
    "session_worktree_root",
    "set_request_project",
    "workspace_path",
    "workspace_root",
]
