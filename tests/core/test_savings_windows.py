"""Windowed savings aggregation: routing rides its own column, never saved_usd."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atelier.core.capabilities import savings_summary as ss


def test_window_aggregate_folds_routing_separately(tmp_path: Path) -> None:
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

    # Routing folds into its own column — never into saved_usd — and the
    # compaction marker row contributes nothing anywhere. calls_usd is
    # credited as written (priced at write time; no display-time discount).
    assert w.routing_usd == 0.5
    assert round(w.saved_usd, 6) == round(0.01 + 0.06, 6)
    assert w.tokens_saved == 1000
    assert w.calls_saved == 1
    # would_have_cost / saved_pct deliberately exclude the routing channel.
    assert w.would_have_cost_usd == w.saved_usd + w.spend_usd
