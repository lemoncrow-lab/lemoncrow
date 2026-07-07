"""Incremental transcript-stats fold: append-only cursor, partial tails, rebuilds.

read_transcript_stats used to re-parse the whole transcript on every call —
O(session) per turn, O(session²) over a session's life. The fold keeps a byte
cursor per source and parses only appended lines; these tests pin the cursor
semantics the statusline render path now depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities import savings_summary as ss


def _entry(msg_id: str, in_t: int = 100, out_t: int = 10, ts: str = "2026-07-02T00:00:00Z") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": in_t, "output_tokens": out_t},
                "content": [{"type": "tool_use", "id": f"tu_{msg_id}", "name": "read"}],
            },
        }
    )


def test_incremental_append_and_cache_hit(tmp_path: Path) -> None:
    ss._transcript_stats_cache.clear()
    t = tmp_path / "s1.jsonl"
    t.write_text(_entry("m1") + "\n" + _entry("m2") + "\n", encoding="utf-8")

    first = ss.read_transcript_stats(t)
    assert first is not None and first.turns == 2 and first.input_tokens == 200

    # Unchanged file → exact cache hit (same object, no re-parse).
    assert ss.read_transcript_stats(t) is first

    # Appended lines are folded in without re-reading the prefix.
    with t.open("a", encoding="utf-8") as fh:
        fh.write(_entry("m3") + "\n")
    second = ss.read_transcript_stats(t)
    assert second is not None and second.turns == 3 and second.input_tokens == 300
    assert second.tool_calls == 3

    # Duplicate message ids stay deduped across incremental folds.
    with t.open("a", encoding="utf-8") as fh:
        fh.write(_entry("m3") + "\n")
    third = ss.read_transcript_stats(t)
    assert third is not None and third.turns == 3 and third.input_tokens == 300


def test_partial_tail_line_left_for_next_call(tmp_path: Path) -> None:
    ss._transcript_stats_cache.clear()
    t = tmp_path / "s2.jsonl"
    full = _entry("m1")
    t.write_text(full + "\n" + full[: len(full) // 2], encoding="utf-8")  # torn tail write

    stats = ss.read_transcript_stats(t)
    assert stats is not None and stats.turns == 1  # torn line not consumed

    # Completing the line (with a fresh id) makes it count on the next call.
    completed = _entry("m2")
    with t.open("a", encoding="utf-8") as fh:
        fh.write(completed[len(full) // 2 :].replace("m1", "mX") + "\n")
    # The stitched line is garbage JSON half + half — must not crash, and a
    # clean follow-up line still lands.
    with t.open("a", encoding="utf-8") as fh:
        fh.write(_entry("m3") + "\n")
    stats2 = ss.read_transcript_stats(t)
    assert stats2 is not None and stats2.turns == 2  # m1 + m3; torn line dropped


def test_shrunken_source_forces_rebuild(tmp_path: Path) -> None:
    ss._transcript_stats_cache.clear()
    t = tmp_path / "s3.jsonl"
    t.write_text(_entry("m1") + "\n" + _entry("m2") + "\n", encoding="utf-8")
    assert ss.read_transcript_stats(t).turns == 2  # type: ignore[union-attr]

    t.write_text(_entry("m9") + "\n", encoding="utf-8")  # rewrite, smaller
    rebuilt = ss.read_transcript_stats(t)
    assert rebuilt is not None and rebuilt.turns == 1 and rebuilt.input_tokens == 100


def test_bump_historical_cache_folds_row_without_rescan() -> None:
    ss._historical_savings_cache.clear()
    key = (7, "/tmp/root")
    ss._historical_savings_cache[key] = (123.0, (1.0, 1000, 2, 5, 50.0, 3.0, 0.25, 0.1, 200, 7))

    ss._bump_historical_savings_cache(
        {"tool": "read", "tokens": 400, "calls": 1, "cost_saved_usd": 0.002, "calls_usd": 0.05, "ts": "x"}
    )
    cached_ts, val = ss._historical_savings_cache[key]
    assert cached_ts == 123.0  # TTL clock untouched — still refreshes on schedule
    usd, tok, calls, turns, spend, carry, routing, read_usd, read_tok, carry_tok = val
    # calls_usd credited as written (priced at write time; no display discount).
    assert round(usd, 6) == round(1.0 + 0.002 + 0.05, 6)
    assert tok == 1400 and calls == 3 and turns == 6
    assert spend == 50.0 and carry == 3.0  # spend/carry never bumped per-row
    assert routing == 0.25  # context rows never touch the routing column
    # tool="read" is a read-lever row: read_usd/read_tok get incremented by
    # the row's raw tokens/cost_saved_usd (not the calls_usd-inclusive total).
    assert round(read_usd, 6) == round(0.1 + 0.002, 6)
    assert read_tok == 200 + 400
    assert carry_tok == 7  # carry tokens never bumped per-row (TTL-refreshed only)

    # Routing rows fold ONLY the routing column, leaving savings untouched.
    ss._bump_historical_savings_cache({"kind": "routing", "usd": 0.75, "ts": "x"})
    _, val = ss._historical_savings_cache[key]
    assert val[0] == usd and val[1] == tok and val[2] == calls and val[3] == turns
    assert val[6] == 1.0
    assert val[7] == read_usd and val[8] == read_tok and val[9] == carry_tok

    ss._historical_savings_cache.clear()
    ss._bump_historical_savings_cache({"tokens": 400})  # empty cache → no-op, no crash
