"""Shared aiohttp process runner with bounded drain and maintenance."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from types import TracebackType
from typing import Any, Protocol

from aiohttp import web

from .config import EngineServerConfig, LocalServerConfig
from .local_app import LocalServerApp

__all__ = ["LocalServer", "ServerRunner"]

_LOG = logging.getLogger("lemoncrow.server.runner")


class RunnerState(Protocol):
    maintenance: Any

    @property
    def in_flight(self) -> int: ...

    def begin_drain(self) -> None: ...

    async def wait_drained(self, timeout_s: float) -> bool: ...


class ServerRunner:
    """Own one aiohttp listener and the shared drain/maintenance lifecycle."""

    __slots__ = ("_app", "_config", "_maintenance", "_runner", "_server", "_site", "_url")

    def __init__(self, server: RunnerState, app: web.Application, config: EngineServerConfig) -> None:
        self._server = server
        self._app = app
        self._config = config
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._url: str | None = None
        self._maintenance: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("server is not started")
        return self._url

    @property
    def state(self) -> RunnerState:
        return self._server

    def _ssl_context(self) -> ssl.SSLContext | None:
        return None

    def _scheme(self) -> str:
        return "http"

    def _maintenance_once(self) -> None:
        self._server.maintenance.run_once()

    async def start(self) -> str:
        if self._runner is not None:
            raise RuntimeError("server is already started")
        runner = web.AppRunner(self._app, shutdown_timeout=self._config.shutdown_timeout_s, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner,
            self._config.listen_host,
            self._config.listen_port,
            ssl_context=self._ssl_context(),
        )
        await site.start()
        self._runner = runner
        self._site = site
        self._url = _resolve_url(runner, self._config, scheme=self._scheme())
        if self._config.maintenance_interval_s > 0:
            self._maintenance = asyncio.create_task(self._maintenance_loop(), name="lemoncrow-server-maintenance")
        return self._url

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._config.maintenance_interval_s)
                await asyncio.to_thread(self._maintenance_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("server maintenance pass failed")

    async def stop(self) -> None:
        runner = self._runner
        if runner is None:
            return
        task = self._maintenance
        self._maintenance = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._server.begin_drain()
        drained = await self._server.wait_drained(self._config.shutdown_timeout_s)
        if not drained:
            _LOG.warning("shutdown drain timed out with %d request(s) in flight", self._server.in_flight)
        await runner.cleanup()
        shutdown_executor = getattr(self._server, "shutdown_executor", None)
        if callable(shutdown_executor):
            shutdown_executor()
        self._runner = None
        self._site = None
        self._url = None

    async def __aenter__(self) -> ServerRunner:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()


def _resolve_url(runner: web.AppRunner, config: EngineServerConfig, *, scheme: str) -> str:
    host = config.listen_host or "127.0.0.1"
    port = config.listen_port
    for socket_ in getattr(runner, "addresses", ()) or ():
        if isinstance(socket_, tuple) and len(socket_) >= 2:
            host = str(socket_[0])
            port = int(socket_[1])
            break
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


class LocalServer(ServerRunner):
    """Public single-user loopback runner."""

    def __init__(self, state: LocalServerApp, app: web.Application, config: LocalServerConfig) -> None:
        super().__init__(state, app, config)
