"""Identity-bound cumulative usage reporting and retry behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.licensing import usage_report as ur


class _Win:
    def __init__(self, saved: float, spend: float = 0.0) -> None:
        self.saved_usd = saved
        self.spend_usd = spend


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None,
    saved: float,
    spend: float = 0.0,
) -> tuple[list[dict], object]:
    from lemoncrow.core.capabilities import savings_summary
    from lemoncrow.core.capabilities.licensing import store

    monkeypatch.setattr(store, "load_auth_token", lambda: token)
    monkeypatch.setattr(store, "load_auth_base", lambda: "https://api.test")
    monkeypatch.setattr(store, "load_or_create_device_id", lambda: "device-1")
    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win(saved, spend))
    posted: list[dict] = []

    def _post(url: str, payload: dict, tok: str) -> dict:
        posted.append({"url": url, "payload": payload, "token": tok})
        return {"capVerdictToken": "signed.cap.token"}

    return posted, _post


def test_synthetic_reconcile_backfill_rows_excluded_from_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Reconcile self-heal + `lc session backfill` rows (kind=backfill) correct
    # only the LOCAL display; reporting them as this device's cumulative would
    # make the server double-count them (runaway account inflation) and consume
    # the plan cap with never-delivered estimates.
    from lemoncrow.core.capabilities.licensing import store

    monkeypatch.setattr(store, "load_auth_token", lambda: "tok")
    monkeypatch.setattr(store, "load_auth_base", lambda: "https://api.test")
    monkeypatch.setattr(store, "load_or_create_device_id", lambda: "device-1")

    def _seed(rel: tuple[str, ...], row: dict) -> None:
        p = tmp_path.joinpath("sessions", *rel, "savings.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(row) + "\n", encoding="utf-8")

    _seed(
        ("2026", "01", "01", "claude", "real"),
        {"tool": "code_search", "tokens": 1000, "cost_saved_usd": 10.0, "ts": "2026-01-01T00:00:00"},
    )
    _seed(
        ("2026", "01", "02", "_reconcile", "ledger-gap"),
        {"tool": "reconcile", "kind": "backfill", "tokens": 1, "cost_saved_usd": 6.0, "ts": "2026-01-02T00:00:00"},
    )
    _seed(
        ("2026", "01", "03", "codex", "sid9"),
        {"tool": "code_search", "kind": "backfill", "tokens": 500, "cost_saved_usd": 4.0, "ts": "2026-01-03T00:00:00"},
    )

    posted: list[dict] = []

    def _post(url: str, payload: dict, tok: str) -> dict:
        posted.append(payload)
        return {"capVerdictToken": "signed.cap.token"}

    assert ur.report_usage_once(tmp_path, http_post=_post) is True  # type: ignore[arg-type]
    # Display total across all rows is $20; only the $10 of real measured
    # savings may reach the server.
    assert posted[-1]["cumulative_saved_usd"] == 10.0


def test_reports_cumulative_total_then_skips_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    assert ur.report_usage_once(tmp_path, http_post=post) is True  # type: ignore[arg-type]
    payload = posted[-1]["payload"]
    assert payload["cumulative_saved_usd"] == 25.0
    assert payload["cumulative_spend_usd"] == 0.0
    assert len(payload["report_id"]) == 64
    assert "delta_saved_usd" not in payload
    assert posted[-1]["token"] == "tok"
    assert posted[-1]["url"].endswith("/api/usage/report")

    assert ur.report_usage_once(tmp_path, http_post=post) is False  # type: ignore[arg-type]
    assert len(posted) == 1


def test_incremental_report_still_sends_monotonic_total(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posted, post = _patch(monkeypatch, token="tok", saved=10.0)
    assert ur.report_usage_once(tmp_path, http_post=post) is True  # type: ignore[arg-type]

    from lemoncrow.core.capabilities import savings_summary

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win(18.0))
    assert ur.report_usage_once(tmp_path, http_post=post) is True  # type: ignore[arg-type]
    assert posted[-1]["payload"]["cumulative_saved_usd"] == 18.0


def test_regressed_counter_is_reported_for_server_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    assert ur.report_usage_once(tmp_path, http_post=post) is True  # type: ignore[arg-type]

    from lemoncrow.core.capabilities import savings_summary

    monkeypatch.setattr(
        savings_summary,
        "aggregate_window_savings",
        lambda *a, **k: _Win(10.0),
    )
    assert ur.report_usage_once(tmp_path, http_post=post) is True  # type: ignore[arg-type]
    assert posted[-1]["payload"]["cumulative_saved_usd"] == 10.0


def test_deleted_local_watermark_replays_same_report_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    assert ur.report_usage_once(tmp_path, http_post=post) is True  # type: ignore[arg-type]
    first_id = posted[-1]["payload"]["report_id"]

    for path in (tmp_path / "usage_report_watermarks").glob("*"):
        path.unlink()

    assert ur.report_usage_once(tmp_path, http_post=post) is True  # type: ignore[arg-type]
    assert posted[-1]["payload"]["report_id"] == first_id


def test_watermarks_are_isolated_by_auth_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities import savings_summary
    from lemoncrow.core.capabilities.licensing import store

    token = {"value": "account-a-token"}
    monkeypatch.setattr(store, "load_auth_token", lambda: token["value"])
    monkeypatch.setattr(store, "load_auth_base", lambda: "https://api.test")
    monkeypatch.setattr(store, "load_or_create_device_id", lambda: "device-1")
    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win(10.0))
    seen: list[str] = []

    def _post(_url: str, payload: dict, _token: str) -> dict:
        seen.append(payload["report_id"])
        return {"capVerdictToken": "signed.cap.token"}

    assert ur.report_usage_once(tmp_path, http_post=_post) is True  # type: ignore[arg-type]
    token["value"] = "account-b-token"
    assert ur.report_usage_once(tmp_path, http_post=_post) is True  # type: ignore[arg-type]
    assert seen[0] != seen[1]
    assert len(list((tmp_path / "usage_report_watermarks").glob("*.json"))) == 2


def test_anonymous_bootstraps_at_zero_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch, token=None, saved=0.0)
    calls: list[tuple] = []

    def _post(url: str, payload: dict, tok: str) -> dict:
        calls.append((url, payload, tok))
        return {"capVerdictToken": "v.tok", "anonToken": "anon.signed.tok"}

    assert ur.report_usage_once(tmp_path, http_post=_post, now=1_000_000) is True  # type: ignore[arg-type]
    assert calls[-1][0].endswith("/api/usage/report-anon")
    assert calls[-1][2] == ""
    assert calls[-1][1]["anon_token"] == ""
    assert len(calls[-1][1]["machine_id"]) == 64
    assert (tmp_path / "cap_anon_token").read_text("utf-8") == "anon.signed.tok"
    assert ur.report_usage_once(tmp_path, http_post=_post, now=1_000_001) is False  # type: ignore[arg-type]


def test_anonymous_refreshes_verdict_without_new_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch, token=None, saved=0.0)
    calls: list[str] = []

    def _post(_url: str, payload: dict, _tok: str) -> dict:
        calls.append(payload["report_id"])
        return {"capVerdictToken": "v.tok", "anonToken": "anon.signed.tok"}

    start = 1_000_000
    assert ur.report_usage_once(tmp_path, http_post=_post, now=start) is True  # type: ignore[arg-type]
    assert (
        ur.report_usage_once(
            tmp_path,
            http_post=_post,
            now=start + ur.VERDICT_REFRESH_SECONDS + 1,
        )
        is True
    )  # type: ignore[arg-type]
    assert calls[0] == calls[1]


def test_authenticated_first_report_mints_at_zero_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A fresh pro login with no local savings yet MUST still mint a verdict:
    # the gate is fail-closed, so "no token" means dormant/empty tool list.
    # (This was the login-then-still-dormant bug: bootstrap was anon-only.)
    posted, post = _patch(monkeypatch, token="tok", saved=0.0)
    assert ur.report_usage_once(tmp_path, http_post=post, now=1_000_000) is True  # type: ignore[arg-type]
    assert posted[-1]["url"].endswith("/api/usage/report")


def test_authenticated_refreshes_verdict_without_new_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Verdict tokens expire in ~8h. An idle-but-under-cap authenticated device
    # must re-mint on the same 2h cadence as anonymous ones, or an overnight
    # pause turns a paying account dormant.
    posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    start = 1_000_000
    assert ur.report_usage_once(tmp_path, http_post=post, now=start) is True  # type: ignore[arg-type]
    assert ur.report_usage_once(tmp_path, http_post=post, now=start + 60) is False  # type: ignore[arg-type]
    assert (
        ur.report_usage_once(
            tmp_path,
            http_post=post,  # type: ignore[arg-type]
            now=start + ur.VERDICT_REFRESH_SECONDS + 1,
        )
        is True
    )
    assert len(posted) == 2


def test_force_mints_even_when_nothing_changed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Identity transitions (login/logout) and the MCP server's dormant
    # self-heal pass force=True: unchanged totals must not skip the mint.
    posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    assert ur.report_usage_once(tmp_path, http_post=post, now=1_000_000) is True  # type: ignore[arg-type]
    assert ur.report_usage_once(tmp_path, http_post=post, now=1_000_001) is False  # type: ignore[arg-type]
    assert ur.report_usage_once(tmp_path, http_post=post, now=1_000_002, force=True) is True  # type: ignore[arg-type]
    assert len(posted) == 2


def test_anonymous_presents_cached_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch, token=None, saved=25.0)
    (tmp_path / "cap_anon_token").write_text("cached.anon.tok", encoding="utf-8")
    seen: list[dict] = []

    def _post(_url: str, payload: dict, _tok: str) -> dict:
        seen.append(payload)
        return {"capVerdictToken": "v2.tok"}

    assert ur.report_usage_once(tmp_path, http_post=_post) is True  # type: ignore[arg-type]
    assert seen[-1]["anon_token"] == "cached.anon.tok"
    assert seen[-1]["cumulative_saved_usd"] == 25.0


def test_missing_signed_verdict_retries_without_advancing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posted, _post = _patch(monkeypatch, token="tok", saved=25.0)

    def invalid(url: str, payload: dict, token: str) -> dict:
        posted.append({"url": url, "payload": payload, "token": token})
        return {}

    assert ur.report_usage_once(tmp_path, http_post=invalid) is False  # type: ignore[arg-type]
    assert ur.report_usage_once(tmp_path, http_post=invalid) is False  # type: ignore[arg-type]
    assert len(posted) == 2


def test_anonymous_offline_returns_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch, token=None, saved=0.0)
    assert ur.report_usage_once(tmp_path, http_post=lambda *a: None) is False  # type: ignore[arg-type]
    assert not (tmp_path / "cap_anon_token").exists()


def test_anonymous_persists_registered_at_and_cycle_resets_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The report-anon response's display-only unix-seconds fields land as ISO
    strings in the local subscription cache (see plugin_runtime.persist_registered_at
    / persist_cycle_resets_at) -- what `lc account cap` reads for its "since" /
    "resets" lines.
    """
    from lemoncrow.core.capabilities import plugin_runtime as pr

    _patch(monkeypatch, token=None, saved=40.0)

    def _post(_url: str, _payload: dict, _tok: str) -> dict:
        return {
            "capVerdictToken": "v.tok",
            "anonToken": "anon.signed.tok",
            "deviceRegisteredAt": 1_700_000_000,
            "cycleResetsAt": 1_702_592_000,
        }

    assert ur.report_usage_once(tmp_path, http_post=_post) is True  # type: ignore[arg-type]
    sub = json.loads(pr.subscription_state_path(tmp_path).read_text("utf-8"))
    assert sub["registeredAt"] == "2023-11-14T22:13:20Z"
    assert sub["cycleResetsAt"] == "2023-12-14T22:13:20Z"


