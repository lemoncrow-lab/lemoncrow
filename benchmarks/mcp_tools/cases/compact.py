"""Benchmark cases for the `compact` MCP tool."""

from __future__ import annotations

from benchmarks.mcp_tools.harness import BenchCase


def _assert_compact(result: dict[str, object]) -> None:
    assert "prompt_block" in result, "compact response must have 'prompt_block'"
    assert "tokens_before" in result, "compact response must have 'tokens_before'"
    assert "tokens_after_estimate" in result, "compact response must have 'tokens_after_estimate'"
    assert "tokens_freed" in result, "compact response must have 'tokens_freed'"
    assert "cost_saved_usd" in result, "compact response must have 'cost_saved_usd'"
    assert isinstance(result["prompt_block"], str), "'prompt_block' must be a string"


COMPACT_CASES: list[BenchCase] = [
    BenchCase(
        op="compact",
        label="compact/default",
        args={},
        assert_keys=["prompt_block", "tokens_before", "tokens_after_estimate", "tokens_freed", "cost_saved_usd"],
        custom_assert=_assert_compact,
        baseline_tokens=600,
    ),
]
