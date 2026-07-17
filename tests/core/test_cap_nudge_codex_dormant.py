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


def test_cap_nudge_never_fires_now(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The OSS runtime is never dormant (cap_exhausted is always False), so the
    # one-shot cap nudge never fires — not on the first call, not for any session,
    # and not even when a legacy over-cap flag is left on disk.
    _patch_saved(monkeypatch, 25.0)
    pr._write_json(pr.subscription_state_path(tmp_path), {"plan": "free", "savingsOverCap": True})
    pr.refresh_subscription_meter(tmp_path)
    assert pr.build_cap_nudge(tmp_path, session_id="s1", host="claude") is None
    assert pr.build_cap_nudge(tmp_path, session_id="s1", host="claude") is None
    # a different session is silent too
    assert pr.build_cap_nudge(tmp_path, session_id="s2", host="claude") is None


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


def test_codex_update_notification_reports_not_dormant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Nothing is ever dormant now, so the codex notification always reports active.
    _patch_saved(monkeypatch, 25.0)
    pr._write_json(pr.subscription_state_path(tmp_path), {"plan": "free"})
    out = pr.codex_update_notification(tmp_path, current_version="1.0.0")
    assert out["dormant"] is False
