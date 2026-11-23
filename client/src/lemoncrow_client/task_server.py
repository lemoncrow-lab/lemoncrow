"""``lc task ... --server URL``: agent tasks on a LemonCrow Workspaces server.

A Workspaces server keeps each task's worktrees on the organization's own machine and serves a JSON HTTP API on a
private address. This module is its client, standard library only:

* :class:`WorkspacesClient` sends one bounded JSON request per call. It takes only a plain ``http`` or ``https`` URL
  (no user name or password, query or fragment), never follows a redirect (``urllib`` would forward the
  ``Authorization`` header to wherever it points), and turns a ``{"error": ...}`` reply into :class:`ServerError`.
* The token store keeps the task token ``POST /v1/tasks`` returns once: one owner-only (0600) JSON file per task,
  ``<LEMONCROW_HOME>/workspaces/tasks/<host>_<port>/<id>.json`` in owner-only (0700) directories, replaced
  atomically, and deleted when ``finish`` succeeds.
* :func:`bridge` is ``lc task mcp``: newline-delimited JSON-RPC on stdin, each message POSTed to the task's MCP
  endpoint with the task token, a few at once, each reply one line on stdout.
* Hosted agents (``start --run``, ``run``, ``watch``, ``tell``, ``stop``, ``attach``): the server runs one of its
  configured agents on the task; ``watch`` polls the run's redacted log, ``attach`` takes the task over with a new
  task token (the server stops the hosted agent first).
* A start the server refuses for the repositories' ``[toolchain]`` (its error reply's ``code`` is one of the
  ``TOOLCHAIN_*`` codes: none named, one not pinned by digest, one it cannot pull) prints the command that fixes it,
  ``lc verify init --toolchain IMAGE``, and to commit and push the file. The client checks nothing itself first: only
  the server knows which repositories and which revision a task starts from.

The organization token comes only from ``LEMONCROW_WORKSPACES_TOKEN``. Without it, a LemonCrow-hosted server
(``https://`` on ``lemoncrow.com`` or a subdomain) gets the person's own sign-in from ``lc auth login`` instead: an
access token issued for that server's own origin (taken from the URL connected to, never from the server), refreshed
when it has less than a minute left. The LemonCrow API's token never goes to a Workspaces server, a token whose
audience is not the server's origin is refused before it is sent, and no sign-in goes to any other server. No token
is printed, logged, put on a command line or written into an agent host's MCP
configuration: the registered attach command names the server and the task (and ``LEMONCROW_HOME`` when it is set,
so the bridge finds the token store), and the bridge reads the task token from its file.

A LemonCrow-hosted server that stops when idle is woken on demand: when it does not take the connection (within ten
seconds), the client asks the LemonCrow API (``POST /v1/workspaces/wake`` with the person's API sign-in, naming the
server by the URL connected to) to start it, waits for the answer, and tries the request again once. That happens
once per client, and never for a server that is not LemonCrow-hosted.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.message import Message
from http.client import HTTPException
from pathlib import Path
from typing import IO, Any, BinaryIO, Final, TextIO

from . import CLIENT_NAME, CLIENT_VERSION
from .config import load_config
from .credentials import (
    NOT_COVERED,
    managed_credentials_path,
    refresh_managed_access,
    resource_origin,
    token_claims,
    workspaces_access,
)
from .environment import ENVIRONMENT_FILE
from .errors import ClientError
from .image_registry import RegistryError, parse_reference

__all__ = [
    "SERVER_ENV",
    "SERVER_ONLY",
    "TOKEN_ENV",
    "TOOLCHAIN_MISSING",
    "TOOLCHAIN_UNAVAILABLE",
    "TOOLCHAIN_UNPINNED",
    "ServerError",
    "TaskRecord",
    "WorkspacesClient",
    "account_resource",
    "bridge",
    "delete_record",
    "has_record",
    "load_record",
    "record_path",
    "save_record",
    "server_task_main",
    "server_url",
]

SERVER_ENV: Final[str] = "LEMONCROW_WORKSPACES_SERVER"
TOKEN_ENV: Final[str] = "LEMONCROW_WORKSPACES_TOKEN"
#: The ``code`` of the Workspaces API's refusals of a task start for its repositories' ``[toolchain]``: none named,
#: one not pinned by digest, one the server cannot pull. ``lc task start`` turns each into the command that fixes it.
TOOLCHAIN_MISSING: Final[str] = "toolchain_missing"
TOOLCHAIN_UNPINNED: Final[str] = "toolchain_unpinned"
TOOLCHAIN_UNAVAILABLE: Final[str] = "toolchain_unavailable"

_API_TIMEOUT_S: Final[float] = 60.0
#: Verify, land, finish and MCP tool calls run checks and commands on the server before they answer.
_LONG_TIMEOUT_S: Final[float] = 1800.0
_MAX_RESPONSE_BYTES: int = 64 * 1024 * 1024
_MAX_ERROR_BYTES: Final[int] = 64 * 1024
_MAX_RECORD_BYTES: Final[int] = 64 * 1024
_MAX_SHOWN: Final[int] = 500
#: MCP requests the bridge has in flight at once; it reads the next line only when one of them is answered.
_BRIDGE_WORKERS: Final[int] = 8
#: What the attach command needs from this environment: where the task token store is.
_HOME_ENV: Final[str] = "LEMONCROW_HOME"
_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,199}")
#: A bearer token is one header value: printable ASCII, no space.
_TOKEN: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]{1,4096}")
_UNSHOWN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_HOST_PORT_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9.-]")
_HELP: Final[frozenset[str]] = frozenset({"-h", "--help"})
#: Workspaces servers LemonCrow runs: the only ones a person's LemonCrow sign-in is sent to.
_ACCOUNT_DOMAIN: Final[str] = "lemoncrow.com"
#: A signed-in access token with less than this left is refreshed before it is sent.
_REFRESH_MARGIN_S: Final[float] = 60.0


class ServerError(Exception):
    """A refusal or a failed exchange with the Workspaces server; ``code`` 2 is a usage error.

    ``refusal`` is the ``code`` the server's error reply gave (``toolchain_missing`` ...), and ``image`` the image it
    named; both are empty when it gave none.
    """

    def __init__(self, message: str, code: int = 1, *, refusal: str = "", image: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.refusal = refusal
        self.image = image


class _Help(Exception):
    pass


def _when(value: object) -> str:
    """A server time (seconds since the epoch) as UTC ``YYYY-MM-DD HH:MM:SSZ``; anything else as it came."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    return _shown(value)


def _shown(value: object, *secrets: str) -> str:
    """``value`` safe to print: no token, no control character (a terminal escape), bounded."""

    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[token]")
    text = _UNSHOWN.sub(" ", text).strip()
    return text if len(text) <= _MAX_SHOWN else text[: _MAX_SHOWN - 3] + "..."


