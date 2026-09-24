from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from lemoncrow_server_core.bootstrap import build_local_server, ensure_local_token, read_local_token
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.index.memory import InMemoryAnalysisStore
from lemoncrow_server_core.index.search import IndexSearchDispatcher
from lemoncrow_server_core.local_app import LocalServerApp
from lemoncrow_server_core.local_mcp_gateway import build_gateway_app
from lemoncrow_server_core.protocol import PROTOCOL_VERSION, REQUIRED_CLIENT_CAPABILITIES

from lemoncrow.gateway.adapters.mcp_oauth import _OAuthStore, default_state_path
from lemoncrow.gateway.mcp_connectors import ConnectorBinding, connector_slug, save_connector


def test_public_loopback_server_runs_shared_engine(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path))


async def _exercise(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    assert not token_file.exists()
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            allow_local_fs=False,
        )
    )
    token = read_local_token(token_file)
    assert isinstance(_state.dispatcher, IndexSearchDispatcher)
    assert token.startswith("lc_local_")
    assert token_file.stat().st_mode & 0o077 == 0
    url = await server.start()
    try:
        async with ClientSession() as client:
            discovery = await client.get(f"{url}/.well-known/lemoncrow-server.json")
            assert discovery.status == 200
            discovery_body = await discovery.json()
            assert discovery_body["deployment"] == {"mode": "local"}
            assert discovery_body["product_capabilities"]["review"]["local_workspace"] is True
            assert discovery_body["product_capabilities"]["review"]["collaboration"] is False

            handshake = {
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                "client_name": "test",
                "client_version": "1",
            }
            response = await client.post(f"{url}/v1/handshake", json=handshake)
            assert response.status == 200

            unauthenticated = await client.post(f"{url}/v1/sessions", json=handshake)
            assert unauthenticated.status == 401

            headers = {"Authorization": f"Bearer {token}"}
            opened = await client.post(f"{url}/v1/sessions", json=handshake, headers=headers)
            assert opened.status == 200
            session = await opened.json()
            headers["X-LemonCrow-Session"] = session["session_id"]

            tools = await client.get(f"{url}/v1/tools", headers=headers)
            assert tools.status == 200
            payload = await tools.json()
            assert payload["dispatcher_available"] is True
            assert payload["tools"]
    finally:
        await server.stop()


def test_loopback_runtime_identity_accepts_only_safe_host_metadata() -> None:
    app = object.__new__(LocalServerApp)
    session = SimpleNamespace(session_id="server-session", client_name="lemoncrow-client")
    request = SimpleNamespace(
        headers={
            "X-LemonCrow-Host-Session": "01a0b52f-5187-7e11-a16b-000a4960428f",
            "X-LemonCrow-Host": "codex",
            "X-LemonCrow-Model": "gpt-5.6-sol",
        }
    )
    assert app._tool_runtime_identity(request, session) == (
        "01a0b52f-5187-7e11-a16b-000a4960428f",
        "codex",
        "gpt-5.6-sol",
    )

    malformed = SimpleNamespace(
        headers={
            "X-LemonCrow-Host-Session": "../../other-session",
            "X-LemonCrow-Host": "codex",
            "X-LemonCrow-Model": "gpt-5.6-sol",
        }
    )
    assert app._tool_runtime_identity(malformed, session) == (
        "server-session",
        "lemoncrow-client",
        "",
    )


def test_ephemeral_local_server_uses_memory_index(tmp_path: Path) -> None:
    state_root = tmp_path / "ephemeral-state"
    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=state_root,
            local_no_auth=True,
            allow_local_fs=False,
            ephemeral=True,
        )
    )

    assert isinstance(state.backend.analysis, InMemoryAnalysisStore)
    assert not (state_root / "index.sqlite3").exists()

    close_dispatcher = getattr(state.dispatcher, "close", None)
    if callable(close_dispatcher):
        close_dispatcher()
    if state.workspace is not None:
        state.workspace.close()
    del server


