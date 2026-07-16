"""Unit tests for the local billing usage meter.

The meter (:func:`compute_usage_meter` / :func:`refresh_subscription_meter` in
``plugin_runtime``) prices trailing-window spend from the canonical per-session
savings ledger and compares it to the plan's ``monthlyLimitInUsd``. It is
non-blocking: it only annotates the subscription and produces ``subscription.json``
for the statusline warning surface (``_resolve_status_text``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr
from lemoncrow.core.capabilities.savings_summary import _find_savings_sidecar, _resolve_status_text


@pytest.fixture()
def lemoncrow_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("lemoncrow.core.capabilities.licensing.entitlements.auth_user", lambda: None)
    root = tmp_path / ".lemoncrow"
    root.mkdir()
    return root


def _seed_ledger(root: Path, session_id: str, *, est_cost: float, saved: float) -> None:
    """Write a per-session savings.jsonl the windowed aggregator understands.

    A ``session_end`` row supplies the realized spend (``est_cost_usd``); a
    normal row supplies pre-priced context savings (``cost_saved_usd``).
    """
    sidecar = _find_savings_sidecar(session_id, root)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts": pr._iso_now(),
            "session_id": session_id,
            "tokens": 1000,
            "calls": 1,
            "cost_saved_usd": saved,
            "model": "claude-sonnet-4-5",
        },
        {"ts": pr._iso_now(), "session_id": session_id, "kind": "session_end", "est_cost_usd": est_cost},
    ]
    sidecar.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _stamp_hosted_plan(root: Path, *, plan: str = "PRO") -> None:
    """Re-plan the stamped auth.json: positive limits only apply to hosted/licensed plans, never LOCAL."""
    path = pr.auth_state_path(root)
    auth = json.loads(path.read_text(encoding="utf-8"))
    auth["subscriptionStatus"]["plan"] = plan
    path.write_text(json.dumps(auth), encoding="utf-8")


def test_no_limit_reports_spend_but_never_warns(lemoncrow_root: Path) -> None:
    _seed_ledger(lemoncrow_root, "s", est_cost=999.0, saved=1.0)
    sub = pr.compute_usage_meter(lemoncrow_root, subscription={"monthlyLimitInUsd": 0.0})
    assert sub["monthlySpendInUsd"] == pytest.approx(999.0)
    assert sub["monthlySavingsInUsd"] == pytest.approx(1.0)
    # No limit configured -> reported but never blocking/warning.
    assert sub["warning"] is False
    assert sub["overLimit"] is False
    assert sub["remainingUsd"] is None
    assert sub["usageFraction"] == 0.0


def test_below_warn_threshold(lemoncrow_root: Path) -> None:
    _seed_ledger(lemoncrow_root, "s", est_cost=2.0, saved=0.9)
    sub = pr.compute_usage_meter(lemoncrow_root, subscription={"monthlyLimitInUsd": 5.0})
    assert sub["monthlySpendInUsd"] == pytest.approx(2.0)
    assert sub["usageFraction"] == pytest.approx(0.4)
    assert sub["remainingUsd"] == pytest.approx(3.0)
    assert sub["warning"] is False
    assert sub["overLimit"] is False


def test_warning_band(lemoncrow_root: Path) -> None:
    # 84% of the $5 limit -> at/over the 80% warn fraction, not yet over.
    _seed_ledger(lemoncrow_root, "s", est_cost=4.2, saved=1.0)
    sub = pr.compute_usage_meter(lemoncrow_root, subscription={"monthlyLimitInUsd": 5.0})
    assert sub["warning"] is True
    assert sub["overLimit"] is False
    assert "Approaching" in sub["message"]


def test_over_limit(lemoncrow_root: Path) -> None:
    _seed_ledger(lemoncrow_root, "s", est_cost=6.0, saved=2.0)
    sub = pr.compute_usage_meter(lemoncrow_root, subscription={"monthlyLimitInUsd": 5.0})
    assert sub["overLimit"] is True
    assert sub["warning"] is True
    assert sub["remainingUsd"] == 0.0
    assert sub["usageFraction"] == pytest.approx(1.2)
    assert "reached" in sub["message"]


def test_warn_boundary_is_inclusive(lemoncrow_root: Path) -> None:
    # Exactly 80% must warn (>= comparison), exactly 100% must be over.
    _seed_ledger(lemoncrow_root, "s", est_cost=4.0, saved=0.0)
    sub = pr.compute_usage_meter(lemoncrow_root, subscription={"monthlyLimitInUsd": 5.0})
    assert sub["warning"] is True and sub["overLimit"] is False


def test_default_trial_has_no_limit_and_never_warns(lemoncrow_root: Path) -> None:
    # The open-source free core stamps no cap: the default trial has
    # monthlyLimitInUsd == 0.0 and the meter is report-only (never warns).
    _seed_ledger(lemoncrow_root, "s", est_cost=999.0, saved=1.0)
    pr.claim_anonymous_trial(lemoncrow_root)
    pr.refresh_subscription_meter(lemoncrow_root)
    persisted = json.loads((lemoncrow_root / "subscription.json").read_text())
    assert persisted["monthlyLimitInUsd"] == 0.0
    assert persisted["warning"] is False
    assert persisted["overLimit"] is False
    assert persisted["remainingUsd"] is None


def test_legacy_trial_cap_is_ignored(lemoncrow_root: Path) -> None:
    # Old builds stamped the anonymous trial with a $5 cap ("Local anonymous
    # trial active."); the free core is uncapped, so the meter must neutralize
    # the persisted limit and stay report-only however large the spend.
    _seed_ledger(lemoncrow_root, "s", est_cost=999.0, saved=1.0)
    pr.write_auth_state(
        lemoncrow_root,
        {
            "accessToken": "local-anonymous-legacy",
            "userId": "anon-legacy",
            "isAnonymous": True,
            "subscriptionStatus": {
                "isValid": True,
                "status": "FREE",
                "plan": "LOCAL",
                "monthlySavingsInUsd": 0.0,
                "monthlyLimitInUsd": 5.0,
                "message": "Local anonymous trial active.",
            },
        },
    )
    sub = pr.compute_usage_meter(lemoncrow_root)
    assert sub["monthlyLimitInUsd"] == 0.0
    assert sub["warning"] is False
    assert sub["overLimit"] is False
    assert sub["remainingUsd"] is None

    pr.refresh_subscription_meter(lemoncrow_root)
    persisted = json.loads((lemoncrow_root / "subscription.json").read_text())
    assert persisted["monthlyLimitInUsd"] == 0.0
    assert persisted["warning"] is False


def test_cumulative_session_end_snapshots_count_once(lemoncrow_root: Path) -> None:
    # The stop hook appends one session_end row per Stop fire, each carrying the
    # session's cumulative cost; only the last snapshot is the session's total.
    sidecar = _find_savings_sidecar("s", lemoncrow_root)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": pr._iso_now(), "session_id": "s", "kind": "session_end", "est_cost_usd": cost}
        for cost in (10.0, 20.0, 30.0)
    ]
    sidecar.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    sub = pr.compute_usage_meter(lemoncrow_root, subscription={"monthlyLimitInUsd": 0.0})
    assert sub["monthlySpendInUsd"] == pytest.approx(30.0)


def test_refresh_persists_and_statusline_surfaces_warning(lemoncrow_root: Path) -> None:
    _seed_ledger(lemoncrow_root, "s", est_cost=6.0, saved=2.0)
    pr.claim_anonymous_trial(lemoncrow_root, monthly_limit_usd=5.0)
    _stamp_hosted_plan(lemoncrow_root)
    metered = pr.refresh_subscription_meter(lemoncrow_root)
    assert metered["overLimit"] is True

    # subscription.json is the file the statusline reads.
    persisted = json.loads((lemoncrow_root / "subscription.json").read_text())
    assert persisted["warning"] is True
    assert "reached" in persisted["message"]

    # The existing statusline consumer surfaces the plan message (auth present,
    # so it does not short-circuit to the 'login' tip).
    status_text = _resolve_status_text(lemoncrow_root)
    assert "Monthly limit reached" in status_text


def test_auth_status_enrichment_is_additive_and_live(lemoncrow_root: Path) -> None:
    _seed_ledger(lemoncrow_root, "s", est_cost=6.0, saved=2.0)
    pr.claim_anonymous_trial(lemoncrow_root, monthly_limit_usd=5.0)
    _stamp_hosted_plan(lemoncrow_root)
    status = pr.auth_status(lemoncrow_root)
    sub = status["subscription"]
    # Original plan keys preserved...
    assert sub["status"] == "FREE" and sub["plan"] == "PRO"
    # ...and live meter fields added (monthlySavingsInUsd no longer hardcoded 0).
    assert sub["overLimit"] is True
    assert sub["monthlySavingsInUsd"] == pytest.approx(2.0)


def test_oauth_subscription_overrides_stale_local_trial(lemoncrow_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pr.claim_anonymous_trial(lemoncrow_root)
    stale = pr.compute_usage_meter(lemoncrow_root)
    pr._write_json(pr.subscription_state_path(lemoncrow_root), stale)
    assert stale["plan"] == "LOCAL"
    assert stale["monthlySavingsCapInUsd"] == pr.ANONYMOUS_SAVINGS_CAP_USD

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.licensing.entitlements.auth_user",
        lambda: {
            "plan": "pro",
            "subscriptionStatus": {
                "plan": "pro",
                "monthlySavingsCapInUsd": None,
                "monthlySavingsInUsd": 42.0,
                "savingsMeterSource": "server",
                "savingsOverCap": False,
            },
        },
    )

    resolved = pr.resolve_subscription(lemoncrow_root)
    assert resolved["plan"] == "pro"
    assert resolved["monthlySavingsCapInUsd"] is None

    report = pr.build_savings_report(lemoncrow_root)
    assert report["subscription"]["plan"] == "pro"
    assert report["subscription"]["monthlySavingsCapInUsd"] is None


def test_stop_event_refreshes_meter(lemoncrow_root: Path) -> None:
    _seed_ledger(lemoncrow_root, "s", est_cost=6.0, saved=2.0)
    pr.claim_anonymous_trial(lemoncrow_root, monthly_limit_usd=5.0)
    _stamp_hosted_plan(lemoncrow_root)
    # No subscription.json yet (claim writes only auth.json).
    assert not (lemoncrow_root / "subscription.json").exists()
    pr.update_session_stats(lemoncrow_root, {"hook_event_name": "Stop", "session_id": "s"})
    persisted = json.loads((lemoncrow_root / "subscription.json").read_text())
    assert persisted["overLimit"] is True
