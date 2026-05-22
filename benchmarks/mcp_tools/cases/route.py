"""Benchmark cases for the `route` MCP tool.

Covers:
  decide (cheap/balanced/best budgets, with and without route config)
  spawn  (no-sampling fallback path)
  recommend (hidden op, backward compat)
  verify (hidden op, backward compat)

Baseline estimates are the token cost an agent would incur WITHOUT route:
manually picking a model by scanning docs / vendor pages, or just always
using the host model with no cost awareness.
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase


def _assert_decide_shape(result: dict[str, Any]) -> None:
    assert "model" in result, f"decide response must have 'model', got: {list(result)}"
    assert "tier" in result, f"decide response must have 'tier', got: {list(result)}"
    assert "available_models" in result, f"decide response must have 'available_models', got: {list(result)}"
    assert isinstance(result["available_models"], list), "'available_models' must be a list"
    assert "sampling_supported" in result, f"decide response must have 'sampling_supported', got: {list(result)}"
    assert "_summary" in result, f"decide response must have '_summary', got: {list(result)}"
    summary = result["_summary"]
    assert "recommended" in summary, f"_summary must have 'recommended', got: {summary}"
    assert "budget" in summary, f"_summary must have 'budget', got: {summary}"
    assert "can_spawn" in summary, f"_summary must have 'can_spawn', got: {summary}"


def _assert_decide_cheap(result: dict[str, Any]) -> None:
    _assert_decide_shape(result)
    assert result["tier"] == "cheap", f"budget=cheap must yield tier=cheap, got: {result['tier']}"


def _assert_spawn_no_sampling(result: dict[str, Any]) -> None:
    assert result.get("sampling_supported") is False, (
        f"spawn without sampling must return sampling_supported=False, got: {result}"
    )
    assert "error" in result, "spawn fallback must include 'error' with instructions"
    assert "prompt" in result, "spawn fallback must echo 'prompt'"
    assert "model_hint" in result, "spawn fallback must echo 'model_hint'"


ROUTE_CASES: list[BenchCase] = [
    # ── op=decide ──────────────────────────────────────────────────────────
    BenchCase(
        op="decide",
        label="decide/balanced-feature",
        args={"op": "decide", "task": "implement a new REST endpoint for user profiles", "task_type": "feature"},
        assert_keys=["model", "tier", "available_models", "sampling_supported", "_summary"],
        custom_assert=_assert_decide_shape,
        baseline_description=(
            "Agent manually reads vendor docs or pricing pages to decide which model to use, "
            "then copies the model name into its instructions — ~1000 tokens of reading."
        ),
        baseline_tokens=1000,
    ),
    BenchCase(
        op="decide",
        label="decide/cheap-explain",
        args={"op": "decide", "task": "summarize what this function does", "task_type": "explain", "budget": "cheap"},
        assert_keys=["model", "tier", "sampling_supported"],
        custom_assert=_assert_decide_cheap,
        baseline_description="Agent defaults to current session model without cost awareness — no savings.",
        baseline_tokens=600,
    ),
    BenchCase(
        op="decide",
        label="decide/best-debug",
        args={"op": "decide", "task": "debug a hard concurrency race condition", "task_type": "debug", "budget": "best"},
        assert_keys=["model", "tier", "available_models", "_summary"],
        custom_assert=_assert_decide_shape,
        baseline_description="Agent checks which model to use — ~600 tokens guessing.",
        baseline_tokens=600,
    ),
    BenchCase(
        op="decide",
        label="decide/no-config-fallback",
        args={"op": "decide", "task": "refactor the auth module"},
        assert_keys=["model", "available_models"],
        custom_assert=_assert_decide_shape,
        baseline_description="Agent has no routing info — uses default model.",
        baseline_tokens=0,
    ),
    # ── op=spawn (no-sampling host path) ───────────────────────────────────
    BenchCase(
        op="spawn",
        label="spawn/no-sampling-fallback",
        args={"op": "spawn", "prompt": "List the top 3 causes of this bug", "model": "claude-haiku-4-5"},
        custom_assert=_assert_spawn_no_sampling,
        baseline_description="Agent tries to spawn a sub-task manually — unclear outcome.",
        baseline_tokens=0,
    ),
]
