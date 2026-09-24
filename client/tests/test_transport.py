"""The one module that opens a connection, tested for what it refuses.

Everything about egress in this package reduces to this file's behaviour, so
the tests are about the boundary rather than about HTTP: one origin, no
redirect, no inherited proxy, no global state, and a transport failure that
arrives as a typed answer rather than as an exception the caller has to guess
at.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.transport import HttpTransport


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        return

    def _reply(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self._reply(302, {}, {"Location": "http://elsewhere.invalid/x"})
            return
        if self.path == "/refuse":
            self._reply(
                403,
                {
                    "error": {
                        "code": "forbidden",
                        "message": "nope",
                        "retryable": False,
                        "action": "request_access",
                        "details": {"scope": "tools"},
                    }
                },
            )
            return
        if self.path == "/garbage":
            body = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/echo-headers":
            self._reply(200, {"headers": {key.lower(): value for key, value in self.headers.items()}})
            return
        self._reply(200, {"ok": True, "path": self.path})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("ascii") if length else ""
        self._reply(
            200,
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type", ""),
            },
        )


@pytest.fixture
def endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_successful_call_returns_the_decoded_payload(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    assert transport.get("/anything").require()["path"] == "/anything"


def test_oauth_form_post_is_urlencoded_and_origin_bound(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    payload = transport.post_form(
        "/oauth/token",
        form={"grant_type": "refresh_token", "scope": "openid email"},
    ).require()
    assert payload["path"] == "/oauth/token"
    assert payload["content_type"] == "application/x-www-form-urlencoded"
    assert payload["body"] == "grant_type=refresh_token&scope=openid+email"

    with pytest.raises(ClientError):
        transport.post_form("http://elsewhere.invalid/token", form={"token": "secret"})


def test_a_typed_refusal_is_raised_with_its_code_action_and_details(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    with pytest.raises(ClientError) as caught:
        transport.get("/refuse").require()
    assert caught.value.code is ErrorCode.FORBIDDEN
    assert caught.value.action.value == "request_access"
    assert caught.value.details == {"scope": "tools"}
    assert caught.value.retryable is False


def test_an_unknown_server_code_is_surfaced_rather_than_remapped(endpoint: str) -> None:
    """An unfamiliar refusal must stay visibly unfamiliar."""
    from lemoncrow_client.transport import Response

    error = ClientError.from_wire(
        {"error": {"code": "some_future_code", "message": "m", "retryable": True}}, status=400
    )
    assert error.code is ErrorCode.UNKNOWN
    assert error.server_code == "some_future_code"
    assert isinstance(Response(status=400, payload={}), Response)


def test_a_redirect_is_refused_rather_than_followed(endpoint: str) -> None:
    """A 302 is how a request to the endpoint becomes a request elsewhere."""
    transport = HttpTransport(endpoint, timeout_s=5.0)
    with pytest.raises(ClientError) as caught:
        transport.get("/redirect")
    assert caught.value.code is ErrorCode.NOT_CONFIGURED
    assert caught.value.details["status"] == 302
    assert caught.value.action.value == "abandon"


def test_a_request_to_another_origin_is_refused_before_it_is_sent(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    with pytest.raises(ClientError) as caught:
        transport.get("http://elsewhere.invalid/x")
    assert caught.value.code is ErrorCode.NOT_CONFIGURED
    assert caught.value.action.value == "abandon"


def test_a_closed_port_becomes_a_retryable_server_unreachable() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(ClientError) as caught:
        HttpTransport(f"http://127.0.0.1:{port}", timeout_s=2.0).get("/healthz")
    assert caught.value.code is ErrorCode.SERVER_UNREACHABLE
    assert caught.value.retryable is True


def test_a_server_that_never_answers_times_out_as_unreachable() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    try:
        with pytest.raises(ClientError) as caught:
            HttpTransport(f"http://127.0.0.1:{listener.getsockname()[1]}", timeout_s=0.4).get("/healthz")
    finally:
        listener.close()
    assert caught.value.code is ErrorCode.SERVER_UNREACHABLE


def test_a_non_json_answer_is_a_typed_error_not_a_crash(endpoint: str) -> None:
    with pytest.raises(ClientError) as caught:
        HttpTransport(endpoint, timeout_s=5.0).get("/garbage").require()
    assert caught.value.code is ErrorCode.INTERNAL


def test_the_transport_installs_no_global_opener_and_inherits_no_proxy(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment proxy the client never audited is not a destination."""
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    before = urllib.request._opener  # type: ignore[attr-defined]
    transport = HttpTransport(endpoint, timeout_s=5.0)
    assert transport.get("/anything").require()["ok"] is True
    assert urllib.request._opener is before  # type: ignore[attr-defined]


def test_only_the_headers_the_caller_asked_for_are_sent(endpoint: str) -> None:
    transport = HttpTransport(endpoint, timeout_s=5.0)
    payload = transport.get("/echo-headers", headers={"X-LemonCrow-Session": "s1"}).require()
    headers = payload["headers"]
    assert headers["x-lemoncrow-session"] == "s1"
    assert headers["user-agent"] == "lemoncrow-client"
    assert "cookie" not in headers
    assert "authorization" not in headers
