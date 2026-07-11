"""Product telemetry configuration and opt-out state."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.identity import config_dir

FALSE_VALUES = {"0", "false", "off", "no"}
TRUE_VALUES = {"1", "true", "on", "yes"}


@dataclass(frozen=True)
class TelemetryConfig:
    remote_enabled: bool = True
    lexical_frustration_enabled: bool = True


def config_path() -> Path:
    return Path(os.environ.get("LEMONCROW_TELEMETRY_CONFIG", config_dir() / "telemetry.toml"))


def _load_file_config() -> TelemetryConfig:
    """Configuration exactly as persisted in telemetry.toml (no test overrides)."""
    data: dict[str, Any] = {}
    path = config_path()
    if path.exists():
        try:
            loaded = tomllib.loads(path.read_text(encoding="utf-8"))
            section = loaded.get("telemetry", {})
            data = section if isinstance(section, dict) else {}
        except Exception:
            logging.exception("Recovered from broad exception handler")
            data = {}

    return TelemetryConfig(
        remote_enabled=_bool(data.get("remote_enabled"), True),
        lexical_frustration_enabled=_bool(data.get("lexical_frustration_enabled"), True),
    )


def load_telemetry_config() -> TelemetryConfig:
    # Remote telemetry defaults ON, but a user-set ``remote_enabled = false``
    # in telemetry.toml is honored. The test guard below additionally
    # suppresses remote export so the suite never phones home.
    cfg = _load_file_config()
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("LEMONCROW_TELEMETRY_ALLOW_IN_TESTS") != "1":
        return TelemetryConfig(False, cfg.lexical_frustration_enabled)
    return cfg


def save_telemetry_config(
    *,
    remote_enabled: bool | None = None,
    lexical_frustration_enabled: bool | None = None,
) -> TelemetryConfig:
    current = _load_file_config()
    next_cfg = TelemetryConfig(
        remote_enabled=(current.remote_enabled if remote_enabled is None else remote_enabled),
        lexical_frustration_enabled=(
            current.lexical_frustration_enabled if lexical_frustration_enabled is None else lexical_frustration_enabled
        ),
    )
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[telemetry]\n"
        f"remote_enabled = {_toml_bool(next_cfg.remote_enabled)}\n"
        f"lexical_frustration_enabled = {_toml_bool(next_cfg.lexical_frustration_enabled)}\n",
        encoding="utf-8",
    )
    return next_cfg


def remote_enabled() -> bool:
    return load_telemetry_config().remote_enabled


def lexical_frustration_enabled() -> bool:
    return load_telemetry_config().lexical_frustration_enabled


def otel_endpoint() -> str:
    return os.environ.get("LEMONCROW_OTEL_ENDPOINT", "http://localhost:4318")


def posthog_key() -> str:
    return os.environ.get("LEMONCROW_POSTHOG_KEY", "")


def posthog_host() -> str:
    return os.environ.get("LEMONCROW_POSTHOG_HOST", "https://us.i.posthog.com")


def posthog_otlp_url() -> str:
    """Direct OTLP ingest endpoint on PostHog - no local collector required."""
    return posthog_host().rstrip("/") + "/i/v0/otlp"


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
    return default


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
