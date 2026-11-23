"""Forward a request the core authenticated to the service that owns it.

Bodies stream both ways, so Code Ask's SSE and the Reader's long-polls arrive
chunk by chunk. Credentials the core consumed (bearer, cookie, browser session)
never cross; the service token and the authenticated principal replace them.
A client that disconnects cancels the upstream request, which the service sees
as its own disconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, Final

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from .app import HEADER_REQUEST_ID, REQUEST_ID_KEY
from .errors import AgentAction, ErrorCode, ServerError
from .local_browser_auth import LOCAL_BROWSER_SESSION_HEADER
from .service_process import ServiceEndpoint, ServiceProcess
from .service_protocol import HEADER_PRINCIPAL, HEADER_SERVICE_TOKEN, PRINCIPAL_KEY, encode_principal

__all__ = ["READ_BODY_KEY", "LocalServices", "ServiceUpstream"]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.services")
#: A core handler that read a request's body before forwarding it leaves the bytes here: the stream is spent.
READ_BODY_KEY: Final[web.RequestKey[bytes]] = web.RequestKey("lemoncrow_read_body", bytes)

_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_CONSUMED_CREDENTIALS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        LOCAL_BROWSER_SESSION_HEADER.lower(),
        HEADER_SERVICE_TOKEN.lower(),
        HEADER_PRINCIPAL.lower(),
    }
)
#: Set by the core's own server on the way out; a copy from the service would duplicate them.
_RESPONSE_OWNED: Final[frozenset[str]] = frozenset({"server", "date"})
_DISCONNECT_POLL_S: Final[float] = 0.1
_READY_WAIT_S: Final[float] = 30.0


class ServiceUpstream:
    """One service reached over HTTP."""

    def __init__(self, process: ServiceProcess, *, ready_wait_s: float = _READY_WAIT_S) -> None:
        self._process = process
        self._ready_wait_s = ready_wait_s
        self._session: aiohttp.ClientSession | None = None

    @property
    def name(self) -> str:
        return self._process.name

    def _client(self) -> aiohttp.ClientSession:
        session = self._session
        if session is None or session.closed:
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=256),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5.0),
                auto_decompress=False,
            )
            self._session = session
        return session

    def _headers(self, request: web.Request, endpoint: ServiceEndpoint) -> CIMultiDict[str]:
        headers = CIMultiDict[str]()
        for name, value in request.headers.items():
            lowered = name.lower()
            if lowered in _HOP_HEADERS or lowered in _CONSUMED_CREDENTIALS:
                continue
            headers.add(name, value)
        headers[HEADER_SERVICE_TOKEN] = endpoint.token
        principal = request.get(PRINCIPAL_KEY)
        if principal is not None:
            headers[HEADER_PRINCIPAL] = encode_principal(principal)
        request_id = request.get(REQUEST_ID_KEY)
        if request_id:
            headers[HEADER_REQUEST_ID] = str(request_id)
        headers["Host"] = request.host
        return headers

    async def handle(self, request: web.Request) -> web.StreamResponse:
        # Only a body-less request can be replayed once the worker restarts.
        replayable = not request.body_exists
        for attempt in range(2 if replayable else 1):
            endpoint = await self._process.endpoint(timeout_s=self._ready_wait_s)
            try:
                return await self._relay(request, endpoint)
            except _Unreachable as exc:
                if attempt or not replayable:
                    raise ServerError(
                        ErrorCode.DISPATCHER_UNAVAILABLE,
                        f"the LemonCrow {self.name} service did not answer; retry shortly",
                        details={"service": self.name},
                        retryable=True,
                        action=AgentAction.RETRY_LATER,
                    ) from exc.__cause__
                # The worker likely died; its supervisor is already restarting it.
                await asyncio.sleep(0.2)
        raise AssertionError("unreachable")

    async def _relay(self, request: web.Request, endpoint: ServiceEndpoint) -> web.StreamResponse:
        relay = asyncio.create_task(self._exchange(request, endpoint))

        async def watch_disconnect() -> None:
            while not relay.done():
                transport = request.transport
                if transport is None or transport.is_closing():
                    relay.cancel()
                    return
                await asyncio.sleep(_DISCONNECT_POLL_S)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            return await relay
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def _exchange(self, request: web.Request, endpoint: ServiceEndpoint) -> web.StreamResponse:
        url = endpoint.url + request.rel_url.raw_path_qs
        read = request.get(READ_BODY_KEY)
        data: Any = (
            read if read is not None else request.content.iter_chunked(64 * 1024) if request.body_exists else None
        )
        try:
            upstream = await self._client().request(
                request.method,
                url,
                headers=self._headers(request, endpoint),
                data=data,
                allow_redirects=False,
            )
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
            raise _Unreachable() from exc
        try:
            headers = CIMultiDict[str]()
            for name, value in upstream.headers.items():
                lowered = name.lower()
                if lowered in _HOP_HEADERS or lowered in _RESPONSE_OWNED:
                    continue
                headers.add(name, value)
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason, headers=headers)
            chunked = "chunked" in upstream.headers.get("Transfer-Encoding", "").lower()
            if upstream.content_length is not None and not chunked:
                response.content_length = upstream.content_length
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
            except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError):
                # Headers are on the wire: the only honest signal left is a cut stream.
                _LOG.warning("%s service stream ended early: %s %s", self.name, request.method, request.path)
                return response
            await response.write_eof()
            return response
        finally:
            upstream.release()

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()


class _Unreachable(Exception):
    """The service refused or dropped the connection before answering."""


class LocalServices:
    """The services one core fronts, by name."""

    def __init__(self, processes: Iterable[ServiceProcess]) -> None:
        self._processes = {process.name: process for process in processes}
        self._upstreams = {name: ServiceUpstream(process) for name, process in self._processes.items()}

    @property
    def processes(self) -> Mapping[str, ServiceProcess]:
        return self._processes

    def names(self) -> tuple[str, ...]:
        return tuple(self._processes)

    def tokens(self) -> tuple[str, ...]:
        return tuple(token for token in (process.token() for process in self._processes.values()) if token)

    def forwarder(self, name: str) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        return self._upstreams[name].handle

    def start(self, core_url: str) -> None:
        for process in self._processes.values():
            process.start(core_url)

    async def stop(self) -> None:
        results: list[Any] = await asyncio.gather(
            *(upstream.close() for upstream in self._upstreams.values()),
            *(process.stop() for process in self._processes.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                _LOG.warning("stopping a service failed: %s", result)
