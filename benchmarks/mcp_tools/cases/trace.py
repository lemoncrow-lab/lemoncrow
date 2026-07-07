"""Benchmark cases for the `trace` MCP tool."""

from __future__ import annotations

from benchmarks.mcp_tools.harness import BenchCase


def _assert_trace(result: dict[str, object]) -> None:
    assert "trace_id" in result, "trace response must have 'trace_id'"
    assert "event_recorded" in result, "trace response must have 'event_recorded'"
    assert isinstance(result["trace_id"], str), "'trace_id' must be string"
    assert isinstance(result["event_recorded"], bool), "'event_recorded' must be bool"
    assert result["event_recorded"] is True, f"trace event must be recorded, got: {result}"


_TRACE_SPECS: list[dict[str, object]] = [
    {
        "label": "trace/success/basic",
        "agent": "bench",
        "domain": "benchmarking",
        "task": "run benchmark suite for memory tool",
        "status": "success",
        "output_summary": "All generated cases passed.",
        "learnings": ["memory archive seed should use tool_output source"],
    },
    {
        "label": "trace/success/validation",
        "agent": "bench",
        "domain": "verification",
        "task": "validate public MCP benchmark CSV export",
        "status": "success",
        "validation_results": [{"name": "csv-shape", "passed": True}],
        "capture_files": ["reports/benchmark/mcp/results.csv"],
    },
    {
        "label": "trace/partial/errors",
        "agent": "bench",
        "domain": "debug",
        "task": "rerun failing MCP benchmark shard",
        "status": "partial",
        "errors_seen": ["AssertionError: stale substring expectation"],
        "diff_summary": "Tightened generated benchmark selectors to stable symbols only.",
    },
    {
        "label": "trace/partial/tools",
        "agent": "bench",
        "domain": "debug",
        "task": "investigate code-intel benchmark failures",
        "status": "partial",
        "tools_called": ["atelier-read", "atelier-shell", "apply_patch"],
        "validation_results": [{"name": "focused-pytest", "passed": True}],
    },
    {
        "label": "trace/failed/provider",
        "agent": "bench",
        "domain": "providers",
        "task": "benchmark external provider startup",
        "status": "failed",
        "errors_seen": ["timeout waiting for provider index"],
        "event_type": "provider.startup_failed",
        "event_payload": {"provider": "cocoindex-code"},
    },
    {
        "label": "trace/success/capture",
        "agent": "bench",
        "domain": "analysis",
        "task": "capture provider comparison findings",
        "status": "success",
        "capture_sources": ["providers.csv", "summary.csv"],
        "capture_files": ["benchmark/providers/summary.csv"],
    },
    {
        "label": "trace/partial/monitoring",
        "agent": "bench",
        "domain": "monitoring",
        "task": "stream shard progress to parent process",
        "status": "partial",
        "event_type": "progress.snapshot",
        "event_payload": {"shard": "shard-2", "done": 31, "total": 300},
    },
    {
        "label": "trace/success/learning",
        "agent": "bench",
        "domain": "learning",
        "task": "record benchmark harness lessons",
        "status": "success",
        "learnings": [
            {"surface": "grep", "lesson": "preserve case for substring probes"},
        ],
    },
    {
        "label": "trace/success/sql",
        "agent": "bench",
        "domain": "sql",
        "task": "benchmark SQL batching behavior",
        "status": "success",
        "tools_called": ["sql"],
        "output_summary": "Batched three queries into one call.",
    },
    {
        "label": "trace/partial/edit",
        "agent": "bench",
        "domain": "edit",
        "task": "exercise atomic rollback cases",
        "status": "partial",
        "errors_seen": ["rollback triggered for invalid second descriptor"],
        "capture_files": ["file_a.py", "file_b.py"],
    },
    {
        "label": "trace/failed/session-limit",
        "agent": "bench",
        "domain": "eval",
        "task": "resume eval evaluation after prior partial run",
        "status": "failed",
        "errors_seen": ["session limit reached"],
        "event_type": "cli.session_limited",
    },
    {
        "label": "trace/success/provider-summary",
        "agent": "bench",
        "domain": "providers",
        "task": "summarize provider benchmark outcomes",
        "status": "success",
        "output_summary": "atelier-zoekt led exact and no-hit queries.",
        "validation_results": [{"name": "summary-csv", "passed": True}],
    },
]


TRACE_CASES: list[BenchCase] = [
    BenchCase(
        op="record_trace",
        label=str(spec["label"]),
        args={key: value for key, value in spec.items() if key != "label"},
        assert_keys=["trace_id", "event_recorded"],
        custom_assert=_assert_trace,
        baseline_tokens=0,  # fixed-constant baseline removed; savings not claimed (correctness-only)
    )
    for spec in _TRACE_SPECS
]
