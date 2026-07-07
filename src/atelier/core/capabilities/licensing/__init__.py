"""Atelier licensing -- OAuth-backed Pro entitlements (open-core split).

The open-source core ships every capability. This package is the *gate*: a
feature key is either free (always allowed) or Pro (allowed only when the
signed-in account's plan is Pro/Enterprise). ``atelier login`` creates the
OAuth session; the plan comes from the auth server's ``/api/auth/me`` and is
cached on disk for 6 h. Fail-closed: without a session or a fresh server
answer, gated surfaces stay Free. There is no dev backdoor.

Public API::

    from atelier.core.capabilities import licensing
    licensing.is_pro()
    licensing.require("optimizer")        # raises FeatureLocked if not unlocked
"""

from __future__ import annotations

import os

from atelier.core.capabilities.licensing.entitlements import (
    current_license,
    has_feature,
    is_pro,
    refresh_plan,
    reload,
    require,
    status,
)
from atelier.core.capabilities.licensing.features import PRO_FEATURES
from atelier.core.capabilities.licensing.models import (
    FeatureLocked,
    License,
    LicenseStatus,
)

_DEFAULT_PRO_URL = "https://atelier.ws/pro"


def pro_url() -> str:
    """Where to send users to buy Pro.

    Override with ``ATELIER_PRO_URL`` to point straight at your Stripe Payment
    Link (or any storefront) without rebuilding the client.
    """
    return os.environ.get("ATELIER_PRO_URL", "").strip() or _DEFAULT_PRO_URL


__all__ = [
    "PRO_FEATURES",
    "FeatureLocked",
    "License",
    "LicenseStatus",
    "current_license",
    "has_feature",
    "is_pro",
    "pro_url",
    "refresh_plan",
    "reload",
    "require",
    "status",
]
