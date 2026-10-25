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
    def __init__(
        self,
        content: str,
        *,
        usage: dict[str, Any] | None = None,
        response_cost: float | None = None,
    ) -> None:
        self.choices = [_Choice(content)]
        self.usage = usage or {}
        self.id = "litellm-response"
        self._hidden_params = {} if response_cost is None else {"response_cost": response_cost}


class _FakeLiteLLM:
    def __init__(
        self,
        content: str,
        *,
        usage: dict[str, Any] | None = None,
        reject_response_format: bool = False,
        response_cost: float | None = None,
    ) -> None:
        self._content = content
        self._usage = usage or {}
        self._reject_response_format = reject_response_format
        self._response_cost = response_cost
        self.calls: list[dict[str, Any]] = []

    def completion(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if self._reject_response_format and "response_format" in kwargs:
            raise ValueError("response_format unsupported for this provider")
        return _Response(self._content, usage=self._usage, response_cost=self._response_cost)


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


def test_chat_with_result_preserves_provider_reported_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM(
        '{"ok": true}',
        usage={"prompt_tokens": 10, "completion_tokens": 4},
        response_cost=0.0125,
    )
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)

    result = litellm_client.chat_with_result(
        [{"role": "user", "content": "Return JSON"}],
        json_schema={"type": "object"},
    )

    assert result.provider_cost == pytest.approx(0.0125)
    assert result.currency == "USD"


# --------------------------------------------------------------------------- #
# tolerant JSON (R10): a model that wraps the JSON it was asked for still answers
# --------------------------------------------------------------------------- #


