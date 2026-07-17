"""Open-source runtime: there is no savings cap and no dormancy.

The former signed Ed25519 cap-verdict enforcement was removed (see
docs/maintenance-mode-transition.md). ``resolve_cap_verdict`` now always reports
an active, uncapped, un-gated runtime with no account, no token, and no network.
These tests pin that contract so it cannot silently regress into gating again.
"""

from __future__ import annotations

from pathlib import Path

import lemoncrow.pro.capabilities.licensing_gate as _gate
from lemoncrow.core.capabilities.plugin_runtime import cap_exhausted as _pr_cap_exhausted


def test_gate_is_never_configured() -> None:
    # No signing authority is pinned: nothing can ever be enforced.
    assert _gate.is_configured() is False


def test_resolve_cap_verdict_is_always_active(tmp_path: Path) -> None:
    verdict = _gate.resolve_cap_verdict(tmp_path)
    assert verdict.dormant is False
    assert verdict.verified is True
    assert verdict.plan == "free"
    assert verdict.reason == "oss"


def test_cap_exhausted_is_always_false(tmp_path: Path) -> None:
    assert _gate.cap_exhausted(tmp_path) is False


def test_plugin_runtime_cap_exhausted_is_always_false(tmp_path: Path) -> None:
    assert _pr_cap_exhausted(tmp_path) is False


def test_no_token_and_offline_never_makes_it_dormant(tmp_path: Path, monkeypatch) -> None:
    # Even with a bogus/empty auth base and no token file anywhere, the runtime
    # stays active — no fail-closed behavior, no network dependency.
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)
    verdict = _gate.resolve_cap_verdict(tmp_path)
    assert verdict.dormant is False
    assert _gate.cap_exhausted(tmp_path) is False
