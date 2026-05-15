"""Unit tests for session_report (Spec 02 — Per-Session Cost Report)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from atelier.infra.runtime.session_report import (
    SessionReport,
    _derive_vendor,
    _read_compact_savings,
    _read_routing_savings,
    build_report,
    list_run_files,
    load_report,
    render_json,
    render_text,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2025, 6, 1, 14, 30, 0, tzinfo=UTC)


def _make_snapshot(
    *,
    session_id: str = "abc123",
    status: str = "done",
    created_at: datetime = _NOW,
    updated_at: datetime = _LATER,
    calls: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    tool_call_count: int = 0,
) -> dict[str, Any]:
    """Build a minimal run snapshot dict."""
    default_calls = calls if calls is not None else []
    total_cost = sum(c.get("cost_usd", 0.0) for c in default_calls)
    total_input = sum(c.get("input_tokens", 0) for c in default_calls)
    total_output = sum(c.get("output_tokens", 0) for c in default_calls)
    total_cr = sum(c.get("cache_read_tokens", 0) for c in default_calls)

    return {
        "session_id": session_id,
        "status": status,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "tool_call_count": tool_call_count,
        "cost": {
            "calls": default_calls,
            "total_cost_usd": total_cost,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cr,
        },
        "events": events if events is not None else [],
    }


def _llm_call(
    model: str = "claude-haiku-4-5",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read_tokens: int = 0,
    cost_usd: float = 0.001,
) -> dict[str, Any]:
    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cost_usd": cost_usd,
        "at": _NOW.isoformat(),
    }


def _model_rec_event(
    tool_name: str = "Edit",
    model: str = "claude-haiku-4-5",
    estimated_input_tokens: int = 5000,
    cost_saved_usd: float = 0.01,
) -> dict[str, Any]:
    return {
        "kind": "model_recommendation",
        "at": _NOW.isoformat(),
        "summary": f"recommend haiku for {tool_name}",
        "payload": {
            "tool_name": tool_name,
            "model": model,
            "estimated_input_tokens": estimated_input_tokens,
            "cost_saved_usd": cost_saved_usd,
            "tier": "haiku",
        },
    }


# --------------------------------------------------------------------------- #
# _derive_vendor                                                               #
# --------------------------------------------------------------------------- #

def test_derive_vendor_anthropic() -> None:
    assert _derive_vendor({"claude-haiku-4-5": 10}) == "Anthropic"


def test_derive_vendor_openai() -> None:
    assert _derive_vendor({"gpt-4o": 5}) == "OpenAI"


def test_derive_vendor_google() -> None:
    assert _derive_vendor({"gemini-pro": 3}) == "Google"


def test_derive_vendor_mixed() -> None:
    assert _derive_vendor({"claude-haiku-4-5": 5, "gpt-4o": 3}) == "mixed"


def test_derive_vendor_empty() -> None:
    assert _derive_vendor({}) == "Unknown"


# --------------------------------------------------------------------------- #
# _read_routing_savings                                                        #
# --------------------------------------------------------------------------- #

def test_read_routing_savings_empty() -> None:
    downtiered, saved = _read_routing_savings([])
    assert downtiered == 0
    assert saved == 0.0


def test_read_routing_savings_with_events() -> None:
    events = [
        _model_rec_event(cost_saved_usd=0.05),
        _model_rec_event(cost_saved_usd=0.03),
        {"kind": "tool_call", "at": _NOW.isoformat()},  # non-rec event
    ]
    downtiered, saved = _read_routing_savings(events)
    assert downtiered == 2
    assert abs(saved - 0.08) < 1e-6


def test_read_routing_savings_zero_saved_not_counted() -> None:
    events = [_model_rec_event(cost_saved_usd=0.0)]
    downtiered, saved = _read_routing_savings(events)
    assert downtiered == 0
    assert saved == 0.0


# --------------------------------------------------------------------------- #
# _read_compact_savings                                                        #
# --------------------------------------------------------------------------- #

def test_read_compact_savings_no_file(tmp_path: Path) -> None:
    count, saved = _read_compact_savings("xyz", tmp_path)
    assert count == 0
    assert saved == 0.0


def test_read_compact_savings_filters_by_session_id(tmp_path: Path) -> None:
    jl = tmp_path / "live_savings_events.jsonl"
    jl.write_text(
        json.dumps({"session_id": "abc", "lever": "session_compaction", "cost_saved_usd": 0.10}) + "\n"
        + json.dumps({"session_id": "xyz", "lever": "session_compaction", "cost_saved_usd": 0.20}) + "\n"
        + json.dumps({"session_id": "abc", "lever": "model_routing", "cost_saved_usd": 0.30}) + "\n",
        encoding="utf-8",
    )
    count, saved = _read_compact_savings("abc", tmp_path)
    assert count == 1
    assert abs(saved - 0.10) < 1e-6


def test_read_compact_savings_multiple_events(tmp_path: Path) -> None:
    jl = tmp_path / "live_savings_events.jsonl"
    jl.write_text(
        json.dumps({"session_id": "s1", "lever": "session_compaction", "cost_saved_usd": 0.15}) + "\n"
        + json.dumps({"session_id": "s1", "lever": "session_compaction", "cost_saved_usd": 0.25}) + "\n",
        encoding="utf-8",
    )
    count, saved = _read_compact_savings("s1", tmp_path)
    assert count == 2
    assert abs(saved - 0.40) < 1e-6


# --------------------------------------------------------------------------- #
# build_report — empty session                                                 #
# --------------------------------------------------------------------------- #

def test_build_report_empty_session(tmp_path: Path) -> None:
    snap = _make_snapshot()
    report = build_report(snap, tmp_path)

    assert report.session_id == "abc123"
    assert report.total_turns == 0
    assert report.total_cost_usd == 0.0
    assert report.vendor == "Unknown"
    assert report.models_used == {}
    assert report.routing_downtiered_turns == 0
    assert report.compact_events == 0
    assert report.total_atelier_savings_usd == 0.0
    assert report.top_tools_by_cost == []


# --------------------------------------------------------------------------- #
# build_report — multi-model session                                           #
# --------------------------------------------------------------------------- #

def test_build_report_multi_model(tmp_path: Path) -> None:
    calls = [
        _llm_call(model="claude-sonnet-4-6", input_tokens=5000, cost_usd=0.015),
        _llm_call(model="claude-sonnet-4-6", input_tokens=5000, cost_usd=0.015),
        _llm_call(model="claude-haiku-4-5", input_tokens=1000, cost_usd=0.001),
    ]
    snap = _make_snapshot(calls=calls)
    report = build_report(snap, tmp_path)

    assert report.total_turns == 3
    assert report.models_used["claude-sonnet-4-6"] == 2
    assert report.models_used["claude-haiku-4-5"] == 1
    assert report.vendor == "Anthropic"
    assert abs(report.total_cost_usd - 0.031) < 1e-6


# --------------------------------------------------------------------------- #
# build_report — no tool calls                                                 #
# --------------------------------------------------------------------------- #

def test_build_report_no_tool_calls(tmp_path: Path) -> None:
    calls = [_llm_call(cost_usd=0.005)]
    snap = _make_snapshot(calls=calls, tool_call_count=0)
    report = build_report(snap, tmp_path)
    assert report.tool_call_count == 0
    assert report.total_turns == 1


# --------------------------------------------------------------------------- #
# build_report — ongoing session                                               #
# --------------------------------------------------------------------------- #

def test_build_report_ongoing_session(tmp_path: Path) -> None:
    snap = _make_snapshot(status="running")
    report = build_report(snap, tmp_path)
    assert report.is_running is True
    assert report.ended_at is None


def test_build_report_completed_session(tmp_path: Path) -> None:
    snap = _make_snapshot(status="done")
    report = build_report(snap, tmp_path)
    assert report.is_running is False
    assert report.ended_at is not None


# --------------------------------------------------------------------------- #
# build_report — routing + compact savings                                     #
# --------------------------------------------------------------------------- #

def test_build_report_routing_savings(tmp_path: Path) -> None:
    events = [
        _model_rec_event(cost_saved_usd=0.08),
        _model_rec_event(tool_name="Bash", cost_saved_usd=0.04),
    ]
    snap = _make_snapshot(events=events)
    report = build_report(snap, tmp_path)

    assert report.routing_downtiered_turns == 2
    assert abs(report.routing_savings_usd - 0.12) < 1e-6


def test_build_report_compact_savings(tmp_path: Path) -> None:
    jl = tmp_path / "live_savings_events.jsonl"
    jl.write_text(
        json.dumps({"session_id": "abc123", "lever": "session_compaction", "cost_saved_usd": 0.30}) + "\n",
        encoding="utf-8",
    )
    snap = _make_snapshot()
    report = build_report(snap, tmp_path)

    assert report.compact_events == 1
    assert abs(report.compact_savings_estimate_usd - 0.30) < 1e-6
    assert abs(report.total_atelier_savings_usd - 0.30) < 1e-6


def test_build_report_total_savings_combined(tmp_path: Path) -> None:
    jl = tmp_path / "live_savings_events.jsonl"
    jl.write_text(
        json.dumps({"session_id": "abc123", "lever": "session_compaction", "cost_saved_usd": 0.20}) + "\n",
        encoding="utf-8",
    )
    events = [_model_rec_event(cost_saved_usd=0.10)]
    snap = _make_snapshot(events=events)
    report = build_report(snap, tmp_path)

    assert abs(report.total_atelier_savings_usd - 0.30) < 1e-6


# --------------------------------------------------------------------------- #
# build_report — zero cost (synthetic session)                                 #
# --------------------------------------------------------------------------- #

def test_build_report_zero_cost_no_crash(tmp_path: Path) -> None:
    snap = _make_snapshot(calls=[], events=[])
    report = build_report(snap, tmp_path)
    assert report.total_cost_usd == 0.0
    text = render_text(report)
    assert "$0.00" in text


# --------------------------------------------------------------------------- #
# load_report                                                                  #
# --------------------------------------------------------------------------- #

def test_load_report_missing_returns_none(tmp_path: Path) -> None:
    result = load_report("nonexistent_session_id", tmp_path)
    assert result is None


def test_load_report_reads_run_file(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    snap = _make_snapshot(session_id="mysession")
    (runs_dir / "mysession.json").write_text(json.dumps(snap), encoding="utf-8")

    report = load_report("mysession", tmp_path)
    assert report is not None
    assert report.session_id == "mysession"


# --------------------------------------------------------------------------- #
# list_run_files                                                               #
# --------------------------------------------------------------------------- #

def test_list_run_files_empty_dir(tmp_path: Path) -> None:
    files = list_run_files(tmp_path)
    assert files == []


def test_list_run_files_since_filter(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    old_file = runs_dir / "old.json"
    new_file = runs_dir / "new.json"
    old_file.write_text("{}", encoding="utf-8")
    new_file.write_text("{}", encoding="utf-8")

    import os
    import time

    # Set old file's mtime to 10 days ago
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old_file, (ten_days_ago, ten_days_ago))

    cutoff = datetime.now(UTC) - timedelta(days=7)
    files = list_run_files(tmp_path, since=cutoff)
    assert new_file in files
    assert old_file not in files


# --------------------------------------------------------------------------- #
# render_text                                                                  #
# --------------------------------------------------------------------------- #

def test_render_text_includes_session_id(tmp_path: Path) -> None:
    snap = _make_snapshot(session_id="deadbeef1234")
    report = build_report(snap, tmp_path)
    text = render_text(report)
    assert "deadbe" in text


def test_render_text_ongoing_label(tmp_path: Path) -> None:
    snap = _make_snapshot(status="running")
    report = build_report(snap, tmp_path)
    text = render_text(report)
    assert "(ongoing)" in text


def test_render_text_cost_values(tmp_path: Path) -> None:
    calls = [_llm_call(cost_usd=1.23)]
    snap = _make_snapshot(calls=calls)
    report = build_report(snap, tmp_path)
    text = render_text(report)
    assert "$1.23" in text


# --------------------------------------------------------------------------- #
# render_json                                                                  #
# --------------------------------------------------------------------------- #

def test_render_json_valid(tmp_path: Path) -> None:
    snap = _make_snapshot()
    report = build_report(snap, tmp_path)
    raw = render_json(report)
    parsed = json.loads(raw)
    assert parsed["session_id"] == "abc123"


def test_render_json_parseable_by_jq(tmp_path: Path) -> None:
    """Smoke test: rendered JSON can be parsed and accessed via key."""
    snap = _make_snapshot(calls=[_llm_call(cost_usd=5.00)])
    report = build_report(snap, tmp_path)
    raw = render_json(report)
    parsed = json.loads(raw)
    assert "total_cost_usd" in parsed
    assert "models_used" in parsed
    assert isinstance(parsed["top_tools_by_cost"], list)