class _ScriptedLiteLLM(_FakeLiteLLM):
    """Answers each call with the next scripted reply (the last one repeats), each with its own usage and cost."""

    def __init__(self, replies: list[str], *, usage: dict[str, Any] | None = None, cost: float | None = None) -> None:
        super().__init__(replies[0], usage=usage, response_cost=cost)
        self._replies = replies
        self._served = 0

    def completion(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if self._reject_response_format and "response_format" in kwargs:
            raise ValueError("response_format unsupported for this provider")
        reply = self._replies[min(self._served, len(self._replies) - 1)]
        self._served += 1
        return _Response(reply, usage=self._usage, response_cost=self._response_cost)


def ask(monkeypatch: pytest.MonkeyPatch, fake: _FakeLiteLLM) -> dict[str, Any]:
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)
    payload = litellm_client.chat([{"role": "user", "content": "Return JSON"}], json_schema={"type": "object"})
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    "reply",
    [
        '```json\n{"status": "confirmed", "n": 2}\n```',
        '```\n{"status": "confirmed", "n": 2}\n```',
        '```JSON {"status": "confirmed", "n": 2}```',
        '  \n```json\n{"status": "confirmed", "n": 2}\n```\n\n',
        '```json\n{"status": "confirmed", "n": 2}\n',  # the closing fence never came
        'Here is the JSON you asked for:\n```json\n{"status": "confirmed", "n": 2}\n```\nLet me know if you need more.',
    ],
    ids=["json-fence", "bare-fence", "one-line-fence", "padded", "unclosed", "fence-in-prose"],
)
def test_fenced_json_is_read_without_a_second_call(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    fake = _FakeLiteLLM(reply)
    assert ask(monkeypatch, fake) == {"status": "confirmed", "n": 2}
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        'Sure! Here is my analysis: {"status": "confirmed", "n": 2} Hope that helps.',
        'Result follows.\n\n{"status": "confirmed", "n": 2}\n\nThat is all.',
        'Use {curly} braces and [1] footnotes freely: {"status": "confirmed", "n": 2}',
        'I considered {"a": } first, but the answer is {"status": "confirmed", "n": 2}',
        'The answer: {"status": "confirmed", "n": 2}} (stray brace)',
    ],
    ids=["around", "blank-lines", "braces-and-footnote-before", "broken-json-before", "stray-brace-after"],
)
def test_json_inside_prose_is_read_without_a_second_call(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    fake = _FakeLiteLLM(reply)
    assert ask(monkeypatch, fake) == {"status": "confirmed", "n": 2}
    assert len(fake.calls) == 1


def test_braces_inside_strings_do_not_confuse_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = 'Done: {"note": "use } and { carefully", "items": [{"id": 1}, {"id": 2}]} bye'
    assert ask(monkeypatch, _FakeLiteLLM(reply)) == {"note": "use } and { carefully", "items": [{"id": 1}, {"id": 2}]}


def test_a_list_is_taken_only_when_there_is_no_object(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ask(monkeypatch, _FakeLiteLLM("Findings: [1, 2, 3]")) == {"value": [1, 2, 3]}
    assert ask(monkeypatch, _FakeLiteLLM('[1] see {"ok": true}')) == {"ok": True}


def test_clean_json_is_one_call_and_parsed_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM('{"ok": true}')
    assert ask(monkeypatch, fake) == {"ok": True}
    assert len(fake.calls) == 1 and fake.calls[0]["response_format"] == {"type": "json_object"}


def test_a_reply_with_no_json_is_retried_once_with_an_explicit_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ScriptedLiteLLM(
        ["I am sorry, I cannot produce that in the requested format.", '{"ok": true}'],
        usage={"prompt_tokens": 100, "completion_tokens": 10},
        cost=0.01,
    )
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)
    original = [{"role": "user", "content": "Return JSON"}]
    result = litellm_client.chat_with_result(original, json_schema={"type": "object"})

    assert result.parsed_json == {"ok": True} and result.content == '{"ok": true}'
    assert len(fake.calls) == 2
    retry = fake.calls[1]["messages"]
    assert retry[:-1] == original and retry[-1]["role"] == "user"
    assert "only the JSON object" in retry[-1]["content"] and "no code fences" in retry[-1]["content"]
    assert original == [{"role": "user", "content": "Return JSON"}], "the caller's messages are not changed"
    # Both calls were made and paid for.
    assert (result.input_tokens, result.output_tokens) == (200, 20)
    assert result.provider_cost == pytest.approx(0.02)


def test_two_replies_with_no_json_fail_after_exactly_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _ScriptedLiteLLM(["no json here", "still no json"])
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)
    with pytest.raises(LiteLLMUnavailable, match="invalid JSON, also after one retry"):
        litellm_client.chat([{"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert len(fake.calls) == 2


def test_the_retry_does_not_ask_again_for_a_response_format_the_provider_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ScriptedLiteLLM(["prose only", '{"ok": true}'])
    fake._reject_response_format = True
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)
    assert litellm_client.chat([{"role": "user", "content": "x"}], json_schema={"type": "object"}) == {"ok": True}
    # try with the format (rejected), without it, then the retry without it: no third rejection.
    assert ["response_format" in call for call in fake.calls] == [True, False, False]


def test_a_reply_that_is_not_json_is_not_retried_when_no_json_was_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLiteLLM("just words")
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)
    assert litellm_client.chat([{"role": "user", "content": "hi"}]) == "just words"
    assert len(fake.calls) == 1


def test_streamed_json_gets_the_same_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Delta:
        def __init__(self, text: str) -> None:
            self.content = text

    class _StreamChoice:
        def __init__(self, text: str) -> None:
            self.delta = _Delta(text)

    class _Chunk:
        def __init__(self, text: str) -> None:
            self.choices = [_StreamChoice(text)]

    class _Streaming:
        def completion(self, **kwargs: Any) -> list[_Chunk]:
            return [_Chunk("Here you go:\n```json\n"), _Chunk('{"ok":'), _Chunk(" true}\n```")]

    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: _Streaming())
    seen: list[str] = []
    payload = litellm_client.chat_stream(
        [{"role": "user", "content": "x"}], on_delta=seen.append, json_schema={"type": "object"}
    )
    assert payload == {"ok": True} and len(seen) == 3


