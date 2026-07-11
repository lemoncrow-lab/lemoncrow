"""Benchmark cases for the public `compact` MCP tool."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from benchmarks.mcp_tools.harness import BenchCase
from benchmarks.mcp_tools.repo_facts import (
    collect_repo_file_facts,
    collect_symbol_facts,
    unique_symbol_facts,
)


def _repo_root() -> Path:
    value = os.environ.get("CLAUDE_WORKSPACE_ROOT")
    if value:
        return Path(value)
    return Path(__file__).resolve().parents[3]


def _assert_compact(result: dict[str, object], expected_tokens_before: int) -> None:
    assert "prompt_block" in result, "compact response must have 'prompt_block'"
    assert "tokens_before" in result, "compact response must have 'tokens_before'"
    assert "tokens_after_estimate" in result, "compact response must have 'tokens_after_estimate'"
    assert "tokens_freed" in result, "compact response must have 'tokens_freed'"
    assert "cost_saved_usd" in result, "compact response must have 'cost_saved_usd'"
    assert isinstance(result["prompt_block"], str), "'prompt_block' must be a string"
    assert result["prompt_block"], "compact response must return a non-empty prompt block"
    tokens_before = _result_int(result, "tokens_before")
    tokens_after_estimate = _result_int(result, "tokens_after_estimate")
    assert (
        tokens_before == expected_tokens_before
    ), f"tokens_before must match seeded ledger state ({expected_tokens_before}), got {result['tokens_before']}"
    assert (
        tokens_after_estimate <= expected_tokens_before
    ), f"tokens_after_estimate must not exceed tokens_before, got {result['tokens_after_estimate']}"


def _compact_assert(expected_tokens_before: int) -> Callable[[dict[str, object]], None]:
    def _assert(result: dict[str, object]) -> None:
        _assert_compact(result, expected_tokens_before)

    return _assert


def _result_int(result: dict[str, object], key: str) -> int:
    value = result[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AssertionError(f"{key} must be numeric, got {value!r}")
    return int(value)


def _build_compact_cases() -> list[BenchCase]:
    repo_root = _repo_root()
    symbols = unique_symbol_facts(collect_symbol_facts(repo_root)[0])[:300]
    files = collect_repo_file_facts(repo_root)
    assert len(symbols) == 300, "not enough unique symbols for generated compact cases"
    assert files, "compact benchmarks need repo files"

    cases: list[BenchCase] = []
    for index, symbol in enumerate(symbols, start=1):
        anchor = files[(index - 1) % len(files)]
        tokens_before = 1800 + index * 11
        seed = {
            "task": f"compact benchmark for {symbol.name}",
            "token_count": tokens_before,
            "files_touched": [symbol.path, anchor.path],
            "tools_called": ["symbols", "read", "grep", "context"],
            "commands_run": [f"rg -n {symbol.name} {symbol.path}"],
            "tests_run": [f"tests::{symbol.name}"],
            "errors_seen": [f"{symbol.name} validation warning"] if index % 5 == 0 else [],
            "repeated_failures": [f"{symbol.name} repeated failure"] if index % 7 == 0 else [],
            "verified_facts": [
                f"{symbol.name} lives in {symbol.path}",
                f"anchor file is {anchor.path}",
            ],
            "open_questions": [f"Should {symbol.name} move out of {symbol.path}?"],
            "active_playbooks": [symbol.name, anchor.path],
            "current_plan": [
                f"Inspect {symbol.path}",
                f"Trace callers of {symbol.name}",
                f"Document changes in {anchor.path}",
            ],
            "tool_events": [
                {"tool": "symbols", "args": {"query": symbol.name}, "output": symbol.path},
                {"tool": "read", "args": {"path": symbol.path}, "output": anchor.anchor_text},
            ],
            "command_events": [
                {"command": f"rg -n {symbol.name} src/lemoncrow", "ok": True, "stdout": symbol.path},
            ],
        }
        cases.append(
            BenchCase(
                op="compact",
                label=f"compact/session/{index:03d}",
                args={
                    "session_id": f"bench-compact-{index:03d}",
                    "_seed": seed,
                },
                assert_keys=[
                    "prompt_block",
                    "tokens_before",
                    "tokens_after_estimate",
                    "tokens_freed",
                    "cost_saved_usd",
                ],
                custom_assert=_compact_assert(tokens_before),
                baseline_tokens=0,  # fixed-constant baseline removed; savings not claimed (correctness-only)
            )
        )
    return cases


COMPACT_CASES = _build_compact_cases()
