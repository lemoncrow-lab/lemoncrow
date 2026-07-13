"""Savings-cap metering + dormant cap-state (Free $20 / Lite $200 / Pro uncapped)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("free", pr.FREE_SAVINGS_CAP_USD),
        ("LOCAL", pr.FREE_SAVINGS_CAP_USD),
        ("FREE", pr.FREE_SAVINGS_CAP_USD),
        ("lite", pr.LITE_SAVINGS_CAP_USD),
        ("LITE", pr.LITE_SAVINGS_CAP_USD),
        ("pro", None),
        ("enterprise", None),
        ("team", None),  # unknown paid key -> uncapped (fail-open)
    ],
)
def test_savings_cap_by_plan(plan: str, expected: float | None) -> None:
    assert pr._savings_cap_usd({"plan": plan}) == expected


def test_server_override_wins_and_zero_means_uncapped() -> None:
    assert pr._savings_cap_usd({"plan": "free", "monthlySavingsCapInUsd": 55}) == 55.0
    assert pr._savings_cap_usd({"plan": "free", "monthlySavingsCapInUsd": 0}) is None
    assert pr._savings_cap_usd({"plan": "free", "monthlySavingsCapInUsd": "bad"}) is None


def _patch_window(monkeypatch: pytest.MonkeyPatch, *, saved: float, spend: float = 0.0) -> None:
    from lemoncrow.core.capabilities import savings_summary

    class _W:
        saved_usd = saved
        spend_usd = spend

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _W())


def test_free_over_cap_flips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=25.0)  # > $20 free cap
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "free"})
    assert m["savingsOverCap"] is True
    assert m["savingsRemainingUsd"] == 0.0


def test_free_under_cap_not_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=5.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "free"})
    assert m["savingsOverCap"] is False
    assert m["savingsRemainingUsd"] == 15.0


def test_pro_never_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=9999.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "pro"})
    assert m["monthlySavingsCapInUsd"] is None
    assert m["savingsOverCap"] is False


def test_cap_exhausted_reads_persisted_meter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=25.0)
    # No subscription arg -> reads auth/subscription; seed a free plan blob.
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "free"})
    pr.refresh_subscription_meter(tmp_path)
    assert pr.cap_exhausted(tmp_path) is True


def test_cap_exhausted_fail_open_on_missing(tmp_path: Path) -> None:
    assert pr.cap_exhausted(tmp_path) is False
