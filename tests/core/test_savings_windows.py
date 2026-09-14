"""Windowed savings aggregation: routing and read both fold into saved_usd,
while still riding their own breakdown columns."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities import savings_summary as ss


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


def test_resumed_session_spend_is_not_counted_once_per_date_partition() -> None:
    """A session resumed across days must contribute its spend ONCE.

    A long-running Claude session gets one ledger directory per date partition
    (``sessions/YYYY/MM/DD/claude/<sid>``) and ``_fold_session_file`` derives
    each one's spend from the transcript it resolves BY SESSION ID -- the same
    transcript for every partition. Each fold therefore re-counted every turn
    the previous fold had already counted.

    Measured on a real store before the fix: session
    ``64d17923-...`` had three partitions whose cached turn sets were strict
    prefixes of one another (3,981 turns c 11,291 turns c 19,113 turns, union
    19,113) and whose spends were summed to $4,427.73 instead of the $2,518.76
    the newest snapshot actually recorded. Store-wide that inflated the 30-day
    spend by $6,468.86 of $19,075.95, which is most of why the savings screen
    disagreed with ``lc usage --since 30d`` by 64%.

    The savings columns are NOT de-duplicated: each partition's ledger holds
    its own distinct rows, which really are additive.
    """
    sid = "64d17923-8d4b-4c26-a661-b5c31ea37047"
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    # [usd, tok, calls, turns, spend, carry, routing, read_usd, read_tok, carry_tok]
    agg = {
        "sessions": {
            f"2026/08/08/claude/{sid}": {"days": {day: [1.0, 10, 1, 1, 460.83, 0, 0, 0, 0, 0]}},
            f"2026/08/11/claude/{sid}": {"days": {day: [2.0, 20, 2, 1, 1328.28, 0, 0, 0, 0, 0]}},
            f"2026/08/14/claude/{sid}": {"days": {day: [4.0, 40, 4, 1, 2485.81, 0, 0, 0, 0, 0]}},
            "2026/08/14/claude/other-session": {"days": {day: [8.0, 80, 8, 1, 100.0, 0, 0, 0, 0, 0]}},
        }
    }

    usd, tok, calls, _turns, spend, *_rest = ss._window_from_aggregate(agg, 30, time.time())

    # Spend: the largest snapshot of the resumed session, plus the other one.
    assert spend == pytest.approx(2485.81 + 100.0)
    assert spend != pytest.approx(460.83 + 1328.28 + 2485.81 + 100.0)
    # Savings stay additive across the partitions -- they are distinct rows.
    assert usd == pytest.approx(1.0 + 2.0 + 4.0 + 8.0)
    assert tok == 150
    assert calls == 15


def test_daily_rollup_de_duplicates_spend_exactly_like_the_window_totals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The public daily rollup must not disagree with the window it slices.

    ``_window_from_aggregate`` de-duplicates the spend column per session id
    because every date partition of a resumed session derives its spend from
    the SAME transcript. ``aggregate_savings_by_day`` -- which feeds the public
    rollup that ``lc`` posts -- still plain-summed that column, so the identical
    aggregate produced $2,585.81 through one function and $4,374.92 through the
    other. The savings columns stay additive in both.
    """
    sid = "64d17923-8d4b-4c26-a661-b5c31ea37047"
    now = datetime.now(UTC)
    day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # [usd, tok, calls, turns, spend, carry, routing, read_usd, read_tok, carry_tok]
    agg: dict[str, Any] = {
        "sessions": {
            f"2026/08/08/claude/{sid}": {"days": {day: [1.0, 10, 1, 1, 460.83, 0, 0, 0, 0, 0]}},
            f"2026/08/11/claude/{sid}": {"days": {day: [2.0, 20, 2, 1, 1328.28, 0, 0, 0, 0, 0]}},
            f"2026/08/14/claude/{sid}": {"days": {day: [4.0, 40, 4, 1, 2485.81, 0, 0, 0, 0, 0]}},
            "2026/08/14/claude/other-session": {"days": {day: [8.0, 80, 8, 1, 100.0, 0, 0, 0, 0, 0]}},
        }
    }
    monkeypatch.setattr(ss, "reconcile_savings_aggregate", lambda _root, **_kw: agg)

    by_day = ss.aggregate_savings_by_day(
        tmp_path,
        since_day=(now - timedelta(days=3)).strftime("%Y-%m-%d"),
        today=now.strftime("%Y-%m-%d"),
    )
    bucket = by_day[day]

    assert bucket["est_cost_usd"] == pytest.approx(2485.81 + 100.0)
    assert bucket["est_cost_usd"] != pytest.approx(460.83 + 1328.28 + 2485.81 + 100.0)
    # ...which is exactly what the window totals report for the same aggregate.
    assert bucket["est_cost_usd"] == pytest.approx(ss._window_from_aggregate(agg, 30, time.time())[4])
    # Savings stay additive -- each partition's ledger holds its own rows.
    assert bucket["saved_usd"] == pytest.approx(1.0 + 2.0 + 4.0 + 8.0)
    assert bucket["tokens_saved"] == 150
    assert bucket["calls_avoided"] == 15