def server_url(value: str) -> str:
    """``value`` as a Workspaces server base URL without a trailing slash; :class:`ServerError` when it is not one."""

    text = value.strip()
    if not text or _UNSHOWN.search(text) or " " in text:
        raise ServerError("the Workspaces server URL is empty or holds a space or control character", 2)
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise ServerError(f"a Workspaces server URL starts with http:// or https://, not {_shown(text)}", 2)
    if "@" in parts.netloc:
        raise ServerError(
            f"a Workspaces server URL carries no user name or password; the token comes from {TOKEN_ENV}", 2
        )
    if "?" in text or "#" in text:
        raise ServerError("a Workspaces server URL has no query or fragment", 2)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if not parts.hostname or port == -1:
        raise ServerError(f"not a Workspaces server URL: {_shown(text)}", 2)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _origin(url: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _task_id(value: str, usage: str) -> str:
    if not _ID.fullmatch(value):
        raise ServerError(f"usage: lc task {usage}  (ID: letters, digits, '.', '_', '-'; not starting with '.')", 2)
    return value


def _segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect becomes an error: following it would send the bearer token to wherever it points."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def _read_capped(stream: Any) -> bytes:
    data: bytes = stream.read(_MAX_RESPONSE_BYTES + 1)
    if len(data) > _MAX_RESPONSE_BYTES:
        raise ServerError(f"the Workspaces server sent more than {_MAX_RESPONSE_BYTES} bytes in one reply")
    return data


# --------------------------------------------------------------------------- #
# Waking a stopped LemonCrow-hosted server (plan B0.3)                        #
# --------------------------------------------------------------------------- #

#: A LemonCrow-hosted server that does not take the connection within this is treated as stopped and woken; the
#: system's own connect timeout (over a minute) would spend most of the wake's time first.
_CONNECT_TIMEOUT_S: Final[float] = 10.0
#: LemonCrow answers a wake once the started server answers, within four minutes.
_WAKE_TIMEOUT_S: Final[float] = 300.0
_WAKE_PATH: Final[str] = "/v1/workspaces/wake"


class _PromptHTTPSConnection(http.client.HTTPSConnection):
    """Connects within :data:`_CONNECT_TIMEOUT_S`, then reads with the request's own timeout."""

    def connect(self) -> None:
        wait = self.timeout
        if not isinstance(wait, int | float) or wait <= _CONNECT_TIMEOUT_S:
            super().connect()
            return
        self.timeout = _CONNECT_TIMEOUT_S
        try:
            super().connect()
        finally:
            self.timeout = wait
        if self.sock is not None:
            self.sock.settimeout(wait)


class _PromptHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_PromptHTTPSConnection, req)


#: A connect that no host answered: the address of a stopped VM. A DNS failure (its own error numbers) and a TLS
#: refusal are not among them.
_NOT_TAKEN: Final[frozenset[int]] = frozenset(
    {errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH}
)


def _unreachable(exc: BaseException) -> bool:
    """Whether *exc* says the server did not take the connection, so the request was never sent: a stopped VM.

    Not a DNS failure or a TLS refusal, which a wake cannot change, nor an HTTP answer or a reply that timed out.
    """

    if not isinstance(exc, urllib.error.URLError) or isinstance(exc, urllib.error.HTTPError):
        return False
    reason = exc.reason
    if isinstance(reason, ConnectionRefusedError | TimeoutError):
        return True
    return isinstance(reason, OSError) and reason.errno in _NOT_TAKEN


def _api_sign_in() -> tuple[str, str]:
    """The LemonCrow API's URL and the person's own access token for it, refreshed when about to run out.

    The token is ``""`` without a sign-in, or when the configured API is not LemonCrow-hosted: only LemonCrow is
    asked to wake a server.
    """

    try:
        config = load_config()
    except (ClientError, ValueError) as exc:
        raise ServerError(f"your LemonCrow sign-in could not be read: {_shown(exc)}") from None
    managed = {str(managed_credentials_path(config.account_state_dir)), str(managed_credentials_path(config.state_dir))}
    if (
        not config.hosted
        or not _account_server(config.url)
        or not config.refresh_token
        or config.refresh_token_source not in managed
    ):
        return config.url, ""
    access = config.token
    expires = token_claims(access).get("exp") if access else None
    if not access or (
        isinstance(expires, int | float) and not isinstance(expires, bool) and expires - time.time() < _REFRESH_MARGIN_S
    ):
        try:
            access = (
                refresh_managed_access(
                    config.url,
                    state_dir=config.account_state_dir,
                    legacy_state_dir=config.state_dir,
                    stale_access=access,
                    timeout_s=_API_TIMEOUT_S,
                )
                or access
            )
        except ClientError as exc:
            raise ServerError(
                f"your LemonCrow sign-in could not be refreshed ({_shown(exc, access)}); run `lc auth login`"
            ) from None
    if access and not _TOKEN.fullmatch(access):
        raise ServerError("your LemonCrow sign-in holds a character a token cannot have; run `lc auth login`")
    return config.url, access


