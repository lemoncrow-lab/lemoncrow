"""Incremental savings aggregate == full recompute (the correctness oracle).

The persisted day-bucketed aggregate (``reconcile_savings_aggregate``) must
always equal what a from-scratch fold of every ``sessions/**/savings.jsonl``
produces (``recompute_savings_aggregate``) — across window boundaries, rows
aging out of the 1d/7d/30d windows, and incremental folds of new sessions and
of rows appended to already-folded sessions.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from atelier.core.capabilities import savings_summary as ss
from atelier.core.capabilities.savings_summary import (
    _read_historical_savings_many,
    _window_from_aggregate,
    recompute_savings_aggregate,
    reconcile_savings_aggregate,
)

NOW = time.time()
DAY = 86_400.0
WINDOWS = (1, 7, 30)


def _iso(epoch: float) -> str:
    # Ledger rows are stamped naive-UTC (datetime.utcnow()).
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None).isoformat()


def _row(epoch: float, tokens: int, usd: float, calls: int = 1) -> str:
    return json.dumps({"ts": _iso(epoch), "tokens": tokens, "cost_saved_usd": usd, "calls": calls})


def _end_row(epoch: float, cost: float, carry: float = 0.0) -> str:
    return json.dumps({"ts": _iso(epoch), "kind": "session_end", "est_cost_usd": cost, "carry_usd": carry})


def _append(root: Path, sid: str, lines: list[str]) -> Path:
    d = root / "sessions" / "claude" / sid
    d.mkdir(parents=True, exist_ok=True)
    p = d / "savings.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return p


def _windows(agg: dict[str, Any]) -> dict[int, tuple[float, int, int, int, float, float]]:
    return {d: _window_from_aggregate(agg, d, NOW) for d in WINDOWS}


def _assert_windows_equal(
    incremental: dict[int, tuple[float, int, int, int, float, float]],
    oracle: dict[int, tuple[float, int, int, int, float, float]],
) -> None:
    for d in WINDOWS:
        assert incremental[d] == pytest.approx(oracle[d]), f"{d}d window diverged"


def _reset_process_state(root: Path) -> None:
    """Simulate a fresh process: drop this root's in-memory caches."""
    root_str = str(root)
    ss._aggregate_state.pop(root_str, None)
    ss._aggregate_refreshed_at.pop(root_str, None)
    for key in [k for k in ss._historical_savings_cache if k[1] == root_str]:
        del ss._historical_savings_cache[key]


