"""WS11 G17 -- streamable-HTTP MCP transport + discovery manifest."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lemoncrow.gateway.adapters.mcp_http import (
    MCP_DISCOVERY_PATH,
    MCP_HTTP_PATH,
    create_mcp_http_app,
)


def _client() -> TestClient:
    return TestClient(create_mcp_http_app())


def test_discovery_manifest_served() -> None:
    resp = _client().get(MCP_DISCOVERY_PATH)
    assert resp.status_code == 200
    manifest = resp.json()
    assert manifest["transport"]["type"] == "streamable-http"
    assert manifest["transport"]["endpoint"] == MCP_HTTP_PATH
    assert isinstance(manifest["tools"], list) and manifest["tools"]


def test_tools_list_over_http() -> None:
    resp = _client().post(
        MCP_HTTP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "read" in names  # a public tool is advertised over HTTP too
    assert "scan" not in names  # hidden tools stay hidden across transports


def test_initialize_over_http() -> None:
    resp = _client().post(
        MCP_HTTP_PATH,
        json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"]


def test_parse_error_returns_jsonrpc_error() -> None:
    resp = _client().post(MCP_HTTP_PATH, content=b"not json")
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32700


def test_tools_call_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tools/call flows through the same dispatcher as stdio, so a real tool runs
    # end-to-end and returns the MCP content envelope over HTTP.
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "mod.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
    resp = _client().post(
        MCP_HTTP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "grep", "arguments": {"content_regex": "alpha", "path": "mod.py"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 3
    content = body["result"]["content"]
    assert isinstance(content, list) and content[0]["type"] == "text"


def test_tools_call_unknown_tool_returns_error() -> None:
    resp = _client().post(
        MCP_HTTP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "nope_not_a_tool", "arguments": {}},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32601


def test_sse_response_when_accept_event_stream() -> None:
    resp = _client().post(
        MCP_HTTP_PATH,
        headers={"accept": "text/event-stream"},
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "tools/list" not in resp.text  # method echoed only inside the JSON-RPC payload
    assert '"tools"' in resp.text and resp.text.startswith("data:")


# --------------------------------------------------------------------------- #
# C1 — /mcp is gated by the same auth dependency as /v1/*                        #
# --------------------------------------------------------------------------- #


def _authed_client() -> TestClient:
    """An app that mounts /mcp behind the real gateway auth dependency, exactly
    as production wires it via register_mcp_http(app, auth_dependency=...)."""
    from fastapi import FastAPI

    from lemoncrow.gateway.adapters.mcp_http import register_mcp_http
    from lemoncrow.gateway.openai_gateway.app import _require_auth

    app = FastAPI()
    register_mcp_http(app, auth_dependency=_require_auth)
    return TestClient(app)


def test_mcp_post_without_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_GATEWAY_TOKEN", "s3cret")
    resp = _authed_client().post(
        MCP_HTTP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert resp.status_code in (401, 403)  # no bearer token -> not reachable


def test_mcp_get_without_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_GATEWAY_TOKEN", "s3cret")
    resp = _authed_client().get(MCP_HTTP_PATH, headers={"accept": "text/event-stream"})
    assert resp.status_code in (401, 403)


def test_mcp_post_with_valid_token_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_GATEWAY_TOKEN", "s3cret")
    resp = _authed_client().post(
        MCP_HTTP_PATH,
        headers={"Authorization": "Bearer s3cret"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 200
    assert "read" in {t["name"] for t in resp.json()["result"]["tools"]}


def test_mcp_discovery_stays_public_even_when_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_GATEWAY_TOKEN", "s3cret")
    resp = _authed_client().get(MCP_DISCOVERY_PATH)  # no token at all
    assert resp.status_code == 200
    assert resp.json()["transport"]["type"] == "streamable-http"


# --------------------------------------------------------------------------- #
# H2 — request body is size-capped before parsing; dispatch runs off-loop       #
# --------------------------------------------------------------------------- #


def test_oversized_body_returns_413(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_HTTP_MAX_BODY_BYTES", "65536")  # 64 KiB floor
    big = "x" * 200_000
    payload = ('{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pad": "' + big + '"}}').encode("utf-8")
    resp = _client().post(
        MCP_HTTP_PATH,
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert "too large" in resp.text


def test_normal_body_still_dispatches_under_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_MCP_HTTP_MAX_BODY_BYTES", "65536")
    resp = _client().post(
        MCP_HTTP_PATH,
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 7


# --------------------------------------------------------------------------- #
# M2 — internal errors return a generic message, never the raw exception text   #
# --------------------------------------------------------------------------- #


def test_internal_error_does_not_leak_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    secret = "SECRET-PATH-/etc/lemoncrow/should-not-leak"

    def _boom(_request: object) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr(mcp_server, "_handle", _boom)
    resp = _client().post(
        MCP_HTTP_PATH,
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "read"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32603
    assert secret not in resp.text  # raw exception text must not reach the client
    assert "RuntimeError" not in resp.text
    assert "correlation_id=" in body["error"]["message"]  # operator can still trace it
