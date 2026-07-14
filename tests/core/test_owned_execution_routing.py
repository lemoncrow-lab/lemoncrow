from __future__ import annotations

import pytest

from lemoncrow.core.capabilities.owned_execution_routing import (
    CrossVendorRouter,
    OwnedExecutionRouteSelector,
    OwnedRouteRequest,
)
from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import (
    RouteConfig,
    save_route_config,
)
from lemoncrow.pro.capabilities.cross_vendor_routing.router import NoFeasibleRouteError


def test_explicit_owned_route_selects_requested_provider_model_and_runner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"codex", "copilot"} else None,
    )
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["openai"]))

    selector = OwnedExecutionRouteSelector(tmp_path, env={"OPENAI_API_KEY": "openai-key"})
    decision = selector.select(
        OwnedRouteRequest(
            tool_name="agent",
            task_text="Implement the fix.",
            mode="explicit",
            provider="openai",
            model="gpt-4o",
            runner="codex",
        )
    )

    assert decision.mode == "explicit"
    assert decision.provider == "openai"
    assert decision.model == "gpt-4o"
    assert decision.runner == "codex"
    assert decision.transport == "openai"


def test_explicit_owned_route_supports_bedrock_sonnet_4_6_with_bearer_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _command: None)
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["bedrock"]))

    selector = OwnedExecutionRouteSelector(
        tmp_path,
        env={"AWS_BEARER_TOKEN_BEDROCK": "bedrock-token"},
    )
    decision = selector.select(
        OwnedRouteRequest(
            tool_name="run",
            task_text="Implement the fix.",
            mode="explicit",
            provider="bedrock",
            model="bedrock/us.anthropic.claude-sonnet-4-6",
        )
    )

    assert decision.provider == "bedrock"
    assert decision.model == "bedrock/us.anthropic.claude-sonnet-4-6"
    assert decision.runner == "litellm"
    assert decision.transport == "litellm"


def test_explicit_owned_route_rejects_unavailable_requested_runner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _command: None)
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["openai"]))

    selector = OwnedExecutionRouteSelector(tmp_path, env={"OPENAI_API_KEY": "openai-key"})

    with pytest.raises(NoFeasibleRouteError, match="runner 'codex' cannot execute"):
        selector.select(
            OwnedRouteRequest(
                tool_name="agent",
                task_text="Implement the fix.",
                mode="explicit",
                provider="openai",
                model="gpt-4o",
                runner="codex",
            )
        )


def test_auto_owned_route_filters_unhealthy_provider_before_budget_choice(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"claude", "codex", "copilot"} else None,
    )
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["anthropic", "openai"]))

    selector = OwnedExecutionRouteSelector(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "anthropic-key", "OPENAI_API_KEY": "openai-key"},
    )
    decision = selector.select(
        OwnedRouteRequest(
            tool_name="agent",
            task_text="Design the owned execution route.",
            mode="auto",
            budget="best",
            session_state={"provider_health": {"anthropic": "unhealthy"}},
        )
    )

    assert decision.provider == "openai"
    assert decision.model == "gpt-4o"
    assert decision.transport == "openai"
    assert decision.runner in {"openai", "copilot", "codex"}


def test_catalog_includes_google_when_owned_transport_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _command: None)
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["google", "openai"]))

    selector = OwnedExecutionRouteSelector(
        tmp_path,
        env={"GOOGLE_API_KEY": "google-key", "OPENAI_API_KEY": "openai-key"},
    )
    catalog = selector.catalog(OwnedRouteRequest(tool_name="agent", task_text="Design the route."))

    assert {entry.provider for entry in catalog} == {"google", "openai"}
    google = next(entry for entry in catalog if entry.provider == "google")
    assert google.transport == "litellm"
    assert google.default_runner == "litellm"


def test_auto_owned_route_prefers_warm_sticky_affinity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _command: None)
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["anthropic", "openai"]))

    selector = OwnedExecutionRouteSelector(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "anthropic-key", "OPENAI_API_KEY": "openai-key"},
    )
    catalog = selector.catalog(OwnedRouteRequest(tool_name="agent", task_text="Design the route."))
    warm_entry = next(entry for entry in catalog if entry.provider == "anthropic")
    warm_model = warm_entry.models[0]

    monkeypatch.setattr(
        CrossVendorRouter,
        "recommend",
        lambda self, **_kwargs: type(
            "Recommendation",
            (),
            {
                "vendor": "openai",
                "model": "gpt-4o",
                "tier": "high",
                "reasons": ("openai recommended",),
                "alternatives": (),
                "applied_lessons": (),
                "cost_cap_triggered": False,
                "cost_cap_limit_usd_per_session": None,
                "projected_session_cost_usd": None,
            },
        )(),
    )

    decision = selector.select(
        OwnedRouteRequest(
            tool_name="agent",
            task_text="Design the route.",
            session_state={
                "cache_affinity": {
                    "provider": "anthropic",
                    "model": warm_model,
                    "transport": "litellm",
                    "stable_prefix_hash": "warm-prefix",
                    "stable_prefix_tokens": 1200,
                    "eviction_cost_usd": 0.5,
                    "stickiness_remaining": 2,
                }
            },
        )
    )

    assert decision.provider == "anthropic"
    assert decision.model == warm_model
    assert "cache_affinity: retained warm sticky route" in decision.reason


def test_fresh_owned_route_ignores_warm_sticky_affinity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _command: None)
    save_route_config(tmp_path, RouteConfig(enabled_vendors=["anthropic", "openai"]))

    selector = OwnedExecutionRouteSelector(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "anthropic-key", "OPENAI_API_KEY": "openai-key"},
    )
    catalog = selector.catalog(OwnedRouteRequest(tool_name="agent", task_text="Design the route."))
    warm_entry = next(entry for entry in catalog if entry.provider == "anthropic")
    warm_model = warm_entry.models[0]

    monkeypatch.setattr(
        CrossVendorRouter,
        "recommend",
        lambda self, **_kwargs: type(
            "Recommendation",
            (),
            {
                "vendor": "openai",
                "model": "gpt-4o",
                "tier": "high",
                "reasons": ("openai recommended",),
                "alternatives": (),
                "applied_lessons": (),
                "cost_cap_triggered": False,
                "cost_cap_limit_usd_per_session": None,
                "projected_session_cost_usd": None,
            },
        )(),
    )

    decision = selector.select(
        OwnedRouteRequest(
            tool_name="agent",
            task_text="Research independently.",
            cache_policy="fresh",
            session_state={
                "cache_affinity": {
                    "provider": "anthropic",
                    "model": warm_model,
                    "transport": "litellm",
                    "stable_prefix_hash": "warm-prefix",
                    "stable_prefix_tokens": 1200,
                    "eviction_cost_usd": 0.5,
                    "stickiness_remaining": 2,
                }
            },
        )
    )

    assert decision.provider == "openai"
    assert "cache_affinity: retained warm sticky route" not in decision.reason
