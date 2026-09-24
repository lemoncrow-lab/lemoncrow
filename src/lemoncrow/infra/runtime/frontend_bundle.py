"""Locate the frontend bundle used by the loopback server and review UI."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def _installation_root() -> Path:
    configured = os.environ.get("LEMONCROW_INSTALL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    record = Path.home() / ".lemoncrow" / "install_dir"
    with contextlib.suppress(OSError):
        value = record.read_text(encoding="utf-8").strip()
        if value:
            path = Path(value).expanduser().resolve()
            if path.exists():
                return path
    return Path(__file__).resolve().parents[4]


def frontend_dir() -> Path:
    """Return the best frontend source/bundle directory for this installation."""
    candidates: list[Path] = []
    configured = os.environ.get("LEMONCROW_FRONTEND_DIR", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    workspace = os.environ.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
    if workspace:
        candidates.append(Path(workspace) / "frontend")
    candidates.append(_installation_root() / "frontend")
    for candidate in candidates:
        if (candidate / "package.json").exists() or (candidate / "index.html").exists():
            return candidate
    return candidates[-1]


def frontend_is_prebuilt(frontend: Path) -> bool:
    """True when *frontend* is a built SPA bundle rather than a source app."""
    return not (frontend / "package.json").exists() and (frontend / "index.html").exists()


__all__ = ["frontend_dir", "frontend_is_prebuilt"]
