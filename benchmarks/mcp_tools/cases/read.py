"""Benchmark cases for the `read` MCP tool.

Covers 300 real repo-backed scenarios:
- 100 full reads of small files
- 100 outline reads of large files
- 100 targeted range reads around real symbol anchors
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from benchmarks.mcp_tools.harness import BenchCase
from benchmarks.mcp_tools.repo_facts import benchmark_repo_root, collect_repo_file_facts

_TARGET_PER_FAMILY = 100


def _assert_read_common(result: dict[str, object], expected_path: str) -> None:
    assert "path" in result, "read response must have 'path'"
    actual_path = str(result["path"])
    assert actual_path.endswith(
        expected_path
    ), f"read response path must end with {expected_path!r}, got {actual_path!r}"


def _assert_read_full(result: dict[str, object], expected_path: str, expected_marker: str) -> None:
    _assert_read_common(result, expected_path)
    assert "content" in result, "read response must have 'content'"
    assert "mode" in result, "read response must have 'mode'"
    assert "tokens_saved" in result, "read response must have 'tokens_saved'"
    assert result["mode"] == "full", f"small file should use 'full' mode, got {result['mode']!r}"
    assert isinstance(result["content"], str), "'content' must be a string"
    assert expected_marker in result["content"], f"full read must include anchor text {expected_marker!r}"


def _assert_read_large(result: dict[str, object], expected_path: str, expected_symbols: tuple[str, ...]) -> None:
    _assert_read_common(result, expected_path)
    assert "mode" in result, "read response must have 'mode'"
    assert result["mode"] in {
        "outline",
        "full",
    }, f"large file should use 'outline' or 'full' mode, got {result['mode']!r}"
    if result["mode"] == "outline":
        assert "tokens_saved" in result, "read response must have 'tokens_saved'"
        tokens_saved = result["tokens_saved"]
        assert isinstance(tokens_saved, int), f"'tokens_saved' must be an int, got {type(tokens_saved).__name__}"
        assert tokens_saved > 0, f"outline mode must save tokens for large file, got tokens_saved={tokens_saved}"
        assert "outline" in result, "outline mode response must have 'outline'"
        outline_text = str(result["outline"])
        assert any(
            symbol in outline_text for symbol in expected_symbols
        ), f"outline must include one of {expected_symbols!r}, got {outline_text[:300]!r}"
        return
    assert "content" in result, "full-mode large read must have 'content'"
    content = result["content"]
    assert isinstance(content, str), "'content' must be a string"
    assert any(
        symbol in content for symbol in expected_symbols
    ), f"full-mode large read must include one of {expected_symbols!r}"


def _assert_read_range(result: dict[str, object], expected_path: str, expected_marker: str) -> None:
    _assert_read_common(result, expected_path)
    assert "content" in result, "read response must have 'content'"
    assert "range" in result, "read response must have 'range'"
    assert isinstance(result["content"], str), "'content' must be a string"
    assert expected_marker in result["content"], f"range read must include anchor text {expected_marker!r}"


def _full_assert(expected_path: str, expected_marker: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_read_full(result, expected_path, expected_marker)

    return _assert


def _outline_assert(expected_path: str, expected_symbols: tuple[str, ...]) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_read_large(result, expected_path, expected_symbols)

    return _assert


def _range_assert(expected_path: str, expected_marker: str) -> Callable[[dict[str, Any]], None]:
    def _assert(result: dict[str, Any]) -> None:
        _assert_read_range(result, expected_path, expected_marker)

    return _assert


def _build_read_cases() -> list[BenchCase]:
    file_facts = collect_repo_file_facts(benchmark_repo_root())
    small_files = [
        fact for fact in file_facts if 5 <= fact.line_count <= 120 and fact.char_count <= 5000 and fact.anchor_text
    ][:_TARGET_PER_FAMILY]
    large_files = [fact for fact in file_facts if fact.line_count >= 220 and len(fact.symbols) >= 3][
        :_TARGET_PER_FAMILY
    ]
    range_files = [fact for fact in file_facts if fact.line_count >= 40 and fact.anchor_text][:_TARGET_PER_FAMILY]

    assert len(small_files) == _TARGET_PER_FAMILY, "not enough small files for read benchmark"
    assert len(large_files) == _TARGET_PER_FAMILY, "not enough large files for read benchmark"
    assert len(range_files) == _TARGET_PER_FAMILY, "not enough range files for read benchmark"

    cases: list[BenchCase] = []
    for index, fact in enumerate(small_files, start=1):
        cases.append(
            BenchCase(
                op="read",
                label=f"read/full/{index:03d}",
                args={"path": fact.path, "include_meta": True},
                assert_keys=["content", "path", "mode", "tokens_saved"],
                custom_assert=_full_assert(fact.path, fact.anchor_text),
                baseline_tokens=max(1200, fact.char_count // 2),
            )
        )
    for index, fact in enumerate(large_files, start=1):
        cases.append(
            BenchCase(
                op="read",
                label=f"read/outline/{index:03d}",
                args={"path": fact.path, "include_meta": True},
                assert_keys=["mode", "tokens_saved", "outline", "path"],
                custom_assert=_outline_assert(fact.path, fact.symbols[:3]),
                baseline_tokens=max(12_000, fact.char_count // 2),
            )
        )
    for index, fact in enumerate(range_files, start=1):
        start = max(1, fact.anchor_line - 2)
        end = min(fact.line_count, start + 14)
        cases.append(
            BenchCase(
                op="read",
                label=f"read/range/{index:03d}",
                args={"path": fact.path, "range": f"{start}-{end}", "include_meta": True},
                assert_keys=["content", "range", "path"],
                custom_assert=_range_assert(fact.path, fact.anchor_text),
                baseline_tokens=max(1000, fact.char_count // 3),
            )
        )
    return cases


READ_CASES: list[BenchCase] = _build_read_cases()
