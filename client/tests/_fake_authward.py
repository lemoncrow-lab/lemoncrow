"""A fake Authward, with hosted LemonCrow discovery, on 127.0.0.1 that keeps the multi-resource sign-in contract.

A sign-in names every resource by repeating ``resource`` at ``/device/code`` (one Authward does not know gets 403).
Every ``/oauth2/token`` request names exactly one of them: two, none, or one the sign-in did not name gets
``400 invalid_target`` and uses nothing up. Each refresh answers with a new refresh token that still covers every
named resource; sending a spent one ends the whole sign-in (``invalid_grant``), as Authward does outside its reuse
window. Access tokens are JWT-shaped (unsigned): ``aud`` is the resource plus the issuer's userinfo, ``sub`` is the same
for every resource, and each carries ``authward_resource_subject``.
"""

from __future__ import annotations

import base64
import itertools
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

API = "https://api.lemoncrow.com"
CLIENT_ID = "isolated-lemoncrow-cli"
SUBJECT = "person-0001"


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


@dataclass
class FakeAuthward:
    base: str = ""
    #: Resources Authward knows; the LemonCrow API's always.
    registered: set[str] = field(default_factory=lambda: {API})
    #: Seconds an access token lives.
    ttl: float = 900.0
    #: sign-in -> the resources it named.
    sign_ins: dict[str, tuple[str, ...]] = field(default_factory=dict)
    devices: dict[str, str] = field(default_factory=dict)
    #: sign-in -> its newest refresh token.
    newest: dict[str, str] = field(default_factory=dict)
    spent: dict[str, str] = field(default_factory=dict)
    ended: set[str] = field(default_factory=set)
    #: (path, the form's resource values) for every OAuth request, in order.
    requests: list[tuple[str, list[str]]] = field(default_factory=list)
    #: Refresh tokens in the order they were sent.
    refreshes: list[str] = field(default_factory=list)
    #: (resource, access token) in the order they were issued.
    issued: list[tuple[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    counter: Iterator[int] = field(default_factory=itertools.count)

    def access_token(self, resource: str) -> str:
        claims = {
            "iss": self.base,
            "sub": SUBJECT,
            "aud": [resource, f"{self.base}/oauth2/userinfo"],
            "exp": int(time.time() + self.ttl),
            "authward_resource_subject": f"{resource}#{SUBJECT}",
            "jti": f"access-{next(self.counter)}",
        }
        token = f"{_segment({'alg': 'EdDSA', 'typ': 'JWT'})}.{_segment(claims)}.c2lnbmF0dXJl"
        self.issued.append((resource, token))
        return token

    def _refresh_token(self, sign_in: str) -> str:
        token = f"refresh-{sign_in}-{next(self.counter)}"
        self.newest[sign_in] = token
        return token

    def answer(self, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        resources = form.get("resource", [])
        self.requests.append((path, list(resources)))
        if form.get("client_id") != [CLIENT_ID]:
            return 401, {"error": "invalid_client"}
        if path == "/device/code":
            if not resources or any(resource not in self.registered for resource in resources):
                return 403, {"error": "invalid_target"}
            sign_in = f"s{next(self.counter)}"
            self.sign_ins[sign_in] = tuple(resources)
            self.devices[f"device-{sign_in}"] = sign_in
            return 200, {
                "device_code": f"device-{sign_in}",
                "user_code": "ABCD-EFGH",
                "verification_uri": f"{self.base}/device",
                "expires_in": 60,
                "interval": 1,
            }
        if path != "/oauth2/token":
            return 404, {"error": "not_found"}
        if len(resources) != 1:
            return 400, {"error": "invalid_target"}
        (resource,) = resources
        grant = form.get("grant_type", [""])[0]
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            sign_in = self.devices.get(form.get("device_code", [""])[0], "")
            if not sign_in:
                return 400, {"error": "invalid_grant"}
            if resource not in self.sign_ins[sign_in]:
                return 400, {"error": "invalid_target"}
            del self.devices[f"device-{sign_in}"]
            return 200, {"access_token": self.access_token(resource), "refresh_token": self._refresh_token(sign_in)}
        if grant == "refresh_token":
            sent = form.get("refresh_token", [""])[0]
            self.refreshes.append(sent)
            if sent in self.spent:
                self.ended.add(self.spent[sent])  # reuse of a spent refresh token ends the whole sign-in
                return 400, {"error": "invalid_grant"}
            sign_in = next((name for name, token in self.newest.items() if token == sent), "")
            if not sign_in or sign_in in self.ended:
                return 400, {"error": "invalid_grant"}
            if resource not in self.sign_ins[sign_in]:
                return 400, {"error": "invalid_target"}
            self.spent[sent] = sign_in
            return 200, {"access_token": self.access_token(resource), "refresh_token": self._refresh_token(sign_in)}
        return 400, {"error": "unsupported_grant_type"}

    def discovery(self, path: str) -> dict[str, Any] | None:
        if path == "/.well-known/lemoncrow-server.json":
            authentication = {"authorization_server": self.base, "client_id": CLIENT_ID, "resource": API}
            return {"authentication": {**authentication, "scopes": ["openid", "offline_access", "lemoncrow:access"]}}
        if path == "/.well-known/openid-configuration":
            return {
                "issuer": self.base,
                "device_authorization_endpoint": f"{self.base}/device/code",
                "token_endpoint": f"{self.base}/oauth2/token",
            }
        return None


def _handler(state: FakeAuthward) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            body = state.discovery(self.path)
            if body is None:
                return self._reply(404, {"error": "not_found"})
            return self._reply(200, body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("ascii"), keep_blank_values=True)
            with state.lock:  # Authward's own state changes one request at a time
                status, body = state.answer(self.path, form)
            self._reply(status, body)

    return Handler


@contextmanager
def serve_authward() -> Iterator[FakeAuthward]:
    state = FakeAuthward()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
