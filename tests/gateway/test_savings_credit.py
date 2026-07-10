"""Tests for avoided-call pricing and context-carry credit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities.pricing import get_model_pricing
from atelier.core.capabilities.savings_summary import (
    _carry_credit,
    _read_claude_session_savings,
    read_transcript_stats,
)

MODEL = "claude-sonnet-4-5"


def _write_sidecar(root: Path, session_id: str, rows: list[dict[str, Any]]) -> None:
    from atelier.core.foundation.paths import session_dir

    d = session_dir(root, "claude", session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "savings.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _usage_line(msg_id: str, ts: str) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": MODEL,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


@pytest.mark.parametrize(
    ("model", "input_rate"),
    (("gpt-5.6-sol", 5.00), ("gpt-5.6-terra", 2.50), ("gpt-5.6-luna", 1.00)),
)
def test_legacy_saved_tokens_reprice_with_gpt_5_6_models(model: str, input_rate: float) -> None:
    from atelier.core.capabilities.savings_summary import _price_savings_row

    tokens, usd, calls, calls_usd, unpriced = _price_savings_row({"tokens": 1_000_000, "model": model})
    assert (tokens, calls, calls_usd, unpriced) == (1_000_000, 0, 0.0, 0)
    assert usd == pytest.approx(input_rate)


def test_read_claude_session_savings_includes_calls_usd(tmp_path: Path) -> None:
    _write_sidecar(
        tmp_path,
        "s1",
        [
            {"tool": "edit", "tokens": 0, "calls": 5, "calls_usd": 0.12, "model": ""},
            {"tool": "read", "tokens": 1000, "calls": 0, "model": MODEL},
        ],
    )
    priced, calls, usd, unpriced = _read_claude_session_savings("s1", tmp_path)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None and pricing.known
    assert calls == 5
    assert priced == 1000
    assert unpriced == 0
    # calls_usd (0.12) is credited as written — each avoided roundtrip was
    # priced at write time (ctx re-read + avg output); no display-time discount.
    assert usd == pytest.approx(0.12 + pricing.input / 1_000_000 * 1000)


def test_read_claude_session_savings_excludes_compaction_rows(tmp_path: Path) -> None:
    _write_sidecar(
        tmp_path,
        "s1",
        [{"kind": "compaction", "tokens": 50_000, "usd": 0.25, "model": MODEL}],
    )
    priced, calls, usd, unpriced = _read_claude_session_savings("s1", tmp_path)
    assert (priced, calls, usd, unpriced) == (0, 0, 0.0, 0)


def test_routing_rows_summed_separately_from_context(tmp_path: Path) -> None:
    from atelier.core.capabilities.savings_summary import _read_session_routing_usd

    _write_sidecar(
        tmp_path,
        "s1",
        [
            {"kind": "routing", "usd": 0.012, "tool": "edit", "model": MODEL, "ts": "2026-06-16T10:00:00+00:00"},
            {"kind": "routing", "usd": 0.008, "tool": "read", "model": MODEL, "ts": "2026-06-16T10:01:00+00:00"},
            {"tool": "read", "tokens": 5000, "calls": 0, "model": MODEL, "ts": "2026-06-16T10:02:00+00:00"},
        ],
    )
    # Routing rows are summed by the dedicated reader (0.012 + 0.008).
    assert _read_session_routing_usd("s1", tmp_path) == pytest.approx(0.02)
    # ...and ignored by the context-savings reader (no token or usd leak).
    priced, calls, usd, _unpriced = _read_claude_session_savings("s1", tmp_path)
    assert priced == 5000
    assert calls == 0
    pricing = get_model_pricing(MODEL)
    assert pricing is not None and pricing.known
    assert usd == pytest.approx(pricing.input / 1_000_000 * 5000)


def test_carry_credit_counts_only_later_turns(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    row_ts = (base + timedelta(minutes=1)).isoformat()
    _write_sidecar(
        tmp_path,
        "s1",
        [
            # 1000 tokens saved before two later turns -> 2 carry turns.
            {"tool": "read", "tokens": 1000, "calls": 0, "model": MODEL, "ts": row_ts},
            # Compaction rows reset the window; they are not Atelier savings.
            {"kind": "compaction", "tokens": 50_000, "usd": 0.01, "model": MODEL, "ts": row_ts},
            # Unknown-model rows contribute nothing (never guess a rate).
            {"tool": "grep", "tokens": 1000, "calls": 0, "model": "mystery-model-x", "ts": row_ts},
        ],
    )
    turn_ts = [
        base.isoformat(),  # before the row - no carry
        (base + timedelta(minutes=2)).isoformat(),
        (base + timedelta(minutes=3)).isoformat(),
    ]
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, turn_ts)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    assert carry_tokens == 2000
    assert carry_usd == pytest.approx(pricing.tokens_to_usd(2000, "cache_read"), abs=1e-9)
    assert carry_usd > 0


def test_carry_credit_stops_at_next_compaction(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _write_sidecar(
        tmp_path,
        "s1",
        [
            {
                "tool": "read",
                "tokens": 1000,
                "calls": 0,
                "model": MODEL,
                "ts": (base + timedelta(minutes=1)).isoformat(),
            },
            {
                "kind": "compaction",
                "tokens": 50_000,
                "usd": 0.01,
                "model": MODEL,
                "ts": (base + timedelta(minutes=3)).isoformat(),
            },
        ],
    )
    turn_ts = [
        (base + timedelta(minutes=2)).isoformat(),
        (base + timedelta(minutes=4)).isoformat(),
        (base + timedelta(minutes=5)).isoformat(),
    ]
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, turn_ts)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    assert carry_tokens == 1000
    assert carry_usd == pytest.approx(pricing.tokens_to_usd(1000, "cache_read"), abs=1e-9)


def test_carry_credit_handles_naive_row_timestamps(tmp_path: Path) -> None:
    # mcp_server writes naive utcnow().isoformat() rows; transcripts use "Z".
    _write_sidecar(
        tmp_path,
        "s1",
        [{"tool": "read", "tokens": 500, "calls": 0, "model": MODEL, "ts": "2026-01-01T12:01:00"}],
    )
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, ["2026-01-01T12:02:00Z"])
    assert carry_tokens == 500
    assert carry_usd > 0


def test_carry_credit_empty_without_transcript_turns(tmp_path: Path) -> None:
    _write_sidecar(
        tmp_path,
        "s1",
        [{"tool": "read", "tokens": 500, "calls": 0, "model": MODEL, "ts": "2026-01-01T12:01:00"}],
    )
    assert _carry_credit("s1", tmp_path, []) == (0, 0.0)
    assert _carry_credit("", tmp_path, ["2026-01-01T12:02:00Z"]) == (0, 0.0)


def test_carry_credit_attributes_subagent_rows_to_their_own_window(tmp_path: Path) -> None:
    """A token a subagent saved carries across that subagent's own later turns,
    not the main thread's — even though its sidecar row lands in the parent's
    savings.jsonl (the shared MCP process keys by the parent session id)."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def at(minutes: int, seconds: int = 0) -> str:
        return (base + timedelta(minutes=minutes, seconds=seconds)).isoformat()

    _write_sidecar(
        tmp_path,
        "s1",
        [
            {"tool": "read", "tokens": 1000, "calls": 0, "model": MODEL, "ts": at(0, 30)},
            {"tool": "read", "tokens": 500, "calls": 0, "model": MODEL, "ts": at(10, 30)},
        ],
    )
    main_turns = [at(0), at(1), at(30)]
    subagent_turns = [[at(10), at(11), at(12)]]
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, main_turns, subagent_turns)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    # main row (12:00:30) -> 2 later MAIN turns (12:01, 12:30) => 1000 * 2.
    # subagent row (12:10:30, inside [12:10, 12:12]) -> 2 later SUBAGENT turns
    # (12:11, 12:12) => 500 * 2. Under the old main-only logic the subagent row
    # would have counted only 1 later main turn (12:30) => 500.
    assert carry_tokens == 3000
    assert carry_usd == pytest.approx(pricing.tokens_to_usd(3000, "cache_read"), abs=1e-9)


