"""Configuration helpers for cross-vendor routing."""

from __future__ import annotations

import functools
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from atelier.core.foundation.paths import default_store_root

ROUTE_CONFIG_VERSION = 1
SUPPORTED_ROUTE_VENDORS = (
    "anthropic",
    "openai",
    "google",
    "bedrock",
    "vertex",
    "azure",
    "openrouter",
    "groq",
    "mistral",
    "ollama",
    "together",
    "fireworks",
)
_VENDOR_ENV_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "bedrock": ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK"),
    "vertex": ("VERTEXAI_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"),
    "azure": ("AZURE_API_KEY", "AZURE_OPENAI_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "ollama": ("OLLAMA_HOST",),  # Ollama uses base URL, not API key
    "together": ("TOGETHER_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
}
_VENDOR_HOST_COMMANDS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude",),
    "openai": ("codex",),
    "google": ("agy", "antigravity"),
    "bedrock": (),
    "vertex": (),
    "azure": (),
    "openrouter": (),
    "groq": (),
    "mistral": (),
    "ollama": ("ollama",),  # detect local ollama
    "together": (),
    "fireworks": (),
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


@functools.cache
def _detect_configured_vendors_cached() -> tuple[str, ...]:
    """Process-level cached vendor detection using os.environ (invariant for process lifetime)."""
    enabled: list[str] = []
    for vendor in SUPPORTED_ROUTE_VENDORS:
        has_env = any(str(os.environ.get(key, "")).strip() for key in _VENDOR_ENV_VARS[vendor])
        has_host_surface = any(shutil.which(command) is not None for command in _VENDOR_HOST_COMMANDS[vendor])
        if has_env or has_host_surface:
            enabled.append(vendor)
    return tuple(enabled)


def detect_configured_vendors(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    if env is not None:
        # Custom env provided — bypass cache and compute directly.
        enabled: list[str] = []
        for vendor in SUPPORTED_ROUTE_VENDORS:
            has_env = any(str(env.get(key, "")).strip() for key in _VENDOR_ENV_VARS[vendor])
            has_host_surface = any(shutil.which(command) is not None for command in _VENDOR_HOST_COMMANDS[vendor])
            if has_env or has_host_surface:
                enabled.append(vendor)
        return tuple(enabled)
    return _detect_configured_vendors_cached()


def detect_api_key_vendors(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return vendors reachable via an API key in the environment.

    Unlike :func:`detect_configured_vendors`, this ignores installed host CLIs:
    owned execution runs through the litellm/openai HTTP transports, which need
    a real API key, so a host-CLI subscription alone cannot execute an owned
    turn. Used to seed a default route config that only enables vendors that can
    actually run.
    """
    source = env if env is not None else os.environ
    enabled: list[str] = []
    for vendor in SUPPORTED_ROUTE_VENDORS:
        if vendor == "ollama":
            has_host = bool(str(source.get("OLLAMA_HOST", "")).strip())
            has_binary = shutil.which("ollama") is not None
            if has_host or has_binary:
                enabled.append(vendor)
            continue
        if any(str(source.get(key, "")).strip() for key in _VENDOR_ENV_VARS[vendor]):
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


# Module-level cache for load_route_config_or_default.
# Key: (resolved_path_str, mtime_ns, file_size) — or (resolved_path_str, None, None) when absent.
# Value: RouteConfig | RouteConfigError  (we cache errors too so absent-file calls don't stat repeatedly)
_route_config_cache: dict[tuple[str, int | None, int | None], RouteConfig | RouteConfigError] = {}


def load_route_config_or_default(
    root: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    path: Path | str | None = None,
) -> RouteConfig:
    """Load ``route.yaml`` or synthesise a default from detected vendors.

    Owned routing should work out of the box: when no ``route.yaml`` has been
    written yet, build a low-risk config enabling every vendor reachable via an
    API key. Host-CLI-only vendors are intentionally excluded — owned execution
    runs over the litellm/openai HTTP transports and needs a real key, so a
    bare CLI subscription cannot execute a turn. Re-raises ``RouteConfigError``
    only when the file is genuinely missing *and* no API-key vendor is present,
    or when the file exists but is invalid (so real config mistakes are never
    silently masked).

    Results are cached keyed on (resolved_path, mtime_ns, file_size) so repeated
    calls within a session pay no I/O cost.  A custom ``env`` mapping bypasses
    the cache because the synthesised default depends on the caller-supplied env.
    """
    config_path = Path(path).expanduser().resolve() if path is not None else route_config_path(root)
    resolved = str(config_path)

    # Build cache key from file metadata (or sentinel when absent).
    if config_path.exists():
        stat = config_path.stat()
        cache_key: tuple[str, int | None, int | None] = (resolved, stat.st_mtime_ns, stat.st_size)
    else:
        cache_key = (resolved, None, None)

    # Only use cache when env is None (default os.environ path).
    if env is None and cache_key in _route_config_cache:
        cached = _route_config_cache[cache_key]
        if isinstance(cached, RouteConfigError):
            raise cached
        return cached

    # Cache miss (or custom env) — compute the result.
    try:
        result = load_route_config(root, path=path)
    except RouteConfigError as exc:
        if config_path.exists():
            # File present but invalid — cache and re-raise.
            if env is None:
                _route_config_cache[cache_key] = exc
            raise
        vendors = list(detect_api_key_vendors(env))
        if not vendors:
            if env is None:
                _route_config_cache[cache_key] = exc
            raise
        result = RouteConfig(enabled_vendors=vendors)

    if env is None:
        _route_config_cache[cache_key] = result
    return result


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
