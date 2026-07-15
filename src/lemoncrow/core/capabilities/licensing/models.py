"""Data models for OAuth-backed LemonCrow entitlements."""

from __future__ import annotations

from dataclasses import dataclass

from lemoncrow.core.capabilities.licensing.features import PAID_PLANS

# Compatibility name used throughout the runtime. Lite is a paid plan, while
# feature-level access is decided by the explicit matrix in features.py.
PRO_PLANS: frozenset[str] = PAID_PLANS


class FeatureLocked(Exception):
    """Raised when an entitlement does not grant a requested feature."""

    def __init__(self, feature: str, message: str | None = None) -> None:
        self.feature = feature
        super().__init__(message or f"'{feature}' requires a LemonCrow paid plan")


@dataclass(frozen=True)
class License:
    """The verified entitlement of the signed-in account."""

    license_id: str
    email: str
    plan: str
    device_id: str = ""
    features: tuple[str, ...] = ()

    def grants(self, feature: str) -> bool:
        """Whether this entitlement explicitly grants a feature."""

        if self.features:
            return feature in self.features
        from lemoncrow.core.capabilities.licensing.features import plan_grants

        return plan_grants(self.plan, feature)


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
