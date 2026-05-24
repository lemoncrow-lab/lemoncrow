"""Benchmark cases for the `search` MCP tool.

Covers 2 modes: chunks and map.
"""

from __future__ import annotations

from benchmarks.mcp_tools.harness import BenchCase


def _assert_search(result: dict[str, object]) -> None:
    assert "mode" in result, "search response must have 'mode'"


def _assert_search_chunks(result: dict[str, object]) -> None:
    _assert_search(result)
    assert "backend" in result, "chunks response must have 'backend'"
    assert "cache_hit" in result, "chunks response must have 'cache_hit'"
    assert "matches" in result, "chunks response must have 'matches'"
    assert isinstance(result["matches"], list), "'matches' must be a list"
    assert isinstance(result["backend"], str), "'backend' must be a string"
    assert isinstance(result["cache_hit"], bool), "'cache_hit' must be bool"
    assert result["mode"] == "chunks", f"expected 'chunks' mode, got {result['mode']!r}"


def _assert_search_map(result: dict[str, object]) -> None:
    _assert_search(result)
    assert "outline" in result, "map response must have 'outline'"
    assert "ranked_files" in result, "map response must have 'ranked_files'"
    assert "token_count" in result, "map response must have 'token_count'"
    assert "budget_tokens" in result, "map response must have 'budget_tokens'"
    assert result["mode"] == "map", f"expected 'map' mode, got {result['mode']!r}"


SEARCH_CASES: list[BenchCase] = [
    BenchCase(
        op="search",
        label="search/chunks",
        args={
            "query": "get_context bootstrap repo warm status",
            "mode": "chunks",
            "max_files": 3,
            "include_meta": True,
        },
        assert_keys=["backend", "cache_hit", "matches", "mode"],
        custom_assert=_assert_search_chunks,
        # baseline = agent opens several files hunting for bootstrap logic (~8000 tokens)
        baseline_tokens=8000,
    ),
    BenchCase(
        op="search",
        label="search/map",
        args={
            "query": "smart search backend ripgrep zoekt selection",
            "mode": "map",
            "max_files": 2,
            "seed_files": [
                "src/atelier/gateway/adapters/mcp_server.py",
                "src/atelier/core/capabilities/tool_supervision/native_search.py",
            ],
            "include_meta": True,
        },
        assert_keys=["outline", "ranked_files", "token_count", "budget_tokens", "mode"],
        custom_assert=_assert_search_map,
        # baseline = agent opens several files to answer semantic query
        baseline_tokens=12000,
    ),
]
