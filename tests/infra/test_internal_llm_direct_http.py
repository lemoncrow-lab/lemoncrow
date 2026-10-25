"""The SDK-free model calls an installed Review engine falls back on: Anthropic Messages and Chat Completions.

Each test runs a real HTTP server on loopback, so the request a provider would receive (path, headers, body) and the
way every kind of reply is read are what is asserted, with no key and no network.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from lemoncrow.infra.internal_llm import direct_http
from lemoncrow.infra.internal_llm.exceptions import LiteLLMUnavailable, OpenAIClientUnavailable

Reply = tuple[int, dict[str, Any]]


class Provider:
    def __init__(self, answer: Callable[[str, dict[str, str], dict[str, Any]], Reply]) -> None:
        self.answer = answer
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.url = ""


@contextmanager
def provider(answer: Callable[[str, dict[str, str], dict[str, Any]], Reply]) -> Iterator[Provider]:
    state = Provider(answer)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            headers = {key.lower(): value for key, value in self.headers.items()}
            state.requests.append((self.path, headers, body))
            status, payload = state.answer(self.path, headers, body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def anthropic_reply(text: str, **usage: int) -> Reply:
    return 200, {
        "id": "msg_1",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 11, "output_tokens": 7, **usage},
    }


MESSAGES = [{"role": "system", "content": "You review code."}, {"role": "user", "content": "Is this safe?"}]


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "LEMONCROW_OPENAI_API_KEY",
        "LEMONCROW_OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_anthropic_gets_the_messages_request_the_api_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    with provider(lambda *_: anthropic_reply('{"verdict": "ok"}', cache_read_input_tokens=3)) as server:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url + "/")
        result = direct_http.anthropic_chat_with_result(
            MESSAGES, model="anthropic/claude-sonnet-4-6", json_schema={"type": "object"}, cache_metadata=None
        )
    [(path, headers, body)] = server.requests
    assert path == "/v1/messages"
    assert headers["x-api-key"] == "sk-ant-test" and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-sonnet-4-6"  # the routing prefix is not the API's
    assert body["messages"] == [{"role": "user", "content": "Is this safe?"}]
    assert body["system"][0] == {"type": "text", "text": "You review code."}
    assert "JSON object" in body["system"][-1]["text"]  # JSON mode is asked for in words
    assert result.parsed_json == {"verdict": "ok"} and result.model == "anthropic/claude-sonnet-4-6"
    assert (result.input_tokens, result.output_tokens, result.cache_read_input_tokens) == (11, 7, 3)
    assert result.reported_model == "claude-sonnet-4-6" and result.request_id == "msg_1"
    assert result.provider_cost is None  # no cost is invented for a provider that did not report one


def test_prose_around_the_json_is_read_and_a_reply_without_any_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter([anthropic_reply("I think so."), anthropic_reply('Sure:\n```json\n{"a": 1}\n```')])
    with provider(lambda *_: next(replies)) as server:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
        result = direct_http.anthropic_chat_with_result(MESSAGES, model="anthropic/m", json_schema={"type": "object"})
    assert result.parsed_json == {"a": 1}
    assert len(server.requests) == 2 and server.requests[1][2]["messages"][-1]["role"] == "user"
    assert (result.input_tokens, result.output_tokens) == (22, 14)  # both calls are paid for
    with provider(lambda *_: anthropic_reply("no json here")) as server:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
        with pytest.raises(LiteLLMUnavailable, match="invalid JSON"):
            direct_http.anthropic_chat_with_result(MESSAGES, model="anthropic/m", json_schema={"type": "object"})
    assert len(server.requests) == 2  # one retry, not a loop


def test_cache_control_blocks_the_review_adds_reach_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    blocks = [{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}]
    with provider(lambda *_: anthropic_reply("text")) as server:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
        result = direct_http.anthropic_chat_with_result(
            [{"role": "system", "content": blocks}, {"role": "user", "content": "hi"}],
            model="anthropic/m",
            json_schema=None,
            cache_metadata={"prompt_cache_key": "k1"},
        )
    assert server.requests[0][2]["system"] == blocks
    assert result.content == "text" and result.parsed_json is None and result.cache_capability == "explicit"


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (401, "invalid x-api-key", "rejected the API key (HTTP 401): check the key"),
        (429, "slow down", "rate limit or quota reached (HTTP 429)"),
        (500, "overloaded", "answered HTTP 500: overloaded"),
    ],
)
def test_an_error_from_anthropic_says_what_to_do_not_just_the_status(
    monkeypatch: pytest.MonkeyPatch, status: int, message: str, expected: str
) -> None:
    with provider(lambda *_: (status, {"error": {"message": message}})) as server:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
        with pytest.raises(LiteLLMUnavailable) as raised:
            direct_http.anthropic_chat_with_result(MESSAGES, model="anthropic/m", json_schema=None)
    assert expected in str(raised.value)


def test_an_unreachable_provider_is_a_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    with pytest.raises(LiteLLMUnavailable, match="could not reach Anthropic"):
        direct_http.anthropic_chat_with_result(MESSAGES, model="anthropic/m", json_schema=None, timeout=2)


def test_openai_compatible_chat_uses_json_mode_and_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = (
        200,
        {
            "id": "c1",
            "model": "gpt-4o-mini-2024",
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}},
        },
    )
    with provider(lambda *_: reply) as server:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("LEMONCROW_OPENAI_BASE_URL", server.url)
        result = direct_http.openai_chat_with_result(
            MESSAGES, model="gpt-4o-mini", json_schema={"type": "object"}, max_tokens=50, timeout=5
        )
    [(path, headers, body)] = server.requests
    assert path == "/chat/completions" and headers["authorization"] == "Bearer sk-test"
    assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 50
    assert result.parsed_json == {"ok": True}
    assert (result.input_tokens, result.output_tokens, result.cache_read_input_tokens) == (5, 2, 4)


def test_openai_with_no_key_and_no_custom_server_names_the_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(OpenAIClientUnavailable, match="OPENAI_API_KEY"):
        direct_http.openai_chat_with_result(MESSAGES, model="m", json_schema=None)
    with provider(lambda *_: (401, {"error": {"message": "bad key"}})) as server:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-bad")
        monkeypatch.setenv("LEMONCROW_OPENAI_BASE_URL", server.url)
        with pytest.raises(OpenAIClientUnavailable, match="rejected the API key"):
            direct_http.openai_chat_with_result(MESSAGES, model="m", json_schema=None)


def test_only_an_anthropic_model_with_a_key_takes_the_direct_route(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not direct_http.handles_anthropic("anthropic/claude")  # no key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert direct_http.handles_anthropic("anthropic/claude")
    assert not direct_http.handles_anthropic("gpt-4o-mini") and not direct_http.handles_anthropic("bedrock/claude")


def test_the_drivers_fall_back_here_only_when_their_sdk_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.infra.internal_llm import litellm_client, openai_client

    reached: list[str] = []
    monkeypatch.setattr(direct_http, "anthropic_chat_with_result", lambda *a, **k: reached.append("anthropic") or "A")
    monkeypatch.setattr(direct_http, "openai_chat_with_result", lambda *a, **k: reached.append("openai") or "O")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    def missing() -> Any:
        raise LiteLLMUnavailable("litellm package is not installed")

    monkeypatch.setattr(litellm_client, "_litellm_module", missing)
    assert litellm_client.chat_with_result(MESSAGES, model="anthropic/claude") == "A"
    with pytest.raises(LiteLLMUnavailable):  # another provider's model has no direct route
        litellm_client.chat_with_result(MESSAGES, model="bedrock/claude")
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a, **k: None if name == "openai" else real_find_spec(name, *a, **k)
    )
    assert openai_client.chat_with_result(MESSAGES, model="gpt-4o-mini") == "O"
    assert reached == ["anthropic", "openai"]
