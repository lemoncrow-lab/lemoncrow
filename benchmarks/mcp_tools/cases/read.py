"""Benchmark cases for the `read` MCP tool.

Covers 3 scenarios: small file (full mode), large file (outline mode), range read.

Baseline estimates:
  - small: naive `cat file` — ~800 tokens
  - large: naive `cat large_file` — mcp_server.py is ~30k tokens
  - range: naive `cat file | head -30` — ~150 tokens (still reads full file first)

Note: CLAUDE_WORKSPACE_ROOT must be set to the repo root for relative paths to resolve.
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase

_SMALL_FILE = "src/atelier/core/environment.py"
_LARGE_FILE = "src/atelier/gateway/adapters/mcp_server.py"
_RANGE_FILE = "src/atelier/core/environment.py"


def _assert_read_full(result: dict[str, Any]) -> None:
    assert "content" in result, "read response must have 'content'"
    assert "path" in result, "read response must have 'path'"
    assert "mode" in result, "read response must have 'mode'"
    assert "tokens_saved" in result, "read response must have 'tokens_saved'"
    assert result["mode"] == "full", f"small file should use 'full' mode, got {result['mode']!r}"
    assert isinstance(result["content"], str), "'content' must be a string"
    assert len(result["content"]) > 0, "'content' must be non-empty"


def _assert_read_outline(result: dict[str, Any]) -> None:
    assert "mode" in result, "read response must have 'mode'"
    assert result["mode"] == "outline", f"large file should use 'outline' mode, got {result['mode']!r}"
    assert "tokens_saved" in result, "read response must have 'tokens_saved'"
    assert result["tokens_saved"] > 0, (
        f"outline mode must save tokens for large file, got tokens_saved={result['tokens_saved']}"
    )
    assert "outline" in result, "outline mode response must have 'outline'"
    assert isinstance(result["outline"], dict), "'outline' must be a dict"


def _assert_read_range(result: dict[str, Any]) -> None:
    assert "content" in result, "read response must have 'content'"
    assert "range" in result, "read response must have 'range'"
    assert isinstance(result["content"], str), "'content' must be a string"
    assert len(result["content"]) > 0, "range content must be non-empty"


READ_CASES: list[BenchCase] = [
    BenchCase(
        op="read",
        label="read/small_file",
        args={"file_path": _SMALL_FILE},
        assert_keys=["content", "path", "mode", "tokens_saved"],
        custom_assert=_assert_read_full,
        # baseline = naive cat of environment.py with framing/selection overhead (~1000 tokens)
        baseline_tokens=1000,
    ),
    BenchCase(
        op="read",
        label="read/large_file_outline",
        args={"file_path": _LARGE_FILE},
        assert_keys=["mode", "tokens_saved", "outline"],
        custom_assert=_assert_read_outline,
        # baseline = naive cat of mcp_server.py (>30k tokens)
        baseline_tokens=30000,
    ),
    BenchCase(
        op="read",
        label="read/range",
        args={"file_path": _RANGE_FILE, "range": "1-20"},
        assert_keys=["content", "range"],
        custom_assert=_assert_read_range,
        # baseline = naive cat of the full file even to read 20 lines (~800 tokens)
        baseline_tokens=800,
    ),
]
