"""Client persistence of the server's signed cap-verdict token.

The compiled gate reads ``capVerdictToken`` from ``auth.json`` /
``subscription.json``; these tests cover the two writers that deliver it there
(``/api/auth/me`` via entitlements, and ``/api/usage/report`` via the reporter),
plus the round-trip back into the gate's reader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr


def test_persist_writes_into_auth_subscription_status(tmp_path: Path) -> None:
    auth = {"authenticated": True, "subscriptionStatus": {"plan": "PRO"}}
    pr._write_json(pr.auth_state_path(tmp_path), auth, mode=0o600)
    pr.persist_cap_verdict_token(tmp_path, "sig.tok")
    saved = json.loads(pr.auth_state_path(tmp_path).read_text("utf-8"))
    assert saved["subscriptionStatus"]["capVerdictToken"] == "sig.tok"
    # existing plan blob is preserved, not clobbered.
    assert saved["subscriptionStatus"]["plan"] == "PRO"


def test_persist_falls_back_to_subscription_json_without_auth(tmp_path: Path) -> None:
    pr.persist_cap_verdict_token(tmp_path, "sig.tok")
    assert not pr.auth_state_path(tmp_path).exists()
    sub = json.loads(pr.subscription_state_path(tmp_path).read_text("utf-8"))
    assert sub["capVerdictToken"] == "sig.tok"


def test_persist_ignores_empty_token(tmp_path: Path) -> None:
    pr.persist_cap_verdict_token(tmp_path, None)
    pr.persist_cap_verdict_token(tmp_path, "")
    assert not pr.auth_state_path(tmp_path).exists()
    assert not pr.subscription_state_path(tmp_path).exists()


def test_persist_is_idempotent_no_rewrite(tmp_path: Path) -> None:
    auth = {"authenticated": True, "subscriptionStatus": {"plan": "PRO"}}
    pr._write_json(pr.auth_state_path(tmp_path), auth, mode=0o600)
    pr.persist_cap_verdict_token(tmp_path, "tok1")
    mtime = pr.auth_state_path(tmp_path).stat().st_mtime_ns
    pr.persist_cap_verdict_token(tmp_path, "tok1")  # unchanged -> skip write
    assert pr.auth_state_path(tmp_path).stat().st_mtime_ns == mtime


def test_gate_reader_finds_persisted_token(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.licensing_gate import _cap_verdict_token

    auth = {"authenticated": True, "subscriptionStatus": {"plan": "PRO"}}
    pr._write_json(pr.auth_state_path(tmp_path), auth, mode=0o600)
    pr.persist_cap_verdict_token(tmp_path, "abc.def")
    assert _cap_verdict_token(tmp_path) == "abc.def"


def test_usage_report_persists_token_from_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities import savings_summary
    from lemoncrow.core.capabilities.licensing import store
    from lemoncrow.core.capabilities.licensing import usage_report as ur

    class _Win:
        saved_usd = 25.0
        spend_usd = 0.0

    monkeypatch.setattr(store, "load_auth_token", lambda: "tok")
    monkeypatch.setattr(store, "load_auth_base", lambda: "https://api.test")
    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win())

    def _post(url: str, payload: dict, tok: str) -> dict:
        return {"capVerdictToken": "server.signed.tok", "savingsOverCap": True}

    assert ur.report_usage_once(tmp_path, http_post=_post) is True  # type: ignore[arg-type]
    sub = json.loads(pr.subscription_state_path(tmp_path).read_text("utf-8"))
    assert sub["capVerdictToken"] == "server.signed.tok"


def test_usage_report_without_verdict_does_not_advance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A 2xx without the required signed verdict is incomplete and must retry.
    from lemoncrow.core.capabilities import savings_summary
    from lemoncrow.core.capabilities.licensing import store
    from lemoncrow.core.capabilities.licensing import usage_report as ur

    class _Win:
        saved_usd = 25.0
        spend_usd = 0.0

    monkeypatch.setattr(store, "load_auth_token", lambda: "tok")
    monkeypatch.setattr(store, "load_auth_base", lambda: "https://api.test")
    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _Win())

    assert ur.report_usage_once(tmp_path, http_post=lambda *a: {}) is False  # type: ignore[arg-type]
    assert not pr.subscription_state_path(tmp_path).exists()
    # The watermark did not advance, so the same cumulative report retries.
    assert ur.report_usage_once(tmp_path, http_post=lambda *a: {}) is False  # type: ignore[arg-type]


def test_entitlements_persists_token_from_me(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.licensing import entitlements
    from lemoncrow.core.foundation import paths

    monkeypatch.setattr(paths, "default_store_root", lambda: tmp_path)
    entitlements._persist_cap_verdict_token({"plan": "pro", "capVerdictToken": "me.tok"})
    sub = json.loads(pr.subscription_state_path(tmp_path).read_text("utf-8"))
    assert sub["capVerdictToken"] == "me.tok"


def test_entitlements_persists_token_nested_in_subscription_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lemoncrow.core.capabilities.licensing import entitlements
    from lemoncrow.core.foundation import paths

    monkeypatch.setattr(paths, "default_store_root", lambda: tmp_path)
    entitlements._persist_cap_verdict_token({"plan": "pro", "subscriptionStatus": {"capVerdictToken": "nested.tok"}})
    sub = json.loads(pr.subscription_state_path(tmp_path).read_text("utf-8"))
    assert sub["capVerdictToken"] == "nested.tok"
