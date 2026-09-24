"""Persistent registry for local LemonCrow projects.

The MCP daemon is long lived and may serve many workspaces.  Wire requests must
therefore identify a project without being allowed to point at an arbitrary host
path.  This module gives every canonical project root a deterministic opaque-ish
``proj_`` id and persists one small record per project under LemonCrow's global
state directory.  Per-project indexes remain inside the project itself.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import default_store_root

_PROJECT_ID_PREFIX = "proj_"
_PROJECT_ID_HEX = 20
_REGISTRY_VERSION = 1


@dataclass(frozen=True, slots=True)
class RegisteredProject:
    project_id: str
    root: Path


def project_id_for_root(root: str | Path) -> str:
    """Return the stable local id for a canonical project path."""

    resolved = Path(root).expanduser().resolve()
    digest = sha256(str(resolved).encode("utf-8")).hexdigest()[:_PROJECT_ID_HEX]
    return f"{_PROJECT_ID_PREFIX}{digest}"


def _registry_dir(store_root: str | Path | None = None) -> Path:
    base = Path(store_root).expanduser().resolve() if store_root is not None else default_store_root()
    return base / "projects"


def register_project(root: str | Path, *, store_root: str | Path | None = None) -> RegisteredProject:
    """Persist *root* as an allowed local project and return its identity.

    One file per project avoids a shared JSON read-modify-write race when several
    MCP processes start concurrently.  ``os.replace`` makes each record update
    atomic to readers.
    """

    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"project root does not exist: {resolved}")
    project_id = project_id_for_root(resolved)
    directory = _registry_dir(store_root)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{project_id}.json"
    temp = directory / f".{project_id}.{os.getpid()}.{threading.get_ident()}.tmp"
    payload = {
        "version": _REGISTRY_VERSION,
        "project_id": project_id,
        "root": str(resolved),
    }
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, destination)
    return RegisteredProject(project_id=project_id, root=resolved)


def _project_from_payload(payload: Any) -> RegisteredProject | None:
    if not isinstance(payload, dict):
        return None
    project_id = payload.get("project_id")
    root = payload.get("root")
    if not isinstance(project_id, str) or not isinstance(root, str):
        return None
    try:
        resolved = Path(root).expanduser().resolve()
    except OSError:
        return None
    if not resolved.is_dir() or project_id != project_id_for_root(resolved):
        return None
    return RegisteredProject(project_id=project_id, root=resolved)


def registered_projects(*, store_root: str | Path | None = None) -> tuple[RegisteredProject, ...]:
    """Return valid registered projects in deterministic id order.

    Stale or malformed records are ignored rather than deleted.  Discovery is a
    read path and must never mutate user state merely because a disk is
    temporarily unavailable.
    """

    directory = _registry_dir(store_root)
    if not directory.is_dir():
        return ()
    projects: list[RegisteredProject] = []
    for path in sorted(directory.glob(f"{_PROJECT_ID_PREFIX}*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        project = _project_from_payload(payload)
        if project is not None:
            projects.append(project)
    return tuple(projects)


def resolve_registered_project(
    reference: str,
    *,
    store_root: str | Path | None = None,
) -> RegisteredProject | None:
    """Resolve a project id or an exact already-registered canonical path.

    Exact registered paths are accepted for backward-compatible MCP metadata;
    arbitrary paths are deliberately not.  New clients should send project ids.
    """

    value = reference.strip()
    if not value:
        return None
    projects = registered_projects(store_root=store_root)
    if value.startswith(_PROJECT_ID_PREFIX):
        return next((project for project in projects if project.project_id == value), None)
    try:
        candidate = Path(value).expanduser().resolve()
    except OSError:
        return None
    return next((project for project in projects if project.root == candidate), None)


__all__ = [
    "RegisteredProject",
    "project_id_for_root",
    "register_project",
    "registered_projects",
    "resolve_registered_project",
]
