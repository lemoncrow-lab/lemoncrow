"""Benchmark cases for the `grep` MCP tool.

Covers 3 output modes: file_paths_only, file_paths_with_match_count, ranked_file_map.

Baseline estimates:
  - paths_only: agent runs `find . -name "*.py"` + scans each file manually (~5000 tokens)
  - match_count: agent reads each matched file to count occurrences (~3000 tokens)
  - ranked_map: agent scans many files + reads relevant sections (~8000 tokens)

Note: The grep tool searches within CLAUDE_WORKSPACE_ROOT. The benchmark fixture
sets it to the actual repo root so patterns find real files.

Bug fixed: _iter_files previously returned no results when no file_glob_patterns or
type was specified with a directory path. Now falls back to walking all files.
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase


def _assert_grep_paths(result: dict[str, Any]) -> None:
    assert result.get("isError") is False, f"grep returned error: {result.get('content')}"
    assert "_meta" in result, "grep response must have '_meta'"
    meta = result["_meta"]
    assert "fileMatchCount" in meta, "_meta must have 'fileMatchCount'"
    assert meta["fileMatchCount"] > 0, (
        f"expected matches for 'def tool_', got fileMatchCount={meta['fileMatchCount']}"
    )
    content = result.get("content", [])
    assert isinstance(content, list), "'content' must be a list"
    assert len(content) > 0, "expected non-empty content list"


def _assert_grep_count(result: dict[str, Any]) -> None:
    assert result.get("isError") is False, f"grep returned error: {result.get('content')}"
    meta = result.get("_meta", {})
    assert meta.get("fileMatchCount", 0) > 0, (
        f"expected matches for 'BenchCase', got fileMatchCount={meta.get('fileMatchCount')}"
    )


def _assert_grep_ranked(result: dict[str, Any]) -> None:
    assert result.get("isError") is False, f"grep returned error: {result.get('content')}"
    meta = result.get("_meta", {})
    # ranked_file_map returns fileMatchCount or match count
    assert "fileMatchCount" in meta or "matchCount" in meta, (
        "ranked_file_map response must have fileMatchCount in _meta"
    )


GREP_CASES: list[BenchCase] = [
    BenchCase(
        op="grep",
        label="grep/file_paths_only",
        args={
            "path": "src",
            "content_regex": "def tool_",
            "output_mode": "file_paths_only",
        },
        assert_keys=["isError", "_meta", "content"],
        custom_assert=_assert_grep_paths,
        # baseline = agent reads directory listing then grep-scans manually (~5000 tokens)
        baseline_tokens=5000,
    ),
    BenchCase(
        op="grep",
        label="grep/match_count",
        args={
            "path": "src",
            "content_regex": "BenchCase",
            "output_mode": "file_paths_with_match_count",
            "file_glob_patterns": ["**/*.py"],
        },
        assert_keys=["isError", "_meta"],
        custom_assert=_assert_grep_count,
        # baseline = agent reads many Python files to count occurrences manually (~6000 tokens)
        baseline_tokens=6000,
    ),
    BenchCase(
        op="grep",
        label="grep/ranked_file_map",
        args={
            "path": "src",
            "content_regex": "search_workspace",
            "output_mode": "ranked_file_map",
            "type": "python",
            "context_budget_tokens": 2000,
        },
        assert_keys=["isError", "_meta"],
        custom_assert=_assert_grep_ranked,
        # baseline = agent reads multiple Python files to find search_workspace usages (~8000 tokens)
        baseline_tokens=8000,
    ),
]
