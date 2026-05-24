"""Benchmark cases for the `route` MCP tool."""

from __future__ import annotations

from benchmarks.mcp_tools.harness import BenchCase


def _assert_route_shape(result: dict[str, object]) -> None:
    assert "model" in result, f"route response must have 'model', got: {list(result)}"
    assert "tier" in result, f"route response must have 'tier', got: {list(result)}"
    assert "route_tier" in result, f"route response must have 'route_tier', got: {list(result)}"
    assert "rationale" in result, f"route response must have 'rationale', got: {list(result)}"


def _assert_route_cheap(result: dict[str, object]) -> None:
    _assert_route_shape(result)
    assert result["tier"] == "cheap", f"budget=cheap must yield tier=cheap, got: {result['tier']}"


def _assert_route_best(result: dict[str, object]) -> None:
    _assert_route_shape(result)
    assert result["tier"] in {"best", "high"}, f"budget=best must yield high-capability tier, got: {result['tier']}"


ROUTE_CASES: list[BenchCase] = [
    BenchCase(
        op="route",
        label="route/balanced-feature",
        args={"task": "implement a new REST endpoint for user profiles", "task_type": "feature"},
        assert_keys=["model", "tier", "route_tier", "rationale"],
        custom_assert=_assert_route_shape,
        baseline_tokens=1000,
    ),
    BenchCase(
        op="route",
        label="route/cheap-explain",
        args={"task": "summarize what this function does", "task_type": "explain", "budget": "cheap"},
        assert_keys=["model", "tier", "route_tier", "rationale"],
        custom_assert=_assert_route_cheap,
        baseline_tokens=600,
    ),
    BenchCase(
        op="route",
        label="route/best-debug",
        args={"task": "debug a hard concurrency race condition", "task_type": "debug", "budget": "best"},
        assert_keys=["model", "tier", "route_tier", "rationale"],
        custom_assert=_assert_route_best,
        baseline_tokens=600,
    ),
]
