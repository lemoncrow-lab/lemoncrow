"""The single source of truth for spend, and what it does when it cannot answer.

The rule under test is narrow: a spend figure is either the usage read model's
answer for exactly the window asked for, or it is explicitly *unknown*. There is
no third state, and in particular there is no ``0.0`` standing in for "could not
read the store" -- that is how a busy month gets rendered as free, and how the
savings screen ends up dividing by a denominator that does not exist.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.usage import spend as spend_module
from lemoncrow.pro.capabilities.usage.spend import (
    SPEND_SOURCE,
    WindowSpend,
    reconcile_spend,
    spend_by_window,
    window_spend,
)


def _write_run(root: Path, *, session_id: str, model: str, days_ago: float) -> None:
    started = datetime.now(UTC) - timedelta(days=days_ago)
    snapshot: dict[str, Any] = {
        "session_id": session_id,
        "status": "completed",
        "created_at": started.isoformat(),
        "updated_at": started.isoformat(),
        "workspace_path": "/home/dev/alpha",
        "tool_call_count": 0,
        "telemetry": {},
        "cost": {
            "total_cost_usd": 0.0,
            "calls": [
                {
                    "model": model,
                    "input_tokens": 100_000,
                    "output_tokens": 5_000,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
            ],
        },
        "events": [],
    }
    target = (
        root
        / "sessions"
        / started.strftime("%Y")
        / started.strftime("%m")
        / started.strftime("%d")
        / "claude"
        / session_id
    )
    target.mkdir(parents=True, exist_ok=True)
    (target / "run.json").write_text(json.dumps(snapshot), encoding="utf-8")


def test_window_spend_matches_the_usage_aggregate_it_wraps(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.usage.aggregate import totals
    from lemoncrow.pro.capabilities.usage.collect import collect_usage_rows

    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="a", model="claude-sonnet-4-5", days_ago=0.5)
    _write_run(root, session_id="b", model="claude-opus-4-1", days_ago=9.0)

    now = datetime.now(UTC)
    measured = window_spend(root, days=30, now=now)
    expected = totals(collect_usage_rows(root, since=now - timedelta(days=30)))

    assert measured.available is True
    assert measured.total_usd == pytest.approx(expected.total_usd)
    assert measured.billed_usd == pytest.approx(expected.billed_usd)
    assert measured.rows == expected.rows
    assert measured.unpriced_rows == expected.unpriced_rows


def test_narrower_window_is_collected_not_sliced(tmp_path: Path) -> None:
    """A 1-day window must exclude a 9-day-old session, not inherit it."""

    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="a", model="claude-sonnet-4-5", days_ago=0.5)
    _write_run(root, session_id="b", model="claude-opus-4-1", days_ago=9.0)

    windows = spend_by_window(root, windows=(1, 7, 30))

    assert windows[1].rows == 1
    assert windows[7].rows == 1
    assert windows[30].rows == 2
    assert windows[1].total_usd < windows[30].total_usd


def test_unpriced_rows_make_the_total_a_floor(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="a", model="claude-sonnet-4-5", days_ago=0.5)
    _write_run(root, session_id="z", model="some-unlisted-model-v9", days_ago=0.5)

    measured = window_spend(root, days=7)

    assert measured.rows == 2
    assert measured.unpriced_rows == 1
    assert measured.is_floor is True
    assert measured.total_usd > 0  # the priced row still counts


def test_unreadable_store_is_unknown_not_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The degraded path emits a value and says the value is unknown."""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk gone")

    monkeypatch.setattr("lemoncrow.pro.capabilities.usage.collect.collect_usage_rows", _boom)

    measured = window_spend(tmp_path, days=30)

    assert measured.available is False
    assert measured.total_usd == 0.0
    assert measured.window_days == 30
    # The caller can tell "unknown" from "$0.00" -- which is the whole point.
    assert measured.to_dict()["available"] is False


