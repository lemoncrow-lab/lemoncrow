"""Local feature-access contract for the open-source runtime.

The OSS product ships every local capability. Hosted entitlement, billing and
organization policy belong to the private service composition and must never be
consulted by the local engine.
"""

from __future__ import annotations


def has_feature(_feature: str) -> bool:
    """Every local LemonCrow capability is available."""

    return True


def require(_feature: str) -> None:
    """Compatibility seam for callers that express a capability requirement."""

    return None


def is_pro() -> bool:
    """Legacy semantic alias: the complete local engine is always enabled."""

    return True


__all__ = ["has_feature", "is_pro", "require"]
