"""Shared runtime env helpers for MCP benchmarks."""

from __future__ import annotations

import os
from pathlib import Path


def configure_benchmark_runtime(root: Path, *, workspace_root: Path | None = None) -> Path:
    """Point benchmark runtime state at a temp root while preserving file access.

    Benchmarks often need to search or read the real repository, so ``workspace_root``
    may legitimately point at the repo checkout. Runtime state and lessons must still
    stay under the benchmark temp directory to avoid polluting the working tree.
    """
    resolved_root = root.expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    resolved_workspace = (workspace_root or resolved_root).expanduser().resolve()

    os.environ["ATELIER_ROOT"] = str(resolved_root / ".atelier")
    os.environ["ATELIER_LESSONS_ROOT"] = str(resolved_root / ".lessons")
    os.environ["ATELIER_WORKSPACE_ROOT"] = str(resolved_workspace)
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(resolved_workspace)
    os.environ.pop("CURSOR_WORKSPACE_ROOT", None)
    os.environ.pop("VSCODE_CWD", None)
    os.environ.pop("ATELIER_MEM_ROOT", None)
    return resolved_root
