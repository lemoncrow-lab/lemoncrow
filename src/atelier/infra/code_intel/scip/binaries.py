"""Environment-aware local SCIP binary discovery."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_SCIP_BINARIES = {
    "python": ("ATELIER_SCIP_PYTHON_BIN", "scip-python"),
    "typescript": ("ATELIER_SCIP_TYPESCRIPT_BIN", "scip-typescript"),
    "javascript": ("ATELIER_SCIP_TYPESCRIPT_BIN", "scip-typescript"),
}


def discover_scip_binary(language: str) -> Path | None:
    """Resolve a supported local SCIP indexer binary if one is installed."""

    env_var, fallback = _SCIP_BINARIES.get(language, ("", ""))
    candidates = [os.environ.get(env_var, ""), fallback]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate) if Path(candidate).name == candidate else candidate
        if not resolved:
            continue
        path = Path(resolved).expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def discover_scip_binaries() -> dict[str, Path]:
    """Return the supported SCIP binaries that are already available locally."""

    discovered: dict[str, Path] = {}
    for language in ("python", "typescript"):
        path = discover_scip_binary(language)
        if path is not None:
            discovered[language] = path
    return discovered


__all__ = ["discover_scip_binaries", "discover_scip_binary"]
