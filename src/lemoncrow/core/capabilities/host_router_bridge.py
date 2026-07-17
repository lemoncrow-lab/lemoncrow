from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from lemoncrow.core.capabilities.pricing import active_model
from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import RouteConfigError
from lemoncrow.pro.capabilities.cross_vendor_routing.router import NoFeasibleRouteError
from lemoncrow.pro.capabilities.owned_execution_routing import OwnedRouteRequest, select_owned_route

HostRouterMode = Literal["disabled", "shadow", "enforced"]
PresetRouteMode = Literal["auto", "explicit"]

_DEFAULT_UPSTREAM_BASE_URL = "http://127.0.0.1:4000"
_ENFORCEMENT_FLAG = "LEMONCROW_HOST_ROUTER_ENABLE"


@dataclass(frozen=True)
class HostRouterPreset:
    name: str
    path: str
    route_mode: PresetRouteMode = "auto"
    provider: str = ""
    model: str = ""
    runner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "route_mode": self.route_mode,
            "provider": self.provider,
            "model": self.model,
            "runner": self.runner,
        }


@dataclass(frozen=True)
class HostRouterBridgeConfig:
    mode: HostRouterMode
    upstream_base_url: str
    pass_through_auth: bool
    presets: tuple[HostRouterPreset, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "upstream_base_url": self.upstream_base_url,
            "pass_through_auth": self.pass_through_auth,
            "presets": [preset.to_dict() for preset in self.presets],
        }


def default_host_router_config(
    *, mode: HostRouterMode | None = None, env: Mapping[str, str] | None = None
) -> HostRouterBridgeConfig:
    source = env or os.environ
    resolved_mode = mode or _normalize_mode(source.get("LEMONCROW_HOST_ROUTER_MODE"))
    return HostRouterBridgeConfig(
        mode=resolved_mode,
        upstream_base_url=str(source.get("LEMONCROW_HOST_ROUTER_UPSTREAM_BASE_URL") or _DEFAULT_UPSTREAM_BASE_URL),
        pass_through_auth=True,
        presets=(
            HostRouterPreset(name="auto", path="/router-preset/claudecode/auto", route_mode="auto"),
            HostRouterPreset(
                name="openai",
                path="/router-preset/claudecode/openai",
                route_mode="explicit",
                provider="openai",
            ),
            HostRouterPreset(
                name="anthropic",
                path="/router-preset/claudecode/anthropic",
                route_mode="explicit",
                provider="anthropic",
            ),
        ),
    )


def evaluate_host_router_request(
    *,
    root: Path | str,
    path: str,
    model: str = "",
    messages: list[dict[str, Any]] | None = None,
    system: str = "",
    session_state: Mapping[str, Any] | None = None,
    mode: HostRouterMode | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source = env or os.environ
    config = default_host_router_config(mode=mode, env=source)
    preset = _match_preset(config, path)
    requested_model = model.strip() or active_model()
    requested_provider = _provider_for_model(requested_model)
    task_text = _task_text(messages or [], system=system)
    enforcement_active = config.mode == "enforced" and str(source.get(_ENFORCEMENT_FLAG) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    recommendation: dict[str, Any] = {}
    recommendation_error = ""
    if config.mode != "disabled" and task_text:
        try:
            decision = select_owned_route(
                root,
                OwnedRouteRequest(
                    tool_name="agent",
                    task_text=task_text,
                    mode=preset.route_mode,
                    provider=preset.provider,
                    model=preset.model,
                    runner=preset.runner,
                    session_state=dict(session_state or {}),
                ),
            )
            recommendation = decision.to_dict()
        except (NoFeasibleRouteError, RouteConfigError, ValueError) as exc:
            recommendation_error = str(exc)
    bridge_mode = "active" if enforcement_active else ("shadow" if config.mode != "disabled" else "native")
    routed_model = str(recommendation.get("model") or requested_model)
    routed_provider = str(recommendation.get("provider") or requested_provider)
    return {
        "bridge_mode": bridge_mode,
        "benchmark_bridge_mode": bridge_mode,
        "config": config.to_dict(),
        "preset": preset.to_dict(),
        "requested_model": requested_model,
        "requested_provider": requested_provider,
        "requested_path": path,
        "recommendation": recommendation,
        "recommendation_error": recommendation_error,
        "enforcement_requested": config.mode == "enforced",
        "enforcement_active": enforcement_active,
        "enforcement_flag": _ENFORCEMENT_FLAG,
        "native_request_unchanged": bridge_mode != "active",
        "would_mutate_provider": bridge_mode == "active" and routed_provider != requested_provider,
        "would_mutate_model": bridge_mode == "active" and routed_model != requested_model,
        "resolved_provider": routed_provider if bridge_mode == "active" else requested_provider,
        "resolved_model": routed_model if bridge_mode == "active" else requested_model,
        "resolved_upstream": {
            "base_url": config.upstream_base_url,
            "auth_mode": "pass_through",
        },
    }


def _normalize_mode(value: str | None) -> HostRouterMode:
    raw = str(value or "shadow").strip().lower()
    if raw == "disabled":
        return "disabled"
    if raw == "enforced":
        return "enforced"
    if raw == "shadow":
        return "shadow"
    return "shadow"


def _match_preset(config: HostRouterBridgeConfig, path: str) -> HostRouterPreset:
    requested = str(path or "").strip() or "/router-preset/claudecode/auto"
    for preset in config.presets:
        if requested == preset.path:
            return preset
    return config.presets[0]


def _task_text(messages: list[dict[str, Any]], *, system: str) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "").strip() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return system.strip()


def _provider_for_model(model_id: str) -> str:
    normalized = model_id.strip().lower()
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if normalized.startswith("gemini"):
        return "google"
    return ""


__all__ = [
    "HostRouterBridgeConfig",
    "HostRouterMode",
    "HostRouterPreset",
    "default_host_router_config",
    "evaluate_host_router_request",
]
