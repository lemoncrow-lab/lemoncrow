"""Benchmark cases for the `trace` MCP tool."""

from __future__ import annotations

from benchmarks.mcp_tools.harness import BenchCase


def _assert_trace(result: dict[str, object]) -> None:
    assert "trace_id" in result, "trace response must have 'trace_id'"
    assert "event_recorded" in result, "trace response must have 'event_recorded'"
    assert isinstance(result["trace_id"], str), "'trace_id' must be string"
    assert isinstance(result["event_recorded"], bool), "'event_recorded' must be bool"


TRACE_CASES: list[BenchCase] = [
    BenchCase(
        op="record_trace",
        label="trace/success",
        args={
            "agent": "bench",
            "domain": "test",
            "task": "run benchmark suite for memory tool",
            "status": "success",
            "output_summary": "All 8 cases passed with avg 82% savings.",
            "learnings": ["memory.archive requires source in allowed literals"],
        },
        assert_keys=["trace_id", "event_recorded"],
        custom_assert=_assert_trace,
        baseline_tokens=2600,
    ),
    BenchCase(
        op="record_trace",
        label="trace/partial",
        args={
            "agent": "bench",
            "domain": "test",
            "task": "run benchmark suite for route tool",
            "status": "partial",
            "errors_seen": ["AssertionError: can_spawn key missing", "KeyError: sampling_supported"],
            "diff_summary": "Fixed route case assertions to match actual response shape",
            "validation_results": [
                {"name": "correctness", "passed": True, "detail": "5/5 route cases pass"},
            ],
        },
        assert_keys=["trace_id", "event_recorded"],
        custom_assert=_assert_trace,
        baseline_tokens=2600,
    ),
]
