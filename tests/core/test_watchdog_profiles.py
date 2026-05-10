from __future__ import annotations

from pathlib import Path

from atelier.core.foundation.monitor_profiles import (
    active_watchdog_weights,
    frontend_watchdog_profile_config,
    load_watchdog_profile_config,
    save_watchdog_profile_config,
    watchdog_profile_config_path,
)


def test_watchdog_profile_config_round_trips_and_clamps_weights(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"

    save_watchdog_profile_config(
        root,
        active_profile="qa",
        profiles={
            "qa": {
                "repeated_tool_call": 0.0,
                "context_bloat": 2.0,
            }
        },
    )

    loaded = load_watchdog_profile_config(root)

    assert loaded.active_profile == "qa"
    assert loaded.profiles["qa"]["repeated_tool_call"] == 0.0
    assert loaded.profiles["qa"]["context_bloat"] == 1.0
    assert watchdog_profile_config_path(root).exists()


def test_frontend_watchdog_profile_config_returns_runtime_payload(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"

    payload = frontend_watchdog_profile_config(root)

    assert payload["active_profile"] == "coding"
    assert payload["runtime_wired"] is True
    assert payload["profiles"]
    assert payload["library"]
    assert active_watchdog_weights(root)["repeated_command_failure"] > 0
