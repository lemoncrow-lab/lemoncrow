"""Environment-aware local SCIP binary discovery."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from atelier.infra.code_intel.languages import language_by_name

# Canonical-keyed env-var names. These strings are operator-supplied config and
# MUST stay byte-identical across refactors (DLS-LANG-04). The indexer binary
# name (the fallback) is sourced from the canonical registry's `scip_indexer`.
_SCIP_ENV_VARS = {
    "python": "ATELIER_SCIP_PYTHON_BIN",
    "typescript": "ATELIER_SCIP_TYPESCRIPT_BIN",
    "javascript": "ATELIER_SCIP_TYPESCRIPT_BIN",
}


def discover_scip_binary(language: str) -> Path | None:
    """Resolve a supported local SCIP indexer binary if one is installed."""

    env_var = _SCIP_ENV_VARS.get(language, "")
    lang = language_by_name(language)
    fallback = lang.scip_indexer if lang is not None and lang.scip_indexer else ""
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
