from __future__ import annotations

from lemoncrow.core.capabilities.host_router_bridge import evaluate_host_router_request


def test_shadow_router_bridge_classifies_without_mutation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.host_router_bridge.select_owned_route",
        lambda root, request: type(
            "Decision",
            (),
            {
                "to_dict": lambda self: {
                    "mode": "auto",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "transport": "openai",
                    "runner": "openai",
                }
            },
        )(),
    )

    result = evaluate_host_router_request(
        root=tmp_path,
        path="/router-preset/claudecode/auto",
        model="claude-sonnet-4.6",
        messages=[{"role": "user", "content": "Plan the refactor."}],
        mode="shadow",
    )

    assert result["bridge_mode"] == "shadow"
    assert result["native_request_unchanged"] is True
    assert result["recommendation"]["provider"] == "openai"
    assert result["resolved_model"] == "claude-sonnet-4.6"
    assert result["resolved_upstream"]["base_url"] == "http://127.0.0.1:4000"


def test_enforced_router_bridge_requires_explicit_enable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.host_router_bridge.select_owned_route",
        lambda root, request: type(
            "Decision",
            (),
            {
                "to_dict": lambda self: {
                    "mode": "auto",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "transport": "openai",
                    "runner": "openai",
                }
            },
        )(),
    )

    result = evaluate_host_router_request(
        root=tmp_path,
        path="/router-preset/claudecode/auto",
        model="claude-sonnet-4.6",
        messages=[{"role": "user", "content": "Plan the refactor."}],
        mode="enforced",
        env={},
    )

    assert result["bridge_mode"] == "shadow"
    assert result["enforcement_requested"] is True
    assert result["enforcement_active"] is False
    assert result["resolved_model"] == "claude-sonnet-4.6"


def test_enforced_router_bridge_mutates_only_with_flag(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.host_router_bridge.select_owned_route",
        lambda root, request: type(
            "Decision",
            (),
            {
                "to_dict": lambda self: {
                    "mode": "auto",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "transport": "openai",
                    "runner": "openai",
                }
            },
        )(),
    )

    result = evaluate_host_router_request(
        root=tmp_path,
        path="/router-preset/claudecode/auto",
        model="claude-sonnet-4.6",
        messages=[{"role": "user", "content": "Plan the refactor."}],
        mode="enforced",
        env={"LEMONCROW_HOST_ROUTER_ENABLE": "1"},
    )

    assert result["bridge_mode"] == "active"
    assert result["enforcement_active"] is True
    assert result["resolved_provider"] == "openai"
    assert result["resolved_model"] == "gpt-4o"