def test_incremental_equals_full_recompute_multi_day(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    # s1: rows on both sides of the 30d boundary (40d ago ages out, 20d ago in).
    _append(root, "aggtest-s1", [_row(NOW - 40 * DAY, 4000, 4.0), _row(NOW - 20 * DAY, 2000, 2.0)])
    # s2: rows straddling the 7d boundary + a finished-session cost snapshot
    # (spend/carry attributed whole at end time).
    _append(
        root,
        "aggtest-s2",
        [
            _row(NOW - 8 * DAY, 800, 0.8),
            _row(NOW - 6 * DAY, 600, 0.6),
            _end_row(NOW - 6 * DAY + 60, 12.5, carry=1.25),
        ],
    )
    agg = reconcile_savings_aggregate(root)
    _assert_windows_equal(_windows(agg), _windows(recompute_savings_aggregate(root)))

    # Fold in new work: a brand-new session inside the 1d window, and rows
    # appended to an already-folded session — the re-fold must REPLACE s2's
    # previous buckets, not add to them.
    _append(root, "aggtest-s2", [_row(NOW - 3600, 50, 0.05)])
    # s3 resumed after its snapshot (savings row newer than session_end) with
    # no transcript on disk → the stale snapshot stays the spend fallback.
    _append(
        root,
        "aggtest-s3",
        [_row(NOW - 3 * 3600, 300, 0.3), _end_row(NOW - 2 * 3600, 5.0, carry=0.5), _row(NOW - 3600, 100, 0.1)],
    )
    agg2 = reconcile_savings_aggregate(root)
    oracle = recompute_savings_aggregate(root)
    _assert_windows_equal(_windows(agg2), _windows(oracle))

    # Bucket-level equality, not just window sums.
    assert agg2["sessions"].keys() == oracle["sessions"].keys()
    for key, entry in oracle["sessions"].items():
        inc_days = agg2["sessions"][key]["days"]
        assert inc_days.keys() == entry["days"].keys(), key
        for day, vals in entry["days"].items():
            assert inc_days[day] == pytest.approx(vals), f"{key} {day}"
    assert agg2["watermark"] == pytest.approx(oracle["watermark"])
    assert agg2["first_ts"] == pytest.approx(oracle["first_ts"])
    assert agg2["first_ts"] > 0


def test_rows_age_out_of_windows(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _append(root, "aggtest-age", [_row(NOW - 40 * DAY, 40, 40.0)])  # out of every window
    _append(root, "aggtest-age", [_row(NOW - 10 * DAY, 10, 10.0)])  # 30d only
    _append(root, "aggtest-age", [_row(NOW - 3 * DAY, 3, 3.0)])  # 7d + 30d
    _append(root, "aggtest-age", [_row(NOW - 3600, 1, 1.0)])  # every window
    agg = reconcile_savings_aggregate(root)
    w = _windows(agg)
    assert w[1][1] == 1
    assert w[7][1] == 1 + 3
    assert w[30][1] == 1 + 3 + 10
    assert w[1][0] == pytest.approx(1.0)
    assert w[7][0] == pytest.approx(4.0)
    assert w[30][0] == pytest.approx(14.0)
    _assert_windows_equal(w, _windows(recompute_savings_aggregate(root)))


def test_blocking_read_path_matches_oracle(tmp_path: Path) -> None:
    """A fresh process reading the persisted aggregate + folding newer ledgers
    must answer exactly what a full recompute answers."""
    root = tmp_path / ".atelier"
    _append(root, "aggtest-r1", [_row(NOW - 2 * DAY, 200, 0.2), _end_row(NOW - 2 * DAY + 30, 3.0, carry=0.3)])
    reconcile_savings_aggregate(root)

    # New ledger written AFTER the aggregate was persisted.
    _append(root, "aggtest-r2", [_row(NOW - 3600, 500, 0.5)])
    _reset_process_state(root)

    res = _read_historical_savings_many(WINDOWS, root, block=True)
    oracle = _windows(recompute_savings_aggregate(root))
    _assert_windows_equal(res, oracle)
    assert res[1][1] == 500  # the unfolded ledger was picked up


def test_carry_attributed_per_day(tmp_path: Path) -> None:
    """Session carry is attributed to the day each saved token was generated
    (proportional to that day's token share), not dumped on the end day — so
    windowed carry scales with the reporting window (index 5 = carry)."""
    root = tmp_path / ".atelier"
    _append(
        root,
        "aggtest-carry",
        [
            _row(NOW - 5 * DAY, 300, 0.3),
            _row(NOW - 1 * DAY, 100, 0.1),
            _end_row(NOW - 1 * DAY + 60, 5.0, carry=4.0),
        ],
    )
    agg = reconcile_savings_aggregate(root)
    w = _windows(agg)
    # carry 4.0 splits by token share: day-5 → 3.0 (300/400), day-1 → 1.0 (100/400).
    # 1d window sees only the recent day; 7d and 30d see both.
    assert w[1][5] == pytest.approx(1.0), w
    assert w[7][5] == pytest.approx(4.0), w
    assert w[30][5] == pytest.approx(4.0), w
    _assert_windows_equal(w, _windows(recompute_savings_aggregate(root)))


def test_first_savings_ts_folds_min(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    p1 = _append(root, "aggtest-t1", [_row(NOW - 3600, 10, 0.1)])
    agg = reconcile_savings_aggregate(root)
    assert agg["first_ts"] == pytest.approx(p1.stat().st_mtime)
    # A second, older-mtimed ledger drags first_ts down; appending to the
    # existing one (mtime moves forward) must never raise it.
    import os

    p2 = _append(root, "aggtest-t2", [_row(NOW - 2 * 3600, 20, 0.2)])
    os.utime(p2, (NOW - 9 * DAY, NOW - 9 * DAY))
    first_after_old = reconcile_savings_aggregate(root)["first_ts"]
    assert first_after_old == pytest.approx(NOW - 9 * DAY)
    _append(root, "aggtest-t2", [_row(NOW - 60, 5, 0.05)])
    assert reconcile_savings_aggregate(root)["first_ts"] == pytest.approx(first_after_old)
