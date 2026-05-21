"""Static test cases used by the benchmark runner.

Cases are tuples of (label, tool_args_for_atelier).
The builtin equivalent is computed by the runner from those same args.
"""
from __future__ import annotations

from pathlib import Path

# Repo root — resolved relative to this file so it works from any cwd.
REPO = str(Path(__file__).resolve().parents[3])

# ---------------------------------------------------------------------------
# read cases: (label, atelier args)
# ---------------------------------------------------------------------------
READ_CASES: list[tuple[str, dict]] = [
    (
        "small/config",
        {"path": f"{REPO}/pyproject.toml"},
    ),
    (
        "medium python",
        {"path": f"{REPO}/src/atelier/core/capabilities/cross_vendor_routing/router.py"},
    ),
    (
        "large python",
        {"path": f"{REPO}/src/atelier/gateway/adapters/mcp_server.py"},
    ),
    (
        "markdown docs",
        {"path": f"{REPO}/AGENTS.md"},
    ),
    (
        "test file",
        {"path": f"{REPO}/tests/gateway/test_mcp_jsonrpc_e2e.py"},
    ),
]

# ---------------------------------------------------------------------------
# shell cases: (label, atelier args)
# ---------------------------------------------------------------------------
SHELL_CASES: list[tuple[str, dict]] = [
    (
        "git log",
        {"command": "git log --oneline -10", "cwd": REPO},
    ),
    (
        ".py file count",
        {"command": "find src -name '*.py' | wc -l", "cwd": REPO},
    ),
    (
        "test file list",
        {"command": "find tests -name 'test_*.py' | sort | head -15", "cwd": REPO},
    ),
    (
        "python version",
        {"command": "python --version 2>&1", "cwd": REPO},
    ),
    (
        "capabilities ls",
        {"command": "ls src/atelier/core/capabilities/", "cwd": REPO},
    ),
]

# ---------------------------------------------------------------------------
# search cases: (label, atelier args)
# ---------------------------------------------------------------------------
SEARCH_CASES: list[tuple[str, dict]] = [
    (
        "detect_configured",
        {
            "query": "detect_configured_vendors",
            "path": REPO,
            "content_regex": "detect_configured_vendors",
            "file_glob_patterns": ["**/*.py"],
            "budget_tokens": 2000,
            "mode": "chunks",
        },
    ),
    (
        "NoFeasibleRouteError",
        {
            "query": "NoFeasibleRouteError",
            "path": REPO,
            "content_regex": "NoFeasibleRouteError",
            "file_glob_patterns": ["**/*.py"],
            "budget_tokens": 2000,
            "mode": "chunks",
        },
    ),
    (
        "tool_smart_read",
        {
            "query": "tool_smart_read",
            "path": REPO,
            "content_regex": "tool_smart_read",
            "file_glob_patterns": ["**/*.py"],
            "budget_tokens": 2000,
            "mode": "chunks",
        },
    ),
    (
        "CrossVendorRouter",
        {
            "query": "CrossVendorRouter",
            "path": REPO,
            "content_regex": "CrossVendorRouter",
            "file_glob_patterns": ["**/*.py"],
            "budget_tokens": 2000,
            "mode": "chunks",
        },
    ),
    (
        "@mcp_tool decorator",
        {
            "query": "@mcp_tool",
            "path": REPO,
            "content_regex": "@mcp_tool",
            "file_glob_patterns": ["**/*.py"],
            "budget_tokens": 2000,
            "mode": "chunks",
        },
    ),
]

ALL_CASES: dict[str, list[tuple[str, dict]]] = {
    "read": READ_CASES,
    "shell": SHELL_CASES,
    "search": SEARCH_CASES,
}
