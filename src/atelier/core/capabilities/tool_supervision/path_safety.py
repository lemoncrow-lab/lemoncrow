"""Shared path-safety constants and helpers for edit modules.

Centralises the protected-directory set so rich_edit and batch_edit cannot
drift out of sync with each other.
"""

from __future__ import annotations

from pathlib import Path

#: Directory names that must never be modified by any edit tool.
PROTECTED_PARTS: frozenset[str] = frozenset({".git", ".atelier", "node_modules", ".venv"})


def check_protected(path: Path, raw: str = "") -> None:
    """Raise :class:`ValueError` if *path* contains a protected directory component.

    Args:
        path: Resolved absolute path to check.
        raw:  Original user-supplied path string used in the error message.
              Falls back to ``str(path)`` when omitted.
    """
    label = raw or str(path)
    if any(part in PROTECTED_PARTS for part in path.parts):
        raise ValueError(f"protected path denied: {label}")
