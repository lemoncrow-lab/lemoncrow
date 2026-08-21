"""Integration tests for the LemonCrow OpenAI-compatible gateway.

These tests verify the HTTP surface (schemas, routing, streaming format) using
FastAPI's TestClient. They do NOT start a real LemonCrow runtime — the runtime is
mocked so tests run offline and quickly.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Minimal LemonCrowEvent stubs — avoids importing the full runtime
# ---------------------------------------------------------------------------


class _Delta:
    type = "assistant.delta"

    def __init__(self, text: str) -> None:
        self.text = text


class _Message:
    type = "assistant.message"

    def __init__(self, text: str) -> None:
        self.text = text


class _Error:
    type = "error"

    def __init__(self, message: str) -> None:
        self.message = message


class _Usage:
    type = "context.usage.updated"
    input_tokens = 80
    cache_read_tokens = 20
    cache_write_tokens = 0
    output_tokens = 10


class _Route:
    type = "route.selected"

    def __init__(self, provider, model) -> None:
        self.provider = provider
        self.model = model
        self.reason = "test"


async def _stream(*events) -> AsyncIterator:
    for ev in events:
        yield ev


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_runtime():
    """Return a mock InteractiveRuntime that does NOT call the real LLM."""
    rt = MagicMock()
    rt.start_session = AsyncMock(return_value="test-session-id")
    rt.shutdown = MagicMock()
    rt._sessions = {}
    rt.restore_session = MagicMock(
        side_effect=lambda session_id, messages: rt._sessions.__setitem__(
            session_id,
            list(messages),
        )
    )
    rt.drop_session = MagicMock(side_effect=lambda session_id: rt._sessions.pop(session_id, None))
    return rt


_TEST_TOKEN = "test-gateway-token"


@pytest.fixture()
def client(mock_runtime, monkeypatch):
    """Return a TestClient wired to a mock runtime.

    The gateway gates /v1/* behind LEMONCROW_GATEWAY_TOKEN (the runtime auto-runs
    shell/edit tools), and TestClient is not a loopback client, so the token is
    set here and the client sends it by default.
    """
    monkeypatch.setenv("LEMONCROW_GATEWAY_TOKEN", _TEST_TOKEN)
    with patch(
        "lemoncrow.gateway.openai_gateway.app.InteractiveRuntime",
        return_value=mock_runtime,
    ):
        from lemoncrow.gateway.openai_gateway.app import create_app

        app = create_app(project_root=None, yolo=True)
        with TestClient(app, raise_server_exceptions=True) as c:
            c.headers["Authorization"] = f"Bearer {_TEST_TOKEN}"
            yield c, mock_runtime


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health(client):
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_models(client):
    c, _ = client
    resp = c.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    # List may be empty if no API keys are set; when populated, all entries must have an id
    for m in data["data"]:
        assert m["id"]


def test_chat_nonstreaming(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("Hello"), _Message("Hello world")))

    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "lemoncrow-default",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    content = body["choices"][0]["message"]["content"]
    assert "Hello" in content


def test_chat_streaming(client):
    c, rt = client
    rt.handle_user_message = MagicMock(
        return_value=_stream(
            _Delta("tok1"),
            _Delta("tok2"),
            _Message("tok1tok2"),
            _Usage(),
        )
    )
    rt.drop_session.reset_mock()

    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "lemoncrow-default",
            "messages": [{"role": "user", "content": "stream test"}],
            "stream": True,
        },
        headers={"Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    raw = resp.text
    assert "data: " in raw
    assert "[DONE]" in raw
    assert '"total_tokens":110' in raw, raw
    rt.drop_session.assert_called_once()

    # Every data line (except [DONE]) must be valid JSON with choices
    for line in raw.splitlines():
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            obj = json.loads(line[6:])
            assert "choices" in obj, f"Missing choices in chunk: {line}"


def test_chat_nonstreaming_surfaces_real_route_model(client):
    """The response's `model` must be the real backend the runtime routed to
    (e.g. a Zen or local model), never the virtual "lemoncrow" model the
    client requested -- that echo was the reported "blind" model choice."""
    c, rt = client
    rt.handle_user_message = MagicMock(
        return_value=_stream(_Route("zen", "big-pickle"), _Delta("Hello"), _Message("Hello world"))
    )

    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "lemoncrow-default",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "zen/big-pickle"


def test_chat_streaming_surfaces_real_route_model(client):
    c, rt = client
    rt.handle_user_message = MagicMock(
        return_value=_stream(_Route("ollama", "llama3"), _Delta("tok1"), _Message("tok1"), _Usage())
    )

    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "lemoncrow-default",
            "messages": [{"role": "user", "content": "stream test"}],
            "stream": True,
        },
        headers={"Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    raw = resp.text
    models_seen = {
        json.loads(line[6:])["model"]
        for line in raw.splitlines()
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    }
    assert models_seen == {"ollama/llama3"}


def test_empty_messages(client):
    c, _ = client
    resp = c.post(
        "/v1/chat/completions",
        json={"model": "lemoncrow-default", "messages": []},
    )
    assert resp.status_code == 422


def test_no_user_message(client):
    c, _ = client
    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "lemoncrow-default",
            "messages": [{"role": "system", "content": "You are helpful."}],
        },
    )
    assert resp.status_code == 422


def test_error_event_in_stream(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Error("something went wrong")))

    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "lemoncrow-default",
            "messages": [{"role": "user", "content": "trigger error"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    raw = resp.text
    assert "error" in raw.lower()
    assert "[DONE]" in raw


def test_health_needs_no_auth(client):
    c, _ = client
    resp = c.get("/health", headers={"Authorization": ""})
    assert resp.status_code == 200


def test_v1_rejects_missing_token(client):
    c, _ = client
    resp = c.get("/v1/models", headers={"Authorization": ""})
    assert resp.status_code == 401


def test_v1_rejects_wrong_token(client):
    c, _ = client
    resp = c.get("/v1/models", headers={"Authorization": "Bearer not-the-token"})
    assert resp.status_code == 401


def test_models_refresh_is_post(client):
    c, _ = client
    # GET is no longer allowed on the state-mutating refresh route
    assert c.get("/v1/models/refresh").status_code == 405
    assert c.post("/v1/models/refresh").status_code == 200


def test_v1_blocks_non_loopback_without_token(mock_runtime, monkeypatch):
    monkeypatch.delenv("LEMONCROW_GATEWAY_TOKEN", raising=False)
    with patch(
        "lemoncrow.gateway.openai_gateway.app.InteractiveRuntime",
        return_value=mock_runtime,
    ):
        from lemoncrow.gateway.openai_gateway.app import create_app

        app = create_app(project_root=None, yolo=True)
        with TestClient(app, raise_server_exceptions=True) as c:
            assert c.get("/v1/models").status_code == 403


def test_chat_decodes_whole_string_host_envelope(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("ok"), _Message("ok")))

    response = c.post(
        "/v1/chat/completions",
        json={
            "model": "lc/lemoncrow",
            "messages": [{"role": "user", "content": json.dumps("do it")}],
            "stream": False,
        },
    )

    assert response.status_code == 200
    assert rt.handle_user_message.call_args.args[1] == "do it"


def test_chat_preserves_multimodal_user_content_for_owned_runtime(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("ok"), _Message("ok")))
    prior_image = "data:image/png;base64,prior"
    current_image = "data:image/png;base64,current"

    response = c.post(
        "/v1/chat/completions",
        json={
            "model": "openai/gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "earlier"},
                        {"type": "image_url", "image_url": {"url": prior_image}},
                    ],
                },
                {"role": "assistant", "content": "noted"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what changed?"},
                        {"type": "image_url", "image_url": {"url": current_image, "detail": "high"}},
                        {"type": "host_private", "payload": "drop-me"},
                    ],
                },
            ],
            "stream": False,
        },
    )

    assert response.status_code == 200
    restored = rt.restore_session.call_args.args[1]
    assert restored[0]["content"] == [
        {"type": "text", "text": "earlier"},
        {"type": "image_url", "image_url": {"url": prior_image}},
    ]
    assert restored[1] == {"role": "assistant", "content": "noted"}
    kwargs = rt.handle_user_message.call_args.kwargs
    assert kwargs["user_content"] == [
        {"type": "text", "text": "what changed?"},
        {"type": "image_url", "image_url": {"url": current_image, "detail": "high"}},
    ]
    assert rt.handle_user_message.call_args.args[1] == "what changed?"


def test_virtual_model_drops_host_system_prompt(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("ok"), _Message("ok")))

    response = c.post(
        "/v1/chat/completions",
        json={
            "model": "lc/lemoncrow",
            "messages": [
                {"role": "system", "content": "very large host prompt"},
                {"role": "user", "content": "do it"},
            ],
            "stream": False,
            "max_tokens": 9999,
        },
    )

    assert response.status_code == 200
    restored = rt.restore_session.call_args.args[1]
    assert restored == []
    kwargs = rt.handle_user_message.call_args.kwargs
    assert kwargs["model_override"] is None
    assert kwargs["max_output_tokens"] == 9999


def test_responses_api_drops_codex_host_scaffolding(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("frontend-ok"), _Message("frontend-ok"), _Usage()))

    response = c.post(
        "/v1/responses",
        json={
            "model": "lemoncrow",
            "instructions": "twenty kilobytes of Codex host instructions",
            "input": [
                {"type": "message", "role": "developer", "content": "host policy"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "# AGENTS.md instructions for /repo\n...\n"
                            "<environment_context>host context</environment_context>",
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "do the work"}],
                },
            ],
            "stream": False,
            "max_output_tokens": 4321,
            "tools": [{"type": "function", "name": "exec_command"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "frontend-ok"
    assert body["usage"]["total_tokens"] == 110
    assert rt.restore_session.call_args.args[1] == []
    kwargs = rt.handle_user_message.call_args.kwargs
    assert rt.handle_user_message.call_args.args[1] == "do the work"
    assert kwargs["model_override"] is None
    assert kwargs["max_output_tokens"] == 4321


def test_responses_api_streams_official_lifecycle(client):
    c, rt = client
    rt.handle_user_message = MagicMock(
        return_value=_stream(_Delta("front"), _Delta("end-ok"), _Message("frontend-ok"), _Usage())
    )
    rt.drop_session.reset_mock()

    response = c.post(
        "/v1/responses",
        json={
            "model": "lemoncrow",
            "input": "say frontend-ok",
            "stream": True,
        },
        headers={"Accept": "text/event-stream"},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    event_types = []
    payloads = []
    for line in response.text.splitlines():
        if line.startswith("event: "):
            event_types.append(line[7:])
        elif line.startswith("data: "):
            payloads.append(json.loads(line[6:]))
    assert event_types[0] == "response.created"
    assert "response.output_text.delta" in event_types
    assert event_types[-1] == "response.completed"
    assert [item["sequence_number"] for item in payloads] == list(range(len(payloads)))
    completed = payloads[-1]["response"]
    assert completed["output"][0]["content"][0]["text"] == "frontend-ok"
    assert completed["usage"]["total_tokens"] == 110
    rt.drop_session.assert_called_once()


def test_anthropic_messages_supports_claude_code_headers(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("hello"), _Message("hello"), _Usage()))

    response = c.post(
        "/v1/messages",
        headers={"Authorization": "", "x-api-key": _TEST_TOKEN},
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": "<system-reminder>host-only current date</system-reminder>\n\n hi",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert rt.handle_user_message.call_args.args[1] == "hi"
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "hello"
    assert body["usage"] == {"input_tokens": 100, "output_tokens": 10}


def test_anthropic_stream_and_count_tokens(client):
    c, rt = client
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("hello"), _Message("hello"), _Usage()))

    stream = c.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 4096,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert stream.status_code == 200
    assert "event: message_start" in stream.text
    assert "event: content_block_delta" in stream.text
    assert "event: message_stop" in stream.text

    count = c.post(
        "/v1/messages/count_tokens",
        json={
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert count.status_code == 200
    assert count.json()["input_tokens"] > 0