def test_unreadable_session_tree_is_unknown_not_a_free_month(tmp_path: Path) -> None:
    """The degradation has to fire for a REAL I/O failure, not only a mocked one.

    ``test_unreadable_store_is_unknown_not_zero`` above proves the branch by
    making ``collect_usage_rows`` raise -- which it never does. The collector
    swallows every ``OSError`` by contract (``list_run_files`` globs, and
    ``pathlib`` drops a mid-walk ``PermissionError`` silently), so a store
    whose ``sessions/`` tree became unreadable reported ``available=True`` with
    ``$0.00`` and ``rows=0``. On the savings screen that renders as
    ``Spend $0.00 measured`` with ``Share 100.0%`` -- a fabricated free month,
    which is the exact signal this module exists to prevent.
    """

    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="a", model="claude-sonnet-4-5", days_ago=0.5)
    assert window_spend(root, days=30).total_usd > 0  # readable baseline

    sessions = root / "sessions"
    sessions.chmod(0o000)
    try:
        measured = window_spend(root, days=30)
    finally:
        sessions.chmod(0o755)

    assert measured.available is False
    assert measured.total_usd == 0.0
    assert measured.rows == 0
    assert measured.to_dict()["available"] is False


def test_a_store_whose_session_tree_is_not_a_directory_is_unknown_too(tmp_path: Path) -> None:
    """The same guard, in a form no user's privileges can read through.

    A truncated or half-synced store where ``sessions`` is not a directory
    walks the identical path: every reader degrades to zero rows, and the
    window would otherwise be published as a measured $0.00.
    """

    root = tmp_path / ".lemoncrow"
    root.mkdir(parents=True)
    (root / "sessions").write_text("not a directory", encoding="utf-8")

    measured = window_spend(root, days=30)

    assert measured.available is False
    assert measured.rows == 0


def test_an_unreadable_run_ledger_is_unknown_even_when_every_directory_reads(tmp_path: Path) -> None:
    """The permission bit a directory walk cannot see.

    The first guard only walked DIRECTORIES, so ``chmod 000`` on ``sessions/``
    was the one shape it caught. Every directory readable and the ``run.json``
    itself unreadable -- a bad umask, a half-synced mount -- went straight past
    it: ``rows_from_run_file`` swallows the ``PermissionError`` by contract, so
    the window came back ``available=True, total_usd=0.0, rows=0``, which is
    the fabricated free month one permission bit further in.
    """

    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="a", model="claude-sonnet-4-5", days_ago=0.5)
    assert window_spend(root, days=30).total_usd > 0  # readable baseline

    run_file = next(iter((root / "sessions").rglob("run.json")))
    run_file.chmod(0o000)
    try:
        measured = window_spend(root, days=30)
    finally:
        run_file.chmod(0o644)

    assert measured.available is False
    assert measured.total_usd == 0.0
    assert measured.rows == 0
    assert measured.to_dict()["available"] is False


def test_a_trace_only_store_whose_database_will_not_open_is_unknown(tmp_path: Path) -> None:
    """The collector's second source was never probed at all.

    ``collect_usage_rows`` reads run ledgers *and* the history database, and a
    store whose sessions were imported without a ledger has its whole spend in
    the latter. ``load_trace_facts`` / ``_token_rows`` catch every exception, so
    a database that will not open produced the same measured zero.
    """

    root = tmp_path / ".lemoncrow"
    root.mkdir(parents=True)
    database = root / spend_module._HISTORY_DB_NAME
    database.write_bytes(b"SQLite format 3\x00")
    database.chmod(0o000)
    try:
        measured = window_spend(root, days=30)
    finally:
        database.chmod(0o644)

    assert measured.available is False
    assert measured.rows == 0


def test_the_probed_history_db_name_is_the_one_the_store_uses(tmp_path: Path) -> None:
    """The probe spells the database's name out; this is the anti-drift guard."""

    from lemoncrow.core.foundation.history_store import HistoryStore

    assert HistoryStore(tmp_path).db_path.name == spend_module._HISTORY_DB_NAME