def test_read_transcript_stats_collects_turn_timestamps(tmp_path: Path) -> None:
    lines = [
        _usage_line("m1", "2026-01-01T12:00:00Z"),
        _usage_line("m2", "2026-01-01T12:01:00Z"),
        # Duplicate message id -> deduped, timestamp not double-counted.
        _usage_line("m2", "2026-01-01T12:01:30Z"),
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    stats = read_transcript_stats(p)
    assert stats is not None
    assert stats.turn_timestamps == ["2026-01-01T12:00:00Z", "2026-01-01T12:01:00Z"]
    assert [u["ts"] for u in stats.turn_usage] == ["2026-01-01T12:00:00Z", "2026-01-01T12:01:00Z"]
    assert stats.turn_usage[0] == {"ts": "2026-01-01T12:00:00Z", "model": MODEL, "in": 10, "out": 5, "cR": 0, "cW": 0}


def test_read_transcript_stats_prices_1h_cache_writes_and_long_context(tmp_path: Path) -> None:
    """1h-TTL cache writes bill at $20/M (not $12.50) and >200k-context
    messages bill the whole request at the long-context premium."""
    base_msg = {
        "timestamp": "2026-01-01T12:00:00Z",
        "message": {
            "id": "small",
            "model": "claude-fable-5",
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 1_000_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 1_000_000,
                },
            },
        },
    }
    p = tmp_path / "sess.jsonl"
    p.write_text(json.dumps(base_msg) + "\n", encoding="utf-8")
    stats = read_transcript_stats(p)
    assert stats is not None
    # flat: in 1M*$10 + cW1h 1M*$20 = $30 (no long_context premium — Claude doesn't bill it)
    assert stats.est_cost_usd == pytest.approx(10.0 + 20.0)

    # Same usage but under the threshold: in 1M*$10 + cW1h 1M*$20
    small = json.loads(json.dumps(base_msg))
    small["message"]["usage"]["input_tokens"] = 100_000
    small["message"]["usage"]["cache_creation_input_tokens"] = 50_000
    small["message"]["usage"]["cache_creation"]["ephemeral_1h_input_tokens"] = 50_000
    p.write_text(json.dumps(small) + "\n", encoding="utf-8")
    stats = read_transcript_stats(p)
    assert stats is not None
    assert stats.est_cost_usd == pytest.approx(0.1 * 10.0 + 0.05 * 20.0)


