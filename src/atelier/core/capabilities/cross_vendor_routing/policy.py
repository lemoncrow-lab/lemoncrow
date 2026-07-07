"""Policy helpers for cross-vendor routing."""

from __future__ import annotations

from atelier.core.capabilities.counterfactual.capabilities import classify_turn_kind

from .configuration import RouteConfig


class RoutePolicyError(ValueError):
    """Raised when routing policy cannot be applied safely."""


def turn_kind_for_tool(tool_name: str) -> str:
    return classify_turn_kind(tool_name)


def allowed_vendors(
    config: RouteConfig,
    *,
    tool_name: str,
    actual_vendor: str | None,
    configured_vendors: tuple[str, ...],
) -> tuple[str, ...]:
    turn_kind = turn_kind_for_tool(tool_name)
    if turn_kind == "edit" and config.edit_mode == "pin-actual-vendor":
        vendor = (actual_vendor or "").strip().lower()
        if not vendor:
            raise RoutePolicyError("actual_vendor is required for edit routing when edit_mode pins to actual vendor")
        if vendor not in configured_vendors:
            raise RoutePolicyError(f"actual vendor {vendor!r} is not configured for routing")
        return (vendor,)
    return configured_vendors


__all__ = ["RoutePolicyError", "allowed_vendors", "turn_kind_for_tool"]
