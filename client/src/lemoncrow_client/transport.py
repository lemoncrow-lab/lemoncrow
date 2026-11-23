"""The only module in this package that opens a network connection.

That is the point of putting it in one file: an enterprise reviewer reading
this package for egress reads *this* module and is done.
``tests/test_packaging_audit.py`` asserts the structural boundary mechanically:
all network I/O still lives in this module. A transport instance is pinned to
one origin. Normal product traffic uses ``LEMONCROW_URL``; hosted auth may create
a second instance only after LemonCrow advertises Authward and Authward discovery
confirms the same issuer/origin. All network I/O uses :mod:`http.client` here.

Three deliberate properties:

**No listening socket API.** Connections use :mod:`http.client`; this package
never opens a network listener. Each transport retains at most four idle
connections, exclusively checked out by requests, and closes them on teardown.

**No process-wide HTTP state.** Direct connections inherit no environment proxy,
follow no redirects and use no cookie jar.

**The origin is checked, not trusted.** Every request URL is compared against
the configured origin before it is sent. A bug that assembled the wrong URL
raises rather than connects.

**An unreachable server is a typed answer, never an exception that escapes.**
Connection refused, DNS failure, TLS failure and timeout all become
``server_unreachable`` with ``retryable=True``, which is what the degradation
table needs to distinguish "the server said no" from "there was no server".
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import select
import socket
import ssl
import struct
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

from .errors import AgentAction, ClientError, ErrorCode

__all__ = ["HttpTransport", "Response"]

_USER_AGENT: Final[str] = "lemoncrow-client"
_MAX_RESPONSE_BYTES: Final[int] = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Response:
    """One decoded answer. ``payload`` is always a JSON object."""

    status: int
    payload: Mapping[str, Any]

    def require(self) -> Mapping[str, Any]:
        """The payload, or the refusal it carries, raised."""
        if self.status >= 400 or "error" in self.payload:
            raise ClientError.from_wire(self.payload, status=self.status)
        return self.payload


_MAX_IDLE_CONNECTIONS: Final[int] = 4


def _ws_read_exact(sock: socket.socket | ssl.SSLSocket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        fragment = sock.recv(size - len(chunks))
        if not fragment:
            raise OSError("Review WebSocket closed during a frame")
        chunks.extend(fragment)
    return bytes(chunks)


def _ws_masked_pong(sock: socket.socket | ssl.SSLSocket, message: bytes) -> None:
    key = os.urandom(4)
    data = bytes(value ^ key[index % 4] for index, value in enumerate(message))
    sock.sendall(bytes([0x8A, 0x80 | len(message)]) + key + data)


class HttpTransport:
    """Bounded HTTP against exactly one origin, with JSON and OAuth form bodies."""

    __slots__ = ("_closed", "_connections", "_idle", "_lock", "_origin", "_ssl_context", "_timeout_s", "_url")

    def __init__(self, url: str, *, timeout_s: float, ssl_context: ssl.SSLContext | None = None) -> None:
        self._url = url.rstrip("/")
        parts = urlsplit(self._url)
        scheme = parts.scheme or "http"
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        if scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
            raise ClientError(ErrorCode.NOT_CONFIGURED, "transport requires an HTTP(S) origin without credentials")
        self._origin = (scheme, host, int(port))
        self._timeout_s = timeout_s
        self._ssl_context = ssl_context
        self._lock = threading.Condition()
        self._idle: list[http.client.HTTPConnection] = []
        self._connections: set[http.client.HTTPConnection] = set()
        self._closed = False

    def _acquire(self, timeout_s: float) -> tuple[http.client.HTTPConnection, bool]:
        with self._lock:
            if self._closed:
                raise ClientError(ErrorCode.NOT_CONFIGURED, "transport is closed")
            # A fully consumed HTTP response leaves no application bytes to read.
            # Readability while idle means EOF (or unsolicited bytes), so discard
            # the socket BEFORE a POST rather than replaying a potentially sent write.
            while self._idle:
                candidate = self._idle[-1]
                try:
                    dropped = candidate.sock is None or bool(select.select([candidate.sock], [], [], 0)[0])
                except (OSError, ValueError):
                    dropped = True
                if not dropped:
                    break
                self._idle.pop()
                self._connections.discard(candidate)
                candidate.close()
            reused = bool(self._idle)
            if reused:
                connection = self._idle.pop()
            elif self._origin[0] == "https":
                connection = http.client.HTTPSConnection(
                    self._origin[1], self._origin[2], timeout=timeout_s, context=self._ssl_context
                )
            else:
                connection = http.client.HTTPConnection(self._origin[1], self._origin[2], timeout=timeout_s)
            self._connections.add(connection)
            connection.timeout = timeout_s
            if connection.sock is not None:
                connection.sock.settimeout(timeout_s)
            return connection, reused

    def _release(self, connection: http.client.HTTPConnection, *, reusable: bool) -> None:
        with self._lock:
            if (
                reusable
                and not self._closed
                and connection.sock is not None
                and len(self._idle) < _MAX_IDLE_CONNECTIONS
            ):
                self._idle.append(connection)
            else:
                self._connections.discard(connection)
                connection.close()
            self._lock.notify_all()

    def close(self) -> None:
        """Close idle connections and drain requests before completing teardown."""
        with self._lock:
            self._closed = True
            for connection in self._idle:
                connection.close()
                self._connections.discard(connection)
            self._idle.clear()
            # An active request owns its connection until release. Closing it here
            # could race with HTTPConnection.request() automatically reconnecting.
            while self._connections:
                self._lock.wait()

    def __del__(self) -> None:
        # Also reclaim short-lived CLI/auth transports whose owners do not keep them.
        if hasattr(self, "_lock"):
            self.close()

    @property
    def url(self) -> str:
        return self._url

    @property
    def origin(self) -> tuple[str, str, int]:
        return self._origin

    def _check_origin(self, target: str) -> None:
        parts = urlsplit(target)
        scheme = parts.scheme or "http"
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        if (scheme, host, int(port)) != self._origin:
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                "refusing to open a connection to an origin other than LEMONCROW_URL",
                details={"configured": f"{self._origin[0]}://{self._origin[1]}:{self._origin[2]}"},
                action=AgentAction.ABANDON,
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        form: Mapping[str, str | Sequence[str]] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Response:
        """One request. Raises for no answer, for a redirect, or for a foreign origin.

        A ``form`` value that is a sequence repeats its field once per item (RFC 8707 ``resource``).

        ``path`` is normally a path. A caller that passes a whole URL is not
        trusted to have picked the right one: it goes through the same origin
        check, so a bug that assembled a destination cannot connect to it.
        """
        target = path if "://" in path[:8] else f"{self._url}{path}"
        self._check_origin(target)
        if body is not None and form is not None:
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                "one request cannot carry both JSON and form bodies",
                action=AgentAction.FIX_REQUEST,
            )
        data = None
        request_headers = {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
            **dict(headers or {}),
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(dict(form), doseq=True).encode("ascii")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        budget = self._timeout_s if timeout_s is None else timeout_s
        parts = urlsplit(target)
        request_path = parts.path or "/"
        if parts.query:
            request_path += "?" + parts.query
        for attempt in range(2):
            connection, reused = self._acquire(budget)
            reusable = False
            try:
                connection.request(method, request_path, body=data, headers=request_headers)
                with connection.getresponse() as handle:
                    status = handle.status
                    raw = handle.read(_MAX_RESPONSE_BYTES + 1)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise ClientError(ErrorCode.INTERNAL, "server response exceeds the byte limit")
                    if handle.length not in (None, 0):
                        raise http.client.IncompleteRead(raw, handle.length)
                    reusable = not handle.will_close
                    if 300 <= status < 400:
                        raise _redirect_refused(status)
                    return Response(status=status, payload=_decode(raw))
            except TimeoutError as exc:
                raise _unreachable(self._url, f"timed out after {budget:g}s", timed_out=True) from exc
            except (OSError, http.client.HTTPException) as exc:
                # A stale keep-alive socket can fail after the request was sent.
                # Replay only reads, once; never replay a tool POST or OAuth rotation.
                if reused and attempt == 0 and method.upper() in {"GET", "HEAD"}:
                    continue
                raise _unreachable(self._url, exc) from exc
            finally:
                self._release(connection, reusable=reusable)
        raise AssertionError("unreachable")

    def websocket_hints(
        self, path: str, *, bearer: str, timeout_s: float = 15.0, max_messages: int = 10
    ) -> Iterator[Mapping[str, Any]]:
        """Strict outbound-only RFC6455 text receiver, scoped to this HTTPS origin.

        Server frames are untrusted *hints*, never commands. No listener,
        redirects, optional dependencies, proxy discovery or subprotocols.
        This deliberately minimal receiver refuses fragmentation, compression,
        oversized messages, masked server data and unexpected opcodes.
        """
        if not 1 <= max_messages <= 100 or not 1 <= timeout_s <= 60:
            raise ValueError("Review wakeup bounds are invalid")
        if not bearer or any(ch in bearer for ch in "\r\n\x00 "):
            raise ValueError("Review machine token is invalid")
        target = path if "://" in path[:8] else f"{self._url}{path}"
        self._check_origin(target)
        parsed = urlsplit(target)
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Review wakeup URI may not carry credentials or fragments")
        scheme, host, port = self._origin
        if scheme != "https" and host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Review machine WebSocket bearer requires TLS")
        resource = parsed.path or "/"
        if parsed.query:
            resource += "?" + parsed.query
        if not resource.startswith("/v1/review-workers/wakeup?execution_class="):
            raise ValueError("Review WebSocket may only read fixed pending-work hints")
        client_key = base64.b64encode(os.urandom(16)).decode("ascii")
        accept_expected = base64.b64encode(
            hashlib.sha1((client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest(),
        ).decode("ascii")
        host_header = f"[{host}]" if ":" in host else host
        if (scheme, port) not in {("https", 443), ("http", 80)}:
            host_header += f":{port}"
        request = (
            f"GET {resource} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Connection: Upgrade\r\nUpgrade: websocket\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {client_key}\r\n"
            f"Authorization: Bearer {bearer}\r\n"
            f"User-Agent: {_USER_AGENT}\r\n\r\n"
        ).encode("ascii")
        sock: socket.socket | ssl.SSLSocket = socket.create_connection((host, port), timeout=timeout_s)
        try:
            if scheme == "https":
                context = self._ssl_context or ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            sock.settimeout(timeout_s)
            sock.sendall(request)
            response = bytearray()
            while not response.endswith(b"\r\n\r\n"):
                if len(response) >= 8192:
                    raise ValueError("Review WebSocket handshake is oversized")
                chunk = sock.recv(1)
                if not chunk:
                    raise OSError("Review WebSocket handshake closed")
                response.extend(chunk)
            lines = response.decode("ascii").split("\r\n")
            if lines[0] != "HTTP/1.1 101 Switching Protocols":
                raise ValueError("Review WebSocket upgrade refused (HTTPS polling fallback applies)")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, sep, value = line.partition(":")
                if not sep:
                    raise ValueError("Review WebSocket malformed upgrade header")
                key = name.strip().lower()
                if key in headers:
                    raise ValueError("Review WebSocket ambiguous upgrade header")
                headers[key] = value.strip()
            if (
                headers.get("sec-websocket-accept") != accept_expected
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in {p.strip().lower() for p in headers.get("connection", "").split(",")}
                or "sec-websocket-extensions" in headers
                or "sec-websocket-protocol" in headers
            ):
                raise ValueError("Review WebSocket upgrade cannot be authenticated")
            for _ in range(max_messages):
                while True:
                    head = _ws_read_exact(sock, 2)
                    first, second = head
                    if not first & 0x80 or first & 0x70 or second & 0x80:
                        raise ValueError("Review WebSocket requires unfragmented plain unmasked frames")
                    size = second & 0x7F
                    if size == 126:
                        size = struct.unpack("!H", _ws_read_exact(sock, 2))[0]
                    elif size == 127:
                        raise ValueError("Review WebSocket frame is oversized")
                    if size > 2048:
                        raise ValueError("Review WebSocket frame is oversized")
                    message = _ws_read_exact(sock, size)
                    opcode = first & 0x0F
                    if opcode == 8:
                        return
                    if opcode == 9:
                        if size > 125:
                            raise ValueError("Review WebSocket ping oversized")
                        _ws_masked_pong(sock, message)
                        continue
                    if opcode == 10:
                        continue
                    if opcode != 1:
                        raise ValueError("Review WebSocket accepts only text hints")
                    decoded = json.loads(message.decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise ValueError("Review WebSocket hint must be an object")
                    yield decoded
                    break
        finally:
            sock.close()

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def post_binary(
        self,
        path: str,
        *,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> tuple[int, Mapping[str, str], bytes]:
        """Bounded binary Snapshot transfer over the exact pinned origin.

        Unlike JSON responses, source blobs are never interpreted as control
        messages. No redirects, proxy inheritance or automatic POST replay.
        """
        if not 0 < max_bytes <= 16 * 1024 * 1024:
            raise ValueError("binary response cap must be at most 16 MiB")
        target = path if "://" in path[:8] else f"{self._url}{path}"
        self._check_origin(target)
        parsed = urlsplit(target)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
            "User-Agent": _USER_AGENT,
            **dict(headers or {}),
        }
        conn, _reused = self._acquire(self._timeout_s)
        reusable = False
        try:
            conn.request("POST", request_path, body=data, headers=request_headers)
            with conn.getresponse() as response:
                status = response.status
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise ClientError(ErrorCode.REQUEST_TOO_LARGE, "Revision source exceeded binary transfer cap")
                if 300 <= status < 400:
                    raise _redirect_refused(status)
                reusable = not response.will_close
                return (
                    status,
                    {
                        "content-type": response.getheader("Content-Type", ""),
                        "content-digest": response.getheader("X-LemonCrow-Content-Digest", ""),
                    },
                    payload,
                )
        except TimeoutError as exc:
            raise _unreachable(self._url, "binary revision request timed out", timed_out=True) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise _unreachable(self._url, exc) from exc
        finally:
            self._release(conn, reusable=reusable)

    def post_form(
        self,
        path: str,
        *,
        form: Mapping[str, str | Sequence[str]],
        timeout_s: float | None = None,
    ) -> Response:
        """POST an OAuth-style form to this transport's exact origin."""
        return HttpTransport.request(self, "POST", path, form=form, timeout_s=timeout_s)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)