def test_read_transcript_stats_includes_subagent_usage(tmp_path: Path) -> None:
    """Subagent transcripts (<session>/subagents/*.jsonl) count toward tokens/cost,
    but not toward turns or tool calls."""
    p = tmp_path / "sess.jsonl"
    p.write_text(json.dumps(_usage_line("m1", "2026-01-01T12:00:00Z")) + "\n", encoding="utf-8")
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-a1.jsonl").write_text(
        json.dumps(_usage_line("sub1", "2026-01-01T12:00:30Z")) + "\n", encoding="utf-8"
    )

    stats = read_transcript_stats(p)
    assert stats is not None
    # Usage buckets include both the main turn and the subagent turn.
    assert stats.input_tokens == 20
    assert stats.output_tokens == 10
    # Turns/timestamps remain main-transcript-only.
    assert stats.turns == 1
    assert stats.turn_timestamps == ["2026-01-01T12:00:00Z"]
    # Subagent assistant turns are bucketed separately for per-window carry.
    assert stats.subagent_turn_timestamps == [["2026-01-01T12:00:30Z"]]


def test_price_avoided_calls_usd_uses_cache_read_rate() -> None:
    from atelier.gateway.adapters.mcp_server import _price_avoided_calls_usd

    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    # Per-request flat semantics: each avoided call re-reads a <200k window at
    # the BASE cache-read rate — never progressive-tiered over the aggregate.
    expected = pricing.request_cost_usd(cache_read_tokens=3 * 100_000)
    assert expected > 0
    assert _price_avoided_calls_usd(MODEL, 3, 100_000) == pytest.approx(expected)
    assert _price_avoided_calls_usd("", 3, 100_000) == 0.0
    assert _price_avoided_calls_usd(MODEL, 3, 0) == 0.0
    assert _price_avoided_calls_usd(MODEL, 0, 100_000) == 0.0
    # Each avoided roundtrip also carries the turn's average output: billed at
    # the output rate and re-entering context as a cache write.
    with_out = _price_avoided_calls_usd(MODEL, 3, 100_000, avg_output_tokens=500)
    assert with_out == pytest.approx(
        pricing.request_cost_usd(cache_read_tokens=3 * 100_000, output_tokens=1500, cache_write_tokens=1500)
    )
    assert with_out > expected


