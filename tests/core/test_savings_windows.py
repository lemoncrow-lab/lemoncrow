"""Windowed savings aggregation: routing and read both fold into saved_usd,
while still riding their own breakdown columns."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from atelier.core.capabilities import savings_summary as ss


def test_window_aggregate_folds_routing_and_read_into_saved_usd(tmp_path: Path) -> None:
    sid = "11111111-1111-1111-1111-111111111111"
    sdir = tmp_path / "sessions" / sid
    sdir.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    rows = [
        {"tool": "read", "tokens": 1000, "calls": 1, "cost_saved_usd": 0.01, "calls_usd": 0.06, "ts": now},
        {"kind": "routing", "usd": 0.5, "tool": "edit", "model": "claude-sonnet-4-5", "ts": now},
        {"kind": "compaction", "model": "claude-sonnet-4-5", "ts": now},
    ]
    (sdir / "savings.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    w = ss.aggregate_window_savings(tmp_path, days=7)

    # Routing still rides its own column (breakdown detail)...
    assert w.routing_usd == 0.5
    # ...but is now folded into the headline saved_usd (Total saved = Read +
    # Carry + Output + Routing, per the locked-in decision). calls_usd is
    # credited as written (priced at write time; no display-time discount).
    assert round(w.saved_usd, 6) == round(0.01 + 0.06 + 0.5, 6)
    assert w.tokens_saved == 1000
    assert w.calls_saved == 1
    # would_have_cost / saved_pct derive from saved_usd, so they include
    # routing too now.
    assert w.would_have_cost_usd == w.saved_usd + w.spend_usd
    # The "read" row is ALSO a read-lever row: it shows up in the Read
    # breakdown too (raw cost_saved_usd/tokens, not the calls_usd-inclusive
    # total — mirrors the per-session _read_session_read_savings rule).
    assert w.read_saved_usd == pytest.approx(0.01)
    assert w.read_saved_tokens == 1000
    # No session_end row in this ledger -> no carry.
    assert w.carry_usd == 0.0
    assert w.carry_tokens == 0
    assert w.total_saved_usd == pytest.approx(w.saved_usd)
