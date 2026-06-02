"""Benchmark cases for the `search` MCP tool.

Covers 300 real repo-backed scenarios:
- 150 chunks-mode lookups
- 150 map-mode lookups
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from benchmarks.mcp_tools.harness import BenchCase
from benchmarks.mcp_tools.repo_facts import (
    benchmark_repo_root,
    collect_repo_file_facts,
    collect_symbol_facts,
    stable_symbol_facts,
    unique_substring_queries,
    unique_symbol_facts,
)

_TARGET_PER_MODE = 150


def _assert_search_common(result: dict[str, object], expected_mode: str) -> None:
    assert "mode" in result, "search response must have 'mode'"
    assert result["mode"] == expected_mode, f"expected {expected_mode!r} mode, got {result['mode']!r}"


def _assert_search_chunks(result: dict[str, object], expected_path: str, expected_needle: str) -> None:
    _assert_search_common(result, "chunks")
    assert "backend" in result, "chunks response must have 'backend'"
    assert "cache_hit" in result, "chunks response must have 'cache_hit'"
    assert "matches" in result, "chunks response must have 'matches'"
    matches = result["matches"]
    assert isinstance(matches, list), "'matches' must be a list"
    assert matches, "chunks mode must return at least one match"
    match_text = str(matches)
    assert expected_path in match_text, f"chunks response must include {expected_path!r}"
    assert expected_needle in str(result), f"chunks response must include {expected_needle!r}"


def _assert_search_map(result: dict[str, object], expected_path: str) -> None:
    _assert_search_common(result, "map")
    assert "outline" in result, "map response must have 'outline'"
    assert "ranked_files" in result, "map response must have 'ranked_files'"
    assert "token_count" in result, "map response must have 'token_count'"
    assert "budget_tokens" in result, "map response must have 'budget_tokens'"
    ranked_files = result["ranked_files"]
    assert isinstance(ranked_files, list), "'ranked_files' must be a list"
    assert ranked_files, "map mode must return ranked files"
    assert expected_path in str(ranked_files), f"map response must include {expected_path!r}"


def _chunks_assert(expected_path: str, expected_needle: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_search_chunks(result, expected_path, expected_needle)

    return _assert


def _map_assert(expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_search_map(result, expected_path)

    return _assert


def _seed_files(expected_path: str, all_paths: list[str]) -> list[str]:
    seeds = [expected_path]
    for path in all_paths:
        if path != expected_path:
            seeds.append(path)
            break
    return seeds


def _build_search_cases() -> list[BenchCase]:
    repo_root = benchmark_repo_root()
    symbol_facts, _ = collect_symbol_facts(repo_root)
    unique_symbols = stable_symbol_facts(unique_symbol_facts(symbol_facts))
    substring_pairs = unique_substring_queries(repo_root, unique_symbols)
    file_paths = [fact.path for fact in collect_repo_file_facts(repo_root)]

    chunk_symbols = unique_symbols[:75]
    chunk_substrings = substring_pairs[:75]
    map_symbols = unique_symbols[75 : 75 + _TARGET_PER_MODE]

    assert len(chunk_symbols) == 75, "not enough unique symbols for chunk search benchmark"
    assert len(chunk_substrings) == 75, "not enough unique substrings for chunk search benchmark"
    assert len(map_symbols) == _TARGET_PER_MODE, "not enough unique symbols for map search benchmark"

    cases: list[BenchCase] = []
    for index, symbol in enumerate(chunk_symbols, start=1):
        cases.append(
            BenchCase(
                op="search",
                label=f"search/chunks-symbol/{index:03d}",
                args={
                    "query": symbol.name,
                    "mode": "chunks",
                    "max_files": 4,
                    "include_meta": True,
                },
                assert_keys=["backend", "cache_hit", "matches", "mode"],
                custom_assert=_chunks_assert(symbol.path, symbol.name),
                baseline_tokens=8000,
            )
        )
    for index, (token, symbol) in enumerate(chunk_substrings, start=1):
        cases.append(
            BenchCase(
                op="search",
                label=f"search/chunks-substring/{index:03d}",
                args={
                    "query": token,
                    "mode": "chunks",
                    "max_files": 4,
                    "include_meta": True,
                },
                assert_keys=["backend", "cache_hit", "matches", "mode"],
                custom_assert=_chunks_assert(symbol.path, token),
                baseline_tokens=8000,
            )
        )
    for index, symbol in enumerate(map_symbols, start=1):
        cases.append(
            BenchCase(
                op="search",
                label=f"search/map/{index:03d}",
                args={
                    "query": symbol.name,
                    "mode": "map",
                    "max_files": 3,
                    "seed_files": _seed_files(symbol.path, file_paths),
                    "include_meta": True,
                },
                assert_keys=["outline", "ranked_files", "token_count", "budget_tokens", "mode"],
                custom_assert=_map_assert(symbol.path),
                baseline_tokens=12_000,
            )
        )
    return cases


SEARCH_CASES: list[BenchCase] = _build_search_cases()
