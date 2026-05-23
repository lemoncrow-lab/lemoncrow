"""Static test cases used by the benchmark runner.

Cases are tuples of (label, tool_args_for_atelier).
The builtin equivalent is computed by the runner from those same args.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Repo root — resolved relative to this file so it works from any cwd.
REPO = str(Path(__file__).resolve().parents[3])

# ---------------------------------------------------------------------------
# read cases: (label, atelier args)
# ---------------------------------------------------------------------------
_READ_BASE: list[tuple[str, str]] = [
    ("small/config", f"{REPO}/pyproject.toml"),
    ("medium python", f"{REPO}/src/atelier/core/capabilities/cross_vendor_routing/router.py"),
    ("large python", f"{REPO}/src/atelier/gateway/adapters/mcp_server.py"),
    ("markdown docs", f"{REPO}/AGENTS.md"),
    ("test file", f"{REPO}/tests/gateway/test_mcp_jsonrpc_e2e.py"),
]

READ_CASES: list[tuple[str, dict[str, Any]]] = []
for idx in range(10):
    for label, file_path in _READ_BASE:
        READ_CASES.append((f"{label} [{idx + 1}]", {"file_path": file_path}))

# ---------------------------------------------------------------------------
# shell cases: (label, atelier args)
# ---------------------------------------------------------------------------
_SAFE_SHELL_BASE: list[tuple[str, str]] = [
    ("git log", "git log --oneline -10"),
    (".py file count", "find src -name '*.py' | wc -l"),
    ("test file list", "find tests -name 'test_*.py' | sort | head -15"),
    ("python version", "python --version 2>&1"),
    ("capabilities ls", "ls src/atelier/core/capabilities/"),
]

_RG_REWRITE_CASES: list[tuple[str, str]] = [
    # High-hit searches so rewrite impact is meaningful (not tiny one-line outputs).
    ("mcp_server defs", "rg '^def ' src/atelier/gateway/adapters/mcp_server.py"),
    ("smart_search defs", "rg '^def ' src/atelier/core/capabilities/tool_supervision/smart_search.py"),
    ("search_read defs", "rg '^def ' src/atelier/core/capabilities/tool_supervision/search_read.py"),
    ("mcp_server returns", "rg 'return ' src/atelier/gateway/adapters/mcp_server.py"),
    ("native_search blocks", "rg 'blocks' src/atelier/core/capabilities/tool_supervision/native_search.py"),
]

_CAT_REWRITE_FILES: list[str] = [
    "src/atelier/core/environment.py",
    "src/atelier/core/capabilities/tool_supervision/smart_search.py",
    "src/atelier/core/capabilities/tool_supervision/search_read.py",
]

SHELL_CASES: list[tuple[str, dict[str, Any]]] = []

# 40 safe cases (including truncation-heavy variants that should save tokens)
for idx in range(8):
    for label, cmd in _SAFE_SHELL_BASE:
        SHELL_CASES.append((f"{label} [{idx + 1}]", {"command": cmd, "cwd": REPO}))

# 5 rg->grep rewrite cases (now high-output)
for idx, (label, command) in enumerate(_RG_REWRITE_CASES, start=1):
    SHELL_CASES.append(
        (
            f"rewrite rg->grep {label} [{idx}]",
            {
                "command": command,
                "cwd": REPO,
                "_expect": {"exit_code": 0},
            },
        )
    )

# 3 cat->read rewrite cases
for idx, rel_path in enumerate(_CAT_REWRITE_FILES, start=1):
    SHELL_CASES.append(
        (
            f"rewrite cat->read [{idx}]",
            {
                "command": f"cat {rel_path}",
                "cwd": REPO,
                "_expect": {"exit_code": 0},
            },
        )
    )

_BLOCK_TARGETS = [
    "src/atelier/gateway/adapters/mcp_server.py",
    "src/atelier/core/capabilities/tool_supervision/search_read.py",
    "src/atelier/core/capabilities/tool_supervision/smart_search.py",
    "src/atelier/core/capabilities/tool_supervision/native_search.py",
    "src/benchmarks/tool_bench/runner.py",
]

# 10 blocked interpreter cases (5 bash + 5 sh)
for idx, rel_path in enumerate(_BLOCK_TARGETS, start=1):
    SHELL_CASES.append(
        (
            f"block bash interpreter [{idx}]",
            {
                "command": f"bash -c 'cat {rel_path}'",
                "cwd": REPO,
                "_expect": {"exit_code": -1},
            },
        )
    )
for idx, rel_path in enumerate(_BLOCK_TARGETS, start=1):
    SHELL_CASES.append(
        (
            f"block shell interpreter [{idx}]",
            {
                "command": f"sh -c 'cat {rel_path}'",
                "cwd": REPO,
                "_expect": {"exit_code": -1},
            },
        )
    )

# ---------------------------------------------------------------------------
# search cases: (label, atelier args)
# ---------------------------------------------------------------------------
_SEARCH_BASE_QUERIES: list[tuple[str, str]] = [
    ("detect_configured", "detect_configured_vendors"),
    ("NoFeasibleRouteError", "NoFeasibleRouteError"),
    ("tool_smart_read", "tool_smart_read"),
    ("CrossVendorRouter", "CrossVendorRouter"),
    ("mcp_tool decorator", "@mcp_tool"),
]
_SEARCH_BUDGETS = (1200, 1600, 2000, 2400, 3000)
_SEARCH_MAX_FILES = (2, 3)

SEARCH_CASES: list[tuple[str, dict[str, Any]]] = []
for label, query in _SEARCH_BASE_QUERIES:
    for budget in _SEARCH_BUDGETS:
        for max_files in _SEARCH_MAX_FILES:
            SEARCH_CASES.append(
                (
                    f"{label} [b{budget}-m{max_files}]",
                    {
                        "query": query,
                        "file_path": REPO,
                        "content_regex": query,
                        "file_glob_patterns": ["**/*.py"],
                        "budget_tokens": budget,
                        "max_files": max_files,
                        "max_chars_per_file": 240,
                        "mode": "chunks",
                    },
                )
            )

# ---------------------------------------------------------------------------
# grep cases: (label, atelier args)
# Content-returning modes only — file_paths_only/files_with_matches are trivial
# and produce the same output as rg --files-with-matches.
# ---------------------------------------------------------------------------
_GREP_BASE: list[tuple[str, dict[str, Any]]] = [
    # Single large file — high match count
    (
        "mcp_server def lines",
        {
            "file_path": f"{REPO}/src/atelier/gateway/adapters/mcp_server.py",
            "content_regex": r"^def ",
            "output_mode": "file_paths_with_content",
        },
    ),
    (
        "mcp_server return lines",
        {
            "file_path": f"{REPO}/src/atelier/gateway/adapters/mcp_server.py",
            "content_regex": r"return ",
            "output_mode": "file_paths_with_content",
        },
    ),
    # Multi-file directory scan
    (
        "capabilities class declarations",
        {
            "file_path": f"{REPO}/src/atelier/core/capabilities",
            "content_regex": r"^class ",
            "output_mode": "file_paths_with_content",
            "file_glob_patterns": ["**/*.py"],
        },
    ),
    (
        "tests assert statements",
        {
            "file_path": f"{REPO}/tests/gateway",
            "content_regex": r"^\s+assert ",
            "output_mode": "file_paths_with_content",
            "file_glob_patterns": ["*.py"],
            "context_budget_tokens": 3000,
        },
    ),
    # Context-heavy content in valid grep output mode.
    (
        "native_search def+context",
        {
            "file_path": f"{REPO}/src/atelier/core/capabilities/tool_supervision/native_search.py",
            "content_regex": r"^def ",
            "output_mode": "file_paths_with_content",
            "lines_before": 0,
            "lines_after": 3,
        },
    ),
]

GREP_CASES: list[tuple[str, dict[str, Any]]] = []
for idx in range(5):
    for label, args in _GREP_BASE:
        GREP_CASES.append((f"{label} [{idx + 1}]", args))

ALL_CASES: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "read": READ_CASES,
    "shell": SHELL_CASES,
    "search": SEARCH_CASES,
    "grep": GREP_CASES,
}
