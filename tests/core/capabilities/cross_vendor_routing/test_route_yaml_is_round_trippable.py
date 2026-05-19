from __future__ import annotations

from atelier.core.capabilities.cross_vendor_routing.configuration import (
    RouteConfig,
    detect_configured_vendors,
    load_route_config,
    route_config_path,
    save_route_config,
)


def test_route_yaml_is_round_trippable(tmp_path) -> None:
    config = RouteConfig(
        enabled_vendors=["anthropic", "google"],
        risk_class="low",
        edit_mode="pin-actual-vendor",
    )

    saved_path = save_route_config(tmp_path, config)

    assert saved_path == route_config_path(tmp_path)
    assert load_route_config(tmp_path) == config


def test_detect_configured_vendors_uses_supported_env_aliases() -> None:
    assert detect_configured_vendors({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}) == (
        "anthropic",
        "google",
    )
