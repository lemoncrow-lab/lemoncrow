"""Configuration helpers for cross-vendor routing."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from atelier.core.foundation.paths import default_store_root

ROUTE_CONFIG_VERSION = 1
SUPPORTED_ROUTE_VENDORS = ("anthropic", "openai", "google")
_VENDOR_ENV_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}
_VENDOR_HOST_COMMANDS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude",),
    "openai": ("codex",),
    "google": ("agy", "antigravity"),
}

EditMode = Literal["pin-actual-vendor", "allow-cross-vendor"]
ReadMode = Literal["cheapest-capable"]
AgentMode = Literal["any-tool-use"]


class RouteConfigError(ValueError):
    """Raised when route configuration is missing or invalid."""


class RouteConfig(BaseModel):
    """Persisted user-owned route configuration."""

    model_config = ConfigDict(extra="forbid")

    version: int = ROUTE_CONFIG_VERSION
    risk_class: Literal["low", "medium", "high"] = "low"
    enabled_vendors: list[str] = Field(default_factory=list)
    read_mode: ReadMode = "cheapest-capable"
    edit_mode: EditMode = "pin-actual-vendor"
    agent_mode: AgentMode = "any-tool-use"

    @field_validator("enabled_vendors")
    @classmethod
    def _validate_enabled_vendors(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for vendor in value:
            item = str(vendor).strip().lower()
            if not item:
                continue
            if item not in SUPPORTED_ROUTE_VENDORS:
                supported = ", ".join(SUPPORTED_ROUTE_VENDORS)
                raise ValueError(f"unsupported vendor {item!r}; expected one of: {supported}")
            if item in seen:
                continue
            normalized.append(item)
            seen.add(item)
        if not normalized:
            raise ValueError("enabled_vendors must contain at least one supported vendor")
        return normalized


def route_config_path(root: Path | str | None = None) -> Path:
    base = Path(root).expanduser().resolve() if root is not None else default_store_root()
    return base / "route.yaml"


def detect_configured_vendors(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    source = env if env is not None else os.environ
    enabled: list[str] = []
    for vendor in SUPPORTED_ROUTE_VENDORS:
        has_env = any(str(source.get(key, "")).strip() for key in _VENDOR_ENV_VARS[vendor])
        has_host_surface = any(shutil.which(command) is not None for command in _VENDOR_HOST_COMMANDS[vendor])
        if has_env or has_host_surface:
            enabled.append(vendor)
    return tuple(enabled)


def load_route_config(root: Path | str | None = None, *, path: Path | str | None = None) -> RouteConfig:
    config_path = Path(path).expanduser().resolve() if path is not None else route_config_path(root)
    if not config_path.exists():
        raise RouteConfigError(f"route config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RouteConfigError(f"route config is not valid YAML: {config_path}") from exc
    if not isinstance(raw, dict):
        raise RouteConfigError(f"route config at {config_path} must be a mapping")
    try:
        config = RouteConfig.model_validate(raw)
    except ValidationError as exc:
        raise RouteConfigError(f"route config is invalid: {exc}") from exc
    if config.version != ROUTE_CONFIG_VERSION:
        raise RouteConfigError(f"unsupported route config version {config.version}; expected {ROUTE_CONFIG_VERSION}")
    return config


def save_route_config(
    root: Path | str | None = None,
    config: RouteConfig | None = None,
    *,
    path: Path | str | None = None,
) -> Path:
    if config is None:
        raise RouteConfigError("route config is required")
    config_path = Path(path).expanduser().resolve() if path is not None else route_config_path(root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return config_path


__all__ = [
    "ROUTE_CONFIG_VERSION",
    "RouteConfig",
    "RouteConfigError",
    "detect_configured_vendors",
    "load_route_config",
    "route_config_path",
    "save_route_config",
]
