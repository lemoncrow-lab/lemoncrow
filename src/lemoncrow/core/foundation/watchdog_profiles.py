from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.watchdogs import (
    builtin_watchdog_profiles,
    default_watchdog_profile_id,
    normalize_watchdog_weights,
    watchdog_library,
)


@dataclass(frozen=True)
class WatchdogProfileConfig:
    active_profile: str = default_watchdog_profile_id()
    profiles: dict[str, dict[str, float]] = field(default_factory=dict)


def watchdog_profile_config_path(root: str | Path) -> Path:
    return Path(root) / "watchdog_profiles.json"


def load_watchdog_profile_config(root: str | Path) -> WatchdogProfileConfig:
    path = watchdog_profile_config_path(root)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except Exception:
            logging.exception("Recovered from broad exception handler")
            raw = {}

    builtin = {profile.id: profile for profile in builtin_watchdog_profiles()}
    active_profile = str(raw.get("active_profile") or default_watchdog_profile_id())
    if active_profile not in builtin:
        active_profile = default_watchdog_profile_id()

    stored_profiles = raw.get("profiles")
    profiles: dict[str, dict[str, float]] = {}
    if isinstance(stored_profiles, dict):
        for profile_id, weights in stored_profiles.items():
            if profile_id in builtin and isinstance(weights, dict):
                profiles[profile_id] = normalize_watchdog_weights(weights)

    return WatchdogProfileConfig(active_profile=active_profile, profiles=profiles)


def save_watchdog_profile_config(
    root: str | Path,
    *,
    active_profile: str | None = None,
    profiles: dict[str, dict[str, float]] | None = None,
) -> WatchdogProfileConfig:
    current = load_watchdog_profile_config(root)
    builtin = {profile.id for profile in builtin_watchdog_profiles()}
    next_active_profile = current.active_profile if active_profile is None else active_profile
    if next_active_profile not in builtin:
        raise ValueError(f"unknown watchdog profile: {next_active_profile}")

    next_profiles = dict(current.profiles)
    if profiles is not None:
        next_profiles = {}
        for profile_id, weights in profiles.items():
            if profile_id in builtin and isinstance(weights, dict):
                next_profiles[profile_id] = normalize_watchdog_weights(weights)

    next_config = WatchdogProfileConfig(active_profile=next_active_profile, profiles=next_profiles)
    path = watchdog_profile_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "active_profile": next_config.active_profile,
                "profiles": next_config.profiles,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return next_config


def active_watchdog_weights(root: str | Path) -> dict[str, float]:
    config = load_watchdog_profile_config(root)
    for profile in builtin_watchdog_profiles():
        if profile.id == config.active_profile:
            return config.profiles.get(profile.id, normalize_watchdog_weights(profile.weights))
    return normalize_watchdog_weights()


def frontend_watchdog_profile_config(root: str | Path) -> dict[str, Any]:
    config = load_watchdog_profile_config(root)
    profiles = []
    for profile in builtin_watchdog_profiles():
        profiles.append(
            {
                "id": profile.id,
                "label": profile.label,
                "description": profile.description,
                "weights": config.profiles.get(profile.id, normalize_watchdog_weights(profile.weights)),
            }
        )
    library = []
    for definition in watchdog_library():
        library.append(
            {
                "key": definition.key,
                "title": definition.title,
                "description": definition.description,
                "default_weight": definition.default_weight,
                "severity": definition.severity,
            }
        )
    return {
        "active_profile": config.active_profile,
        "profiles": profiles,
        "library": library,
        "runtime_wired": True,
        "config_path": str(watchdog_profile_config_path(root)),
    }
