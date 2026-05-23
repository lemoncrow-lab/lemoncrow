"""Benchmark cases for the `route` MCP tool.

Covers:
  decide (cheap/balanced/best budgets, with and without route config)
  spawn  (directive fallback when no CLI; live subprocess when CLI available)

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
    assert "can_spawn" in result, f"decide response must have 'can_spawn', got: {list(result)}"
    assert "_summary" in result, f"decide response must have '_summary', got: {list(result)}"
    summary = result["_summary"]
    assert "recommended" in summary, f"_summary must have 'recommended', got: {summary}"
    assert "budget" in summary, f"_summary must have 'budget', got: {summary}"
    assert "can_spawn" in summary, f"_summary must have 'can_spawn', got: {summary}"


def _assert_decide_cheap(result: dict[str, Any]) -> None:
    _assert_decide_shape(result)
    assert result["tier"] == "cheap", f"budget=cheap must yield tier=cheap, got: {result['tier']}"


def _assert_spawn_directive(result: dict[str, Any]) -> None:
    """spawn with no CLI available: must return handled=false + spawn_directive."""
    assert result.get("handled") is False, f"expected handled=false, got: {result.get('handled')}"
    assert "spawn_directive" in result, f"spawn must return spawn_directive, got: {list(result)}"
    directive = result["spawn_directive"]
    assert "prompt" in directive, f"spawn_directive must have 'prompt', got: {directive}"
    assert "agent_type" in directive, f"spawn_directive must have 'agent_type', got: {directive}"
    assert "SPAWN_REQUIRED" in result.get("message", ""), f"missing SPAWN_REQUIRED in message: {result.get('message')}"


def _assert_spawn_subprocess(result: dict[str, Any]) -> None:
    """spawn with CLI available: handled=true OR falls back to directive if CLI errors."""
    assert "handled" in result, f"spawn must return 'handled', got: {list(result)}"
    assert "spawn_directive" in result, f"spawn must always return spawn_directive, got: {list(result)}"
    if result["handled"]:
        assert result.get("spawn_method") == "cli_subprocess", f"unexpected spawn_method: {result.get('spawn_method')}"
        assert "response" in result, "handled=true spawn must have 'response'"


ROUTE_CASES: list[BenchCase] = [
    # ── op=decide ──────────────────────────────────────────────────────────
    BenchCase(
        op="decide",
        label="decide/balanced-feature",
        args={"op": "decide", "task": "implement a new REST endpoint for user profiles", "task_type": "feature"},
        assert_keys=["model", "tier", "available_models", "can_spawn", "_summary"],
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
        assert_keys=["model", "tier", "can_spawn"],
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
    # ── op=spawn (no CLI — directive fallback) ─────────────────────────────
    BenchCase(
        op="spawn",
        label="spawn/directive-no-cli",
        args={"op": "spawn", "prompt": "List the top 3 causes of this bug", "model": "claude-haiku-4-5"},
        custom_assert=_assert_spawn_directive,
        baseline_description="Agent tries to spawn a sub-task manually — unclear outcome.",
        baseline_tokens=0,
    ),
    # ── op=spawn (real subprocess via claude CLI) ──────────────────────────
    BenchCase(
        op="spawn",
        label="spawn/subprocess-live",
        args={
            "op": "spawn",
            "prompt": 'Reply with exactly: {"status":"ok","agent":"haiku"}',
            "model": "claude-haiku-4-5",
        },
        custom_assert=_assert_spawn_subprocess,
        baseline_description="Real subprocess spawn via claude CLI — verifies end-to-end delegation.",
        baseline_tokens=0,
    ),
]

