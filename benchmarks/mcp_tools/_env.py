"""Shared runtime env helpers for MCP benchmarks."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any


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
    os.environ["ATELIER_LESSONS_ROOT"] = str(resolved_root / ".atelier/lessons")
    os.environ["ATELIER_WORKSPACE_ROOT"] = str(resolved_workspace)
    os.environ["CLAUDE_WORKSPACE_ROOT"] = str(resolved_workspace)
    os.environ["ATELIER_DEV_MODE"] = "1"
    os.environ["ATELIER_LINEAGE_DISABLED"] = "1"  # commit-lineage bootstrap is unrelated to search benchmarks
    os.environ.pop("CURSOR_WORKSPACE_ROOT", None)
    os.environ.pop("VSCODE_CWD", None)
    os.environ.pop("ATELIER_MEM_ROOT", None)
    return resolved_root


def call_code_op(request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a ``{"op": X, ...}`` code-intel benchmark request to its engine wrapper.

    Replaces the retired ``tool_code`` multiplexer: the MCP surface no longer routes
    code ops through a single handler, so benchmarks call the ``_op_*`` wrappers here.
    """
    from atelier.gateway.adapters import mcp_server

    ops: dict[str, Callable[..., Any]] = {
        "search": mcp_server._op_search,
        "symbol": mcp_server._op_node,
        "node": mcp_server._op_node,
        "callers": mcp_server._op_callers,
        "callees": mcp_server._op_callees,
        "usages": mcp_server._op_usages,
        "explore": mcp_server._op_explore,
        "pattern": mcp_server._op_pattern,
        "index": mcp_server._op_index,
        "blame": mcp_server._op_blame,
        "cache_status": mcp_server._op_cache_status,
        "cache_invalidate": mcp_server._op_cache_invalidate,
    }
    kwargs = {k: v for k, v in request.items() if k != "op"}
    return ops[request["op"]](**kwargs)
