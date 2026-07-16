"""Self-heal a local savings ledger that fell behind the server's verified total.

Distinct from ``session_backfill`` (manual, estimates savings for sessions
that NEVER touched LemonCrow anywhere -- no local OR server record). This is
the automatic, cheap counterpart for the opposite case: real LemonCrow usage
DID happen and the server already knows the true total, but the local ledger
lost it (deleted, disk issue, restored snapshot). No transcript re-parsing --
just closes the numeric gap against the already-verified server figure.
"""

from __future__ import annotations

import json
from pathlib import Path

from lemoncrow.core.capabilities.plugin_runtime import (
    _aggregate_lifetime_saved_usd,
    reconcile_local_savings_gap,
)


def _persisted_total(root: Path) -> float:
    # reconcile_savings_aggregate, not aggregate_window_savings: the latter
    # reads through an in-process TTL cache primed by an EARLIER call in this
    # same test process, which a write moments later wouldn't yet be visible
    # through. This always folds fresh from disk.
    from lemoncrow.core.capabilities.savings_summary import reconcile_savings_aggregate

    return _aggregate_lifetime_saved_usd(reconcile_savings_aggregate(root))


def _seed_local_savings(root: Path, *, usd: float, tokens: int = 1000) -> None:
    sidecar = root / "sessions" / "2026" / "01" / "01" / "claude" / "sid-real" / "savings.jsonl"
    sidecar.parent.mkdir(parents=True)
    row = {
        "tool": "code_search",
        "tokens": tokens,
        "calls": 3,
        "model": "claude-sonnet-5",
        "cost_saved_usd": usd,
        "ts": "2026-01-01T00:00:00.000000",
    }
    sidecar.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_writes_correction_row_when_server_is_ahead(tmp_path: Path) -> None:
    # Local ledger wiped entirely (the exact "someone deleted sessions/"
    # scenario) -- server still knows the true $16 total.
    assert reconcile_local_savings_gap(tmp_path, 16.0) is True
    assert _persisted_total(tmp_path) == 16.0


def test_noop_when_local_already_matches_or_leads(tmp_path: Path) -> None:
    # Real, fresh local usage the throttled report hasn't pushed up yet --
    # server is just stale here, not the local ledger being wrong. Must NOT
    # be dragged down to the server's lower, delayed figure.
    _seed_local_savings(tmp_path, usd=20.0)
    assert reconcile_local_savings_gap(tmp_path, 16.0) is False
    assert _persisted_total(tmp_path) == 20.0  # untouched


def test_noop_for_a_negligible_gap(tmp_path: Path) -> None:
    _seed_local_savings(tmp_path, usd=15.995)
    assert reconcile_local_savings_gap(tmp_path, 16.0) is False


def test_fills_only_the_shortfall_when_local_has_partial_data(tmp_path: Path) -> None:
    # Ledger wasn't fully wiped -- some real local rows survived ($5), server
    # says the true account total is $16. Correction should be exactly the
    # $11 gap, not a second, duplicate $16.
    _seed_local_savings(tmp_path, usd=5.0)
    assert reconcile_local_savings_gap(tmp_path, 16.0) is True
    assert _persisted_total(tmp_path) == 16.0


def test_repeated_calls_do_not_double_count(tmp_path: Path) -> None:
    # Fixed reconciliation slot, not an accumulating append: calling twice
    # for the same gap must not add the correction twice.
    assert reconcile_local_savings_gap(tmp_path, 16.0) is True
    assert reconcile_local_savings_gap(tmp_path, 16.0) is False  # gap now closed
    assert _persisted_total(tmp_path) == 16.0


def test_gap_closes_naturally_as_new_local_usage_lands(tmp_path: Path) -> None:
    # First check: full $16 gap (nothing local yet) -> corrected to $16.
    assert reconcile_local_savings_gap(tmp_path, 16.0) is True
    # Real new usage lands locally in the meantime, bringing local to $18 on
    # its own -- exactly matching where the server has since moved to.
    sidecar = tmp_path / "sessions" / "2026" / "06" / "01" / "claude" / "sid-new" / "savings.jsonl"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps(
            {
                "tool": "code_search",
                "tokens": 500,
                "calls": 1,
                "cost_saved_usd": 2.0,
                "ts": "2026-06-01T00:00:00.000000",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Server has since accumulated further too (18 total) -- but local is
    # ALREADY there via real usage, not a second correction, so this no-ops.
    assert reconcile_local_savings_gap(tmp_path, 18.0) is False
    assert _persisted_total(tmp_path) == 18.0
