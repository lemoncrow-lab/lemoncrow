"""Savings-cap metering: the OSS runtime is uncapped for every plan, always."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr


@pytest.fixture(autouse=True)
def _local_meter_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities import licensing_gate

    monkeypatch.setattr(licensing_gate, "_public_key_hex", lambda: "")


@pytest.mark.parametrize(
    "plan",
    ["anonymous", "LOCAL", "free", "FREE", "lite", "LITE", "pro", "enterprise", "team"],
)
def test_savings_cap_is_none_for_every_plan(plan: str) -> None:
    # There is no savings cap in the OSS runtime — every plan (including the
    # former anonymous $100 tier) is uncapped.
    assert pr._savings_cap_usd({"plan": plan}) is None


def test_server_override_never_reintroduces_a_cap() -> None:
    # Even a server-supplied override or malformed value can never produce a cap.
    assert pr._savings_cap_usd({"plan": "anonymous", "monthlySavingsCapInUsd": 55}) is None
    assert pr._savings_cap_usd({"plan": "anonymous", "monthlySavingsCapInUsd": 0}) is None
    assert pr._savings_cap_usd({"plan": "anonymous", "monthlySavingsCapInUsd": "bad"}) is None
    assert pr._savings_cap_usd({"plan": "free", "monthlySavingsCapInUsd": "bad"}) is None


def _patch_window(monkeypatch: pytest.MonkeyPatch, *, saved: float, spend: float = 0.0) -> None:
    from lemoncrow.core.capabilities import savings_summary

    class _W:
        saved_usd = saved
        spend_usd = spend

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _W())


def test_anonymous_never_flips_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Even a huge trailing-window savings total never trips an over-cap flag now.
    _patch_window(monkeypatch, saved=105.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "LOCAL"})
    assert m["savingsOverCap"] is False
    assert m["savingsRemainingUsd"] is None
    assert m["monthlySavingsCapInUsd"] is None


def test_anonymous_under_cap_not_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_window(monkeypatch, saved=5.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "anonymous"})
    assert m["savingsOverCap"] is False
    assert m["savingsRemainingUsd"] is None  # uncapped -> no remaining figure
    assert m["monthlySavingsCapInUsd"] is None


@pytest.mark.parametrize("plan", ["free", "lite", "pro", "enterprise"])
def test_signed_in_plans_never_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, plan: str) -> None:
    _patch_window(monkeypatch, saved=9999.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": plan})
    assert m["monthlySavingsCapInUsd"] is None
    assert m["savingsOverCap"] is False


def test_cap_exhausted_is_false_even_with_persisted_anonymous_meter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_window(monkeypatch, saved=105.0)
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "LOCAL"})
    pr.refresh_subscription_meter(tmp_path)
    assert pr.cap_exhausted(tmp_path) is False


def test_cap_exhausted_fail_open_on_missing(tmp_path: Path) -> None:
    assert pr.cap_exhausted(tmp_path) is False


def test_legacy_persisted_over_cap_never_makes_it_dormant(tmp_path: Path) -> None:
    # A leftover savingsOverCap flag on disk cannot make the runtime dormant.
    from lemoncrow.core.capabilities.plugin_runtime import _write_json, subscription_state_path

    _write_json(subscription_state_path(tmp_path), {"plan": "LOCAL", "savingsOverCap": True})
    assert pr.cap_exhausted(tmp_path) is False


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


def test_local_meter_never_flags_over_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # With no server source and a large local total, the meter still never flags
    # over-cap — the runtime is uncapped.
    _patch_window(monkeypatch, saved=105.0)
    m = pr.compute_usage_meter(tmp_path, subscription={"plan": "LOCAL"})
    assert m["savingsOverCap"] is False


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
