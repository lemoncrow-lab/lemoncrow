"""Shared browser OAuth login flow.

Extracted from the ``lc account login`` CLI command so it can also run from a
non-interactive context — e.g. a background daemon thread on the MCP server's
stdio startup path — where there is no click/tty and stdout is the JSON-RPC
channel (so it must never be written to directly).

Callers that want CLI-style terminal output (``lc account login``) should format
the returned ``OAuthLoginResult`` themselves; this module only logs via the
standard ``logging`` module.
"""

from __future__ import annotations

import http.server
import json
import logging
import platform
import socket
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

from lemoncrow.core.capabilities.licensing.store import (
    load_or_create_device_id,
    save_auth_base,
    save_auth_token,
    save_auth_user,
)


# mypyc doesn't support nested classes, so this lives at module level.
class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    received: dict[str, str]
    shutdown_event: threading.Event

    def __init__(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
        server: http.server.HTTPServer,
        *,
        received: dict[str, str],
        shutdown_event: threading.Event,
    ) -> None:
        self.received = received
        self.shutdown_event = shutdown_event
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/callback":
            qs = urllib.parse.parse_qs(parsed.query)
            self.received["token"] = qs.get("token", [""])[0]
            self.received["email"] = qs.get("email", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><p>Logged in. You can close this tab.</p>"
                b"<script>window.close()</script></body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()
        self.shutdown_event.set()

    def log_message(self, *args: object) -> None:
        pass  # suppress access log


_log = logging.getLogger("lemoncrow.licensing.oauth")


@dataclass
class OAuthLoginResult:
    token: str
    email: str
    plan: str
    plan_verified: bool
    device_id: str


def run_oauth_login(
    *,
    dev_mode: bool = False,
    timeout: float = 120.0,
    notify: Callable[[str], None] | None = None,
) -> OAuthLoginResult | None:
    """Run the OAuth browser flow and persist the returned session token.

    Best-effort: opens the default browser to ``<base>/account`` with a
    one-shot local callback server, waits up to ``timeout`` seconds, and
    returns ``None`` (never raises) if the user doesn't complete sign-in in
    time — callers should treat that as "not activated yet" and degrade
    gracefully rather than block indefinitely.

    ``notify`` receives human-readable status lines (browser URL, timeout
    notices). Defaults to logging via ``lemoncrow.licensing.oauth`` — pass a
    ``click.echo``-based callable for CLI-style terminal output.
    """

    def _notify(message: str) -> None:
        if notify is not None:
            notify(message)
        else:
            _log.info(message)

    base = "http://localhost:4321" if dev_mode else "https://lemoncrow.com"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    hostname = platform.node() or "cli"
    stable_device_id = load_or_create_device_id()
    cli_redirect = f"http://localhost:{port}/callback"
    oauth_url = (
        f"{base}/account"
        f"?cli_redirect={urllib.parse.quote(cli_redirect, safe='')}"
        f"&device_name={urllib.parse.quote(hostname, safe='')}"
        f"&stable_device_id={urllib.parse.quote(stable_device_id, safe='')}"
    )

    received: dict[str, str] = {}
    server_ready = threading.Event()
    shutdown_event = threading.Event()

    httpd = http.server.HTTPServer(
        ("127.0.0.1", port),
        lambda req, addr, srv: _OAuthCallbackHandler(
            req,
            addr,
            srv,
            received=received,
            shutdown_event=shutdown_event,
        ),
    )
    httpd.timeout = 1

    def _serve() -> None:
        server_ready.set()
        start = time.monotonic()
        while not shutdown_event.is_set() and time.monotonic() - start < timeout:
            httpd.handle_request()
        httpd.server_close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    server_ready.wait()

    _notify(f"Opening browser to sign in: {oauth_url}")
    opened = False
    try:
        opened = webbrowser.open(oauth_url)
    except Exception:  # noqa: BLE001
        opened = False
    if not opened:
        _notify(f"Could not open a browser automatically -- open manually: {oauth_url}")

    shutdown_event.wait(timeout=timeout)
    thread.join(timeout=5)

    session_token = received.get("token", "")
    email = received.get("email", "")
    if not session_token:
        _notify("Login timed out or was cancelled.")
        return None

    save_auth_token(session_token)

    plan = "free"
    plan_verified = False
    device_id = session_token[:8]
    try:
        from lemoncrow.core.capabilities.licensing.entitlements import USER_AGENT

        req = urllib.request.Request(
            f"{base}/api/auth/me",
            headers={"Authorization": f"Bearer {session_token}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data: dict[str, object] = json.loads(resp.read())
        plan = str(data.get("plan") or plan)
        plan_verified = True
        device_id = str(data.get("device_id") or device_id)
        save_auth_user({**data, "_base": base})
        save_auth_base(base)
    except Exception:  # noqa: BLE001
        pass

    return OAuthLoginResult(
        token=session_token,
        email=email,
        plan=plan,
        plan_verified=plan_verified,
        device_id=device_id,
    )
