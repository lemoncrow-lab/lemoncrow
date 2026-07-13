"""Client usage reporter: watermark delta, throttle, no-token, server-priced payload."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities.licensing import usage_report as ur


class _Win:
    def __init__(self, saved: float, spend: float = 0.0) -> None:
        self.saved_usd = saved
        self.spend_usd = spend


def _patch(monkeypatch: pytest.MonkeyPatch, *, token: str | None, saved: float, spend: float = 0.0) -> list[dict]:
    from lemoncrow.core.capabilities import savings_summary
    from lemoncrow.core.capabilities.licensing import store

    monkeypatch.setattr(store, "load_auth_token", lambda: token)
    monkeypatch.setattr(store, "load_auth_base", lambda: "https://api.test")
    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win(saved, spend))
    posted: list[dict] = []

    def _post(url: str, payload: dict, tok: str) -> bool:
        posted.append({"url": url, "payload": payload, "token": tok})
        return True

    return posted, _post  # type: ignore[return-value]


def test_reports_delta_then_no_new_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    assert ur.report_usage_once(tmp_path, http_post=post) is True
    assert posted[-1]["payload"]["delta_saved_usd"] == 25.0
    assert posted[-1]["payload"]["window_saved_usd"] == 25.0
    assert posted[-1]["token"] == "tok"
    assert posted[-1]["url"].endswith("/api/usage/report")
    # same totals -> no new delta -> no post
    assert ur.report_usage_once(tmp_path, http_post=post) is False
    assert len(posted) == 1


def test_incremental_delta(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posted, post = _patch(monkeypatch, token="tok", saved=10.0)
    ur.report_usage_once(tmp_path, http_post=post)
    # window grows to 18 -> delta 8
    from lemoncrow.core.capabilities import savings_summary

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win(18.0))
    assert ur.report_usage_once(tmp_path, http_post=post) is True
    assert posted[-1]["payload"]["delta_saved_usd"] == 8.0


def test_no_token_no_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _posted, post = _patch(monkeypatch, token=None, saved=25.0)
    assert ur.report_usage_once(tmp_path, http_post=post) is False


def test_throttle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _posted, post = _patch(monkeypatch, token="tok", saved=25.0)
    assert ur.maybe_report_usage(tmp_path, http_post=post, now=1_000_000) is True
    # within 30 min -> throttled
    assert ur.maybe_report_usage(tmp_path, http_post=post, now=1_000_000 + 60) is False
    # after 30 min + new data -> reports
    monkeypatch.setattr(
        __import__("lemoncrow.core.capabilities.savings_summary", fromlist=["x"]),
        "aggregate_window_savings",
        lambda *a, **k: _Win(40.0),
    )
    assert ur.maybe_report_usage(tmp_path, http_post=post, now=1_000_000 + ur.REPORT_INTERVAL_SECONDS + 1) is True