def test_savings_price_at_premium_when_long_context() -> None:
    from atelier.gateway.adapters.mcp_server import (
        _price_avoided_calls_usd,
        _price_tokens_saved_usd,
        _savings_long_context,
    )

    pricing = get_model_pricing(MODEL)
    assert pricing is not None and pricing.long_context_threshold() == 200_000
    prem_calls = _price_avoided_calls_usd(MODEL, 2, 150_000, long_context=True)
    assert prem_calls == pytest.approx(pricing.request_cost_usd(cache_read_tokens=300_000, long_context=True))
    assert prem_calls > _price_avoided_calls_usd(MODEL, 2, 150_000)
    base_tok = _price_tokens_saved_usd(MODEL, 10_000)
    prem_tok = _price_tokens_saved_usd(MODEL, 10_000, long_context=True)
    assert base_tok == pytest.approx(pricing.request_cost_usd(input_tokens=10_000))
    assert prem_tok == pytest.approx(pricing.request_cost_usd(input_tokens=10_000, long_context=True))
    assert prem_tok > base_tok
    assert _savings_long_context(MODEL, 250_000) is True
    assert _savings_long_context(MODEL, 150_000) is False
    assert _savings_long_context("claude-fable-5", 250_000) is False  # no premium tier on the rate card
    assert _savings_long_context("", 250_000) is False


def test_carry_credit_ignores_avoided_calls(tmp_path: Path) -> None:
    """Replaced tool calls are NOT a carry multiplier: an avoided call never ran,
    so it re-reads no carried token. Only real later turns carry a saved token;
    the avoided-call counts belong solely to the one-time ``saved`` figure."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def at(minutes: int) -> str:
        return (base + timedelta(minutes=minutes)).isoformat()

    _write_sidecar(
        tmp_path,
        "s1",
        [
            {"tool": "read", "tokens": 0, "calls": 7, "model": MODEL, "ts": at(0)},
            {"tool": "read", "tokens": 1000, "calls": 0, "model": MODEL, "ts": at(1)},
            {"tool": "code_search", "tokens": 0, "calls": 3, "model": MODEL, "ts": at(2)},
        ],
    )
    turn_ts = [at(0), at(3), at(4)]
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, turn_ts)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    # 2 later real turns x 1000 tokens; the 7 and 3 avoided calls add nothing.
    assert carry_tokens == 2000
    assert carry_usd == pytest.approx(pricing.request_cost_usd(cache_read_tokens=2000), abs=1e-9)


def test_carry_credit_ignores_input_style_and_turn_cut_rows(tmp_path: Path) -> None:
    """input_style and turn_cut rows are recurring, per-turn credits re-earned
    fresh at every Stop fire (input_style: this turn's resend-volume gap;
    turn_cut: priced via calls/calls_usd, never a token that entered context).
    Neither represents a token newly written into context, so carrying them
    forward would compound the same per-turn gap on every later turn as well
    -- only real realized/output-style rows (genuine avoided context writes)
    should carry."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def at(minutes: int) -> str:
        return (base + timedelta(minutes=minutes)).isoformat()

    _write_sidecar(
        tmp_path,
        "s1",
        [
            {"kind": "input_style", "tokens": 50_000, "cost_saved_usd": 0.05, "model": MODEL, "ts": at(0)},
            {"kind": "turn_cut", "calls": 5, "calls_usd": 0.10, "model": MODEL, "ts": at(0)},
            {"tool": "read", "tokens": 1000, "calls": 0, "model": MODEL, "ts": at(1)},
        ],
    )
    turn_ts = [at(0), at(3), at(4)]
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, turn_ts)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    # Only the 1000-token realized row carries, over its 2 later real turns;
    # the 50k input_style tokens and the turn_cut calls add nothing.
    assert carry_tokens == 2000
    assert carry_usd == pytest.approx(pricing.request_cost_usd(cache_read_tokens=2000), abs=1e-9)


