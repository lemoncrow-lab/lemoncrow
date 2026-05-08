"""Path helpers for separating runtime state from Git-tracked knowledge."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_STORE_DIRNAME = ".atelier"
DEFAULT_KNOWLEDGE_DIRNAME = ".knowledge"


def default_store_root() -> Path:
    """Return the default runtime store root for traces and SQLite state."""
    configured = os.environ.get("ATELIER_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / DEFAULT_STORE_DIRNAME).resolve()


def resolve_workspace_root(root: Path | str | None = None) -> Path:
    """Resolve the active workspace root used for project-local knowledge."""
    configured = os.environ.get("ATELIER_WORKSPACE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    derived = _derive_workspace_root(root)
    if derived is not None:
        return derived
    return Path.cwd().resolve()


def resolve_knowledge_root(root: Path | str | None = None, knowledge_root: Path | str | None = None) -> Path:
    """Resolve the Git-tracked knowledge root.

    Precedence:
    1. Explicit constructor argument
    2. ATELIER_KNOWLEDGE_ROOT
    3. <workspace>/.knowledge
    """
    if knowledge_root is not None:
        return Path(knowledge_root).expanduser().resolve()

    configured = os.environ.get("ATELIER_KNOWLEDGE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    return (resolve_workspace_root(root) / DEFAULT_KNOWLEDGE_DIRNAME).resolve()


def _derive_workspace_root(root: Path | str | None) -> Path | None:
    if root is None:
        return None

    candidate = Path(root).expanduser().resolve()
    default_home_store = (Path.home() / DEFAULT_STORE_DIRNAME).resolve()
    if candidate == default_home_store:
        return None

    if candidate.name in {DEFAULT_STORE_DIRNAME, DEFAULT_KNOWLEDGE_DIRNAME}:
        return candidate.parent
    if candidate.parent != candidate:
        return candidate.parent
    return candidate


__all__ = [
    "DEFAULT_KNOWLEDGE_DIRNAME",
    "DEFAULT_STORE_DIRNAME",
    "default_store_root",
    "resolve_knowledge_root",
    "resolve_workspace_root",
]