def test_anonymous_ignores_missing_display_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities import plugin_runtime as pr

    _patch(monkeypatch, token=None, saved=0.0)

    def _post(_url: str, _payload: dict, _tok: str) -> dict:
        return {"capVerdictToken": "v.tok", "anonToken": "anon.signed.tok"}

    assert ur.report_usage_once(tmp_path, http_post=_post) is True  # type: ignore[arg-type]
    sub = json.loads(pr.subscription_state_path(tmp_path).read_text("utf-8"))
    assert "registeredAt" not in sub
    assert "cycleResetsAt" not in sub


def test_throttle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    assert ur.maybe_report_usage(tmp_path, http_post=post, now=1_000_000) is True  # type: ignore[arg-type]
    assert ur.maybe_report_usage(tmp_path, http_post=post, now=1_000_060) is False  # type: ignore[arg-type]

    monkeypatch.setattr(
        __import__("lemoncrow.core.capabilities.savings_summary", fromlist=["x"]),
        "aggregate_window_savings",
        lambda *a, **k: _Win(40.0),
    )
    assert (
        ur.maybe_report_usage(
            tmp_path,
            http_post=post,  # type: ignore[arg-type]
            now=1_000_000 + ur.REPORT_INTERVAL_SECONDS + 1,
        )
        is True
    )