def _wake_hosted_server(server: str) -> None:
    """Ask LemonCrow to start the stopped LemonCrow-hosted Workspaces server at *server*; return once it answers.

    The request goes to the LemonCrow API, ``POST /v1/workspaces/wake``, with the person's API sign-in: never to the
    Workspaces server or anywhere else. It names the server by the origin ``lc task`` connects to.
    """

    api, access = _api_sign_in()
    if not access:
        raise ServerError(
            f"cannot reach the Workspaces server at {server}; sign in with `lc auth login` so LemonCrow can start it"
        )
    request = urllib.request.Request(
        f"{api.rstrip('/')}{_WAKE_PATH}",
        data=json.dumps({"server": account_resource(server)}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
        },
        method="POST",
    )
    failed = f"cannot reach the Workspaces server at {server}, and LemonCrow did not start it"
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=_WAKE_TIMEOUT_S) as response:
            _read_capped(response)
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read(_MAX_ERROR_BYTES))
        except (ValueError, OSError, HTTPException):
            parsed = None
        finally:
            exc.close()
        detail = parsed.get("error") if isinstance(parsed, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else None
        raise ServerError(f"{failed}: {_shown(message or exc.reason, access)} (HTTP {exc.code})") from None
    except (urllib.error.URLError, HTTPException, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise ServerError(f"{failed}: {_shown(reason, access) or type(exc).__name__}") from None


class WorkspacesClient:
    """JSON over HTTP to one Workspaces server; the token travels per call and is never kept here."""

    def __init__(self, server: str) -> None:
        self.server = server_url(server)
        hosted = _account_server(self.server)
        self._opener = urllib.request.build_opener(_NoRedirect, *([_PromptHTTPSHandler] if hosted else []))
        #: A stopped LemonCrow-hosted server is woken once per client: ``None`` until asked, then whether it was.
        self._woken: bool | None = None
        self._wake_lock = threading.Lock()
        #: Set while this thread's request is tried again after a wake: it is tried again once.
        self._retrying = threading.local()

    def _wake(self) -> bool:
        """Wake this server when it is LemonCrow-hosted, once per client; whether a request should be tried again."""

        if getattr(self._retrying, "active", False):
            return False
        with self._wake_lock:
            if self._woken is None:
                self._woken = False
                if not _account_server(self.server):
                    return False
                sys.stderr.write(
                    f"lc task: the Workspaces server at {self.server} does not answer; "
                    "asking LemonCrow to start it (this can take a few minutes)\n"
                )
                _wake_hosted_server(self.server)
                self._woken = True
            return self._woken

    def post(self, url: str, token: str, payload: bytes, *, timeout: float) -> tuple[int, bytes]:
        """POST ``payload`` (JSON) to ``url``; the status and body of a 2xx reply, else :class:`ServerError`."""

        return self._send("POST", url, token, payload, timeout)

    def call(
        self,
        method: str,
        path: str,
        token: str,
        body: Mapping[str, object] | None = None,
        *,
        timeout: float = _API_TIMEOUT_S,
    ) -> dict[str, Any]:
        """One API call on ``{server}{path}``; the reply's JSON object."""

        payload = None if method == "GET" else json.dumps(body or {}).encode("utf-8")
        status, data = self._send(method, self.server + path, token, payload, timeout)
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            raise ServerError(f"the Workspaces server's reply is not a JSON object (HTTP {status})")
        return parsed

    def _send(self, method: str, url: str, token: str, payload: bytes | None, timeout: float) -> tuple[int, bytes]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return int(response.status), _read_capped(response)
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, token) from None
        except (urllib.error.URLError, HTTPException, OSError) as exc:
            if _unreachable(exc) and self._wake():
                self._retrying.active = True
                try:
                    return self._send(method, url, token, payload, timeout)
                finally:
                    self._retrying.active = False
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise ServerError(
                f"cannot reach the Workspaces server at {self.server}: {_shown(reason, token) or type(exc).__name__}"
            ) from None


def _http_error(exc: urllib.error.HTTPError, token: str) -> ServerError:
    try:
        raw = exc.read(_MAX_ERROR_BYTES)
    except (OSError, HTTPException):
        raw = b""
    finally:
        exc.close()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    detail = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(detail, str) or not detail.strip():
        detail = exc.reason if isinstance(exc.reason, str) else ""
    if 300 <= exc.code < 400:
        detail = "a redirect, which is not followed: give the server's own URL with --server"
    named = {key: parsed.get(key) if isinstance(parsed, dict) else None for key in ("code", "image")}
    return ServerError(
        f"Workspaces server: {_shown(detail, token) or 'request failed'} (HTTP {exc.code})",
        refusal=named["code"] if isinstance(named["code"], str) else "",
        image=named["image"] if isinstance(named["image"], str) else "",
    )


# --------------------------------------------------------------------------- #
# The task token store                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """What ``lc task start --server`` keeps for one task; ``host`` names the agent host it registered with."""

    server: str
    id: str
    token: str = field(repr=False)
    mcp_url: str
    host: str | None = None


def _state_home() -> Path:
    configured = os.environ.get(_HOME_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".lemoncrow"


def record_path(server: str, task_id: str) -> Path:
    """The token file of task ``task_id`` on ``server``."""

    _, host, port = _origin(server_url(server))
    directory = _HOST_PORT_UNSAFE.sub("_", f"{host}_{port}")
    return _state_home() / "workspaces" / "tasks" / directory / f"{_task_id(task_id, 'ID')}.json"


def has_record(server: str, task_id: str) -> bool:
    """Whether this machine keeps a token file for ``task_id`` on ``server``: it started that task there."""

    try:
        return os.path.lexists(record_path(server, task_id))
    except ServerError:
        return False


def _private_directory(directory: Path) -> None:
    """Create ``directory`` and the store's directories above it owner-only; refuse a symbolic link among them."""

    home = _state_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = home
    for part in directory.relative_to(home).parts:
        current = current / part
        if current.is_symlink():
            raise ServerError(f"{current} is a symbolic link; the task token store does not follow one")
        with contextlib.suppress(FileExistsError):
            current.mkdir(mode=0o700)
        os.chmod(current, 0o700)


def save_record(record: TaskRecord) -> Path:
    """Write ``record`` to its owner-only token file, atomically; the file's path."""

    path = record_path(record.server, record.id)
    _private_directory(path.parent)
    payload = {"server": record.server, "id": record.id, "token": record.token, "mcp_url": record.mcp_url}
    if record.host is not None:
        payload["host"] = record.host
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(payload).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path


def load_record(server: str, task_id: str) -> TaskRecord | None:
    """The token file of ``task_id`` on ``server``; ``None`` when there is none. Refuses one others can read."""

    path = record_path(server, task_id)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ServerError(f"cannot read the task token file {path}: {exc.strerror or exc}") from None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ServerError(f"{path} is not an owner-only file of yours; remove it, or chmod 600 it")
        data = handle.read(_MAX_RECORD_BYTES + 1)
    try:
        parsed = json.loads(data) if len(data) <= _MAX_RECORD_BYTES else None
    except ValueError:
        parsed = None
    fields = {key: parsed.get(key) for key in ("server", "id", "token", "mcp_url")} if isinstance(parsed, dict) else {}
    host = parsed.get("host") if isinstance(parsed, dict) else None
    if not fields or not all(isinstance(value, str) and value for value in fields.values()):
        raise ServerError(f"{path} is not a task token file")
    if server_url(str(fields["server"])) != server_url(server) or fields["id"] != task_id:
        raise ServerError(f"{path} belongs to {_shown(fields['server'])}, not {server_url(server)}")
    return TaskRecord(
        str(fields["server"]),
        task_id,
        str(fields["token"]),
        str(fields["mcp_url"]),
        host if isinstance(host, str) else None,
    )


def delete_record(server: str, task_id: str) -> None:
    record_path(server, task_id).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# The stdio MCP bridge                                                        #
# --------------------------------------------------------------------------- #


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _request_ids(message: object) -> list[object]:
    """The ids of the requests in ``message`` (one, or a batch); a notification has none."""

    items = message if isinstance(message, list) else [message]
    return [item.get("id") for item in items if isinstance(item, dict) and "method" in item and "id" in item]


def _is_initialize(message: object) -> bool:
    items = message if isinstance(message, list) else [message]
    return any(isinstance(item, dict) and item.get("method") == "initialize" for item in items)


def _emit(sink: TextIO, message: object) -> None:
    sink.write(json.dumps(message, separators=(",", ":")) + "\n")
    sink.flush()


def _errors(message: object, code: int, text: str) -> object | None:
    """One JSON-RPC error per request in ``message``, shaped like it (a batch gets a list); ``None`` for none."""

    errors = [_rpc_error(request_id, code, text) for request_id in _request_ids(message)]
    if not errors:
        return None
    return errors if isinstance(message, list) else errors[0]


def _relay(
    client: WorkspacesClient, record: TaskRecord, line: bytes, message: object, timeout: float
) -> tuple[object | None, str | None]:
    """POST one message: the reply for stdout (``None``: nothing) and a line for the log (``None``: nothing)."""

    try:
        status, body = client.post(record.mcp_url, record.token, line, timeout=timeout)
        try:
            reply = json.loads(body) if body.strip() else None
        except ValueError:
            reply = False
        if reply is not None and not isinstance(reply, (dict, list)):
            raise ServerError(f"Workspaces server: the MCP reply is not JSON-RPC (HTTP {status})")
    except ServerError as exc:
        errors = _errors(message, -32000, str(exc))
        return errors, (None if errors is not None else f"lc task mcp: {exc}\n")
    return reply, None


def bridge(
    source: BinaryIO,
    sink: TextIO,
    *,
    client: WorkspacesClient,
    record: TaskRecord,
    log: TextIO,
    timeout: float = _LONG_TIMEOUT_S,
    workers: int = _BRIDGE_WORKERS,
) -> int:
    """Relay newline-delimited JSON-RPC from ``source`` to the task's MCP endpoint, each reply one line on ``sink``.

    Requests are posted concurrently, at most ``workers`` at a time, and each reply is written whole, under one lock,
    as soon as it arrives: replies can come out in another order than their requests, and JSON-RPC matches them by
    id. The orderings MCP needs are kept. ``initialize`` is posted once every earlier request is answered, and
    nothing after it is posted before it is answered. A notification (or a reply to the server) is posted at once,
    in order, so ``notifications/initialized`` reaches the server before the requests after it, and
    ``notifications/cancelled`` never waits behind the call it cancels.

    A request the server could not answer (unreachable, a non-2xx status, a reply that is not JSON) gets a JSON-RPC
    error with its id; a notification gets no output, and its failure goes to ``log``. Returns at end of input once
    every request is answered, or once ``sink`` is closed.
    """

    lock = threading.Lock()
    slots = threading.BoundedSemaphore(workers)
    closed = threading.Event()

    def deliver(reply: object | None, note: str | None) -> None:
        with lock:
            if closed.is_set():
                return
            try:
                if reply is not None:
                    _emit(sink, reply)
                if note is not None:
                    log.write(note)
                    log.flush()
            except (BrokenPipeError, ValueError):  # the agent host closed its end
                closed.set()

    def answer(line: bytes, message: object) -> None:
        try:
            deliver(*_relay(client, record, line, message, timeout))
        except Exception as exc:  # a request is never left unanswered by a crashed worker
            kind = type(exc).__name__
            deliver(
                _errors(message, -32603, f"lc task mcp: {kind}"), f"lc task mcp: {kind}: {_shown(exc, record.token)}\n"
            )
        finally:
            slots.release()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lc-task-mcp") as pool:
        for raw in iter(source.readline, b""):
            if closed.is_set():
                break
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                deliver(_rpc_error(None, -32700, "Parse error: not a JSON-RPC message"), None)
                continue
            if not _request_ids(message):
                deliver(*_relay(client, record, line, message, timeout))
            elif _is_initialize(message):
                held = [slots.acquire() for _ in range(workers)]  # every earlier request answered
                try:
                    deliver(*_relay(client, record, line, message, timeout))
                finally:
                    for _ in held:
                        slots.release()
            else:
                slots.acquire()  # at most ``workers`` in flight; reading waits for a free one
                pool.submit(answer, line, message)
    return 0


# --------------------------------------------------------------------------- #
# lc task ... --server URL                                                    #
# --------------------------------------------------------------------------- #


def _account_server(server: str) -> bool:
    """Whether *server* is LemonCrow-hosted: ``https://`` on ``lemoncrow.com`` or one of its subdomains."""

    scheme, host, _port = _origin(server)
    return scheme == "https" and (host == _ACCOUNT_DOMAIN or host.endswith("." + _ACCOUNT_DOMAIN))


def account_resource(server: str) -> str:
    """The resource a person's sign-in names for the Workspaces server at *server*: its own ``https://`` origin.

    It comes from the URL the client connects to, never from anything the server serves, so a server cannot ask for
    another one's token. :class:`ServerError` when *server* is not LemonCrow-hosted: no other server gets a sign-in.
    """

    url = server_url(server)
    if not _account_server(url):
        raise ServerError(
            f"{_shown(url)} is not LemonCrow-hosted (https:// on {_ACCOUNT_DOMAIN}): a sign-in is never sent to it; "
            f"it takes {TOKEN_ENV}",
            2,
        )
    try:
        return resource_origin(url)
    except ValueError:
        raise ServerError(f"not a Workspaces server URL: {_shown(url)}", 2) from None


def _require_audience(token: str, origin: str) -> None:
    """Refuse to send *token* to *origin* unless it was issued for *origin* alone (besides the issuer's userinfo)."""

    claims = token_claims(token)
    issuer, audience = claims.get("iss"), claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    userinfo = f"{issuer}/oauth2/userinfo" if isinstance(issuer, str) and issuer else None
    if [entry for entry in audiences if entry != userinfo] != [origin]:
        raise ServerError(
            f"refusing to send {origin} a sign-in issued for another resource; run `lc auth login --server {origin}`"
        )


def _signed_in_token(server: str) -> str:
    """The person's own access token for *server*'s origin from ``lc auth login``; ``""`` without a sign-in.

    It is refreshed when it has less than a minute left. The LemonCrow API's token is never sent here.
    """

    origin = account_resource(server)
    try:
        config = load_config()
    except (ClientError, ValueError) as exc:
        raise ServerError(f"your LemonCrow sign-in could not be read: {_shown(exc)}") from None
    managed = {str(managed_credentials_path(config.account_state_dir)), str(managed_credentials_path(config.state_dir))}
    if not config.hosted or not config.refresh_token or config.refresh_token_source not in managed:
        return ""
    try:
        access = workspaces_access(
            config.url,
            state_dir=config.account_state_dir,
            legacy_state_dir=config.state_dir,
            origin=origin,
            refresh_within_s=_REFRESH_MARGIN_S,
            timeout_s=_API_TIMEOUT_S,
        )
    except ClientError as exc:
        if exc.server_code == NOT_COVERED:
            raise ServerError(_shown(exc.message)) from None
        raise ServerError(
            f"your LemonCrow sign-in could not be refreshed for {origin} ({_shown(exc)}); "
            f"run `lc auth login --server {origin}`"
        ) from None
    if not access:
        return ""
    if not _TOKEN.fullmatch(access):
        raise ServerError("your LemonCrow sign-in holds a character a token cannot have; run `lc auth login`")
    _require_audience(access, origin)
    return access


def _organization_token(server: str) -> str:
    """``LEMONCROW_WORKSPACES_TOKEN``; without it, on a LemonCrow-hosted *server*, the person's own sign-in for it."""

    value = os.environ.get(TOKEN_ENV, "").strip()
    if value:
        if not _TOKEN.fullmatch(value):
            raise ServerError(f"{TOKEN_ENV} holds a character a token cannot have")
        return value
    if _account_server(server):
        signed_in = _signed_in_token(server)
        if signed_in:
            return signed_in
        origin = account_resource(server)
        raise ServerError(
            f"sign in with `lc auth login --server {origin}` (or set {TOKEN_ENV} to the organization's token)"
        )
    raise ServerError(f"set {TOKEN_ENV} to your organization's Workspaces server token")


def _task_token(client: WorkspacesClient, task_id: str) -> str:
    """The task's own token when this machine started it, else the organization token."""

    record = load_record(client.server, task_id)
    return record.token if record is not None else _organization_token(client.server)


#: Option values that are prose (a commit message, a goal, a message to an agent): they may start with "-".
_PROSE_KEYS: Final[frozenset[str]] = frozenset({"message", "goal"})


def _parse(
    arguments: Sequence[str],
    usage: str,
    count: int | range,
    options: Mapping[str, str] | None = None,
    flags: Mapping[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """``count`` positionals (a number, or a range of them), the values of ``options`` (option to key) and ``flags``
    that are given (key to ``"1"``); ``-m`` and ``--goal`` take any non-empty value."""

    known, switches = options or {}, flags or {}
    positionals: list[str] = []
    values: dict[str, str] = {}
    items = iter(arguments)
    for item in items:
        if item in _HELP:
            raise _Help
        if not item.startswith("-"):
            positionals.append(item)
            continue
        if item in switches:
            values[switches[item]] = "1"
            continue
        option, equals, inline = item.partition("=")
        key = known.get(option)
        if key is None:
            raise ServerError(f"unknown argument: {item}", 2)
        value = inline if equals else next(items, "")
        if not value.strip() or (key not in _PROSE_KEYS and value.startswith("-")):
            raise ServerError(f"{option} needs a value", 2)
        values[key] = value
    if len(positionals) not in (range(count, count + 1) if isinstance(count, int) else count):
        raise ServerError(f"usage: lc task {usage}", 2)
    return positionals, values


def _attach_argv(task_id: str, server: str) -> list[str]:
    return ["lc", "task", "mcp", task_id, "--server", server]


def _attach_environment() -> dict[str, str]:
    """What the attach command needs from this environment: ``LEMONCROW_HOME`` when it is set, made absolute (the
    agent host starts the command in a directory of its own), so the bridge finds the token file ``start`` wrote.
    Nothing else, and never a token: the server is on the command line, and the task token is in that file."""

    if not os.environ.get(_HOME_ENV, "").strip():
        return {}
    return {_HOME_ENV: str(_state_home().absolute())}


def _shell_line(env: Mapping[str, str], argv: Sequence[str]) -> str:
    return " ".join([*(f"{key}={shlex.quote(value)}" for key, value in env.items()), shlex.join(argv)])


def _registration_name(task_id: str) -> str:
    return f"lc-task-{task_id}"


def _register(host: str, record: TaskRecord, sink: TextIO) -> int:
    """Register MCP server ``lc-task-<id>`` in ``host``, running the attach command; record the host on success."""

    from . import host_install

    name = _registration_name(record.id)
    binary = host_install._host_executable(host)
    if binary is None:
        sink.write(f"lc task: {host}: host CLI not found on PATH; {name} not registered\n")
        return 1
    command = (*host_install._client_launcher(), *_attach_argv(record.id, record.server)[1:])
    env = _attach_environment()
    stale = host_install._unregistration_command(host, binary, name=name)
    if stale is not None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            host_install._run(stale, check=False)
    argv = host_install._registration_command(host, binary, command, name=name, env=env)
    try:
        host_install._run(argv, check=True)
    except subprocess.CalledProcessError as exc:
        detail = _shown(exc.stderr or exc.stdout or f"exit {exc.returncode}", record.token)
        sink.write(f"lc task: {host}: registering {name} failed: {detail}\n")
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        sink.write(f"lc task: {host}: registering {name} failed: {_shown(exc, record.token)}\n")
        return 1
    save_record(replace(record, host=host))
    sink.write(f"lc task: {host}: registered MCP server {name} -> {_shell_line(env, command)}\n")
    return 0


def _unregister(host: str, task_id: str, sink: TextIO) -> int:
    from . import host_install

    name = _registration_name(task_id)
    if host not in host_install.SUPPORTED_AGENTS:
        sink.write(f"lc task: remove MCP server {name} from {_shown(host)} by hand\n")
        return 1
    binary = host_install._host_executable(host)
    argv = None if binary is None else host_install._unregistration_command(host, binary, name=name)
    if binary is None or argv is None:
        why = "host CLI not found on PATH" if binary is None else "its CLI has no MCP remove command"
        sink.write(f"lc task: {host}: {why}; remove MCP server {name} in its settings\n")
        return 0 if binary is not None else 1
    try:
        done = host_install._run(argv, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        sink.write(f"lc task: {host}: removing {name} failed: {_shown(exc)}\n")
        return 1
    if done.returncode != 0:
        detail = _shown(done.stderr or done.stdout or f"exit {done.returncode}")
        sink.write(f"lc task: {host}: removing {name} failed: {detail}\n")
        return 1
    sink.write(f"lc task: {host}: removed MCP server {name}\n")
    return 0


def _items(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _describe(task: Mapping[str, Any], server: str, sink: TextIO) -> None:
    sink.write(f"lc task: {_shown(task.get('id'))} {_shown(task.get('state'))} on the Workspaces server {server}\n")
    for repo in _items(task.get("repos")):
        head = repo.get("head")
        at = f" at {_shown(head)[:12]}" if isinstance(head, str) and head else ""
        sink.write(f"  {_shown(repo.get('name'))}: {_shown(repo.get('branch'))} from {_shown(repo.get('base'))}{at}\n")


_START_USAGE: Final[str] = (
    "start ID --server URL [--repos A,B] [--base REF] [--host claude|codex|opencode] [--model MODEL]"
    " | [[--run AGENT] --goal TEXT [--land]]"
)
#: A model name as the server records it for the task's agent (Review's independence rule reads its family).
_MODEL: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]{1,200}")
#: Review findings ``lc task land`` prints for a repository that did not pass; the rest are counted.
_SHOWN_FINDINGS: Final[int] = 20


def _toolchain_fix(error: ServerError) -> str | None:
    """The lines that fix a start the server refused for the repositories' ``[toolchain]``; ``None`` for any other
    refusal. The command is this client's own: of what the server sent, only a well-formed image name goes into it."""

    example = "IMAGE: the image the checks run in, such as python:3.12-slim"
    if error.refusal == TOOLCHAIN_UNAVAILABLE:
        command, what = "lc verify init --toolchain IMAGE", "IMAGE: one this server can pull, such as a public image"
    elif error.refusal == TOOLCHAIN_MISSING:
        command, what = "lc verify init --toolchain IMAGE", example
    elif error.refusal == TOOLCHAIN_UNPINNED:
        try:
            name = parse_reference(error.image).name
        except RegistryError:
            name = ""
        command = shlex.join(["lc", "verify", "init", "--toolchain", name or "IMAGE"])
        what = "it pins the image to the digest its registry serves" if name else example
    else:
        return None
    return (
        f"lc task: fix it in the repository with: {command}  ({what})\n"
        f"lc task: then commit {ENVIRONMENT_FILE}, push it, and start the task again"
    )


def _start(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO, *, register: bool = True) -> int:
    """``lc task start``. With ``register=False`` (``lc task claude|codex``) ``--host`` is recorded on the server but
    nothing is registered in the host's own MCP configuration: the launcher hands it the task's tools itself."""

    from .host_install import SUPPORTED_AGENTS

    options = {"--repos": "repos", "--base": "base", "--host": "host", "--model": "model"}
    (given,), values = _parse(
        arguments, _START_USAGE, 1, {**options, "--run": "run", "--goal": "goal"}, {"--land": "land"}
    )
    task_id = _task_id(given, _START_USAGE)
    host = values.get("host")
    if host is not None and host not in SUPPORTED_AGENTS:
        raise ServerError(f"--host is one of {', '.join(SUPPORTED_AGENTS)}, not {_shown(host)}", 2)
    model = values.get("model")
    if model is not None and not _MODEL.fullmatch(model):
        raise ServerError("--model is a model name (up to 200 characters, no spaces)", 2)
    body: dict[str, object] = {"id": task_id}
    agent = {key: value for key, value in (("host", host), ("model", model)) if value}
    if "run" in values or "goal" in values:
        if agent:
            raise ServerError(
                "--run and --goal give the task to one of the server's hosted agents; --host and --model name your own",
                2,
            )
        body["run"] = _run_body(values)
    elif "land" in values:
        raise ServerError("--land goes with --goal TEXT (a hosted agent's run)", 2)
    if agent:
        # The agent that works the task, recorded by the server so Review can hold its reviewer to another family.
        body["agent"] = agent
    if "repos" in values:
        repos = list(dict.fromkeys(name.strip().strip("/") for name in values["repos"].split(",") if name.strip()))
        if not repos:
            raise ServerError("--repos needs a value", 2)
        body["repos"] = repos
    if "base" in values:
        body["base"] = values["base"]
    token = _organization_token(client.server)
    try:
        reply = client.call("POST", "/v1/tasks", token, body, timeout=_LONG_TIMEOUT_S)
    except ServerError as exc:
        fix = _toolchain_fix(exc)
        if fix is None:
            raise
        raise ServerError(f"{exc}\n{fix}") from None
    task, task_token, mcp_url = reply.get("task"), reply.get("token"), reply.get("mcp_url")
    if not isinstance(task, Mapping) or not isinstance(task_token, str) or not isinstance(mcp_url, str):
        raise ServerError("the Workspaces server's reply has no task, token and mcp_url")
    if not _TOKEN.fullmatch(task_token):
        raise ServerError("the Workspaces server sent a task token a header cannot carry")
    if task.get("id") != task_id:
        raise ServerError(f"the Workspaces server started task {_shown(task.get('id'), task_token)}, not {task_id}")
    try:
        endpoint = server_url(mcp_url)
    except ServerError:
        endpoint = ""
    if not endpoint or _origin(endpoint) != _origin(client.server):
        # The task token goes only where the person pointed --server.
        raise ServerError(
            f"the Workspaces server named an MCP endpoint off {client.server}; not using it "
            f"(discard {task_id} with the organization token)"
        )
    record = TaskRecord(client.server, task_id, task_token, endpoint)
    path = save_record(record)
    _describe(task, client.server, sink)
    sink.write(f"lc task: task token kept in {path} (owner-only)\n")
    if "run" in body:
        failed = reply.get("run_error")
        if isinstance(failed, str):
            sink.write(f"lc task: the hosted agent did not start: {_shown(failed, task_token)}\n")
            return 1
        _describe_run(task.get("run"), task_id, sink)
        sink.write(
            f"lc task: follow it with: {shlex.join(['lc', 'task', 'watch', task_id, '--server', client.server])}\n"
        )
        return 0
    if not register:
        return 0
    sink.write(f"lc task: MCP endpoint {endpoint}\n")
    attach = _shell_line(_attach_environment(), _attach_argv(task_id, client.server))
    sink.write(f"lc task: attach an agent with: {attach}\n")
    if host is not None:
        return _register(host, record, sink)
    return 0


# --------------------------------------------------------------------------- #
# Hosted agents: the server runs one of its agents on the task                #
# --------------------------------------------------------------------------- #

#: Seconds between two looks at a hosted agent's log while ``lc task watch`` follows it.
_WATCH_INTERVAL_S: float = 1.0
_WATCH_PAGE: Final[int] = 200


def _run_body(values: Mapping[str, str]) -> dict[str, object]:
    """A hosted run: ``run`` names the agent; without it the server gives the task to its default agent."""

    if "goal" not in values:
        raise ServerError("a hosted agent needs --goal TEXT: what it should do", 2)
    body: dict[str, object] = {"goal": values["goal"], "land": "land" in values}
    agent = values.get("run")
    if agent is not None:
        if not _ID.fullmatch(agent):
            raise ServerError("--run names a hosted agent (letters, digits, '.', '_', '-')", 2)
        body["agent"] = agent
    return body


def _describe_run(run: object, task_id: str, sink: TextIO) -> None:
    if not isinstance(run, Mapping):
        sink.write(f"lc task: no hosted agent has worked {task_id}\n")
        return
    seconds, spend = run.get("seconds"), run.get("spend_usd")
    minutes = f"{seconds / 60:.1f} min" if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else ""
    dollars = f"${spend:.2f}" if isinstance(spend, (int, float)) and not isinstance(spend, bool) and spend else ""
    spent = ", ".join(part for part in (f"{_shown(run.get('rounds'))} rounds", minutes, dollars) if part)
    land = "lands when done" if run.get("land") is True else "a person lands"
    reason = f": {_shown(run.get('reason'))}" if run.get("reason") else ""
    sink.write(
        f"lc task: {task_id}: hosted agent {_shown(run.get('agent'))} {_shown(run.get('state'))} "
        f"({spent}; {land}){reason}\n"
    )


def _event_lines(record: Mapping[str, Any]) -> list[str]:
    """One logged event as a person reads it: the agent's words, its tool calls, the result, the server's notes."""

    event, stream = record.get("event"), record.get("stream")
    if not isinstance(event, Mapping):
        return []
    kind = event.get("type")
    if stream == "lemoncrow":
        return [f"lc: {_shown(event.get('text'))}"]
    if kind == "text":
        prefix = "stderr: " if stream == "stderr" else ""
        return [f"{prefix}{_shown(event.get('text'))}"]
    if kind == "cut":
        return [f"[an event of {_shown(event.get('bytes'))} bytes, cut]"]
    if kind == "assistant":
        message = event.get("message")
        lines: list[str] = []
        for part in _items(message.get("content") if isinstance(message, Mapping) else None):
            if part.get("type") == "text" and part.get("text"):
                lines.append(_shown(part.get("text")))
            elif part.get("type") == "tool_use":
                arguments = json.dumps(part.get("input"), sort_keys=True)
                lines.append(f"-> {_shown(part.get('name'))} {_shown(arguments)[:200]}")
        return lines
    if kind == "result":
        cost = event.get("total_cost_usd")
        spent = f", ${cost:.2f}" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else ""
        turns = event.get("num_turns")
        took = f", {turns} turns" if isinstance(turns, int) and not isinstance(turns, bool) else ""
        if event.get("is_error") is True:  # Claude Code says subtype "success" for a refused key, with is_error set
            said = event.get("result")
            return [f"result: error{took}{spent}" + (f": {_shown(said)[:300]}" if isinstance(said, str) else "")]
        return [f"result: {_shown(event.get('subtype') or 'done')}{took}{spent}"]
    return []  # system and tool-result events: the log has them, a person watching does not need them


def _run(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    usage = "run ID [AGENT] --goal TEXT [--land] --server URL"
    positionals, values = _parse(arguments, usage, range(1, 3), {"--goal": "goal"}, {"--land": "land"})
    task_id = _task_id(positionals[0], usage)
    body = _run_body({**values, **({"run": positionals[1]} if len(positionals) == 2 else {})})
    reply = client.call("POST", f"/v1/tasks/{_segment(task_id)}/agent/start", _organization_token(client.server), body)
    _describe_run(reply.get("run"), task_id, sink)
    sink.write(f"lc task: follow it with: {shlex.join(['lc', 'task', 'watch', task_id, '--server', client.server])}\n")
    return 0


def _watch(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    """Print the hosted agent's events as they come, until its run ends; 1 when it ends stopped or interrupted."""

    usage = "watch ID --server URL"
    (given,), _ = _parse(arguments, usage, 1)
    task_id = _task_id(given, usage)
    token = _task_token(client, task_id)
    base = f"/v1/tasks/{_segment(task_id)}/agent"
    after = 0

    def drain() -> int:
        nonlocal after
        shown = 0
        while True:
            page = client.call("GET", f"{base}/log?after={after}&limit={_WATCH_PAGE}", token)
            records = _items(page.get("events"))
            for record in records:
                for line in _event_lines(record):
                    sink.write(f"{line}\n")
                number = record.get("n")
                if isinstance(number, int) and not isinstance(number, bool) and number > after:
                    after = number
            sink.flush()
            shown += len(records)
            if len(records) < _WATCH_PAGE:
                return shown

    try:
        while True:
            drain()
            status = client.call("GET", base, token)
            if status.get("working") is not True:
                drain()  # what it logged while ending
                break
            time.sleep(_WATCH_INTERVAL_S)
    except KeyboardInterrupt:
        sink.write("\n")
        return 130
    run = status.get("run")
    _describe_run(run, task_id, sink)
    state = run.get("state") if isinstance(run, Mapping) else None
    return 0 if state in {"landed", "waiting"} else 1


def _stop(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    usage = "stop ID --server URL"
    (given,), _ = _parse(arguments, usage, 1)
    task_id = _task_id(given, usage)
    reply = client.call("POST", f"/v1/tasks/{_segment(task_id)}/agent/stop", _task_token(client, task_id))
    sink.write(f"lc task: asked the hosted agent on {task_id} to stop; its round ends in seconds\n")
    _describe_run(reply.get("run"), task_id, sink)
    return 0


def _tell(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    usage = "tell ID -m MESSAGE --server URL"
    (given,), values = _parse(arguments, usage, 1, {"-m": "message", "--message": "message"})
    task_id = _task_id(given, usage)
    if "message" not in values:
        raise ServerError(f"usage: lc task {usage}  (-m: what the agent should do next)", 2)
    path = f"/v1/tasks/{_segment(task_id)}/agent/message"
    reply = client.call("POST", path, _task_token(client, task_id), {"text": values["message"]})
    _describe_run(reply.get("run"), task_id, sink)
    return 0


def _attach(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    """Take a task over (a hosted agent's, or one this machine did not start): a new task token, kept here."""

    from .host_install import SUPPORTED_AGENTS

    usage = "attach ID --server URL [--host claude|codex|opencode] [--model MODEL]"
    (given,), values = _parse(arguments, usage, 1, {"--host": "host", "--model": "model"})
    task_id = _task_id(given, usage)
    host, model = values.get("host"), values.get("model")
    if host is not None and host not in SUPPORTED_AGENTS:
        raise ServerError(f"--host is one of {', '.join(SUPPORTED_AGENTS)}, not {_shown(host)}", 2)
    if model is not None and not _MODEL.fullmatch(model):
        raise ServerError("--model is a model name (up to 200 characters, no spaces)", 2)
    agent = {key: value for key, value in (("host", host), ("model", model)) if value}
    reply = client.call(
        "POST",
        f"/v1/tasks/{_segment(task_id)}/token",
        _organization_token(client.server),
        {"agent": agent},
        timeout=_LONG_TIMEOUT_S,
    )
    task_token, mcp_url = reply.get("token"), reply.get("mcp_url")
    if not isinstance(task_token, str) or not _TOKEN.fullmatch(task_token) or not isinstance(mcp_url, str):
        raise ServerError("the Workspaces server's reply has no usable token and mcp_url")
    try:
        endpoint = server_url(mcp_url)
    except ServerError:
        endpoint = ""
    if not endpoint or _origin(endpoint) != _origin(client.server):
        raise ServerError(f"the Workspaces server named an MCP endpoint off {client.server}; not using it")
    record = TaskRecord(client.server, task_id, task_token, endpoint)
    path = save_record(record)
    sink.write(f"lc task: {task_id} is yours; any earlier token for it no longer works\n")
    sink.write(f"lc task: task token kept in {path} (owner-only)\n")
    attach = _shell_line(_attach_environment(), _attach_argv(task_id, client.server))
    sink.write(f"lc task: attach an agent with: {attach}\n")
    if host is not None:
        return _register(host, record, sink)
    return 0


# --------------------------------------------------------------------------- #
# lc task claude|codex: this machine's agent, with the task's tools only       #
# --------------------------------------------------------------------------- #

#: The task's MCP tools an attached agent calls without asking; ``land`` asks the person every time.
_ATTACHED_TOOLS: Final[tuple[str, ...]] = ("read", "code_search", "edit", "bash", "blame", "log", "merge_base")
_TASK_TOOLS: Final[tuple[str, ...]] = (*_ATTACHED_TOOLS, "land")
#: Seconds Codex waits for one task tool call: land and bash run checks on the server before they answer.
_CODEX_TOOL_TIMEOUT_S: Final[int] = int(_LONG_TIMEOUT_S)


def _attached_prompt(task_id: str) -> str:
    return (
        f"You are working LemonCrow Workspaces task {task_id}. Its files, commands and checks live on the Workspaces "
        "server and are reached only through the lc MCP server's tools: read, code_search and edit on the task's "
        "files, bash in the task's sandbox, and Git history (blame, log, merge_base). Nothing on this machine is the "
        "task's. Run the repository's own checks with bash before you call the work done, and use land (Review, then "
        "push) only when the person asks."
    )


def _toml(value: str) -> str:
    # A JSON string is a TOML basic string: the same quotes and escapes.
    return json.dumps(value)


def _launch_argv(
    host: str, binary: str, task_id: str, bridge_cmd: Sequence[str], env: Mapping[str, str], work: Path, model: str
) -> list[str]:
    """The host's command line: the task's tools as MCP server ``lc``, the host's own tools off, *work* (an empty
    directory) as its working directory."""

    prompt = _attached_prompt(task_id)
    if host == "claude":
        config = work.parent / "mcp.json"
        server = {"type": "stdio", "command": bridge_cmd[0], "args": list(bridge_cmd[1:]), "env": dict(env)}
        config.write_text(json.dumps({"mcpServers": {"lc": server}}), encoding="utf-8")
        return [
            binary,
            "--mcp-config",
            str(config),
            "--strict-mcp-config",  # only the task's tools: none of the person's other MCP servers
            "--tools",
            "",  # no built-in Read, Edit, Bash ...: nothing on this machine
            "--allowedTools",
            ",".join(f"mcp__lc__{tool}" for tool in _ATTACHED_TOOLS),
            "--append-system-prompt",
            prompt,
            *(["--model", model] if model else []),
        ]
    # Codex has no strict MCP mode: ``lc`` replaces any server of that name, the others stay. Its shell is off and
    # its sandbox is read-only, so the task's tools are the only way to change anything.
    settings = [
        f"mcp_servers.lc.command={_toml(bridge_cmd[0])}",
        f"mcp_servers.lc.args=[{', '.join(_toml(item) for item in bridge_cmd[1:])}]",
        f"mcp_servers.lc.env={{{', '.join(f'{key} = {_toml(value)}' for key, value in env.items())}}}",
        f"mcp_servers.lc.enabled_tools=[{', '.join(_toml(tool) for tool in _TASK_TOOLS)}]",
        "mcp_servers.lc.disabled_tools=[]",
        f"mcp_servers.lc.tool_timeout_sec={_CODEX_TOOL_TIMEOUT_S}",
        f"developer_instructions={_toml(prompt)}",
    ]
    return [
        binary,
        "-C",
        str(work),
        "--sandbox",
        "read-only",
        "--disable",
        "unified_exec",
        "--disable",
        "shell_tool",
        *(item for setting in settings for item in ("-c", setting)),
        *(["--model", model] if model else []),
    ]


def _launcher(host: str) -> Callable[[WorkspacesClient, Sequence[str], TextIO], int]:
    """``lc task claude|codex ID``: start the task when this machine has not, then run that agent on it."""

    def launch(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
        from . import host_install

        usage = f"{host} ID --server URL [--repos A,B] [--base REF] [--model MODEL] [-- {host.upper()} ARGUMENTS]"
        items = list(arguments)
        cut = items.index("--") if "--" in items else len(items)
        extra = items[cut + 1 :]
        (given,), values = _parse(items[:cut], usage, 1, {"--repos": "repos", "--base": "base", "--model": "model"})
        task_id = _task_id(given, usage)
        binary = host_install._host_executable(host)
        if binary is None:
            raise ServerError(f"{host} is not on PATH")
        model = values.get("model", "")
        if load_record(client.server, task_id) is None:
            start = [
                task_id,
                "--host",
                host,
                *(item for key in ("repos", "base", "model") if key in values for item in (f"--{key}", values[key])),
            ]
            code = _start(client, start, sink, register=False)
            if code != 0:
                return code
        elif "repos" in values or "base" in values:
            raise ServerError(
                f"{task_id} is already started from this machine; --repos and --base apply to a new task", 2
            )
        bridge_cmd = (*host_install._client_launcher(), *_attach_argv(task_id, client.server)[1:])
        with tempfile.TemporaryDirectory(prefix=f"lc-task-{task_id}-") as scratch:
            work = Path(scratch) / "work"
            work.mkdir(mode=0o700)
            argv = _launch_argv(host, binary, task_id, bridge_cmd, _attach_environment(), work, model)
            sink.write(f"lc task: {host} on {task_id} (the task's tools only; its own tools are off)\n")
            sink.flush()
            try:
                return subprocess.run([*argv, *extra], cwd=work, check=False).returncode
            except OSError as exc:
                raise ServerError(f"{host} did not start: {_shown(exc)}") from None

    return launch


def _verify(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    (given,), _ = _parse(arguments, "verify ID --server URL", 1)
    task_id = _task_id(given, "verify ID --server URL")
    reply = client.call(
        "POST", f"/v1/tasks/{_segment(task_id)}/verify", _task_token(client, task_id), timeout=_LONG_TIMEOUT_S
    )
    for repo in _items(reply.get("repos")):
        state = "passed" if repo.get("passed") is True else "failed"
        commit = repo.get("commit")
        at = f" at {_shown(commit)[:12]}" if isinstance(commit, str) and commit else ""
        saved = f", saved as {_shown(repo.get('saved_ref'))}" if repo.get("saved_ref") else ""
        sink.write(f"{_shown(repo.get('repo'))}: {state}{at}{saved}\n")
        for check in _items(repo.get("checks")):
            seconds = check.get("seconds")
            took = f"  {seconds:.1f}s" if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else ""
            sink.write(f"  {_shown(check.get('id'))}  {_shown(check.get('status'))}{took}\n")
    passed = reply.get("passed") is True
    sink.write(f"lc task: {task_id} {'passed' if passed else 'failed'} its checks on the Workspaces server\n")
    return 0 if passed else 1


def _land(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    usage = "land ID -m MESSAGE --server URL"
    (given,), values = _parse(arguments, usage, 1, {"-m": "message", "--message": "message"})
    task_id = _task_id(given, usage)
    if "message" not in values:
        raise ServerError(f"usage: lc task {usage}  (-m: the commit message)", 2)
    reply = client.call(
        "POST",
        f"/v1/tasks/{_segment(task_id)}/land",
        _task_token(client, task_id),
        {"message": values["message"]},
        timeout=_LONG_TIMEOUT_S,
    )
    if reply.get("landed") is not True:
        sink.write(f"lc task: {task_id} not landed: {_shown(reply.get('reason') or 'the server gave no reason')}\n")
        _findings(reply, sink)
        return 1
    for pushed in _items(reply.get("pushed")):
        sink.write(f"lc task: {_shown(pushed.get('repo'))}: pushed {_shown(pushed.get('commit'))[:12]}\n")
    sink.write(
        f"lc task: landed {task_id}. Next: {shlex.join(['lc', 'task', 'finish', task_id, '--server', client.server])}\n"
    )
    return 0


def _findings(reply: Mapping[str, Any], sink: TextIO) -> None:
    """Review's findings for each repository it did not pass, as the agent should read them."""

    for review in _items(reply.get("reviews")):
        if review.get("verdict") == "pass":
            continue
        findings = _items(review.get("findings"))
        sink.write(f"  {_shown(review.get('repo'))}: Review: {_shown(review.get('reason') or 'failed')}\n")
        for finding in findings[:_SHOWN_FINDINGS]:
            path, line = finding.get("path"), finding.get("line")
            where = f" {_shown(path)}" if isinstance(path, str) and path else ""
            if where and isinstance(line, int) and not isinstance(line, bool) and line > 0:
                where += f":{line}"
            severity = _shown(finding.get("severity") or "?").upper()
            sink.write(f"    {severity}{where}  {_shown(finding.get('title'))}\n")
        if len(findings) > _SHOWN_FINDINGS:
            sink.write(f"    ... and {len(findings) - _SHOWN_FINDINGS} more\n")


def _finish(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    (given,), _ = _parse(arguments, "finish ID --server URL", 1)
    task_id = _task_id(given, "finish ID --server URL")
    record = load_record(client.server, task_id)
    token = record.token if record is not None else _organization_token(client.server)
    reply = client.call("POST", f"/v1/tasks/{_segment(task_id)}/finish", token, timeout=_LONG_TIMEOUT_S)
    if reply.get("finished") is not True:
        raise ServerError(f"the Workspaces server did not finish {task_id}")
    for saved in _items(reply.get("saved")):
        commit = _shown(saved.get("commit"))[:12]
        sink.write(f"lc task: {_shown(saved.get('repo'))}: work kept as {_shown(saved.get('ref'))} ({commit})\n")
    code = 0
    if record is not None and record.host is not None:
        code = _unregister(record.host, task_id, sink)
    delete_record(client.server, task_id)
    sink.write(f"lc task: finished {task_id} on the Workspaces server; its token is removed from this machine\n")
    return code


def _discard(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    (given,), _ = _parse(arguments, "discard ID --server URL", 1)
    task_id = _task_id(given, "discard ID --server URL")
    reply = client.call("POST", f"/v1/tasks/{_segment(task_id)}/discard", _task_token(client, task_id))
    discarded = _items(reply.get("discarded"))
    for item in discarded:
        sink.write(f"lc task: {_shown(item.get('repo'))}: discarded {_shown(item.get('ref'))}\n")
    if not discarded:
        sink.write(f"lc task: {task_id}: nothing to discard\n")
    return 0


def _list(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    _parse(arguments, "list --server URL", 0)
    tasks = _items(client.call("GET", "/v1/tasks", _organization_token(client.server)).get("tasks"))
    for task in tasks:
        repos = ", ".join(
            f"{_shown(repo.get('name'))} {_shown(repo.get('branch'))}" for repo in _items(task.get("repos"))
        )
        sink.write(
            f"{_shown(task.get('id'))}  {_shown(task.get('state'))}  {repos or '(no repositories)'}  "
            f"last used {_when(task.get('last_used_at'))}\n"
        )
    if not tasks:
        sink.write(f"lc task: no tasks on the Workspaces server {client.server}\n")
    return 0


def _preview(client: WorkspacesClient, arguments: Sequence[str], sink: TextIO) -> int:
    usage = "preview ID NAME --server URL"
    (given, name), _ = _parse(arguments, usage, 2)
    task_id = _task_id(given, usage)
    if not _ID.fullmatch(name):
        raise ServerError(f"usage: lc task {usage}  (NAME: letters, digits, '.', '_', '-')", 2)
    path = f"/v1/tasks/{_segment(task_id)}/previews/{_segment(name)}"
    url = client.call("POST", path, _task_token(client, task_id)).get("url")
    if not isinstance(url, str) or not url:
        raise ServerError("the Workspaces server's reply has no preview url")
    sink.write(f"lc task: one-time link to preview {name}: {_shown(url)}\n")
    return 0


def _mcp(client: WorkspacesClient, arguments: Sequence[str], source: BinaryIO, sink: TextIO, log: TextIO) -> int:
    (given,), _ = _parse(arguments, "mcp ID --server URL", 1)
    task_id = _task_id(given, "mcp ID --server URL")
    record = load_record(client.server, task_id)
    if record is None:
        start = shlex.join(["lc", "task", "start", task_id, "--server", client.server])
        raise ServerError(f"no task token for {task_id} on this machine; start the task with: {start}")
    try:
        return bridge(source, sink, client=client, record=record, log=log)
    except BrokenPipeError:
        return 0  # the agent host closed its end


_COMMANDS: Final[dict[str, Callable[[WorkspacesClient, Sequence[str], TextIO], int]]] = {
    "start": _start,
    "verify": _verify,
    "land": _land,
    "finish": _finish,
    "discard": _discard,
    "list": _list,
    "preview": _preview,
    "run": _run,
    "watch": _watch,
    "stop": _stop,
    "tell": _tell,
    "attach": _attach,
    "claude": _launcher("claude"),
    "codex": _launcher("codex"),
}
#: Verbs that exist only for tasks on a Workspaces server.
SERVER_ONLY: Final[frozenset[str]] = frozenset(
    {"verify", "discard", "preview", "mcp", "run", "watch", "stop", "tell", "attach", "claude", "codex"}
)


def server_task_main(
    arguments: Sequence[str],
    server: str,
    sink: TextIO,
    *,
    usage: str,
    source: BinaryIO | None = None,
    log: TextIO | None = None,
) -> int:
    """``lc task VERB ... --server URL``. ``lc task mcp`` keeps ``sink`` for JSON-RPC and reports on ``log``."""

    if not arguments or arguments[0] in {*_HELP, "help"}:
        sink.write(usage)
        return 0 if arguments else 2
    verb, rest = arguments[0], arguments[1:]
    command = _COMMANDS.get(verb)
    if command is None and verb != "mcp":
        sink.write(f"lc task: unknown command: {verb}\n{usage}")
        return 2
    out = (log or sys.stderr) if verb == "mcp" else sink
    try:
        client = WorkspacesClient(server)
        if command is None:
            return _mcp(client, rest, source or sys.stdin.buffer, sink, out)
        return command(client, rest, sink)
    except _Help:
        out.write(usage)
        return 0
    except ServerError as exc:
        out.write(f"lc task: {exc}\n")
        return exc.code