def _decode(raw: bytes) -> Mapping[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return {
            "error": {
                "code": ErrorCode.INTERNAL.value,
                "message": "server response was not valid JSON",
                "retryable": True,
                "action": AgentAction.RETRY_LATER.value,
                "details": {"bytes": len(raw)},
            }
        }
    if not isinstance(parsed, dict):
        return {
            "error": {
                "code": ErrorCode.INTERNAL.value,
                "message": "server response was not a JSON object",
                "retryable": False,
                "action": AgentAction.ABANDON.value,
                "details": {},
            }
        }
    return parsed


def _redirect_refused(status: int) -> ClientError:
    return ClientError(
        ErrorCode.NOT_CONFIGURED,
        (
            f"the endpoint answered with an HTTP {status} redirect; this client follows none, "
            "because a redirect is how a request to the configured endpoint becomes a request "
            "somewhere else"
        ),
        details={"status": status},
        action=AgentAction.ABANDON,
    )


def _unreachable(url: str, reason: object, *, timed_out: bool = False) -> ClientError:
    # ``timed_out`` lets recovery tell "the server is gone" (safe calls are
    # retried after reconnect) from "the server is slow" (never re-sent).
    return ClientError(
        ErrorCode.SERVER_UNREACHABLE,
        f"cannot reach the LemonCrow server at {url}: {reason}",
        details={"url": url, **({"timed_out": True} if timed_out else {})},
        retryable=True,
        action=AgentAction.RETRY_LATER,
    )
