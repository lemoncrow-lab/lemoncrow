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

from lemoncrow.core.capabilities import savings_summary as ss
from lemoncrow.core.capabilities.savings_summary import (
    _read_historical_savings_many,
    _window_from_aggregate,
    aggregate_usage_totals_since_day,
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
    root = tmp_path / ".lemoncrow"
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


def test_large_writer_accumulated_row_survives_window_fold(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    tokens = 7_214_776
    usd = 3.607388
    _append(
        root,
        "aggtest-large",
        [json.dumps({"ts": _iso(NOW - 3600), "kind": "input_style", "tokens": tokens, "cost_saved_usd": usd})],
    )

    window = _windows(reconcile_savings_aggregate(root))[1]
    assert window[0] == pytest.approx(usd)
    assert window[1] == tokens


def test_rows_age_out_of_windows(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
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
    root = tmp_path / ".lemoncrow"
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
    root = tmp_path / ".lemoncrow"
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


def _write_transcript(claude_root: Path, session_id: str, events: list[dict[str, Any]]) -> Path:
    p = claude_root / "projects" / "proj" / f"{session_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    return p


def _assistant_turn(ts_epoch: float, *, in_t: int, out_t: int, tool: str | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    if tool:
        content = [{"type": "tool_use", "name": tool, "id": f"tu-{ts_epoch}", "input": {}}]
    return {
        "type": "assistant",
        "timestamp": datetime.fromtimestamp(ts_epoch, UTC).isoformat().replace("+00:00", "Z"),
        "message": {
            "id": f"msg-{ts_epoch}",
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": in_t, "output_tokens": out_t},
            "content": content,
        },
    }


def test_aggregate_usage_totals_since_day_reads_real_transcript_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tokens_processed/calls_made/time_spent_seconds are real per-session
    transcript totals (read_transcript_stats), not derived from the $-savings
    ledger -- independent of aggregate_savings_since_day's bucket machinery."""
    root = tmp_path / ".lemoncrow"
    claude_root = tmp_path / "claude_home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))

    in_range_ts = NOW - 1 * DAY
    _append(root, "aggtest-usage", [_end_row(in_range_ts, 1.0)])
    _write_transcript(
        claude_root,
        "aggtest-usage",
        [
            _assistant_turn(in_range_ts, in_t=100, out_t=50, tool="Bash"),
            _assistant_turn(in_range_ts + 330, in_t=80, out_t=30),
        ],
    )

    # A second session outside the (since_day, today) window must be excluded.
    out_of_range_ts = NOW - 10 * DAY
    _append(root, "aggtest-old", [_end_row(out_of_range_ts, 1.0)])
    _write_transcript(
        claude_root,
        "aggtest-old",
        [_assistant_turn(out_of_range_ts, in_t=999, out_t=999, tool="Bash")],
    )

    since_day = ss._day_key(NOW - 2 * DAY)
    today = ss._day_key(NOW)
    totals = aggregate_usage_totals_since_day(root, since_day=since_day, today=today)

    assert totals["tokens_processed"] == 260  # (100+50) + (80+30), old session excluded
    assert totals["calls_made"] == 1  # one tool_use, in the in-range session only
    assert totals["time_spent_seconds"] == pytest.approx(330.0)


def test_first_savings_ts_folds_min(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
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


def test_turn_cut_rows_populate_turns_avoided(tmp_path: Path) -> None:
    """turn_cut rows (whole avoided turns) sum into the daily rollup's
    turns_avoided, and still count toward calls_avoided (the credit rides
    the row's ``calls`` field)."""
    from lemoncrow.core.capabilities.savings_summary import aggregate_savings_since_day

    root = tmp_path / ".lemoncrow"
    yday = NOW - DAY
    _append(
        root,
        "turncut-1",
        [
            _row(yday, 1000, 0.5, calls=2),
            json.dumps({"ts": _iso(yday), "kind": "turn_cut", "calls": 7, "calls_usd": 0.3}),
        ],
    )
    totals, last_day = aggregate_savings_since_day(root, since_day="2000-01-01", today=_iso(NOW)[:10])
    assert last_day == _iso(yday)[:10]
    assert totals["turns_avoided"] == 7
    assert totals["calls_avoided"] == 9  # 2 tool calls + 7 turn_cut credit


def test_aggregate_savings_since_day_excludes_reconcile_ledger_gap(tmp_path: Path) -> None:
    """The "_reconcile/ledger-gap" self-heal placeholder (see
    plugin_runtime._RECONCILE_HOST) is a synthetic account-watermark patch,
    not a real session's savings. It must never leak into the public daily
    rollup total -- it can dwarf a real day's total and would blow past the
    rollup server's per-post $ cap, permanently wedging the flush."""
    from lemoncrow.core.capabilities.savings_summary import aggregate_savings_since_day

    root = tmp_path / ".lemoncrow"
    yday = NOW - DAY
    _append(root, "real-session", [_row(yday, 1000, 12.5, calls=3)])

    gap_dir = root / "sessions" / "_reconcile" / "ledger-gap"
    gap_dir.mkdir(parents=True, exist_ok=True)
    (gap_dir / "savings.jsonl").write_text(_row(yday, 1, 2786.52, calls=0) + "\n", encoding="utf-8")

    totals, last_day = aggregate_savings_since_day(root, since_day="2000-01-01", today=_iso(NOW)[:10])
    assert last_day == _iso(yday)[:10]
    assert totals["saved_usd"] == pytest.approx(12.5)