def test_carry_credit_caps_resident_at_context_window(tmp_path: Path) -> None:
    """Tokens carried into any single later turn cannot exceed the model's
    context window: a session that saved more than a window's worth before a
    turn is capped at the window, not credited the unbounded cumulative sum."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None and pricing.context_window > 0
    window = pricing.context_window
    _write_sidecar(
        tmp_path,
        "s1",
        [
            {
                "tool": "read",
                "tokens": window,
                "calls": 0,
                "model": MODEL,
                "ts": (base + timedelta(seconds=i)).isoformat(),
            }
            for i in range(1, 4)  # 3 x window tokens saved before the turn
        ],
    )
    turn_ts = [(base + timedelta(minutes=1)).isoformat()]
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, turn_ts)
    # One later turn: resident capped at one window, not 3x the window.
    assert carry_tokens == window
    assert carry_usd == pytest.approx(pricing.request_cost_usd(cache_read_tokens=window), abs=1e-9)


def test_carry_credit_caps_resident_at_actual_context_ceiling(tmp_path: Path) -> None:
    """Resident carry is capped at the session's ACTUAL peak context, not the
    model's theoretical window — a session that never grew past context_ceiling
    can't carry a full window's worth per turn."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _write_sidecar(
        tmp_path,
        "s1",
        [
            {
                "tool": "read",
                "tokens": 20_000,
                "calls": 0,
                "model": MODEL,
                "ts": (base + timedelta(seconds=1)).isoformat(),
            }
        ],
    )
    turn_ts = [(base + timedelta(minutes=1)).isoformat()]
    capped, _ = _carry_credit("s1", tmp_path, turn_ts, context_ceiling=5_000)
    uncapped, _ = _carry_credit("s1", tmp_path, turn_ts)
    assert capped == 5_000  # one later turn; resident clamped to the actual peak
    assert uncapped == 20_000  # no ceiling -> the full 20k saved carries


