"""Cap nudge (once/session, user-only) + codex dormant policy neutralization."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime as pr


def _patch_saved(monkeypatch: pytest.MonkeyPatch, saved: float) -> None:
    from lemoncrow.core.capabilities import savings_summary

    class _W:
        saved_usd = saved
        spend_usd = 0.0

    monkeypatch.setattr(savings_summary, "aggregate_window_savings", lambda *a, **k: _W())


def test_cap_nudge_fires_once_then_rate_limited(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 25.0)
    pr._write_json(pr.subscription_state_path(tmp_path), {"plan": "free"})
    pr.refresh_subscription_meter(tmp_path)
    first = pr.build_cap_nudge(tmp_path, session_id="s1", host="claude")
    assert first is not None and "cap reached" in first.lower()
    assert pr.build_cap_nudge(tmp_path, session_id="s1", host="claude") is None  # rate-limited
    # a different session nudges once too
    assert pr.build_cap_nudge(tmp_path, session_id="s2", host="claude") is not None


def test_cap_nudge_silent_when_under_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 3.0)
    pr._write_json(pr.subscription_state_path(tmp_path), {"plan": "free"})
    pr.refresh_subscription_meter(tmp_path)
    assert pr.build_cap_nudge(tmp_path, session_id="s1", host="claude") is None


def test_codex_policy_neutral_when_dormant() -> None:
    active = pr._codex_session_start_tool_policy(dormant=False)
    dormant = pr._codex_session_start_tool_policy(dormant=True)
    assert "additionalContext" in active
    assert "additionalContext" not in dormant  # no steering when dormant


def test_codex_update_notification_reports_dormant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_saved(monkeypatch, 25.0)
    pr._write_json(pr.subscription_state_path(tmp_path), {"plan": "free"})
    out = pr.codex_update_notification(tmp_path, current_version="1.0.0")
    assert out["dormant"] is True
