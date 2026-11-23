"""P9-6: the local Inbox page and Home's read cost the same at 500 reviews as the budget allows at 50.

The budget is ``inbox_load`` in ``evals/performance/slo.toml`` (p95 for the first page of 50 local reviews); it is read,
never copied, so changing the table changes the guard. Time is the process's CPU seconds, not wall seconds, so a loaded
machine cannot fail it; the statement counts are exact and do not depend on the machine at all.
"""

from __future__ import annotations

import sqlite3
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_server_core.local_inbox import LocalInbox
from test_local_review_inbox_views import REPO, _Local, _ok, _refused, _review, _run

from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

SLO = Path(__file__).resolve().parents[2] / "evals" / "performance" / "slo.toml"
RUNS = 7

# The SLO table is not in the public mirror: there the whole module skips instead of crashing in _budget_ms.
pytestmark = pytest.mark.skipif(not SLO.exists(), reason="the SLO table is not published")


def _budget_ms() -> float:
    table = tomllib.loads(SLO.read_text(encoding="utf-8"))["target"]
    return float(next(row for row in table if row["id"] == "inbox_load")["p95"])


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, -(-95 * len(ordered) // 100) - 1)]


async def _timed(local: _Local, **params: Any) -> tuple[float, dict[str, Any]]:
    started = time.process_time()
    body = await _ok(await local.get("/v1/reviews", **params))
    return (time.process_time() - started) * 1000.0, body


def _seed(local: _Local, count: int) -> None:
    store = local.store(REPO)
    for index in range(count):
        _review(store, f"speed-{index:04d}", units=4)


def _selects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statements: list[str] = []
    original = SqliteTableStore._connect

    def counting(self: SqliteTableStore) -> sqlite3.Connection:
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(SqliteTableStore, "_connect", counting)
    return statements


def test_the_page_at_500_reviews_reads_no_more_statements_than_at_50_and_stays_in_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results: dict[int, tuple[int, float]] = {}
    for count in (50, 500):

        async def body(local: _Local, count: int = count) -> None:
            _seed(local, count)
            params = {"view": "all", "status": "all", "limit": "100"}
            await _timed(local, **params)  # derives and stores each review's progress and title once
            statements = _selects(monkeypatch)
            samples: list[float] = []
            for _ in range(RUNS):
                statements.clear()
                elapsed, page = await _timed(local, **params)
                samples.append(elapsed)
                assert len(page["reviews"]) == min(count, 100) and page["total"] == count
            monkeypatch.undo()
            selects = sum(1 for text in statements if text.lstrip().upper().startswith("SELECT"))
            results[count] = (selects, _p95(samples))

        _run(tmp_path / str(count), body)
    small, large = results[50], results[500]
    # No per-review fan-out: the page reads the same number of statements whatever the number of reviews is.
    assert large[0] <= small[0] + 2, results
    assert large[1] <= _budget_ms(), f"p95 {large[1]:.0f} ms of CPU at 500 reviews, budget {_budget_ms():.0f} ms"


def test_home_reads_its_500_reviews_in_one_request_inside_the_same_budget(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        _seed(local, 500)
        params = {"view": "all", "status": "all", "read": "home"}
        await _timed(local, **params)
        samples: list[float] = []
        for _ in range(RUNS):
            elapsed, page = await _timed(local, **params)
            samples.append(elapsed)
            assert len(page["reviews"]) == 500 and page["next_cursor"] == "" and page["limit"] == 500
            assert all("progress" in row and "queue" in row for row in page["reviews"])
        assert (
            _p95(samples) <= _budget_ms()
        ), f"p95 {_p95(samples):.0f} ms of CPU for Home's 500, budget {_budget_ms():.0f}"

    _run(tmp_path, body)


def test_a_home_read_is_the_only_read_that_may_ask_for_more_than_100(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        _seed(local, 120)
        everything = await _ok(await local.get("/v1/reviews", view="all", read="home"))
        assert len(everything["reviews"]) == 120 and everything["next_cursor"] == ""
        capped = await _ok(await local.get("/v1/reviews", view="all", read="home", limit="60"))
        assert len(capped["reviews"]) == 60 and capped["next_cursor"]
        await _refused(await local.get("/v1/reviews", view="all", limit="101"), 400, "limit")
        await _refused(await local.get("/v1/reviews", view="all", read="home", limit="501"), 400, "limit")
        await _refused(await local.get("/v1/reviews", view="all", read="everything"), 400, "read")

    _run(tmp_path, body)


def test_a_home_read_scores_every_review_once_not_once_per_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(local: _Local) -> None:
        _seed(local, 300)
        scored: list[int] = []
        original = LocalInbox._page

        def counting(self: LocalInbox, *args: Any, **kwargs: Any) -> Any:
            scored.append(1)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(LocalInbox, "_page", counting)
        await _ok(await local.get("/v1/reviews", view="all", status="all", read="home"))
        assert len(scored) == 1
        scored.clear()
        for _ in range(3):  # what Home did before: a page of 100 at a time
            await _ok(await local.get("/v1/reviews", view="all", status="all", limit="100"))
        assert len(scored) == 3

    _run(tmp_path, body)
