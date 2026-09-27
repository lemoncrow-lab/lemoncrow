"""Unix-domain-socket HTTP transport isolated behind an infra boundary."""

from __future__ import annotations

from typing import Any

import httpx

UDS_CONNECTION_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.ConnectTimeout,
)


def uds_http_client(socket_path: str, *, timeout: Any = 2.0) -> httpx.Client:
    """Return a proxy-independent HTTP client pinned to *socket_path*."""
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=socket_path),
        trust_env=False,
        timeout=timeout,
    )


def unbounded_request_timeout(*, connect: float = 10.0) -> httpx.Timeout:
    """Allow long tool calls while keeping daemon connection failures bounded."""
    return httpx.Timeout(None, connect=connect)
