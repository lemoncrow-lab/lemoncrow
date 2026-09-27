"""A long client-side call must never make the persistent MCP gateway unreachable.

Regression: the gateway serialised every call for one OAuth client behind a lock
with a 24h admission wait, so one long ``bash`` (or a cancelled one) queued
``echo``, ``ping`` and ``tools/list`` behind it until the connector timed out.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, web
from lemoncrow_server_core.local_mcp_gateway import build_gateway_app
from lemoncrow_server_core.local_remote_mcp import _CACHES_PER_PROCESS, LocalRemoteMcpSurface, _lane_for

from lemoncrow.gateway.adapters.mcp_oauth import _OAuthStore, default_state_path
from lemoncrow.gateway.mcp_connectors import ConnectorBinding, connector_slug, save_connector

_LONG_SECONDS = 6
_PROMPT_SECONDS = 2.5


def _connector(tmp_path: Path, hostname: str) -> str:
    workspace = tmp_path / hostname
    workspace.mkdir()
    save_connector(ConnectorBinding(hostname, str(workspace)))
    store = _OAuthStore(default_state_path(connector_slug(hostname)))
    client_id = str(
        store.register_client(
            redirect_uris=["https://chatgpt.com/connector/oauth/test"],
            client_name=hostname,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )["client_id"]
    )
    access_token, _refresh = store.issue_tokens(client_id)
    return access_token


async def _rpc(
    client: ClientSession, url: str, host: str, token: str, method: str, params: dict[str, Any] | None = None
):
    started = time.monotonic()
    response = await client.post(
        f"{url}/mcp",
        headers={"Host": host, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    body = await response.json()
    return response.status, body, time.monotonic() - started


def _bash(command: str) -> dict[str, Any]:
    return {"name": "bash", "arguments": {"command": command}}


@pytest.mark.parametrize(
    ("body", "lane"),
    [
        ({"method": "ping"}, "control"),
        ({"method": "tools/list"}, "control"),
        ({"method": "notifications/cancelled"}, "control"),
        ({"method": "initialize"}, "serial"),
        ({"method": "tools/call", "params": {"name": "bash"}}, "client"),
        ({"method": "tools/call", "params": {"name": "grep"}}, "client"),
        ({"method": "tools/call", "params": {"name": "edit"}}, "serial"),  # pushes blobs to the session
        ({"method": "tools/call", "params": {"name": "code_search"}}, "serial"),
        ({"method": "tools/call", "params": {"name": "no-such-tool"}}, "serial"),
        ({"method": "tools/call", "params": "bash"}, "serial"),
        ({"method": ["ping"]}, "serial"),
        ({}, "serial"),
    ],
)
def test_only_stateless_calls_bypass_the_serial_lane(body: dict[str, Any], lane: str) -> None:
    assert _lane_for(body) == lane


def test_gateway_sessions_share_one_process_cache_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Regression: each session sized its cache from the whole machine, so a
    # gateway holding many sessions could grow to tens of GiB (73G observed).
    monkeypatch.setenv("LEMONCROW_RESOLUTION_CACHE_BYTES", "2G")
    surface = LocalRemoteMcpSurface(backend_port=7420)
    try:
        binding = ConnectorBinding("cache.example.test", str(tmp_path))
        per_session = surface._client_config(binding).resolution_cache_bytes
        assert 0 < per_session
        assert per_session * _CACHES_PER_PROCESS <= 2 * 1024**3
    finally:
        surface.close()


def test_bg_bash_runs_detached_from_the_request_and_is_polled_by_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asyncio.run(_exercise_bg(monkeypatch, tmp_path))


async def _exercise_bg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / ".lemoncrow"))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / ".lemoncrow"))
    host = "lc-bg.example.com"
    token = _connector(tmp_path, host)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        backend_port = int(reserved.getsockname()[1])
    _surface, app = build_gateway_app(backend_port=backend_port)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    url = f"http://127.0.0.1:{int(site._server.sockets[0].getsockname()[1])}"
    try:
        async with ClientSession() as client:
            status, started, took = await _rpc(
                client,
                url,
                host,
                token,
                "tools/call",
                {"name": "bash", "arguments": {"command": "sleep 2; printf bg-done", "bg": True}},
            )
            assert status == 200 and started["result"]["isError"] is False, started
            assert took < 1.5, "bg must answer before the command finishes"
            first = started["result"]["content"][0]["text"]
            run_id = first.split("id=", 1)[1].split()[0]

            status, done, _ = await _rpc(
                client, url, host, token, "tools/call", {"name": "bash", "arguments": {"id": run_id, "timeout": 30}}
            )
            assert status == 200 and "bg-done" in done["result"]["content"][0]["text"], done
    finally:
        await runner.cleanup()


def test_long_bash_does_not_block_other_calls_on_any_connector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asyncio.run(_exercise(monkeypatch, tmp_path))


async def _exercise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_HOME", str(root))
    host_a, host_b = "lc-a.example.com", "lc-b.example.com"
    token_a, token_b = _connector(tmp_path, host_a), _connector(tmp_path, host_b)
    small_file = tmp_path / "small.txt"
    small_file.write_text("tiny\n", encoding="utf-8")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        backend_port = int(reserved.getsockname()[1])  # nothing listens: the indexed backend is down

    _surface, app = build_gateway_app(backend_port=backend_port)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    url = f"http://127.0.0.1:{int(site._server.sockets[0].getsockname()[1])}"

    try:
        async with ClientSession() as client:
            # Warm the session first so the probes below measure queueing, not bootstrap.
            status, warm, _ = await _rpc(client, url, host_a, token_a, "tools/call", _bash("printf warm"))
            assert status == 200 and warm["result"]["isError"] is False

            long_call = asyncio.create_task(
                _rpc(client, url, host_a, token_a, "tools/call", _bash(f"sleep {_LONG_SECONDS}; printf long-done"))
            )
            await asyncio.sleep(0.5)
            assert not long_call.done()

            probes = {
                "echo": (host_a, token_a, "tools/call", _bash("printf alive")),
                "ping": (host_a, token_a, "ping", None),
                "tools/list": (host_a, token_a, "tools/list", None),
                "read": (host_a, token_a, "tools/call", {"name": "read", "arguments": {"files": [str(small_file)]}}),
                "other connector": (host_b, token_b, "tools/call", _bash("printf alive-b")),
            }
            results = await asyncio.gather(*(_rpc(client, url, *args) for args in probes.values()))
            for name, (status, body, elapsed) in zip(probes, results, strict=True):
                assert status == 200, (name, body)
                assert elapsed < _PROMPT_SECONDS, f"{name} queued behind the long call for {elapsed:.1f}s"
            assert "alive" in results[0][1]["result"]["content"][0]["text"]
            assert "alive-b" in results[4][1]["result"]["content"][0]["text"]
            assert not long_call.done(), "the long call must still be running while the probes returned"

            status, done, _ = await long_call
            assert status == 200 and "long-done" in done["result"]["content"][0]["text"]
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------- #
# project_id: routed to a registered project inside the connector's workspace  #
# --------------------------------------------------------------------------- #


def test_project_id_routes_inside_the_connector_workspace_and_never_silently_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asyncio.run(_exercise_projects(monkeypatch, tmp_path))


async def _exercise_projects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lemoncrow.core.service.project_registry import register_project

    root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_HOME", str(root))
    host = "lc-projects.example.com"
    token = _connector(tmp_path, host)
    workspace = tmp_path / host  # the connector's bound workspace
    inside = workspace / "app"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    inside_id = register_project(inside).project_id
    outside_id = register_project(outside).project_id

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        backend_port = int(reserved.getsockname()[1])
    _surface, app = build_gateway_app(backend_port=backend_port)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    url = f"http://127.0.0.1:{int(site._server.sockets[0].getsockname()[1])}"

    async def pwd(project_id: object | None = "unset") -> tuple[str, bool]:
        arguments: dict[str, Any] = {"command": "pwd"}
        if project_id != "unset":
            arguments["project_id"] = project_id
        async with ClientSession() as client:
            status, body, _ = await _rpc(
                client, url, host, token, "tools/call", {"name": "bash", "arguments": arguments}
            )
        assert status == 200, body
        return body["result"]["content"][0]["text"], bool(body["result"]["isError"])

    try:
        text, is_error = await pwd()
        assert str(workspace.resolve()) in text and not is_error

        text, is_error = await pwd(inside_id)
        assert str(inside.resolve()) in text and not is_error, text

        text, is_error = await pwd("")  # empty means the connector's own workspace
        assert str(workspace.resolve()) in text and not is_error

        text, is_error = await pwd("proj_00000000000000000000")
        assert is_error and "unknown registered project" in text

        text, is_error = await pwd(outside_id)
        assert is_error and "outside this connector's workspace" in text
        assert str(outside.resolve()) not in text.replace("outside this connector's workspace", "")

        text, is_error = await pwd(12345)
        assert is_error and "must be a string" in text
    finally:
        await runner.cleanup()
