"""Backfilling the savings ledger from sessions LemonCrow never saw.

Covers the write path directly (backfill_host_savings) plus one real
end-to-end pass through a synthetic Claude transcript, proving a backfilled
row actually moves ``aggregate_window_savings`` (the same function ``lc
savings`` / ``lc account cap`` read).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import session_backfill as sb
from lemoncrow.core.capabilities.session_replay import Replay, ReplaySummary


def _replay(session_id: str, *, source_path: str, host: str = "claude") -> Replay:
    return Replay(
        host=host,
        session_id=session_id,
        model="claude-sonnet-5",
        task="task",
        turns=[],
        collapsed_indices=[],
        episodes=[],
        summary=ReplaySummary(
            total_turns=4,
            total_tool_calls=3,
            kept_tool_calls=1,
            calls_saved=2,
            episode_count=1,
            batch_count=0,
            search_calls_saved=2,
            batch_calls_saved=0,
            verbose_output_tokens=0,
        ),
        source_path=source_path,
    )


def _estimate(*, saved_usd: float, ran_with_lemoncrow: bool = False, calls_saved: int = 2) -> dict[str, object]:
    return {
        "model": "claude-sonnet-5",
        "saved_usd": saved_usd,
        "ran_with_lemoncrow": ran_with_lemoncrow,
        "calls_saved": calls_saved,
        "collapsed_output_tokens": 500,
    }


def test_backfills_vanilla_session_above_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    transcript = tmp_path / "old-session.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    old_ts = datetime(2025, 3, 4, 12, 0, 0, tzinfo=UTC).timestamp()
    import os

    os.utime(transcript, (old_ts, old_ts))

    monkeypatch.setattr(sb, "load_replays", lambda **kw: [_replay("sid-1", source_path=str(transcript))])
    monkeypatch.setattr(sb, "estimate_savings", lambda replay: _estimate(saved_usd=0.5))

    result = sb.backfill_host_savings(root, "claude", limit=10)
    assert len(result.backfilled) == 1
    assert result.total_saved_usd == 0.5

    sidecar = root / "sessions" / "2025" / "03" / "04" / "claude" / "sid-1" / "savings.jsonl"
    assert sidecar.exists()
    row = json.loads(sidecar.read_text(encoding="utf-8").strip())
    assert row["cost_saved_usd"] == 0.5
    assert row["calls"] == 2
    assert row["kind"] == "backfill"


def test_already_tracked_session_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    # A LIVE savings.jsonl already exists for this session/date -- must never
    # be touched or double-counted.
    when = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)
    existing = (
        root
        / "sessions"
        / when.strftime("%Y")
        / when.strftime("%m")
        / when.strftime("%d")
        / "claude"
        / "sid-2"
        / "savings.jsonl"
    )
    existing.parent.mkdir(parents=True)
    existing.write_text(json.dumps({"tool": "bash", "cost_saved_usd": 9.0, "ts": "2020-01-01T00:00:00"}) + "\n")

    monkeypatch.setattr(sb, "load_replays", lambda **kw: [_replay("sid-2", source_path=str(transcript))])
    monkeypatch.setattr(sb, "estimate_savings", lambda replay: _estimate(saved_usd=5.0))

    result = sb.backfill_host_savings(root, "claude", limit=10)
    assert result.backfilled == []
    assert result.already_tracked == 1
    # Untouched: still the original single row, not appended to.
    assert existing.read_text(encoding="utf-8").count("\n") == 1


def test_session_that_ran_with_lemoncrow_is_never_estimated_on_top(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / ".lemoncrow"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sb, "load_replays", lambda **kw: [_replay("sid-3", source_path=str(transcript))])
    monkeypatch.setattr(sb, "estimate_savings", lambda replay: _estimate(saved_usd=1.0, ran_with_lemoncrow=True))

    result = sb.backfill_host_savings(root, "claude", limit=10)
    assert result.backfilled == []
    assert result.ran_with_lemoncrow == 1


def test_below_threshold_session_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sb, "load_replays", lambda **kw: [_replay("sid-4", source_path=str(transcript))])
    monkeypatch.setattr(sb, "estimate_savings", lambda replay: _estimate(saved_usd=0.001))

    result = sb.backfill_host_savings(root, "claude", limit=10, min_saved_usd=0.01)
    assert result.backfilled == []
    assert result.below_threshold == 1


def test_dry_run_computes_without_writing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sb, "load_replays", lambda **kw: [_replay("sid-5", source_path=str(transcript))])
    monkeypatch.setattr(sb, "estimate_savings", lambda replay: _estimate(saved_usd=1.25))

    result = sb.backfill_host_savings(root, "claude", limit=10, dry_run=True)
    assert len(result.backfilled) == 1
    assert result.total_saved_usd == 1.25
    assert not (root / "sessions").exists()


def test_rerun_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sb, "load_replays", lambda **kw: [_replay("sid-6", source_path=str(transcript))])
    monkeypatch.setattr(sb, "estimate_savings", lambda replay: _estimate(saved_usd=2.0))

    first = sb.backfill_host_savings(root, "claude", limit=10)
    assert len(first.backfilled) == 1

    second = sb.backfill_host_savings(root, "claude", limit=10)
    assert second.backfilled == []
    assert second.already_tracked == 1


def test_end_to_end_moves_the_real_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Real Claude transcript + real load_replays (host discovery via
    # CLAUDE_CONFIG_DIR, unmocked) with a genuine grep->read loop -- only
    # estimate_savings' dollar figure is pinned, since the real pricing
    # pipeline it calls into is exercised by savings_summary's own test
    # suite, not this one. Proves the backfilled row actually reaches
    # aggregate_window_savings, the function lc savings/account cap read.
    from lemoncrow.core.capabilities.savings_summary import reconcile_savings_aggregate

    root = tmp_path / ".lemoncrow"
    claude_home = tmp_path / "claude_home"
    projects = claude_home / "projects" / "proj1"
    projects.mkdir(parents=True)
    events = [
        {"type": "user", "sessionId": "sid-e2e", "message": {"content": "find TokenRefresh"}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "x"}}],
                "usage": {},
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "20 files matched"}]},
        },
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a.py"}}],
                "usage": {},
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m3",
                "content": [{"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "b.py"}}],
                "usage": {},
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m4",
                "content": [{"type": "tool_use", "id": "t4", "name": "Read", "input": {"file_path": "c.py"}}],
                "usage": {"input_tokens": 4000, "output_tokens": 300},
            },
        },
    ]
    transcript = projects / "sid-e2e.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setattr(sb, "estimate_savings", lambda replay: _estimate(saved_usd=1.23, calls_saved=2))

    result = sb.backfill_host_savings(root, "claude", limit=10, min_saved_usd=0.0)
    assert len(result.backfilled) == 1
    assert result.backfilled[0].session_id == "sid-e2e"

    # reconcile_savings_aggregate is what a separate `lc savings` process reads
    # next (savings_aggregate.json) -- read that persisted file directly rather
    # than through aggregate_window_savings, which caches per (days, root) in
    # this process and would otherwise just replay whatever it saw first.
    agg = reconcile_savings_aggregate(root)
    day_totals = next(iter(agg["sessions"].values()))["days"]
    saved_usd, _tokens, calls = next(iter(day_totals.values()))[:3]
    assert saved_usd == 1.23
    assert calls == 2
