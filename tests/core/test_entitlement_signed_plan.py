"""Open-source runtime: entitlements are local and every feature is unlocked.

The former signed-plan verification against a hosted account server was removed
(see docs/maintenance-mode-transition.md). ``is_pro`` / ``has_feature`` /
``require`` now resolve locally to "granted", with no token, no account, and no
network call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities import licensing
from lemoncrow.core.capabilities.licensing import entitlements
from lemoncrow.core.capabilities.licensing.features import FREE_FEATURES, PRO_FEATURES


def test_is_pro_is_always_true() -> None:
    assert licensing.is_pro() is True


def test_every_registered_feature_is_unlocked() -> None:
    for feature in set(FREE_FEATURES) | set(PRO_FEATURES):
        assert licensing.has_feature(feature) is True


def test_require_never_raises() -> None:
    licensing.require("optimizer")
    licensing.require("governance")
    licensing.require("unknown_feature_name")


def test_status_reports_local_unlocked() -> None:
    status = licensing.status()
    assert status.licensed is True
    assert status.valid is True
    assert status.plan == "oss"


def test_auth_user_is_offline_and_optional(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No account linked -> None, and never a network fetch.
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_AUTH_TOKEN", raising=False)
    assert entitlements.auth_user() is None
    assert entitlements.current_identity() is None
