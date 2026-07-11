from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("litellm", reason="lemoncrow[litellm] not installed")

from lemoncrow.infra.internal_llm import litellm_client
from lemoncrow.infra.internal_llm.exceptions import LiteLLMUnavailable


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)


class _Response:
    def __init__(self, content: str, *, usage: dict[str, Any] | None = None) -> None:
        self.choices = [_Choice(content)]
        self.usage = usage or {}
        self.id = "litellm-response"


class _FakeLiteLLM:
    def __init__(
        self,
        content: str,
        *,
        usage: dict[str, Any] | None = None,
        reject_response_format: bool = False,
    ) -> None:
        self._content = content
        self._usage = usage or {}
        self._reject_response_format = reject_response_format
        self.calls: list[dict[str, Any]] = []

    def completion(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if self._reject_response_format and "response_format" in kwargs:
            raise ValueError("response_format unsupported for this provider")
        return _Response(self._content, usage=self._usage)


def test_chat_json_schema_requests_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM('{"ok": true}')
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)

    payload = litellm_client.chat(
        [{"role": "user", "content": "Return JSON"}],
        json_schema={"type": "object"},
    )

    assert payload == {"ok": True}
    assert fake.calls[0]["response_format"] == {"type": "json_object"}


def test_chat_falls_back_when_response_format_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM('{"ok": false}', reject_response_format=True)
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)

    payload = litellm_client.chat(
        [{"role": "user", "content": "Return JSON"}],
        json_schema={"type": "object"},
    )

    assert payload == {"ok": False}
    # First call tried response_format; retry dropped it.
    assert "response_format" in fake.calls[0]
    assert "response_format" not in fake.calls[1]


def test_chat_plain_text_without_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM("hello world")
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)

    result = litellm_client.chat([{"role": "user", "content": "hi"}])
    assert result == "hello world"
    assert "response_format" not in fake.calls[0]


def test_resolve_model_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LITELLM_MODEL", "bedrock/anthropic.claude-3-5-sonnet")
    assert litellm_client._resolve_model(None) == "bedrock/anthropic.claude-3-5-sonnet"
    assert litellm_client._resolve_model("vertex_ai/gemini-1.5-pro") == "vertex_ai/gemini-1.5-pro"


def test_invalid_json_raises_litellm_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM("not json at all")
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)

    with pytest.raises(LiteLLMUnavailable):
        litellm_client.chat(
            [{"role": "user", "content": "x"}],
            json_schema={"type": "object"},
        )


def test_backend_dispatch_routes_to_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM('{"routed": true}')
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")

    from lemoncrow.infra import internal_llm

    payload = internal_llm.chat(
        [{"role": "user", "content": "hi"}],
        json_schema={"type": "object"},
    )
    assert payload == {"routed": True}


def test_chat_with_result_preserves_structured_messages_and_prompt_cache_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLiteLLM(
        '{"ok": true}',
        usage={
            "prompt_tokens": 1200,
            "completion_tokens": 42,
            "prompt_tokens_details": {"cached_tokens": 900},
            "cache_creation_input_tokens": 300,
        },
    )
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)
    messages = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "Long cached prefix",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "user", "content": "Use the cached prefix."},
    ]

    result = litellm_client.chat_with_result(messages, api_key="bedrock-token")

    assert fake.calls[0]["messages"] == messages
    assert fake.calls[0]["api_key"] == "bedrock-token"
    assert result.cache_read_input_tokens == 900
    assert result.cache_write_input_tokens == 300
