from __future__ import annotations

from datetime import UTC, datetime

from lemoncrow.pro.capabilities.optimization.runtime_decisions import (
    OptimizationTraceRecorder,
    load_runtime_decision_traces,
    runtime_decision_trace_path,
    summarize_runtime_decisions,
)


def test_runtime_decision_trace_is_redacted_and_summarized(tmp_path) -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    recorder = OptimizationTraceRecorder(
        tmp_path,
        session_id="secret-session",
        task_text="fix /private/repo/parser.py with token SECRET",
        mode="shadow",
        now=now,
    )
    recorder.decision(
        "route",
        phase="execute",
        proposed={"model": "cheap/model", "prompt": "never persist this"},
        actual={"model": "strong/model", "prompt": "never persist this"},
        reason="shadow calibration",
    )
    recorder.record_provider_call(
        phase="execute",
        model="strong/model",
        finish_reason="tool_calls",
        output_limit=1200,
        reasoning_effort="medium",
        fresh_input_tokens=100,
        cache_read_tokens=200,
        cache_write_tokens=50,
        output_tokens=25,
        cost_usd=0.25,
    )
    recorder.record_tool("read", ok=True)
    recorder.record_tool("mcp_tool", ok=False)
    recorder.record_verification(ok=True)
    path = recorder.finish(accepted=True)

    assert path == runtime_decision_trace_path(tmp_path)
    raw = path.read_text(encoding="utf-8")
    assert "secret-session" not in raw
    assert "/private/repo/parser.py" not in raw
    assert "token SECRET" not in raw
    assert "never persist this" not in raw

    rows = load_runtime_decision_traces(tmp_path, days=7, now=now)
    assert len(rows) == 1
    summary = summarize_runtime_decisions(tmp_path, days=7, now=now)
    assert summary["runs"] == 1
    assert summary["accepted_runs"] == 1
    assert summary["cost_per_accepted_run_usd"] == 0.25
    assert summary["tokens"]["cache_read"] == 200
    assert summary["provider_calls"] == 1
    assert summary["tool_calls"] == 2
    assert summary["broker_calls"] == 1
    assert summary["proposed_changes"] == 1


def test_runtime_decision_trace_off_mode_writes_nothing(tmp_path) -> None:
    recorder = OptimizationTraceRecorder(
        tmp_path,
        session_id="session",
        task_text="task",
        mode="off",
    )
    recorder.record_tool("read", ok=True)
    assert recorder.finish(accepted=True) is None
    assert not runtime_decision_trace_path(tmp_path).exists()
