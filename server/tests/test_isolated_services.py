"""Review and Dashboard as worker processes behind the core.

The core is the only public listener; it authenticates, then forwards to the
service that owns the route. The point of the split is that a service burning
CPU (the dashboard decoding the whole trace history) never stalls the core's
coding-agent lane, which shared its GIL before.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import os
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core.bootstrap import build_local_server, read_local_token
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.core_client import CoreClient, RemoteCodeIntelligence
from lemoncrow_server_core.errors import ErrorCode
from lemoncrow_server_core.index.repositories import RepositoryIndex
from lemoncrow_server_core.local_app import LocalServerApp
from lemoncrow_server_core.service_process import ServiceProcess, service_command
from lemoncrow_server_core.service_protocol import (
    HEADER_PRINCIPAL,
    HEADER_SERVICE_TOKEN,
    SERVICE_DASHBOARD,
    SERVICE_REVIEW,
    decode_principal,
    jsonable,
)
from lemoncrow_server_core.service_proxy import LocalServices
from test_review_code_intelligence import CAP, _open, _provider

# A stand-in worker: announces READY, echoes what reached it, streams on a
# gate file, records a client that went away, and exits when stdin closes.
_ECHO_WORKER = r"""
import asyncio, os, sys, threading
from pathlib import Path
from aiohttp import web

async def echo(request):
    return web.json_response({
        "pid": os.getpid(),
        "path": request.path_qs,
        "headers": {k.lower(): v for k, v in request.headers.items()},
        "body": (await request.read()).decode(),
    })

async def stream(request):
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    await response.write(b"first\n")
    gate = Path(request.query["gate"])
    while not gate.exists():
        if request.transport is None or request.transport.is_closing():
            Path(request.query["gone"]).write_text("gone")
            return response
        await asyncio.sleep(0.02)
    await response.write(b"second\n")
    await response.write_eof()
    return response

async def main():
    app = web.Application()
    app.router.add_route("*", "/api/stream", stream)
    app.router.add_route("*", "/{tail:.*}", echo)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    print(f"READY http://127.0.0.1:{runner.addresses[0][1]}", flush=True)
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    def watch():
        while sys.stdin.buffer.read(4096):
            pass
        loop.call_soon_threadsafe(done.set)
    threading.Thread(target=watch, daemon=True).start()
    await done.wait()
    await runner.cleanup()

asyncio.run(main())
"""

# The dashboard stand-in for the real service host (`--app`). `/burn` holds the
# GIL inside single C calls, as pydantic decoding the trace history did.
_DASHBOARD_APP = r"""
import os, re, time
from fastapi import FastAPI, Response

def _hold_gil(seconds):
    n = 14
    while True:
        started = time.perf_counter()
        re.match(r"(a+)+$", "a" * n + "b")
        if (time.perf_counter() - started) * 2 > seconds:
            return n
        n += 1

def create():
    app = FastAPI()

    @app.get("/burn")
    async def burn(seconds: float = 1.0):
        return {"pid": os.getpid(), "n": _hold_gil(seconds)}

    @app.get("/hello")
    async def hello():
        return {"pid": os.getpid()}

    @app.get("/cookies")
    async def cookies():
        response = Response("ok")
        response.set_cookie("alpha", "1")
        response.set_cookie("beta", "2")
        return response

    return app