# --------------------------------------------------------------------------- #
# the review's model call: a rejected key is not retried, and the output is bounded
# --------------------------------------------------------------------------- #


class _AuthError(Exception):
    status_code = 401


class _RejectingLiteLLM(_FakeLiteLLM):
    """A provider that answers every request with an error."""

    def __init__(self, error: Exception) -> None:
        super().__init__("{}")
        self._error = error

    def completion(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        _AuthError("litellm.AuthenticationError: invalid x-api-key"),
        type("RateLimitError", (Exception,), {})("litellm.RateLimitError: Your credit balance is too low"),
        ValueError("Your credit balance is too low to access the Anthropic API"),
    ],
)
def test_a_rejected_key_or_an_empty_balance_is_asked_once_not_twice(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    fake = _RejectingLiteLLM(error)
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)

    with pytest.raises(LiteLLMUnavailable):
        litellm_client.chat_with_result([{"role": "user", "content": "x"}], json_schema={"type": "object"})

    assert len(fake.calls) == 1, "the retry without response_format cannot change a 401 and would be paid or counted"


def test_a_provider_that_rejects_the_response_format_is_still_retried_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLiteLLM('{"ok": true}', reject_response_format=True)
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: fake)

    result = litellm_client.chat_with_result([{"role": "user", "content": "x"}], json_schema={"type": "object"})

    assert result.parsed_json == {"ok": True} and len(fake.calls) == 2


def test_the_output_limit_is_bounded_by_default_by_the_environment_and_by_what_the_model_can_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Table:
        def __init__(self) -> None:
            self.model_cost = {"claude-small": {"max_output_tokens": 4096}, "claude-big": {"max_output_tokens": 128000}}

    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: Table())
    assert litellm_client.REVIEW_MAX_OUTPUT_TOKENS == 8192
    # LiteLLM sends the model's own maximum (128,000 for current Claude models) unless told otherwise.
    assert litellm_client.bounded_max_tokens("anthropic/claude-big", {}) == 8192
    assert litellm_client.bounded_max_tokens("anthropic/claude-small", {}) == 4096
    assert litellm_client.bounded_max_tokens("unknown-model", {}) == 8192
    assert (
        litellm_client.bounded_max_tokens("anthropic/claude-big", {"LEMONCROW_REVIEW_MAX_OUTPUT_TOKENS": "2000"})
        == 2000
    )
    assert (
        litellm_client.bounded_max_tokens("anthropic/claude-small", {"LEMONCROW_REVIEW_MAX_OUTPUT_TOKENS": "99999"})
        == 4096
    )
    assert (
        litellm_client.bounded_max_tokens("anthropic/claude-big", {"LEMONCROW_REVIEW_MAX_OUTPUT_TOKENS": "lots"})
        == 8192
    )
    assert (
        litellm_client.bounded_max_tokens("anthropic/claude-big", {"LEMONCROW_REVIEW_MAX_OUTPUT_TOKENS": "0"}) == 8192
    )


def test_the_review_route_sends_max_tokens_to_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import intelligence_runtime

    seen: dict[str, Any] = {}

    def fake_chat(messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        from lemoncrow.infra.internal_llm.result import InternalLLMChatResult

        seen.update(kwargs)
        return InternalLLMChatResult(content="{}", parsed_json={}, model="anthropic/x", reported_model="x")

    monkeypatch.setattr(litellm_client, "chat_with_result", fake_chat)
    monkeypatch.setattr(litellm_client, "bounded_max_tokens", lambda model, env=None: 4321)
    intelligence_runtime.execute_structured(
        system="s",
        prompt="p",
        output_schema={"type": "object"},
        driver="litellm",
        model="anthropic/x",
        origin="machine",
    )
    assert seen["extra_kwargs"] == {"max_tokens": 4321} and seen["model"] == "anthropic/x"
