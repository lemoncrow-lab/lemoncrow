"""The only module in this package that opens a network connection.

That is the point of putting it in one file: an enterprise reviewer reading
this package for egress reads *this* module and is done.
``tests/test_packaging_audit.py`` asserts the structural boundary mechanically:
all network I/O still lives in this module. A transport instance is pinned to
one origin. Normal product traffic uses ``LEMONCROW_URL``; hosted auth may create
a second instance only after LemonCrow advertises Authward and Authward discovery
confirms the same issuer/origin. No other module imports ``urllib.request``.

Three deliberate properties:

**No socket API, anywhere in the package.** This module reaches the network
through :mod:`urllib.request` and nothing else; no module in
``lemoncrow_client`` imports ``socket``, ``socketserver``, ``http.server``,
``asyncio`` or ``selectors``. A package that never holds a socket cannot bind
one, which is the structural version of "binds no port" -- checked as an import
scan in the packaging audit, alongside the runtime check that no ``socket.bind``
audit event is ever raised.

**No opener installed globally.** The client builds its own
:class:`urllib.request.OpenerDirector` with no proxy handler, no redirect
handler and no cookie jar, so nothing here mutates process-wide urllib state
and a redirect cannot move a request to another host.

**The origin is checked, not trusted.** Every request URL is compared against
the configured origin before it is sent. A bug that assembled the wrong URL
raises rather than connects.

**An unreachable server is a typed answer, never an exception that escapes.**
Connection refused, DNS failure, TLS failure and timeout all become
``server_unreachable`` with ``retryable=True``, which is what the degradation
table needs to distinguish "the server said no" from "there was no server".
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    A 302 is how a request to the configured endpoint becomes a request to
    somewhere else. The client would rather fail than follow one.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


class HttpTransport:
    """Bounded HTTP against exactly one origin, with JSON and OAuth form bodies."""

    __slots__ = ("_opener", "_origin", "_timeout_s", "_url")

    def __init__(self, url: str, *, timeout_s: float, ssl_context: ssl.SSLContext | None = None) -> None:
        self._url = url.rstrip("/")
        parts = urlsplit(self._url)
        scheme = parts.scheme or "http"
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        self._origin = (scheme, host, int(port))
        self._timeout_s = timeout_s
        handlers: list[urllib.request.BaseHandler] = [
            # An empty ProxyHandler is not "default proxies"; it is *no*
            # proxies. An enterprise proxy is configured by the operator on the
            # endpoint URL, not inherited from an environment variable the
            # client never audited.
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        ]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        self._opener = urllib.request.build_opener(*handlers)
        self._opener.addheaders = []

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
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Response:
        """One request. Raises for no answer, for a redirect, or for a foreign origin.

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
            data = urlencode(dict(form)).encode("ascii")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(target, data=data, headers=request_headers, method=method)
        budget = self._timeout_s if timeout_s is None else timeout_s
        try:
            with self._opener.open(request, timeout=budget) as handle:
                status = int(handle.status)
                if 300 <= status < 400:
                    raise _redirect_refused(status)
                return Response(status=status, payload=_decode(handle.read(_MAX_RESPONSE_BYTES)))
        except urllib.error.HTTPError as exc:
            # A typed refusal arrives as an HTTP error status with a JSON body.
            # It is an answer, not a transport failure, so it is returned --
            # except a redirect, which is a destination change wearing the
            # clothes of an answer and is refused outright.
            status = int(exc.code)
            if 300 <= status < 400:
                raise _redirect_refused(status) from exc
            raw = b""
            try:
                raw = exc.read(_MAX_RESPONSE_BYTES)
            except OSError:
                pass
            return Response(status=status, payload=_decode(raw))
        except urllib.error.URLError as exc:
            raise _unreachable(self._url, exc.reason) from exc
        except TimeoutError as exc:
            raise _unreachable(self._url, f"timed out after {budget:g}s") from exc
        except (OSError, ssl.SSLError) as exc:
            raise _unreachable(self._url, exc) from exc

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def post_form(
        self,
        path: str,
        *,
        form: Mapping[str, str],
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


def _unreachable(url: str, reason: object) -> ClientError:
    return ClientError(
        ErrorCode.SERVER_UNREACHABLE,
        f"cannot reach the LemonCrow server at {url}: {reason}",
        details={"url": url},
        retryable=True,
        action=AgentAction.RETRY_LATER,
    )