def test_carry_credit_call_cross_term_stops_at_compaction(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def at(minutes: int) -> str:
        return (base + timedelta(minutes=minutes)).isoformat()

    _write_sidecar(
        tmp_path,
        "s1",
        [
            {"tool": "read", "tokens": 1000, "calls": 0, "model": MODEL, "ts": at(1)},
            {"kind": "compaction", "tokens": 50_000, "usd": 0.01, "model": MODEL, "ts": at(2)},
            # After the compaction dropped the row from context: no cross credit.
            {"tool": "code_search", "tokens": 0, "calls": 4, "model": MODEL, "ts": at(3)},
        ],
    )
    assert _carry_credit("s1", tmp_path, [at(0), at(4)]) == (0, 0.0)


def test_carry_credit_prices_long_context_rows_at_premium(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    row_ts = (base + timedelta(minutes=1)).isoformat()
    _write_sidecar(
        tmp_path,
        "s1",
        [{"tool": "read", "tokens": 1000, "calls": 0, "model": MODEL, "ts": row_ts, "long_context": True}],
    )
    turn_ts = [base.isoformat(), (base + timedelta(minutes=2)).isoformat()]
    carry_tokens, carry_usd = _carry_credit("s1", tmp_path, turn_ts)
    pricing = get_model_pricing(MODEL)
    assert pricing is not None
    assert carry_tokens == 1000
    assert carry_usd == pytest.approx(pricing.request_cost_usd(cache_read_tokens=1000, long_context=True), abs=1e-9)
    assert carry_usd > pricing.request_cost_usd(cache_read_tokens=1000)


def test_cliff_credit_reprices_turns_kept_under_threshold(tmp_path: Path) -> None:
    from atelier.core.capabilities.savings_summary import _cliff_credit

    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def at(minutes: int) -> str:
        return (base + timedelta(minutes=minutes)).isoformat()

    _write_sidecar(
        tmp_path,
        "s1",
        [{"tool": "read", "tokens": 10_000, "calls": 0, "model": MODEL, "ts": at(1)}],
    )
    pricing = get_model_pricing(MODEL)
    assert pricing is not None and pricing.long_context_threshold() == 200_000
    # 196k actual + 10k saved = 206k > 200k: the whole request would have
    # billed premium — credit the premium-minus-base delta on actual usage.
    turn = {"ts": at(2), "model": MODEL, "in": 1000, "out": 100, "cR": 190_000, "cW": 5000}
    expected = pricing.request_cost_usd(
        input_tokens=1000, output_tokens=100, cache_read_tokens=190_000, cache_write_tokens=5000, long_context=True
    ) - pricing.request_cost_usd(
        input_tokens=1000, output_tokens=100, cache_read_tokens=190_000, cache_write_tokens=5000
    )
    assert expected > 0
    assert _cliff_credit("s1", tmp_path, [turn]) == pytest.approx(expected, abs=1e-6)
    # Would not have crossed even with the saved tokens: no credit.
    small = {"ts": at(2), "model": MODEL, "in": 1000, "out": 100, "cR": 100_000, "cW": 5000}
    assert _cliff_credit("s1", tmp_path, [small]) == 0.0
    # Already over the threshold: billed premium for real; savings rows are
    # premium-priced at write time instead — nothing to credit here.
    over = {"ts": at(2), "model": MODEL, "in": 1000, "out": 100, "cR": 210_000, "cW": 5000}
    assert _cliff_credit("s1", tmp_path, [over]) == 0.0


def test_sidecar_and_identity_route_to_live_window_session_after_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity AND savings must route to the live *window* session, not the
    MCP process's launch-time CLAUDE_CODE_SESSION_ID.

    The MCP server is long-lived: after a /clear the env var still names the
    dead launch session, while SessionStart has written this window's own
    identity file naming the live one. The unified window resolver makes the
    ledger and the savings sidecar agree on the live session, so post-clear
    sessions never report zero savings.
    """
    import json

    from atelier.core.foundation import session_window as sw
    from atelier.gateway.adapters import mcp_server as m

    monkeypatch.setattr(m, "_atelier_root", lambda: tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    ws_hash = sw.workspace_hash(str(workspace))

    # Anchor both resolvers to one fixed window and reset the MCP-side caches so
    # the monkeypatched window id / file are re-read.
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (4242, 99))
    monkeypatch.setattr(m, "_MCP_WINDOW_ID", None, raising=False)
    monkeypatch.setattr(m, "_MCP_WINDOW_ID_RESOLVED", False, raising=False)
    monkeypatch.setattr(m, "_WINDOW_SID_CACHE", None, raising=False)

    # MCP launched in 'launch-sid'; the user then /cleared into 'active-sid',
    # which SessionStart recorded in this window's own identity file.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "launch-sid")
    win_file = sw.window_file_path(tmp_path, ws_hash, 4242, 99)
    win_file.parent.mkdir(parents=True, exist_ok=True)
    win_file.write_text(json.dumps({"session_id": "active-sid"}), encoding="utf-8")

    # Identity (ledger/telemetry) and savings BOTH follow the live window id.
    from atelier.core.foundation.paths import session_dir

    assert m._claude_session_id() == "active-sid"
    assert m._get_host_session_sidecar_path() == session_dir(tmp_path, "claude", "active-sid") / "savings.jsonl"

    # Before SessionStart writes this window's file (first calls / hookless
    # launchers), fall back to the launch env var so early savings still record.
    win_file.unlink()
    monkeypatch.setattr(m, "_WINDOW_SID_CACHE", None, raising=False)
    monkeypatch.setattr(m, "_SAVINGS_SIDECAR_PATH_BY_SID", {}, raising=False)
    assert m._get_host_session_sidecar_path() == session_dir(tmp_path, "claude", "launch-sid") / "savings.jsonl"


def test_append_savings_unresolved_session_routes_to_quarantine_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolved session id must not DROP the savings event.

    Fail-closed is for attribution only: the row lands in a per-workspace
    ``unattributed-<ws_hash>`` quarantine ledger under sessions/ that the
    windowed historical aggregate counts (any savings.jsonl below sessions/)
    but per-session readers (exact-id glob) never see.
    """
    import json

    from atelier.core.capabilities.savings_summary import _scan_savings_files
    from atelier.core.foundation.paths import find_session_dir
    from atelier.gateway.adapters import mcp_server as m

    monkeypatch.setattr(m, "_atelier_root", lambda: tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(m, "_resolved_host_session_id", lambda: "")
    monkeypatch.setattr(m, "_current_context_state", lambda: (0, ""))
    monkeypatch.setattr(m, "_get_mcp_model", lambda: MODEL)

    m._append_savings("read", 1000, 1, rid="7")

    ledgers = list((tmp_path / "sessions").glob("*/*/*/*/unattributed-*/savings.jsonl"))
    assert len(ledgers) == 1
    row = json.loads(ledgers[0].read_text(encoding="utf-8").splitlines()[0])
    assert row["unattributed"] is True
    assert row["tokens"] == 1000 and row["calls"] == 1 and row["model"] == MODEL
    assert row["cost_saved_usd"] > 0  # tokens still priced by the row model
    # Historical scanner counts the quarantine ledger...
    assert ledgers[0] in [p for p, _, _ in _scan_savings_files(tmp_path)]
    # ...while per-session lookups (exact-id glob) can never resolve to it.
    assert find_session_dir(tmp_path, "some-live-session") is None
