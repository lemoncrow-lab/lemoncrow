"""Integration tests for the Atelier OpenAI-compatible gateway.

These tests verify the HTTP surface (schemas, routing, streaming format) using
FastAPI's TestClient. They do NOT start a real Atelier runtime — the runtime is
mocked so tests run offline and quickly.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Minimal AtelierEvent stubs — avoids importing the full runtime
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
    return rt


_TEST_TOKEN = "test-gateway-token"


@pytest.fixture()
def client(mock_runtime, monkeypatch):
    """Return a TestClient wired to a mock runtime.

    The gateway gates /v1/* behind ATELIER_GATEWAY_TOKEN (the runtime auto-runs
    shell/edit tools), and TestClient is not a loopback client, so the token is
    set here and the client sends it by default.
    """
    monkeypatch.setenv("ATELIER_GATEWAY_TOKEN", _TEST_TOKEN)
    with patch(
        "atelier.gateway.openai_gateway.app.InteractiveRuntime",
        return_value=mock_runtime,
    ):
        from atelier.gateway.openai_gateway.app import create_app

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
            "model": "atelier-default",
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
    rt.handle_user_message = MagicMock(return_value=_stream(_Delta("tok1"), _Delta("tok2"), _Message("tok1tok2")))

    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "atelier-default",
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

    # Every data line (except [DONE]) must be valid JSON with choices
    for line in raw.splitlines():
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            obj = json.loads(line[6:])
            assert "choices" in obj, f"Missing choices in chunk: {line}"


def test_empty_messages(client):
    c, _ = client
    resp = c.post(
        "/v1/chat/completions",
        json={"model": "atelier-default", "messages": []},
    )
    assert resp.status_code == 422


def test_no_user_message(client):
    c, _ = client
    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "atelier-default",
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
            "model": "atelier-default",
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
    # When no token is set, only loopback clients may reach /v1/*. TestClient is
    # not loopback (host == "testclient"), so it must be rejected with 403.
    monkeypatch.delenv("ATELIER_GATEWAY_TOKEN", raising=False)
    with patch(
        "atelier.gateway.openai_gateway.app.InteractiveRuntime",
        return_value=mock_runtime,
    ):
        from atelier.gateway.openai_gateway.app import create_app

        app = create_app(project_root=None, yolo=True)
        with TestClient(app, raise_server_exceptions=True) as c:
            assert c.get("/v1/models").status_code == 403
