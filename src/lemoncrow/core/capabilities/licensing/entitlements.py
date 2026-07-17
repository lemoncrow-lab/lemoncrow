"""Entitlement contract — open-source edition: everything is unlocked, locally.

LemonCrow's open-source runtime ships every capability with no paid tiers, no
account requirement, and no network entitlement check. This module is the single
chokepoint every former ``require(feature)`` / ``is_pro()`` call routes through;
it now resolves locally to "all features granted" and never contacts a server.

The optional hosted account (``lc account login``) is fully decoupled: it may
link an account for a hosted service, but it grants nothing here and is never
required. ``auth_user()`` reads only the local on-disk cache — it never fetches
over the network. See docs/maintenance-mode-transition.md.
"""

from __future__ import annotations

from lemoncrow.core.capabilities.licensing import store
from lemoncrow.core.capabilities.licensing.features import (
    FREE_FEATURES,
    PRO_FEATURES,
)
from lemoncrow.core.capabilities.licensing.models import (
    License,
    LicenseStatus,
)

# Retained for the optional account command's local HTTP calls (never used by
# the runtime itself).
USER_AGENT = "LemonCrow-CLI/1.0"

# Every feature, unlocked. The canonical grant for the open-source runtime.
_ALL_FEATURES: tuple[str, ...] = tuple(sorted(set(FREE_FEATURES) | set(PRO_FEATURES)))


def reload() -> None:
    """No-op: there is no cached remote entitlement state."""


def refresh_plan() -> None:
    """No-op: entitlements are local and always unlocked; nothing to refresh."""


def auth_user() -> dict[str, object] | None:
    """Return the locally-cached optional account, if the user linked one.

    Read-only, offline: never performs a network fetch. Returns ``None`` when no
    optional account is linked — the normal, account-free case.
    """
    return store.load_auth_user()


def current_license() -> License | None:
    """The local, all-features-granted license (never ``None``)."""
    return License(
        license_id="local",
        email="",
        plan="oss",
        device_id="",
        features=_ALL_FEATURES,
    )


def current_identity() -> tuple[str, str, str] | None:
    """No account/device identity is bound in the open-source runtime."""
    return None


def is_pro() -> bool:
    """Every capability is available locally."""
    return True


def has_feature(feature: str) -> bool:
    """Every registered feature is unlocked."""
    return True


def require(feature: str) -> None:
    """No-op: no feature is gated in the open-source runtime."""
    return None


def status() -> LicenseStatus:
    return LicenseStatus(
        licensed=True,
        valid=True,
        plan="oss",
        email=None,
        features=_ALL_FEATURES,
        reason="open-source — all features available locally",
        source="local",
    )
