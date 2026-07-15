"""Identity-bound cumulative usage reporting and retry behavior."""

from __future__ import annotations

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