def test_machine_token_is_stable_across_restarts(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    first = ensure_local_token(token_file)
    second = ensure_local_token(token_file)
    assert second == first
    assert read_local_token(token_file) == first
    assert token_file.stat().st_mode & 0o077 == 0


def test_persistent_mcp_reuses_local_oauth_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asyncio.run(_exercise_persistent_mcp_reuses_local_oauth_credentials(monkeypatch, tmp_path))


async def _exercise_persistent_mcp_reuses_local_oauth_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_HOME", str(root))
    hostname = "lc-project.example.com"
    workspace = tmp_path / "project"
    workspace.mkdir()
    save_connector(ConnectorBinding(hostname, str(workspace)))

    store = _OAuthStore(default_state_path(connector_slug(hostname)))
    client_id = str(
        store.register_client(
            redirect_uris=["https://chatgpt.com/connector/oauth/test"],
            client_name="existing connector",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )["client_id"]
    )
    access_token, _refresh_token = store.issue_tokens(client_id)

    # Keep the gateway target stable across a full backend process restart.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        backend_port = int(reserved.getsockname()[1])

    config = LocalServerConfig(
        listen_port=backend_port,
        state_root=tmp_path / "state",
        token_file=root / "token",
        allow_local_fs=False,
    )
    first, _first_state = build_local_server(config)
    await first.start()
    first_running = True
    second = None

    _surface, gateway_app = build_gateway_app(backend_port=backend_port)
    runner = web.AppRunner(gateway_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    gateway_port = int(site._server.sockets[0].getsockname()[1])
    gateway_url = f"http://127.0.0.1:{gateway_port}"

    try:
        async with ClientSession() as client:
            authorize = await client.get(
                f"{gateway_url}/authorize",
                headers={"Host": hostname},
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "https://chatgpt.com/connector/oauth/test",
                    "code_challenge": "challenge",
                    "code_challenge_method": "S256",
                },
            )
            assert authorize.status == 200, await authorize.text()

            accepted = await client.get(
                f"{gateway_url}/mcp",
                headers={"Host": hostname, "Authorization": f"Bearer {access_token}"},
            )
            assert accepted.status == 405, await accepted.text()

        await first.stop()
        first_running = False

        # Backend 7420-equivalent is fully down. The independent thin client
        # gateway still executes client-routed tools locally.
        async with ClientSession() as client:
            local = await client.post(
                f"{gateway_url}/mcp",
                headers={"Host": hostname, "Authorization": f"Bearer {access_token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "bash", "arguments": {"command": "printf gateway-still-local"}},
                },
            )
            assert local.status == 200, await local.text()
            local_payload = await local.json()
            assert local_payload["result"]["isError"] is False
            assert "gateway-still-local" in local_payload["result"]["content"][0]["text"]

        second, _second_state = build_local_server(config)
        await second.start()

        # Same gateway process and OAuth credential survive the backend restart.
        async with ClientSession() as client:
            reused = await client.get(
                f"{gateway_url}/mcp",
                headers={"Host": hostname, "Authorization": f"Bearer {access_token}"},
            )
            assert reused.status == 405, await reused.text()
            metadata = await client.get(
                f"{gateway_url}/.well-known/oauth-protected-resource",
                headers={"Host": hostname, "X-Forwarded-Proto": "https"},
            )
            assert metadata.status == 200, await metadata.text()
            assert (await metadata.json())["authorization_servers"] == [f"https://{hostname}"]
    finally:
        if second is not None:
            await second.stop()
        if first_running:
            await first.stop()
        await runner.cleanup()


def test_machine_token_refuses_unsafe_permissions(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("lc_local_test_token_0123456789abcdef\n", encoding="utf-8")
    token_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="owner-only"):
        read_local_token(token_file)


def test_machine_token_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-token"
    real.write_text("lc_local_test_token_0123456789abcdef\n", encoding="utf-8")
    real.chmod(0o600)
    linked = tmp_path / "token"
    linked.symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        read_local_token(linked)


def test_review_browser_pairing_uses_review_only_origin_bound_capability(tmp_path: Path) -> None:
    asyncio.run(_exercise_review_browser_pairing(tmp_path))


async def _exercise_review_browser_pairing(tmp_path: Path) -> None:
    from lemoncrow_server_core.local_browser_auth import LOCAL_BROWSER_SESSION_HEADER

    token_file = tmp_path / "token"
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>review</html>\n", encoding="utf-8")
    (frontend / "favicon.svg").write_text("<svg></svg>\n", encoding="utf-8")
    (frontend / "site.webmanifest").write_text('{"name":"LemonCrow"}\n', encoding="utf-8")
    (frontend / "lemoncrow-card.png").write_bytes(b"png-card")
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            frontend_dir=frontend,
            allow_local_fs=False,
        )
    )
    machine_token = read_local_token(token_file)
    url = await server.start()
    review_path = "/r/00000000"
    machine_headers = {"Authorization": f"Bearer {machine_token}"}
    try:
        async with ClientSession() as client:
            # A clean Review URL contains no credential and does not itself grant
            # API access. Pairing is an authenticated CLI/server operation.
            opened = await client.get(f"{url}{review_path}")
            assert opened.status == 200
            favicon = await client.get(f"{url}/favicon.svg")
            assert favicon.status == 200
            assert "<svg" in await favicon.text()
            manifest = await client.get(f"{url}/site.webmanifest")
            assert manifest.status == 200
            assert "LemonCrow" in await manifest.text()
            social_card = await client.get(f"{url}/lemoncrow-card.png")
            assert social_card.status == 200
            assert await social_card.read() == b"png-card"
            historical = await client.get(f"{url}/rr/00000000")
            assert historical.status == 200
            assert "Set-Cookie" not in opened.headers
            assert machine_token not in str(opened.url)
            assert (await client.get(f"{url}/v1/reviews")).status == 401

            # The persistent local console lands on Home; Reviews remains a
            # directly reachable trusted same-origin surface.
            home = await client.get(f"{url}/")
            assert home.status == 200
            assert str(home.url).endswith("/home")
            directory_claim = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": "/reviews"},
                headers={"Origin": url, "Sec-Fetch-Site": "same-origin"},
            )
            assert directory_claim.status == 200
            directory_token = str((await directory_claim.json())["token"])
            directory_inbox = await client.get(
                f"{url}/v1/reviews",
                headers={LOCAL_BROWSER_SESSION_HEADER: directory_token},
            )
            assert directory_inbox.status == 200

            # The other persistent control-center pages are directly navigable
            # and bootstrap the same review/control-scoped browser capability.
            for console_path, api_path, expected_key in (
                ("/settings/integrations", "/api/local/integrations", "hosts"),
                ("/settings/diagnostics", "/api/local/system", "protocol_version"),
            ):
                console_page = await client.get(f"{url}{console_path}")
                assert console_page.status == 200
                console_claim = await client.post(
                    f"{url}/v1/auth/local-browser/claim",
                    json={"path": console_path},
                    headers={"Origin": url, "Sec-Fetch-Site": "same-origin"},
                )
                assert console_claim.status == 200
                console_token = str((await console_claim.json())["token"])
                console_response = await client.get(
                    f"{url}{api_path}",
                    headers={LOCAL_BROWSER_SESSION_HEADER: console_token},
                )
                assert console_response.status == 200
                console_payload = await console_response.json()
                assert expected_key in console_payload
                if console_path == "/settings/integrations":
                    assert [tool["name"] for tool in console_payload["tools"]] == [
                        "bash",
                        "code_search",
                        "edit",
                        "read",
                        "web_fetch",
                    ]
                    assert console_payload["internal_dispatchable_count"] >= len(console_payload["tools"])
                if console_path == "/settings/diagnostics":
                    assert console_payload["tool_count"] == 5
            paired = await client.post(
                f"{url}/v1/auth/local-browser/pair",
                json={"path": review_path},
                headers=machine_headers,
            )
            assert paired.status == 200
            assert (await paired.json())["state"] == "armed"

            claim_headers = {"Origin": url, "Sec-Fetch-Site": "same-origin"}
            claimed = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": review_path},
                headers=claim_headers,
            )
            assert claimed.status == 200
            browser_token = str((await claimed.json())["token"])
            assert browser_token.startswith("v1.")
            assert machine_token not in browser_token

            # One pairing window produces one browser capability.
            replay = await client.post(
                f"{url}/v1/auth/local-browser/claim",
                json={"path": review_path},
                headers=claim_headers,
            )
            assert replay.status == 401

            browser_headers = {LOCAL_BROWSER_SESSION_HEADER: browser_token}
            inbox = await client.get(f"{url}/v1/reviews", headers=browser_headers)
            assert inbox.status == 200
            inbox_payload = await inbox.json()
            assert set(inbox_payload) == {"reviews", "status", "query", "next_cursor"}
            assert "counts" not in inbox_payload
            assert "bucket" not in inbox_payload

            # The derived capability is Review-only, never a replacement for the
            # machine credential on session/tool APIs.
            handshake = {
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
                "client_name": "browser-test",
                "client_version": "1",
            }
            sessions = await client.post(f"{url}/v1/sessions", json=handshake, headers=browser_headers)
            assert sessions.status == 401

            # Mutations additionally require explicit same-origin browser context.
            no_origin = await client.post(
                f"{url}/api/reviews/r-00000000/finish",
                json={"status": "finished"},
                headers=browser_headers,
            )
            assert no_origin.status == 401
            same_origin = await client.post(
                f"{url}/api/reviews/r-00000000/finish",
                json={"status": "finished"},
                headers={**browser_headers, "Origin": url, "Sec-Fetch-Site": "same-origin"},
            )
            assert same_origin.status == 404

            # A reverse proxy can connect through loopback; any standard proxy
            # forwarding context therefore disqualifies both pairing and browser
            # capability use. Cover Cloudflare and generic reverse-proxy spellings
            # so adding a tunnel in front of 7420 never turns it into local trust.
            forwarding_headers = {
                "Forwarded": "for=198.51.100.25;proto=https;host=review.example.com",
                "X-Forwarded-For": "198.51.100.25",
                "X-Forwarded-Host": "review.example.com",
                "X-Forwarded-Proto": "https",
                "X-Real-IP": "198.51.100.25",
                "CF-Connecting-IP": "198.51.100.25",
                "True-Client-IP": "198.51.100.25",
            }
            for header_name, header_value in forwarding_headers.items():
                tunneled_pair = await client.post(
                    f"{url}/v1/auth/local-browser/pair",
                    json={"path": review_path},
                    headers={**machine_headers, header_name: header_value},
                )
                assert tunneled_pair.status == 403, header_name
                tunneled_inbox = await client.get(
                    f"{url}/v1/reviews",
                    headers={**browser_headers, header_name: header_value},
                )
                assert tunneled_inbox.status == 401, header_name
    finally:
        await server.stop()


def test_review_browser_capability_is_bound_to_host_and_port() -> None:
    from lemoncrow_server_core.local_browser_auth import LocalBrowserSessions

    now = [1000.0]
    sessions = LocalBrowserSessions("lc_local_test_token_0123456789abcdef", clock=lambda: now[0])
    sessions.arm("/reviews/r-12345678")
    token, _expires = sessions.claim("/reviews/r-12345678", "127.0.0.1:7420")
    assert sessions.verify(token, "127.0.0.1:7420")
    assert not sessions.verify(token, "127.0.0.1:9999")
    assert not sessions.verify(token, "localhost:7420")
    now[0] += 31 * 24 * 60 * 60
    assert not sessions.verify(token, "127.0.0.1:7420")