def test_the_windows_share_one_readability_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A quiet store is walked once per call, not once per window.

    The probe walks the whole ``sessions/`` tree and asks about the STORE, not
    about a window, so paying for it three times over is pure waste --
    ~0.1s per walk on a 4,530-directory store, on every ``lc savings`` and every
    ``statusline_segment`` MCP call whose recent windows happen to be empty.
    """

    root = tmp_path / ".lemoncrow"
    (root / "sessions").mkdir(parents=True)

    walked: list[Path] = []
    real = spend_module._store_is_readable

    def _counted(probe_root: Path) -> bool:
        walked.append(probe_root)
        return real(probe_root)

    monkeypatch.setattr(spend_module, "_store_is_readable", _counted)

    windows = spend_by_window(root, windows=(1, 7, 30))

    assert all(window.available for window in windows.values())
    assert len(walked) == 1


def test_empty_but_readable_store_is_still_a_real_zero(tmp_path: Path) -> None:
    """The probe must not turn an honestly quiet window into "unknown"."""

    root = tmp_path / ".lemoncrow"
    (root / "sessions").mkdir(parents=True)
    measured = window_spend(root, days=30)

    assert measured.available is True
    assert measured.total_usd == 0.0
    assert measured.rows == 0


def test_missing_store_still_answers(tmp_path: Path) -> None:
    """No store on disk is an empty window, not an exception."""

    measured = window_spend(tmp_path / "nope", days=7)
    assert measured.available is True
    assert measured.total_usd == 0.0
    assert measured.rows == 0


def test_reconcile_spend_rewrites_every_window_and_stamps_the_source(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    _write_run(root, session_id="a", model="claude-sonnet-4-5", days_ago=0.5)

    payload: dict[str, Any] = {
        "summary_breakdown": {
            # Deliberately wrong, ledger-derived figures: they must be replaced.
            "1D": {"usd": 1.0, "spend": 9_999.0},
            "7D": {"usd": 2.0, "spend": 9_999.0},
            "30D": {"usd": 3.0, "spend": 9_999.0},
        }
    }

    out = reconcile_spend(payload, root)

    assert out["spend_source"] == SPEND_SOURCE
    for key in ("1D", "7D", "30D"):
        bucket = out["summary_breakdown"][key]
        assert bucket["spend"] != 9_999.0
        assert bucket["spend_available"] is True
        assert bucket["spend_rows"] == 1
        # Savings columns are untouched -- only spend has a new owner.
    assert out["summary_breakdown"]["30D"]["usd"] == 3.0
    assert set(out["spend_windows"]) == {"1D", "7D", "30D"}


def test_reconcile_spend_removes_the_key_when_it_cannot_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown spend must vanish from the payload, not arrive as 0.0.

    The renderer prints "unavailable" for a missing key and "$0.00" for a
    present zero, so this is the difference between an absent signal and a
    false one.
    """

    def _unknown(_root: Any, *, days: int, now: Any = None, store_readable: Any = None) -> WindowSpend:
        return WindowSpend(window_days=days, since="", available=False)

    monkeypatch.setattr("lemoncrow.pro.capabilities.usage.spend.window_spend", _unknown)

    payload: dict[str, Any] = {"summary_breakdown": {"30D": {"usd": 3.0, "spend": 9_999.0}}}
    out = reconcile_spend(payload, Path("/nonexistent"), windows=(30,))

    assert "spend" not in out["summary_breakdown"]["30D"]
    assert out["summary_breakdown"]["30D"]["spend_available"] is False


def test_reconcile_spend_leaves_a_payload_without_a_breakdown_alone() -> None:
    payload: dict[str, Any] = {"saved_usd": 1.0}
    assert reconcile_spend(payload, Path("/nonexistent")) == {"saved_usd": 1.0}
