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
    from unittest import mock

    with mock.patch("shutil.which", return_value=None):
        assert detect_configured_vendors({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}) == (
            "anthropic",
            "google",
        )


def test_detect_configured_vendors_uses_installed_host_clis(monkeypatch) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"claude", "codex", "agy"} else None,
    )

    assert detect_configured_vendors({}) == ("anthropic", "openai", "google")
