"""Benchmark cases for the `search` MCP tool.

Covers 2 modes: chunks and full.

Baseline estimates:
  - chunks: agent reads multiple candidate files to find relevant code (~8000 tokens)
  - full: agent opens several full files to answer a semantic query (~12000 tokens)
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase


def _assert_search(result: dict[str, Any]) -> None:
    assert "backend" in result, "search response must have 'backend'"
    assert "cache_hit" in result, "search response must have 'cache_hit'"
    assert "matches" in result, "search response must have 'matches'"
    assert "mode" in result, "search response must have 'mode'"
    assert isinstance(result["matches"], list), "'matches' must be a list"
    assert isinstance(result["backend"], str), "'backend' must be a string"
    assert isinstance(result["cache_hit"], bool), "'cache_hit' must be bool"


def _assert_search_chunks(result: dict[str, Any]) -> None:
    _assert_search(result)
    assert result["mode"] == "chunks", f"expected 'chunks' mode, got {result['mode']!r}"


def _assert_search_full(result: dict[str, Any]) -> None:
    _assert_search(result)
    assert result["mode"] == "full", f"expected 'full' mode, got {result['mode']!r}"


SEARCH_CASES: list[BenchCase] = [
    BenchCase(
        op="search",
        label="search/chunks",
        args={
            "query": "get_context bootstrap repo warm status",
            "mode": "chunks",
            "max_files": 3,
        },
        assert_keys=["backend", "cache_hit", "matches", "mode"],
        custom_assert=_assert_search_chunks,
        # baseline = agent opens several files hunting for bootstrap logic (~8000 tokens)
        baseline_tokens=8000,
    ),
    BenchCase(
        op="search",
        label="search/full",
        args={
            "query": "smart search backend ripgrep zoekt selection",
            "mode": "full",
            "max_files": 2,
        },
        assert_keys=["backend", "cache_hit", "matches", "mode"],
        custom_assert=_assert_search_full,
        # baseline = agent opens full files to answer semantic query (~12000 tokens)
        baseline_tokens=12000,
    ),
]
