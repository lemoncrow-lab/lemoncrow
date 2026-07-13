from __future__ import annotations

from datetime import UTC, datetime

from lemoncrow.core.capabilities.session_optimizer import (
    build_trace_optimization_report,
    render_session_optimizer_guidance,
    session_optimization_rules,
)
from lemoncrow.core.foundation.models import ToolCall, Trace


def _trace(
    trace_id: str,
    *,
    host: str = "claude",
    domain: str = "project-a",
    input_tokens: int = 100_000,
    output_tokens: int = 5_000,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    model: str = "gpt-5.5-pro",
    files_touched: list[str] | None = None,
    tools: list[str] | None = None,
) -> Trace:
    return Trace(
        id=trace_id,
        agent=host,
        host=host,
        domain=domain,
        task=trace_id,
        status="success",
        files_touched=files_touched or [],
        tools_called=[ToolCall(name=name, args_hash=name) for name in (tools or [])],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        model=model,
        created_at=datetime(2026, 5, 11, tzinfo=UTC),
    )


def test_guidance_contains_all_codeburn_guardrails() -> None:
    guidance = render_session_optimizer_guidance("codex")
    assert "smallest viable plan" in guidance
    assert "under 10 bullets" in guidance
    assert "10 minutes" in guidance
    assert "do not retry a third time" in guidance
    assert {rule["id"] for rule in session_optimization_rules()} == {
        "smallest-reviewable-plan",
        "fresh-bounded-context",
        "delivery-or-stop",
    }


def test_trace_report_flags_outliers_context_and_low_delivery() -> None:
    traces = [
        _trace(
            "peer-low",
            input_tokens=80_000,
            output_tokens=4_000,
            files_touched=["a.py"],
            tools=["Edit"],
        ),
        _trace("outlier", input_tokens=1_000_000, output_tokens=10_000),
        _trace(
            "context-heavy",
            domain="project-b",
            input_tokens=120_000,
            cached_input_tokens=260_000,
            output_tokens=4_000,
            files_touched=["b.py"],
            tools=["Edit"],
        ),
    ]

    report = build_trace_optimization_report(traces, days=3650)
    recommendation_ids = {item["id"] for item in report["recommendations"]}

    assert "high-cost-session-outliers" in recommendation_ids
    assert "context-heavy-sessions" in recommendation_ids
    assert "low-worth-expensive-sessions" in recommendation_ids
    assert report["estimated_tokens_saved"] > 0
    assert report["estimated_usd_saved"] > 0
