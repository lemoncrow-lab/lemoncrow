"""Host one service -- Review or Dashboard -- in its own process.

Started by the core (``python -m lemoncrow_server_core service --name ...``)
with its token and the core's URL in the environment. It binds an ephemeral
loopback port, announces ``READY <url>`` on stdout, answers only requests that
carry the token, and exits when stdin closes (the core stopped or died).
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import importlib
import logging
import os
import signal
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from aiohttp import web

from .context import Principal
from .core_client import CoreClient
from .errors import ErrorCode, ServerError, error_payload
from .http.wire import json_response, parse_json_object, read_capped_body
from .service_protocol import (
    CORE_URL_ENV,
    HEADER_PRINCIPAL,
    HEADER_SERVICE_TOKEN,
    PRINCIPAL_KEY,
    SERVICE_DASHBOARD,
    SERVICE_REVIEW,
    SERVICE_TOKEN_ENV,
    decode_principal,
    token_matches,
)

__all__ = ["build_service_app", "serve_service"]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.service_host")

# The Review worker keeps a large long-lived heap (imported code, Snapshot projections, unit memos), and a request
# on a 10k-file Review builds big short-lived structures. At CPython's default thresholds (2000, 10, 10) those
# survive into the old generation and trigger full collections that walk the whole heap and free almost nothing:
# on the reference runner, 89 full collections in 246 s, 331 ms mean and up to 840 ms, each holding the GIL, so a
# one-file read that lands on one waits for it. The young generation is collected 50x less often, and the heap the
# worker built at startup is frozen out of every collection once warm-up is done (a Review's own app is built on
# its first request, so it is not part of that heap).
_REVIEW_GC_THRESHOLD: Final[tuple[int, int, int]] = (100_000, 10, 10)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class _Idle:
    """No index maintenance here: the core owns the index."""

    last_report = None

    def run_once(self) -> None:
        return None


class _LazyApp:
    """Build the dashboard's ASGI app once, from whichever thread asks first."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._app: Any | None = None
        self._lock = threading.Lock()

    def __call__(self) -> Any:
        app = self._app
        if app is None:
            with self._lock:
                app = self._app
                if app is None:
                    app = self._app = self._factory()
        return app


def _app_factory(reference: str) -> Callable[[], Any]:
    module_name, _, attribute = reference.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"--app must be module:factory, got {reference!r}")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise ValueError(f"{reference} is not callable")
    return factory  # type: ignore[no-any-return]


