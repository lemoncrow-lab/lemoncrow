from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atelier.core.capabilities.reporting.weekly_report import (
    Report,
    generate_report,
    render_markdown,
)
from atelier.core.foundation.lesson_models import LessonCandidate
from atelier.core.foundation.models import ReasonBlock, ToolCall, Trace, ValidationResult
from atelier.core.foundation.savings_models import ContextBudget
from atelier.core.foundation.store import ReasoningStore


def _block(block_id: str, title: str = "Plan Discipline") -> ReasonBlock:
    return ReasonBlock(
        id=block_id,
        title=title,
        domain="coding",
        situation="When changing code.",
        procedure=["Check the plan before editing."],
    )


def _trace(
    trace_id: str,
    *,
    created_at: datetime,
    passed: bool,
    domain: str = "coding",
    session_id: str | None = None,
    rescue: bool = False,
) -> Trace:
    tools = [
        ToolCall(
            name="reasoning",
            args_hash="ctx",
            args={"matched_blocks": ["rb-plan"]},
        )
    ]
    if rescue:
        tools.append(ToolCall(name="rescue", args_hash="rescue"))
    return Trace(
        id=trace_id,
        session_id=session_id,
        agent="codex",
        domain=domain,
        task="implement feature",
        status="success" if passed else "failed",
        files_touched=["src/app.py"],
        tools_called=tools,
        validation_results=[
            ValidationResult(
                name="rubric_code_change",
                passed=passed,
                detail="missing focused verification" if not passed else "ok",
            )
        ],
        output_summary="Focused verification was missing from the trace." if not passed else "ok",
        created_at=created_at,
    )


def test_generate_report_aggregates_weekly_governance(store: ReasoningStore) -> None:
    now = datetime(2026, 5, 5, 12, tzinfo=UTC)
    store.upsert_block(_block("rb-plan"), write_markdown=False)
    store.record_trace(
        _trace("current-pass", created_at=now - timedelta(days=1), passed=True, session_id="run-current"),
        write_json=False,
    )
    store.record_trace(
        _trace(
            "current-fail",
            created_at=now - timedelta(days=2),
            passed=False,
            session_id="run-current-fail",
            rescue=True,
        ),
        write_json=False,
    )
    store.record_trace(
        _trace("prior-pass", created_at=now - timedelta(days=8), passed=True, session_id="run-prior"),
        write_json=False,
    )
    store.persist_context_budget(
        ContextBudget(
            session_id="run-current",
            turn_index=0,
            model="test",
            input_tokens=500,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=0,
            naive_input_tokens=1000,
            lever_savings={},
            tool_calls=1,
        )
    )
    store.persist_context_budget(
        ContextBudget(
            session_id="run-prior",
            turn_index=0,
            model="test",
            input_tokens=800,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=0,
            naive_input_tokens=1000,
            lever_savings={},
            tool_calls=1,
        )
    )
    store.upsert_lesson_candidate(
        LessonCandidate(
            domain="coding",
            cluster_fingerprint="cluster",
            kind="new_block",
            proposed_block=_block("rb-imported", "Imported Rule"),
            evidence_trace_ids=["a", "b", "c"],
            body="Review this candidate.",
            confidence=0.8,
        )
    )

    report = generate_report(timedelta(days=7), store=store, now=now, git_sha="abc123")

    assert report.rubric_pass_rate.total == 2
    assert report.rubric_pass_rate.pass_rate == 0.5
    assert report.rubric_pass_rate_by_domain[0].domain == "coding"
    assert report.top_reasonblocks[0].id == "rb-plan"
    assert report.top_reasonblocks[0].count == 2
    assert report.top_rubric_failures[0].file_paths == ["src/app.py"]
    assert report.pending_lesson_candidates_count == 1
    assert report.top_lesson_candidates[0].cluster_size == 3
    assert report.drift.rubric_pass_rate_delta_pct == -50.0
    assert report.drift.context_budget_usage_delta_pct == -30.0


def test_report_json_round_trips_and_markdown_is_compact(store: ReasoningStore) -> None:
    now = datetime(2026, 5, 5, 12, tzinfo=UTC)
    report = generate_report(timedelta(days=7), store=store, now=now, git_sha="abc123")
    schema = Report.model_json_schema()
    assert "period_start" in schema["properties"]

    payload = report.model_dump(mode="json")
    assert Report.model_validate(payload).git_sha == "abc123"

    markdown = render_markdown(report)
    assert markdown.startswith("# Atelier Weekly Governance Report")
    assert len(markdown.splitlines()) < 80
