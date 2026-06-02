"""Benchmark cases for the `grep` MCP tool.

Covers 300 real repo-backed scenarios:
- 100 file_paths_only lookups
- 100 file_paths_with_match_count lookups
- 100 ranked_file_map lookups
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from benchmarks.mcp_tools.harness import BenchCase
from benchmarks.mcp_tools.repo_facts import (
    benchmark_repo_root,
    collect_symbol_facts,
    stable_symbol_facts,
    unique_substring_queries,
    unique_symbol_facts,
)

_TARGET_PER_MODE = 100


def _assert_meta(result: dict[str, object]) -> dict[str, object]:
    assert "_meta" in result, "grep response must have '_meta'"
    meta = result["_meta"]
    assert isinstance(meta, dict), "'_meta' must be a dict"
    assert "fileMatchCount" in meta, "_meta must have 'fileMatchCount'"
    file_match_count = meta["fileMatchCount"]
    assert isinstance(file_match_count, int), f"fileMatchCount must be int, got {type(file_match_count).__name__}"
    assert file_match_count > 0, f"expected matches, got fileMatchCount={file_match_count}"
    return meta


def _assert_grep_paths(result: dict[str, object], expected_path: str) -> None:
    _assert_meta(result)
    content = result.get("content", [])
    assert isinstance(content, list), "'content' must be a list"
    assert content, "expected non-empty content list"
    assert any(expected_path in str(item) for item in content), f"paths-only grep must include {expected_path!r}"


def _assert_grep_count(result: dict[str, object], expected_path: str) -> None:
    _assert_meta(result)
    content = result.get("content", [])
    assert isinstance(content, list), "'content' must be a list"
    assert content, "expected non-empty content list"
    assert any(expected_path in str(item) for item in content), f"match-count grep must include {expected_path!r}"


def _assert_grep_ranked(result: dict[str, object], expected_path: str) -> None:
    _assert_meta(result)
    assert "matches" in result, "ranked_file_map must contain matches"
    matches = result["matches"]
    assert isinstance(matches, list), "'matches' must be a list"
    assert matches, "ranked_file_map must return at least one match"
    assert expected_path in str(matches), f"ranked_file_map must include {expected_path!r}"


def _paths_assert(expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_grep_paths(result, expected_path)

    return _assert


def _count_assert(expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_grep_count(result, expected_path)

    return _assert


def _ranked_assert(expected_path: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_grep_ranked(result, expected_path)

    return _assert


def _build_grep_cases() -> list[BenchCase]:
    repo_root = benchmark_repo_root()
    symbol_facts, _ = collect_symbol_facts(repo_root)
    unique_symbols = stable_symbol_facts(unique_symbol_facts(symbol_facts))
    substring_pairs = unique_substring_queries(repo_root, unique_symbols)

    path_symbols = unique_symbols[:_TARGET_PER_MODE]
    count_symbols = unique_symbols[_TARGET_PER_MODE : _TARGET_PER_MODE * 2]
    ranked_pairs = substring_pairs[:_TARGET_PER_MODE]

    assert len(path_symbols) == _TARGET_PER_MODE, "not enough unique symbols for grep paths cases"
    assert len(count_symbols) == _TARGET_PER_MODE, "not enough unique symbols for grep count cases"
    assert len(ranked_pairs) == _TARGET_PER_MODE, "not enough substring pairs for grep ranked cases"

    cases: list[BenchCase] = []
    for index, symbol in enumerate(path_symbols, start=1):
        cases.append(
            BenchCase(
                op="grep",
                label=f"grep/file_paths_only/{index:03d}",
                args={
                    "path": "src",
                    "content_regex": re.escape(symbol.name),
                    "output_mode": "file_paths_only",
                    "include_meta": True,
                },
                assert_keys=["_meta", "content"],
                custom_assert=_paths_assert(symbol.path),
                baseline_tokens=5000,
            )
        )
    for index, symbol in enumerate(count_symbols, start=1):
        cases.append(
            BenchCase(
                op="grep",
                label=f"grep/match_count/{index:03d}",
                args={
                    "path": "src",
                    "content_regex": re.escape(symbol.name),
                    "output_mode": "file_paths_with_match_count",
                    "file_glob_patterns": ["**/*.py"],
                    "include_meta": True,
                },
                assert_keys=["_meta", "content"],
                custom_assert=_count_assert(symbol.path),
                baseline_tokens=6000,
            )
        )
    for index, (token, symbol) in enumerate(ranked_pairs, start=1):
        cases.append(
            BenchCase(
                op="grep",
                label=f"grep/ranked_file_map/{index:03d}",
                args={
                    "path": "src",
                    "content_regex": re.escape(token),
                    "output_mode": "ranked_file_map",
                    "type": "python",
                    "context_budget_tokens": 2000,
                    "include_meta": True,
                },
                assert_keys=[
                    "_meta",
                    "matches",
                    "mode",
                    "next",
                    "context_budget_tokens",
                ],
                custom_assert=_ranked_assert(symbol.path),
                baseline_tokens=8000,
            )
        )
    return cases


GREP_CASES: list[BenchCase] = _build_grep_cases()