def build_service_app(
    name: str,
    *,
    token: str,
    core: CoreClient,
    state_root: Path,
    review_root: Path,
    public_port: int,
    max_request_bytes: int,
    app_reference: str = "",
) -> tuple[web.Application, Any, Callable[[], None]]:
    """The worker's aiohttp app, its maintenance, and a warm-up hook."""

    from lemoncrow.core.foundation.paths import default_store_root

    from .local_maintenance import LocalMaintenance

    @web.middleware
    async def errors(request: web.Request, handler: Handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except ServerError as exc:
            return json_response(exc.to_wire(), status=exc.status)
        except (web.HTTPException, asyncio.CancelledError):
            raise
        except Exception:
            correlation_id = uuid.uuid4().hex
            _LOG.exception("unhandled %s service error correlation_id=%s", name, correlation_id)
            return json_response(
                error_payload(
                    ErrorCode.INTERNAL,
                    f"internal error (correlation_id={correlation_id})",
                    details={"correlation_id": correlation_id},
                ),
                status=500,
            )

    @web.middleware
    async def authenticate(request: web.Request, handler: Handler) -> web.StreamResponse:
        # Anything without the token is not the core: answer as if nothing is here.
        if not token_matches(request.headers.get(HEADER_SERVICE_TOKEN, ""), (token,)):
            raise ServerError(ErrorCode.TOOL_UNKNOWN, "no such route")
        principal = decode_principal(request.headers.get(HEADER_PRINCIPAL, ""))
        if principal is not None:
            request[PRINCIPAL_KEY] = principal
        return await handler(request)

    def principal_of(request: web.Request) -> Principal:
        principal = request.get(PRINCIPAL_KEY)
        if not isinstance(principal, Principal):
            raise ServerError(ErrorCode.UNAUTHENTICATED, "a bearer credential is required")
        return principal

    async def body_of(request: web.Request) -> dict[str, Any]:
        return parse_json_object(await read_capped_body(request, max_request_bytes))

    app = web.Application(middlewares=[errors, authenticate], client_max_size=max_request_bytes * 2)
    runtime_root = default_store_root()
    warm: Callable[[], None] = lambda: None  # noqa: E731
    if name == SERVICE_REVIEW:
        from .local_review_service import LocalReviewService
        from .local_reviews import LocalReviewReader, LocalReviewRepository

        repository = LocalReviewRepository(review_root, source_content_reader=core.content)
        reader = LocalReviewReader(
            repository,
            listen_port=public_port,
            code_intelligence_factory=core.review_code_intelligence,
        )
        service = LocalReviewService(repository, reader, principal=principal_of, body=body_of)
        for method, path, handler in service.route_table():
            app.router.add_route(method, path, handler)
        app.router.add_route("*", "/api/{tail:.*}", service.reader_api)

        # Delayed sends are dispatched here, beside the Reader API they record through: a verdict's signing state
        # lives in this process, so the record read that Done makes right after a send must be answered by it too.
        async def start_flow_sends(_app: web.Application) -> None:
            await reader.start_flow_send_worker()

        async def stop_flow_sends(_app: web.Application) -> None:
            await reader.stop_flow_send_worker()

        app.on_startup.append(start_flow_sends)
        app.on_shutdown.append(stop_flow_sends)
        # Merge-when-ready lands only through this watch (never provider auto-merge).
        maintenance = LocalMaintenance(
            _Idle(),
            runtime_root=runtime_root,
            state_root=state_root,
            import_interval_s=0,
            flush_telemetry=False,
            landing_watch=repository.reconcile_landings,
        )
    elif name == SERVICE_DASHBOARD:
        from .dashboard_service import DashboardService, default_dashboard_app

        factory: Callable[[], Any] = (
            _app_factory(app_reference)
            if app_reference
            else (lambda: default_dashboard_app(core.root_code_intelligence))
        )
        lazy = _LazyApp(factory)
        dashboard = DashboardService(lazy)
        app.router.add_route("*", "/api/{tail:.*}", dashboard.handle)
        warm = lazy
        # Host-session import and telemetry rollups feed the dashboard, not coding agents.
        maintenance = LocalMaintenance(_Idle(), runtime_root=runtime_root, state_root=state_root)
    else:
        raise ValueError(f"unknown service {name!r}")
    return app, maintenance, warm


def _discard(_value: object) -> None:
    return None


def _watch_stdin(on_eof: Callable[[], None]) -> None:
    """The core holds our stdin open for its lifetime; EOF means it is gone."""

    def watch() -> None:
        with contextlib.suppress(Exception):
            while sys.stdin.buffer.read(4096):
                pass
        on_eof()

    threading.Thread(target=watch, name="lemoncrow-service-parent-watch", daemon=True).start()


async def serve_service(
    name: str,
    *,
    state_root: Path,
    review_root: Path,
    public_port: int,
    max_request_bytes: int,
    maintenance_interval_s: float,
    app_reference: str = "",
) -> int:
    token = os.environ.get(SERVICE_TOKEN_ENV, "")
    core_url = os.environ.get(CORE_URL_ENV, "")
    if not token or not core_url:
        sys.stderr.write(f"a service needs {SERVICE_TOKEN_ENV} and {CORE_URL_ENV}\n")
        return 2
    if name == SERVICE_REVIEW:
        gc.set_threshold(*_REVIEW_GC_THRESHOLD)
    core = CoreClient(core_url, token)
    app, maintenance, warm = build_service_app(
        name,
        token=token,
        core=core,
        state_root=state_root,
        review_root=review_root,
        # Links a Review renders point at the public listener, which is the core's.
        public_port=public_port or (urlsplit(core_url).port or 0),
        max_request_bytes=max_request_bytes,
        app_reference=app_reference,
    )
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=5.0)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = int(runner.addresses[0][1])

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _watch_stdin(lambda: _discard(loop.call_soon_threadsafe(stop.set)))
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    sys.stdout.write(f"READY http://127.0.0.1:{port}\n")
    sys.stdout.flush()
    # Nothing else may reach the core through that pipe: a full pipe would block us.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    async def maintain() -> None:
        while True:
            await asyncio.sleep(maintenance_interval_s)
            try:
                await asyncio.to_thread(maintenance.run_once)
            except Exception:
                _LOG.exception("%s service maintenance failed", name)

    async def warm_up() -> None:
        # Import ahead of the first request; a failure here surfaces on that request.
        try:
            await asyncio.to_thread(warm)
        except Exception:
            _LOG.exception("%s service warm-up failed", name)
        if name == SERVICE_REVIEW:
            gc.freeze()

    tasks = [asyncio.create_task(warm_up(), name=f"{name}-warm")]
    if maintenance_interval_s > 0:
        tasks.append(asyncio.create_task(maintain(), name=f"{name}-maintenance"))
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
    return 0
