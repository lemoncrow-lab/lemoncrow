"""Savings-cap metering: anonymous $50, every signed-in account uncapped."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr


@pytest.fixture(autouse=True)
def _local_meter_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setattr(licensing_gate, "_public_key_hex", lambda: "")


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("anonymous", pr.ANONYMOUS_SAVINGS_CAP_USD),
        ("LOCAL", pr.ANONYMOUS_SAVINGS_CAP_USD),
        ("free", None),
        ("FREE", None),
        ("lite", None),
        ("LITE", None),
        ("pro", None),
        ("enterprise", None),
        ("team", pr.ANONYMOUS_SAVINGS_CAP_USD),
    ],
)
def test_savings_cap_by_plan(plan: str, expected: float | None) -> None:
    assert pr._savings_cap_usd({"plan": plan}) == expected


def test_server_override_wins_and_malformed_values_use_access_fallback() -> None:
    assert pr._savings_cap_usd({"plan": "anonymous", "monthlySavingsCapInUsd": 55}) == 55.0
    assert pr._savings_cap_usd({"plan": "anonymous", "monthlySavingsCapInUsd": 0}) == pr.ANONYMOUS_SAVINGS_CAP_USD
    assert pr._savings_cap_usd({"plan": "anonymous", "monthlySavingsCapInUsd": "bad"}) == pr.ANONYMOUS_SAVINGS_CAP_USD
    assert pr._savings_cap_usd({"plan": "free", "monthlySavingsCapInUsd": "bad"}) is None


def _patch_window(monkeypatch: pytest.MonkeyPatch, *, saved: float, spend: float = 0.0) -> None:
    from lemoncrow.core.capabilities import savings_summary

    class _W:
        saved_usd = saved
        spend_usd = spend

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _W())


def test_anonymous_over_cap_flips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=55.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "LOCAL"})
    assert m["savingsOverCap"] is True
    assert m["savingsRemainingUsd"] == 0.0


def test_anonymous_under_cap_not_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=5.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "anonymous"})
    assert m["savingsOverCap"] is False
    assert m["savingsRemainingUsd"] == 45.0


@pytest.mark.parametrize("plan", ["free", "lite", "pro", "enterprise"])
def test_signed_in_plans_never_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, plan: str) -> None:
    _patch_window(monkeypatch, saved=9999.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": plan})
    assert m["monthlySavingsCapInUsd"] is None
    assert m["savingsOverCap"] is False


def test_cap_exhausted_reads_persisted_anonymous_meter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=55.0)
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "LOCAL"})
    pr.refresh_subscription_meter(tmp_path)
    assert pr.cap_exhausted(tmp_path) is True


def test_cap_exhausted_fail_open_on_missing(tmp_path: Path) -> None:
    assert pr.cap_exhausted(tmp_path) is False


def test_local_fallback_honors_anonymous_dormancy(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "LOCAL", "savingsOverCap": True})
    assert pr.cap_exhausted(tmp_path) is True


def test_server_meter_trusted_verbatim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=0.0)
    sub = {
        "plan": "anonymous",
        "savingsMeterSource": "server",
        "savingsOverCap": True,
        "monthlySavingsInUsd": 55.0,
    }
    m = pr.compute_usage_meter(tmp_path, subscription=sub)
    assert m["savingsOverCap"] is True
    assert m["monthlySavingsInUsd"] == 55.0


def test_server_meter_not_over_local_cannot_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=9999.0)
    sub = {"plan": "anonymous", "savingsMeterSource": "server", "savingsOverCap": False}
    m = pr.compute_usage_meter(tmp_path, subscription=sub)
    assert m["savingsOverCap"] is False


def test_local_meter_used_when_no_server_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=55.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "LOCAL"})
    assert m["savingsOverCap"] is True


def test_anonymous_cap_nudge_points_to_free_sign_in(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(
        subscription_state_path(tmp_path),
        {"plan": "LOCAL", "monthlySavingsCapInUsd": 50.0, "monthlySavingsInUsd": 55.0},
    )
    text = pr.cap_nudge_text(tmp_path)
    assert "lc account login" in text
    assert "uncapped Free" in text
    assert "upgrade" not in text.lower()
