"""Benchmark cases for the `trace` MCP tool.

Covers 2 scenarios: success trace and partial trace with errors.

Baseline estimates:
  - success: agent manually writes structured trace as JSON to context (~200 tokens)
  - partial_with_errors: longer trace with error list + summary (~400 tokens)
"""

from __future__ import annotations

from typing import Any

from benchmarks.mcp_tools.harness import BenchCase


def _assert_trace(result: dict[str, Any]) -> None:
    assert "ok" in result, "trace response must have 'ok'"
    assert result["ok"] is True, f"trace 'ok' must be True, got {result['ok']}"
    assert "trace_id" in result, "trace response must have 'trace_id'"
    assert isinstance(result["trace_id"], str), "'trace_id' must be a string"
    assert len(result["trace_id"]) > 0, "'trace_id' must be non-empty"
    assert "stored" in result, "trace response must have 'stored'"
    assert "session_id" in result, "trace response must have 'session_id'"


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
        assert_keys=["ok", "trace_id", "stored", "session_id"],
        custom_assert=_assert_trace,
        # baseline = agent writes full trace payload + context summary manually
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
        assert_keys=["ok", "trace_id", "stored"],
        custom_assert=_assert_trace,
        # baseline = agent manually writes longer trace with errors + context payload
        baseline_tokens=2600,
    ),
]