def test_daily_rollup_de_duplicates_partitions_that_land_on_different_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cross-day shape, which a per-(day, session) maximum does not repair.

    De-duplicating per ``(day, session id)`` is a sum of per-day maxima, where
    ``_window_from_aggregate`` takes the maximum of per-partition sums. The two
    agree only when every partition of a resumed session posts on the SAME day
    -- the transcript path, which re-buckets the same turns onto the same
    turn-days. The ``session_end`` path writes each partition's cumulative
    snapshot on that partition's own Stop day, and that is the ordinary shape:
    ``session_dir`` searches back only ``search_days``, so a session resumed
    later mints a new partition on a later day while the old one stops
    receiving rows and keeps its own snapshot where it was, and
    ``stop.py`` writes the whole session's running total into each new one.

    Three such partitions still rolled up to $4,374.92 against the window's
    $2,585.81 -- the original 2x, entirely unmitigated.
    """
    sid = "64d17923-8d4b-4c26-a661-b5c31ea37047"
    now = datetime.now(UTC)
    day_1 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    day_2 = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    day_3 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # [usd, tok, calls, turns, spend, carry, routing, read_usd, read_tok, carry_tok]
    agg: dict[str, Any] = {
        "sessions": {
            f"2026/08/08/claude/{sid}": {"days": {day_1: [1.0, 10, 1, 1, 460.83, 0, 0, 0, 0, 0]}},
            f"2026/08/11/claude/{sid}": {"days": {day_2: [2.0, 20, 2, 1, 1328.28, 0, 0, 0, 0, 0]}},
            f"2026/08/14/claude/{sid}": {"days": {day_3: [4.0, 40, 4, 1, 2485.81, 0, 0, 0, 0, 0]}},
            "2026/08/14/claude/other-session": {"days": {day_3: [8.0, 80, 8, 1, 100.0, 0, 0, 0, 0, 0]}},
        }
    }
    monkeypatch.setattr(ss, "reconcile_savings_aggregate", lambda _root, **_kw: agg)

    by_day = ss.aggregate_savings_by_day(
        tmp_path,
        since_day=(now - timedelta(days=5)).strftime("%Y-%m-%d"),
        today=now.strftime("%Y-%m-%d"),
    )
    rolled_up = sum(float(bucket["est_cost_usd"]) for bucket in by_day.values())

    assert rolled_up == pytest.approx(2485.81 + 100.0)
    assert rolled_up != pytest.approx(460.83 + 1328.28 + 2485.81 + 100.0)
    # ...which is exactly what the window totals report for the same aggregate.
    assert rolled_up == pytest.approx(ss._window_from_aggregate(agg, 30, time.time())[4])
    # Only the dominant partition posts, and it posts on its own day -- so the
    # days still add back up to the window total rather than merely capping it.
    assert by_day[day_1]["est_cost_usd"] == pytest.approx(0.0)
    assert by_day[day_2]["est_cost_usd"] == pytest.approx(0.0)
    assert by_day[day_3]["est_cost_usd"] == pytest.approx(2485.81 + 100.0)
    # Savings stay additive across days -- each partition holds its own rows.
    assert sum(float(bucket["saved_usd"]) for bucket in by_day.values()) == pytest.approx(15.0)
    assert sum(int(bucket["tokens_saved"]) for bucket in by_day.values()) == 150
