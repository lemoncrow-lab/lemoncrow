"""Data models for the LemonCrow licensing layer.

Entitlement comes from the OAuth session created by ``lemon login``: the auth
server reports the account's plan, and paid plans unlock the gated surfaces.
There are no offline license tokens -- the account's plan (checked server-side,
cached locally) plus the proprietary overlay is the whole contract.
"""

from __future__ import annotations

from dataclasses import dataclass

PRO_PLANS: frozenset[str] = frozenset({"pro", "enterprise"})


class FeatureLocked(Exception):
    """Raised when a Pro-only feature is used without a Pro plan.

    Carries the offending ``feature`` key so callers can render a precise
    upgrade prompt.
    """

    def __init__(self, feature: str, message: str | None = None) -> None:
        self.feature = feature
        super().__init__(message or f"'{feature}' requires an LemonCrow Pro license")


@dataclass(frozen=True)
class License:
    """The verified entitlement of the signed-in account."""

    license_id: str
    email: str
    plan: str
    features: tuple[str, ...] = ()

    def grants(self, feature: str) -> bool:
        """Whether this entitlement grants ``feature`` (empty ``features`` = all)."""
        if not self.features:
            return self.plan in PRO_PLANS
        return feature in self.features


@dataclass(frozen=True)
class LicenseStatus:
    """A flattened, render-ready view of the current entitlement state."""

    licensed: bool
    valid: bool
    plan: str | None
    email: str | None
    features: tuple[str, ...]
    reason: str
    source: str
