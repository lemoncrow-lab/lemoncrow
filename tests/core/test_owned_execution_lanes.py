from __future__ import annotations

import pytest

from lemoncrow.core.capabilities.owned_execution_lanes import (
    OwnedExecutionError,
    execute_owned_prompt,
)
from lemoncrow.core.capabilities.owned_execution_routing import OwnedRouteDecision
from lemoncrow.infra.internal_llm.exceptions import OpenAIClientUnavailable
from lemoncrow.infra.internal_llm.result import InternalLLMChatResult


def _decision(
    *,
    mode: str = "auto",
    provider: str = "openai",
    model: str = "gpt-4o",
    runner: str = "openai",
    transport: str = "openai",
) -> OwnedRouteDecision:
    return OwnedRouteDecision(
        mode=mode,
        provider=provider,
        model=model,
        runner=runner,
        transport=transport,
        tier="high",
        route_tier="frontier_llm",
        reason="test route",
        reasons=("test route",),
        execution_mode="wrapper_enforced",
        can_block_start=True,
        can_force_model=False,
        enabled_providers=(provider,),
        available_providers=(),
    )


def test_execute_owned_prompt_returns_structured_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.owned_execution_lanes.openai_chat_with_result",
        lambda messages, model=None: InternalLLMChatResult(
            content="done",
            model=model or "gpt-4o",
            request_id="req-1",
            input_tokens=21,
            output_tokens=7,
            cache_read_input_tokens=5,
        ),
    )

    result = execute_owned_prompt(
        "Keep the prompt prefix stable.\n\nCurrent phase prompt:\nImplement the fix.",
        root=tmp_path,
        tool_name="agent",
        task_text="Implement the fix.",
        decision=_decision(),
    )

    assert result.output == "done"
    assert result.receipt.executed_provider == "openai"
    assert result.receipt.executed_transport == "openai"
    assert result.receipt.request_id == "req-1"
    assert result.receipt.input_tokens == 21
    assert result.receipt.cache_read_input_tokens == 5
    assert result.receipt.cache_affinity["stable_prefix_hash"]
    assert result.receipt.cache_affinity["cache_evidence"] == "actual"


def test_execute_owned_prompt_fresh_policy_disables_cache_affinity(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.owned_execution_lanes.openai_chat_with_result",
        lambda messages, model=None: InternalLLMChatResult(
            content="fresh done",
            model=model or "gpt-4o",
            request_id="req-fresh",
            input_tokens=21,
            output_tokens=7,
            cache_read_input_tokens=5,
            cache_write_input_tokens=3,
        ),
    )

    result = execute_owned_prompt(
        "Keep the prompt prefix stable.\n\nCurrent phase prompt:\nResearch independently.",
        root=tmp_path,
        tool_name="agent",
        task_text="Research independently.",
        decision=_decision(),
        cache_policy="fresh",
        session_state={
            "cache_affinity": {
                "provider": "openai",
                "model": "gpt-4o",
                "transport": "openai",
                "stable_prefix_hash": "warm-prefix",
                "stable_prefix_tokens": 1200,
                "stickiness_remaining": 2,
            }
        },
    )

    assert result.output == "fresh done"
    assert result.receipt.cache_policy == "fresh"
    assert result.receipt.cache_read_input_tokens == 5
    assert result.receipt.cache_write_input_tokens == 3
    assert result.receipt.stable_prefix_hash == ""
    assert result.receipt.cache_evidence == "disabled"
    assert result.receipt.cache_affinity["stickiness_remaining"] == 0
    assert result.receipt.cache_affinity["prefix_invalidated_reason"] == "cache_policy_fresh"


def test_execute_owned_prompt_auto_reroutes_after_provider_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.owned_execution_lanes.openai_chat_with_result",
        lambda messages, model=None: (_ for _ in ()).throw(OpenAIClientUnavailable("openai down")),
    )
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.owned_execution_lanes.litellm_chat_with_result",
        lambda messages, model=None: InternalLLMChatResult(
            content="fallback",
            model=model or "claude-opus-4.1",
            request_id="req-2",
            input_tokens=34,
            output_tokens=9,
        ),
    )
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.owned_execution_lanes.select_owned_route",
        lambda root, request: _decision(
            mode=request.mode,
            provider="anthropic",
            model="claude-opus-4.1",
            runner="litellm",
            transport="litellm",
        ),
    )

    result = execute_owned_prompt(
        "Review the patch.",
        root=tmp_path,
        tool_name="agent",
        task_text="Review the patch.",
        decision=_decision(mode="auto"),
    )

    assert result.output == "fallback"
    assert result.receipt.rerouted is True
    assert result.receipt.executed_provider == "anthropic"
    assert [attempt.status for attempt in result.receipt.attempts] == ["failed", "done"]


def test_execute_owned_prompt_explicit_route_fails_without_fallback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.owned_execution_lanes.openai_chat_with_result",
        lambda messages, model=None: (_ for _ in ()).throw(OpenAIClientUnavailable("openai down")),
    )

    with pytest.raises(OwnedExecutionError, match="openai down") as exc_info:
        execute_owned_prompt(
            "Review the patch.",
            root=tmp_path,
            tool_name="agent",
            task_text="Review the patch.",
            decision=_decision(mode="explicit"),
        )

    assert exc_info.value.receipt.status == "failed"
    assert exc_info.value.receipt.rerouted is False


def test_execute_owned_prompt_records_spawn_scope_and_cache_capability(monkeypatch, tmp_path) -> None:
    def fake_openai(messages, model=None, cache_metadata=None):
        return InternalLLMChatResult(
            content="owned result",
            model=model or "gpt-4o",
            request_id="req-scope",
            input_tokens=20,
            output_tokens=6,
            cache_read_input_tokens=4,
            cache_capability="explicit",
            request_metadata=dict(cache_metadata or {}),
        )

    monkeypatch.setattr("lemoncrow.core.capabilities.owned_execution_lanes.openai_chat_with_result", fake_openai)

    result = execute_owned_prompt(
        "Keep the prompt prefix stable.\n\nCurrent phase prompt:\nImplement the fix.",
        root=tmp_path,
        tool_name="agent",
        task_text="Implement the fix.",
        decision=_decision(),
        compiled_prompt={
            "prompt": "Keep the prompt prefix stable.\n\nCurrent phase prompt:\nImplement the fix.",
            "stable_prefix": "Keep the prompt prefix stable.",
            "dynamic_tail": "Current phase prompt:\nImplement the fix.",
            "stable_prefix_hash": "prefix-123",
            "stable_prefix_tokens": 120,
            "dynamic_tokens": 30,
            "total_tokens": 150,
        },
        spawn_metadata={
            "spawn_group_id": "wave-1",
            "cache_scope_id": "scope-1",
            "requested_fields": ["prompt", "cache_scope_id", "spawn_group_id"],
        },
    )

    assert result.receipt.cache_capability == "explicit"
    assert result.receipt.spawn_group_id == "wave-1"
    assert result.receipt.cache_scope_id == "scope-1"
    assert result.receipt.eligible_for_reuse is True
    assert result.receipt.reuse_observed is True
    assert result.receipt.requested_fields == ("prompt", "cache_scope_id", "spawn_group_id")
    assert result.receipt.honored_fields == ("prompt", "cache_scope_id", "spawn_group_id")
