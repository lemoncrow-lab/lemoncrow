"""``lc chatgpt serve`` request logging (always on) — JSONL entries.

Exercises ``RequestLogMiddleware`` mounted on the real OAuth app (the same
``add_middleware`` wiring ``serve`` uses) plus the CLI banner surface. Focus:
the single concrete log file ``chatgpt.py`` picks (named from the pairing
code slug, or a fixed name for ``--no-auth`` — never split per MCP session,
never the raw unsanitized pairing code), redaction guarantees (auth header,
credential endpoints), truncation, that SSE-framed responses (one-shot POST
and the GET keep-alive) are captured like any other body, and that the stream
itself is neither buffered nor broken.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from click.testing import CliRunner
from fastapi.testclient import TestClient

from lemoncrow.gateway.adapters.mcp_oauth import create_protected_mcp_app
from lemoncrow.gateway.cli.commands._request_log import RequestLogMiddleware, dated_log_dir
from lemoncrow.gateway.cli.commands.chatgpt import _pairing_code_log_slug, chatgpt_group

_PAIRING = "log-test-pair"
_REDIRECT = "https://chatgpt.example.com/cb"


def _logged_client(tmp_path: Path, *, log_path: Path | None = None) -> tuple[TestClient, Path]:
    app = create_protected_mcp_app(pairing_code=_PAIRING, state_path=tmp_path / "state.json")
    path = log_path if log_path is not None else tmp_path / "http.jsonl"
    app.add_middleware(RequestLogMiddleware, log_path=path)
    return TestClient(app), path


def _entries(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _get_token(client: TestClient) -> str:
    """Compact DCR + PKCE handshake; drives /register, /authorize, /token."""
    client_id = client.post("/register", json={"redirect_uris": [_REDIRECT]}).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    form = {
        "client_id": client_id,
        "redirect_uri": _REDIRECT,
        "response_type": "code",
        "state": "s",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "",
        "resource": "",
        "pairing_code": _PAIRING,
    }
    resp = client.post("/authorize", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    code = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)["code"][0]
    token_resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": _REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert token_resp.status_code == 200, token_resp.text
    return str(token_resp.json()["access_token"])


def test_mcp_post_logged_with_redacted_auth_header(tmp_path: Path) -> None:
    client, log_path = _logged_client(tmp_path)
    token = _get_token(client)
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200
    entry = [e for e in _entries(log_path) if e["path"] == "/mcp" and e["method"] == "POST"][-1]
    assert entry["status"] == 200
    assert '"initialize"' in entry["request_body"]  # full JSON-RPC request captured
    assert "serverInfo" in entry["response_body"]  # full JSON-RPC response captured
    assert entry["request_headers"]["authorization"] == "Bearer ***"
    assert token not in json.dumps(entry)  # plaintext token never reaches the log
    assert entry["duration_ms"] >= 0
    assert entry["ts"]
    assert entry["client"]


def test_different_mcp_sessions_share_one_file(tmp_path: Path) -> None:
    """No per-connection split anymore: two different ``Mcp-Session-Id``
    values are just two more lines in the same file."""
    client, log_path = _logged_client(tmp_path)
    token = _get_token(client)
    client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": "session-aaa"},
        json={"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}},
    )
    client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": "session-bbb"},
        json={"jsonrpc": "2.0", "id": 11, "method": "initialize", "params": {}},
    )
    mcp_entries = [e for e in _entries(log_path) if e["path"] == "/mcp" and e["method"] == "POST"]
    assert len(mcp_entries) == 2
    assert any('"id":10' in e["request_body"] for e in mcp_entries)
    assert any('"id":11' in e["request_body"] for e in mcp_entries)


def test_credential_endpoints_bodies_redacted(tmp_path: Path) -> None:
    client, log_path = _logged_client(tmp_path)
    token = _get_token(client)
    entries = _entries(log_path)

    token_entry = [e for e in entries if e["path"] == "/token" and e["method"] == "POST"][-1]
    assert token_entry["request_body"] == "[redacted: credential endpoint]"
    assert token_entry["response_body"] == "[redacted: credential endpoint]"

    authorize_entry = [e for e in entries if e["path"] == "/authorize" and e["method"] == "POST"][-1]
    assert authorize_entry["request_body"] == "[redacted: credential endpoint]"
    assert authorize_entry["response_body"] == "[redacted: credential endpoint]"

    # /register is deliberately NOT redacted (public client_id, no secrets).
    register_entry = [e for e in entries if e["path"] == "/register"][-1]
    assert "redirect_uris" in register_entry["request_body"]

    # Belt and braces: neither human secret nor minted token anywhere in the file.
    raw = log_path.read_text(encoding="utf-8")
    assert _PAIRING not in raw
    assert token not in raw


def test_large_body_truncated_with_marker(tmp_path: Path) -> None:
    client, log_path = _logged_client(tmp_path)
    token = _get_token(client)
    pad = "x" * (100 * 1024)  # ~100KB request, over the 64KB capture cap
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"pad": pad}},
    )
    assert resp.status_code == 200
    entry = [e for e in _entries(log_path) if e["path"] == "/mcp" and e["method"] == "POST"][-1]
    assert "…[truncated" in entry["request_body"]
    assert entry["request_body"].endswith("bytes]")
    assert len(entry["request_body"]) < 70_000  # capped, not the full 100KB


def test_post_mcp_sse_response_body_captured_not_stream_marker(tmp_path: Path) -> None:
    """ChatGPT's connector POSTs /mcp with ``Accept: text/event-stream``; the
    server replies with a one-shot SSE-framed JSON-RPC response. That reply is a
    normal bounded body and MUST be logged verbatim, not as ``"[stream]"`` — the
    exact blind spot the original bug closed."""
    client, log_path = _logged_client(tmp_path)
    token = _get_token(client)
    resp = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "accept": "text/event-stream",  # how ChatGPT calls it
        },
        json={"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200
    assert "serverInfo" in resp.text  # the SSE stream itself is untouched
    entry = [e for e in _entries(log_path) if e["path"] == "/mcp" and e["method"] == "POST"][-1]
    assert entry["response_body"] != "[stream]"
    assert "[stream]" not in entry["response_body"]
    assert "serverInfo" in entry["response_body"]  # real JSON-RPC result captured
    assert '"jsonrpc"' in entry["response_body"]
    assert '"id": 7' in entry["response_body"]  # the request's id echoed back


def test_get_mcp_heartbeat_body_captured_and_stream_still_works(tmp_path: Path) -> None:
    """The GET /mcp keep-alive is the one genuinely long-lived stream. Its tiny
    opening frame is captured (cheap, bounded) rather than blanked to
    ``"[stream]"`` — and the live stream still functions unchanged."""
    client, log_path = _logged_client(tmp_path)
    token = _get_token(client)
    resp = client.get(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert "mcp-stream-open" in resp.text  # the stream itself is untouched
    entry = [e for e in _entries(log_path) if e["path"] == "/mcp" and e["method"] == "GET"][-1]
    assert entry["response_body"] != "[stream]"
    assert "mcp-stream-open" in entry["response_body"]  # heartbeat frame captured


def test_large_response_body_truncated_without_ballooning(tmp_path: Path) -> None:
    """A response far larger than the cap is logged as a bounded prefix plus an
    accurate ``…[truncated N bytes]`` marker, and capture stops at the cap so the
    buffer never balloons even for a long/streamed response."""
    total = 200 * 1024  # ~200KB response, well over the 64KB cap
    chunk = b"y" * 1024

    async def _big_response(scope: Any, receive: Any, send: Any) -> None:
        # Drain the request so the ASGI cycle completes cleanly.
        more = True
        while more:
            message = await receive()
            more = bool(message.get("more_body"))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        sent = 0
        while sent < total:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
            sent += len(chunk)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    log_path = tmp_path / "http.jsonl"
    client = TestClient(RequestLogMiddleware(_big_response, log_path=log_path))
    resp = client.get("/big")
    assert resp.status_code == 200
    assert len(resp.content) == total  # the full body still reaches the client

    entry = _entries(log_path)[-1]
    body = entry["response_body"]
    assert "…[truncated" in body
    assert body.endswith("bytes]")
    assert f"truncated {total - 64 * 1024} bytes" in body  # accurate count off the true total
    assert len(body) < 70_000  # capped near 64KB, not the full 200KB — no balloon


def test_log_file_created_0600(tmp_path: Path) -> None:
    client, log_path = _logged_client(tmp_path)
    client.get("/.well-known/oauth-protected-resource")
    assert log_path.exists()
    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600


# ── _pairing_code_log_slug (pairing code -> filename stem) ─────────────────
def test_pairing_code_log_slug_is_hashed_not_raw() -> None:
    slug = _pairing_code_log_slug("Fona9KHBG4m-")
    assert slug != "Fona9KHBG4m-"  # never the raw secret
    assert re.fullmatch(r"[0-9a-f]{16}", slug)


def test_pairing_code_log_slug_is_deterministic_and_distinct_per_input() -> None:
    assert _pairing_code_log_slug("same-code") == _pairing_code_log_slug("same-code")
    assert _pairing_code_log_slug("code-a") != _pairing_code_log_slug("code-b")


def test_pairing_code_log_slug_handles_any_characters_uniformly() -> None:
    # Hashing (not sanitize-and-keep) means there is no separate
    # unsafe-character or empty-input branch to test: every input, however
    # exotic, maps to the same fixed-shape hex slug.
    for text in ("my code/with spaces", "héllo wörld", "a//b", "x" * 200, "!!!!!", ""):
        assert re.fullmatch(r"[0-9a-f]{16}", _pairing_code_log_slug(text))


# ── CLI wiring: pairing code / --no-auth -> concrete log file ───────────────
def test_serve_always_logs_and_prints_tail_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Logging is always on: plain ``serve`` (no extra flag) wires the middleware
    at a concrete file named from a hash of the pairing code, prints the exact
    singular tail hint (no glob, no per-session hedge), and the file exists
    (0600, empty) before any request, then gets a real entry on the first
    request."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    captured_apps: list[Any] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: captured_apps.append(self.config.app))
    result = CliRunner().invoke(chatgpt_group, ["serve", "--no-tunnel", "--pairing-code", "x"])
    assert result.exit_code == 0, result.output

    log_dir = tmp_path / ".lemoncrow" / "chatgpt" / "sessions"
    today_dir = dated_log_dir(log_dir)
    log_path = today_dir / f"{_pairing_code_log_slug('x')}.jsonl"

    assert f"Request log:   {log_path}" in result.output  # singular wording, exact path
    assert f"tail -f {log_path} | jq ." in result.output
    assert "*.jsonl" not in result.output
    assert "Request logs:" not in result.output
    assert "second terminal" in result.output

    # The CLI eagerly touched the file (0600, empty) before any traffic.
    assert log_path.exists()
    assert log_path.read_text(encoding="utf-8") == ""
    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600

    resp = TestClient(captured_apps[0]).get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    entries = _entries(log_path)
    assert entries[-1]["path"] == "/.well-known/oauth-protected-resource"
    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600


def test_serve_hashes_pairing_code_for_filename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The log filename is a hash of the pairing code — even one with
    filename-unsafe characters — never the raw code: a filename sits in a
    listable directory, so it must not expose the one secret gating shell
    access."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    captured_apps: list[Any] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: captured_apps.append(self.config.app))
    unsafe_code = "my code/with spaces"
    result = CliRunner().invoke(chatgpt_group, ["serve", "--no-tunnel", "--pairing-code", unsafe_code])
    assert result.exit_code == 0, result.output

    expected_slug = _pairing_code_log_slug(unsafe_code)
    log_dir = tmp_path / ".lemoncrow" / "chatgpt" / "sessions"
    log_path = dated_log_dir(log_dir) / f"{expected_slug}.jsonl"
    assert log_path.exists()
    assert f"tail -f {log_path} | jq ." in result.output

    # Neither the raw code nor anything but a hex hash appears as a filename.
    # The pairing code legitimately appears elsewhere in the banner (the
    # operator needs to read it) — only the log *path* lines must never carry
    # it.
    for child in dated_log_dir(log_dir).iterdir():
        assert child.name != f"{unsafe_code}.jsonl"
        assert re.fullmatch(r"[0-9a-f]{16}\.jsonl", child.name)
    log_lines = [line for line in result.output.splitlines() if "Request log" in line or "tail -f" in line]
    assert log_lines and all(unsafe_code not in line for line in log_lines)


def test_no_auth_serve_uses_fixed_filename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--no-auth`` has no pairing code to slug, so the log file is a fixed,
    predictable name."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    captured_apps: list[Any] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: captured_apps.append(self.config.app))
    result = CliRunner().invoke(chatgpt_group, ["serve", "--no-tunnel", "--no-auth"])
    assert result.exit_code == 0, result.output

    log_dir = tmp_path / ".lemoncrow" / "chatgpt" / "sessions"
    log_path = dated_log_dir(log_dir) / "no-auth.jsonl"

    assert f"tail -f {log_path} | jq ." in result.output
    assert "*.jsonl" not in result.output
    assert log_path.exists()
    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600