"""


def _echo_services() -> LocalServices:
    return LocalServices(
        ServiceProcess(name, (sys.executable, "-c", _ECHO_WORKER)) for name in (SERVICE_REVIEW, SERVICE_DASHBOARD)
    )


def _dashboard_host(tmp_path: Path) -> ServiceProcess:
    """The real service host, serving the stand-in dashboard app."""

    (tmp_path / "isolation_apps.py").write_text(_DASHBOARD_APP, encoding="utf-8")
    python_path = os.pathsep.join(filter(None, (str(tmp_path), os.environ.get("PYTHONPATH", ""))))
    command = service_command(
        SERVICE_DASHBOARD,
        extra=(
            "--state-root",
            str(tmp_path / "state"),
            "--maintenance-interval-s",
            "0",
            "--app",
            "isolation_apps:create",
        ),
    )
    return ServiceProcess(SERVICE_DASHBOARD, command, env={"PYTHONPATH": python_path})


@contextlib.asynccontextmanager
async def _core(
    tmp_path: Path, services: LocalServices | None = None, *, isolate: bool = False
) -> AsyncIterator[tuple[str, LocalServerApp, dict[str, str]]]:
    token_file = tmp_path / "token"
    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            token_file=token_file,
            allow_local_fs=False,
            isolate_services=isolate,
        )
    )
    if services is not None:
        state.services = services
    url = await server.start()
    try:
        yield url, state, {"Authorization": f"Bearer {read_local_token(token_file)}"}
    finally:
        await server.stop()


async def _ready(services: LocalServices, names: Iterable[str] | None = None) -> None:
    for name in names or services.names():
        await services.processes[name].endpoint(timeout_s=60)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_forwarded_request_carries_the_principal_and_never_the_credentials(tmp_path: Path) -> None:
    async def exercise() -> None:
        services = _echo_services()
        async with _core(tmp_path, services) as (url, state, auth), ClientSession() as client:
            await _ready(services)
            forged = {HEADER_PRINCIPAL: json.dumps({"subject": "mallory"}), HEADER_SERVICE_TOKEN: "forged"}
            response = await client.get(
                f"{url}/api/traces?limit=3",
                headers={**auth, **forged, "Cookie": "session=abc", "X-Custom": "kept"},
            )
            assert response.status == 200
            seen = await response.json()
            headers = seen["headers"]
            assert seen["path"] == "/api/traces?limit=3"
            assert "authorization" not in headers
            assert "cookie" not in headers
            assert headers["x-custom"] == "kept"
            assert headers["host"] == urlsplit(url).netloc
            assert headers["x-lemoncrow-request-id"]
            # The core's own token and principal replace anything the caller sent.
            assert headers[HEADER_SERVICE_TOKEN.lower()] == services.processes[SERVICE_DASHBOARD].token()
            principal = decode_principal(headers[HEADER_PRINCIPAL.lower()])
            assert principal is not None
            assert principal.subject == state.local_principal.subject

            # Each route reaches the service that owns it, with its body intact.
            review_token = services.processes[SERVICE_REVIEW].token()
            for method, path, body in (
                ("GET", "/api/reviews/rev_1", ""),
                ("GET", "/v1/reviews", ""),
                ("PUT", "/api/repos/repo_1/reviews/rev_1/title", '{"title": "t"}'),
            ):
                answered = await client.request(method, f"{url}{path}", headers=auth, data=body or None)
                assert answered.status == 200, path
                echoed = await answered.json()
                assert echoed["headers"][HEADER_SERVICE_TOKEN.lower()] == review_token, path
                assert echoed["body"] == body

            # Unauthenticated requests stop at the core.
            refused = await client.get(f"{url}/api/traces")
            assert refused.status == 401

    asyncio.run(exercise())


def test_a_body_the_agent_guard_read_is_forwarded_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_server_core import local_app

    monkeypatch.setattr(local_app, "peer_agent_host", lambda _transport: "claude")
    body = json.dumps({"finding_disposition": "fixed", "actor": "agent", "agent_host": "claude"})

    async def exercise() -> None:
        services = _echo_services()
        async with _core(tmp_path, services) as (url, _state, auth), ClientSession() as client:
            await _ready(services)
            answered = await client.patch(f"{url}/api/annotations/ann-1", headers=auth, data=body)
            assert answered.status == 200, await answered.text()
            assert (await answered.json())["body"] == body

    asyncio.run(exercise())


def test_a_streamed_response_arrives_chunk_by_chunk_and_a_client_that_leaves_is_felt_upstream(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        services = _echo_services()
        gate, gone = tmp_path / "gate", tmp_path / "gone"
        async with _core(tmp_path, services) as (url, _state, auth), ClientSession() as client:
            await _ready(services)
            stream_url = f"{url}/api/stream?gate={gate}&gone={gone}"
            streamed = await asyncio.wait_for(client.post(stream_url, headers=auth, json={}), 10)
            assert streamed.status == 200
            assert streamed.headers["Content-Type"].startswith("text/event-stream")
            assert await asyncio.wait_for(streamed.content.readline(), 5) == b"first\n"
            gate.write_text("open")
            assert await asyncio.wait_for(streamed.content.read(), 5) == b"second\n"

            gate.unlink()
            abandoned = await asyncio.wait_for(client.post(stream_url, headers=auth, json={}), 10)
            assert await asyncio.wait_for(abandoned.content.readline(), 5) == b"first\n"
            abandoned.close()
            deadline = time.monotonic() + 5
            while not gone.exists():
                assert time.monotonic() < deadline, "the worker never saw the client leave"
                await asyncio.sleep(0.05)

    asyncio.run(exercise())


def test_a_killed_worker_is_restarted_and_its_routes_recover(tmp_path: Path) -> None:
    async def exercise() -> None:
        services = _echo_services()
        async with _core(tmp_path, services) as (url, _state, auth), ClientSession() as client:
            await _ready(services)
            process = services.processes[SERVICE_DASHBOARD]
            first = process.pid
            assert first is not None
            os.kill(first, signal.SIGKILL)
            deadline = time.monotonic() + 20
            while True:
                assert time.monotonic() < deadline, "the worker never came back"
                response = await client.get(f"{url}/api/traces", headers=auth)
                if response.status == 200:
                    assert (await response.json())["pid"] != first
                    break
                # Meanwhile a caller is told to retry, never handed a hang or a 500.
                assert response.status == 503
                error = (await response.json())["error"]
                assert error["code"] == ErrorCode.DISPATCHER_UNAVAILABLE.value
                assert error["retryable"] is True
                await asyncio.sleep(0.1)
            assert process.restarts >= 1
            # The coding-agent lane never noticed.
            assert (await client.get(f"{url}/healthz")).status == 200

    asyncio.run(exercise())


def test_stopping_the_core_stops_every_worker(tmp_path: Path) -> None:
    async def exercise() -> list[int]:
        services = _echo_services()
        async with _core(tmp_path, services):
            await _ready(services)
            pids = [process.pid for process in services.processes.values()]
        return [pid for pid in pids if pid is not None]

    pids = asyncio.run(exercise())
    assert len(pids) == 2
    assert all(_gone(pid) for pid in pids)


def test_the_back_channel_exists_only_for_a_live_service_token(tmp_path: Path) -> None:
    async def exercise() -> None:
        probe = {"scope": {"project_root": str(tmp_path)}, "method": "probe"}
        digest = "0" * 64
        async with _core(tmp_path / "alone") as (url, _state, auth), ClientSession() as client:
            # Without services, not even the core's own bearer reaches it.
            response = await client.post(f"{url}/internal/v1/code-intelligence", headers=auth, json=probe)
            assert response.status == 404
            assert (await response.json())["error"]["code"] == ErrorCode.TOOL_UNKNOWN.value

        services = _echo_services()
        async with _core(tmp_path / "fronted", services) as (url, _state, auth), ClientSession() as client:
            await _ready(services)
            for headers in (auth, {**auth, HEADER_SERVICE_TOKEN: "forged"}):
                refused = await client.post(f"{url}/internal/v1/code-intelligence", headers=headers, json=probe)
                assert refused.status == 404
                refused = await client.get(f"{url}/internal/v1/content/{digest}", headers=headers)
                assert refused.status == 404
            token = {HEADER_SERVICE_TOKEN: services.processes[SERVICE_REVIEW].token()}
            answered = await client.post(f"{url}/internal/v1/code-intelligence", headers=token, json=probe)
            assert answered.status == 200
            assert await answered.json() == {"result": False}
            unlisted = {**probe, "method": "close"}
            answered = await client.post(f"{url}/internal/v1/code-intelligence", headers=token, json=unlisted)
            assert answered.status == 400
            missing = await client.get(f"{url}/internal/v1/content/{digest}", headers=token)
            assert missing.status == 404
            malformed = await client.get(f"{url}/internal/v1/content/not-a-digest", headers=token)
            assert malformed.status == 400

    asyncio.run(exercise())


def test_a_worker_answers_only_its_core_and_exits_when_the_core_lets_go(tmp_path: Path) -> None:
    async def exercise() -> None:
        process = _dashboard_host(tmp_path)
        process.start("http://127.0.0.1:9")
        try:
            endpoint = await process.endpoint(timeout_s=60)
            async with ClientSession() as client:
                refused = await client.get(f"{endpoint.url}/api/hello")
                assert refused.status == 404
                answered = await client.get(f"{endpoint.url}/api/hello", headers={HEADER_SERVICE_TOKEN: endpoint.token})
                assert answered.status == 200
                assert (await answered.json())["pid"] == process.pid
            worker = process._process
            assert worker is not None
        finally:
            await process.stop()
        # Closing stdin alone ended it: no SIGTERM, no SIGKILL.
        assert worker.returncode == 0

    asyncio.run(exercise())


def _worst_health_gap(url: str, stop: threading.Event) -> float:
    """The longest wait between two answered `/healthz` probes, from a thread of its own."""

    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port, timeout=30)
    worst, last = 0.0, time.perf_counter()
    try:
        while not stop.is_set():
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            response.read()
            assert response.status == 200
            now = time.perf_counter()
            worst, last = max(worst, now - last), now
            time.sleep(0.02)
    finally:
        connection.close()
    return worst


def test_a_busy_dashboard_no_longer_stalls_the_coding_agent_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "isolation_apps.py").write_text(_DASHBOARD_APP, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    import isolation_apps  # type: ignore[import-not-found]

    async def worst_while_burning(url: str, auth: dict[str, str]) -> float:
        stop = threading.Event()
        prober = asyncio.ensure_future(asyncio.to_thread(_worst_health_gap, url, stop))
        try:
            await asyncio.sleep(0.2)
            async with ClientSession() as client:
                response = await client.get(f"{url}/api/burn?seconds=1.0", headers=auth)
                assert response.status == 200
            await asyncio.sleep(0.1)
        finally:
            stop.set()
        return await prober

    async def in_process() -> float:
        legacy = isolation_apps.create()
        monkeypatch.setattr(LocalServerApp, "_legacy_dashboard", lambda self: legacy)
        async with _core(tmp_path / "shared") as (url, _state, auth):
            return await worst_while_burning(url, auth)

    async def isolated() -> float:
        services = LocalServices([_dashboard_host(tmp_path)])
        async with _core(tmp_path / "split", services) as (url, _state, auth):
            await _ready(services)
            async with ClientSession() as client:
                assert (await client.get(f"{url}/api/hello", headers=auth)).status == 200
            return await worst_while_burning(url, auth)

    # Control: sharing the process, the burn stalls every coding-agent call.
    assert asyncio.run(in_process()) >= 0.4
    # Isolated, the same burn costs the core nothing it can feel.
    assert asyncio.run(isolated()) < 0.25


def test_remote_code_intelligence_answers_exactly_as_the_native_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, _view_id = _open(owner)
        native = _provider(owner, repo)
        monkeypatch.setattr(LocalServerApp, "_scoped_code_intelligence", lambda self, scope: native)
        beta = native.find_symbol("beta", path="b.py")
        assert beta is not None
        native.neighborhood(beta)  # builds the graph, so readiness is stable below

        def wire(value: Any) -> Any:
            return json.loads(json.dumps(jsonable(value)))

        async def exercise() -> None:
            services = _echo_services()
            async with _core(tmp_path / "core", services) as (url, _state, _auth):
                await _ready(services)
                client = CoreClient(url, services.processes[SERVICE_REVIEW].token())
                remote = RemoteCodeIntelligence(client, {"repo_id": "repo_1", "revision_id": "rev_1"})
                cases: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
                    ("find_symbol", ("beta",), {"path": "b.py"}),
                    ("find_symbol", ("nothing_by_this_name",), {}),
                    ("neighborhood", (beta,), {"depth": 2, "limit": 10}),
                    ("references", (beta,), {"limit": 5}),
                    ("file_relationships", (("a.py", "b.py"),), {"limit": 100}),
                    ("semantic_search", ("beta caller",), {"limit": 5}),
                    ("ranking_metadata", (beta,), {}),
                    ("repository_file_links", (), {}),
                    ("path_digest", ("b.py",), {}),
                    ("path_digest", ("missing.py",), {}),
                ]
                for method, args, kwargs in cases:
                    expected = wire(getattr(native, method)(*args, **kwargs))
                    got = await asyncio.to_thread(getattr(remote, method), *args, **kwargs)
                    assert wire(got) == expected, method
                readiness = await asyncio.to_thread(remote.readiness)
                assert readiness.to_wire() == native.readiness().to_wire()

        asyncio.run(exercise())
    finally:
        owner.close()


def test_isolated_dashboard_preserves_repeated_response_headers(tmp_path: Path) -> None:
    async def exercise() -> None:
        services = LocalServices([_dashboard_host(tmp_path)])
        async with _core(tmp_path / "core", services) as (url, _state, auth), ClientSession() as client:
            await _ready(services)
            response = await client.get(f"{url}/api/cookies", headers=auth)
            assert response.status == 200
            cookies = response.headers.getall("Set-Cookie", [])
            assert any(value.startswith("alpha=1;") for value in cookies)
            assert any(value.startswith("beta=2;") for value in cookies)
            assert len(cookies) == 2

    asyncio.run(exercise())


def test_isolated_review_publish_path_matches_the_in_process_reader(tmp_path: Path) -> None:
    async def answer(root: Path, *, isolate: bool) -> tuple[int, Any]:
        async with _core(root, isolate=isolate) as (url, state, auth), ClientSession() as client:
            if state.services is not None:
                await _ready(state.services, [SERVICE_REVIEW])
            response = await client.post(
                f"{url}/api/reviews/rev_missing/revisions/0/publish",
                headers=auth,
                json={},
            )
            return response.status, await response.json()

    in_process = asyncio.run(answer(tmp_path / "shared-publish", isolate=False))
    isolated = asyncio.run(answer(tmp_path / "split-publish", isolate=True))
    assert isolated == in_process
    status, body = in_process
    assert status == 404
    assert body["error"]["code"] == ErrorCode.REVIEW_UNKNOWN.value
    assert body["error"]["message"] == "the Review object is not available"


def test_isolated_review_routes_answer_as_the_in_process_ones(tmp_path: Path) -> None:
    paths = ("/v1/reviews", "/api/reviews", "/v1/repos/repo_1/reviews", "/api/reviews/rev_missing")

    async def answers(root: Path, *, isolate: bool) -> list[tuple[int, Any]]:
        async with _core(root, isolate=isolate) as (url, state, auth), ClientSession() as client:
            if state.services is not None:
                await _ready(state.services, [SERVICE_REVIEW])
            rows = []
            for path in paths:
                response = await client.get(f"{url}{path}", headers=auth)
                body = await response.json()
                if isinstance(body, dict) and isinstance(body.get("error"), dict):
                    body["error"].pop("details", None)
                rows.append((response.status, body))
            return rows

    in_process = asyncio.run(answers(tmp_path / "shared", isolate=False))
    isolated = asyncio.run(answers(tmp_path / "split", isolate=True))
    assert isolated == in_process
