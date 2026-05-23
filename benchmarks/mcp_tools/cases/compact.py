"""Benchmark cases for the `compact` MCP tool.

Covers 3 ops: output, advise, score.

Baseline estimates represent what an agent would spend without Atelier:
  - output: manually truncating/summarising large tool output in context (≈ original_tokens)
  - advise: agent parsing full ledger JSON to decide compaction (≈ 800 tokens)
  - score: agent writing a JSON blob to track complexity in context (≈ 150 tokens)
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase

_LONG_CONTENT = ("The system returned a large response with many details. " * 60).strip()


def _assert_output(result: dict[str, Any]) -> None:
    assert "compacted" in result, "output response must have 'compacted'"
    assert "original_tokens" in result, "output response must have 'original_tokens'"
    assert "compacted_tokens" in result, "output response must have 'compacted_tokens'"
    assert "method" in result, "output response must have 'method'"
    assert result["original_tokens"] > 0, "original_tokens must be > 0"
    assert result["compacted_tokens"] > 0, "compacted_tokens must be > 0"


def _assert_advise(result: dict[str, Any]) -> None:
    assert "should_compact" in result, "advise response must have 'should_compact'"
    assert "should_advise" in result, "advise response must have 'should_advise'"
    assert isinstance(result["should_compact"], bool), "'should_compact' must be bool"
    assert "utilisation_pct" in result, "advise response must have 'utilisation_pct'"
    assert "suggested_prompt" in result, "advise response must have 'suggested_prompt'"


def _assert_score(result: dict[str, Any]) -> None:
    assert "complexity" in result, "score response must have 'complexity'"
    assert "message" in result, "score response must have 'message'"
    assert "must_keep_count" in result, "score response must have 'must_keep_count'"
    assert 0.0 <= result["complexity"] <= 1.0, "complexity must be between 0 and 1"
    assert result["must_keep_count"] == 2, f"expected 2 must_keep items, got {result['must_keep_count']}"


COMPACT_CASES: list[BenchCase] = [
    BenchCase(
        op="output",
        label="output/compress",
        args={
            "op": "output",
            "content": _LONG_CONTENT,
            "content_type": "tool_output",
            "budget_tokens": 200,
        },
        assert_keys=["compacted", "original_tokens", "compacted_tokens", "method"],
        custom_assert=_assert_output,
        # baseline = agent keeps full content in context (≈ len/4 tokens)
        baseline_tokens=len(_LONG_CONTENT) // 4,
    ),
    BenchCase(
        op="advise",
        label="advise/utilisation",
        args={"op": "advise"},
        assert_keys=["should_compact", "should_advise", "utilisation_pct"],
        custom_assert=_assert_advise,
        # baseline = agent reads full ledger JSON (~800 tokens) to decide
        baseline_tokens=800,
    ),
    BenchCase(
        op="score",
        label="score/complexity",
        args={
            "op": "score",
            "complexity": 0.7,
            "must_keep": ["tool_memory arbitration", "bootstrap warm"],
        },
        assert_keys=["complexity", "message", "must_keep_count"],
        custom_assert=_assert_score,
        # baseline = agent writes complexity annotation to context manually (~150 tokens)
        baseline_tokens=150,
    ),
]
