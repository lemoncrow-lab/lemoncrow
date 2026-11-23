"""Managed Authward credentials and transparent access-token rotation.

The hosted CLI stores one Authward sign-in under ``LEMONCROW_HOME``: the newest of its rotating refresh tokens and one
access token per resource the sign-in named. The LemonCrow API's token is ``access_token``; each LemonCrow-hosted
Workspaces server the sign-in also named has its own under ``servers``, keyed by the server's ``https://`` origin
(empty until it is first asked for). It is one JSON file so refresh-token rotation is persisted atomically: a process
crash can never leave a fresh access token beside a spent refresh token. Every rotation, for any resource, re-reads the
file under one cross-process lock, so only the newest refresh token is ever sent.

The file before Workspaces servers had their own tokens held the API pair alone (``{"access_token", "refresh_token"}``):
it reads as a sign-in that named no Workspaces server, and the next rotation writes it in this layout. The API pair
keeps its two keys, so a client that predates this layout still reads it. Operator-provided credentials remain
outside this module and retain precedence in :mod:`lemoncrow_client.config`.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import json
import os
import stat
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from .authward import discover_authward
from .errors import AgentAction, ClientError, ErrorCode
from .transport import HttpTransport, Response

__all__ = [
    "NOT_COVERED",
    "ManagedSignIn",
    "RefreshingHttpTransport",
    "clear_managed_credentials",
    "managed_credentials_path",
    "read_managed_credentials",
    "read_managed_sign_in",
    "refresh_managed_access",
    "resource_origin",
    "store_new_sign_in",
    "token_claims",
    "workspaces_access",
    "write_managed_credentials",
]

_MAX_CREDENTIAL_BYTES: Final[int] = 64 * 1024
_MAX_TOKEN_BYTES: Final[int] = 4096
#: Workspaces servers one sign-in may name besides the LemonCrow API.
_MAX_SERVERS: Final[int] = 16
_LAYOUT: Final[int] = 2
#: ``ClientError.server_code`` when the sign-in did not name a Workspaces server's origin.
NOT_COVERED: Final[str] = "sign_in_does_not_cover_server"


@dataclass(frozen=True, slots=True)
class ManagedSignIn:
    """One Authward sign-in as the credential file keeps it.

    ``access_token`` is for the LemonCrow API. ``servers`` maps each Workspaces server origin the sign-in also named to
    its own access token, ``""`` until one is first asked for. ``refresh_token`` is the newest of the sign-in's rotating
    refresh tokens; it covers every one of those resources.
    """

    access_token: str
    refresh_token: str = field(repr=False)
    servers: Mapping[str, str] = field(default_factory=dict, repr=False)


def managed_credentials_path(state_dir: Path) -> Path:
    return state_dir / "auth" / "credentials.json"


def _refresh_lock_path(state_dir: Path) -> Path:
    return managed_credentials_path(state_dir).with_name("refresh.lock")


@contextmanager
def _process_refresh_lock(state_dir: Path) -> Iterator[None]:
    """Serialize one-time refresh rotation across independent CLI processes."""

    path = _refresh_lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _validate_token(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"managed hosted credential has an invalid {field}",
            action=AgentAction.REAUTHENTICATE,
        )
    return value


def resource_origin(url: str) -> str:
    """*url*'s origin as an OAuth resource names it: ``scheme://host[:port]``, lowercase, no default port, no path.

    Raises :class:`ValueError` for anything that is not an ``http(s)`` URL with a plain ASCII host.
    """

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not host or not host.isascii() or parts.username or parts.password:
        raise ValueError("not an http(s) URL with a plain host")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


def _is_origin(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 300:
        return False
    try:
        return resource_origin(value) == value
    except ValueError:
        return False


def token_claims(token: str) -> Mapping[str, Any]:
    """The claims of the JWT *token*, read without verifying it (to refresh in time, or to check whom it is for)."""

    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, binascii.Error):
        return {}
    return claims if isinstance(claims, dict) else {}


def _expires_within(token: str, seconds: float) -> bool:
    expires = token_claims(token).get("exp")
    if not isinstance(expires, int | float) or isinstance(expires, bool):
        return False
    return bool(expires - time.time() < seconds)


def _invalid(path: Path, what: str) -> ClientError:
    return ClientError(
        ErrorCode.NOT_CONFIGURED,
        f"managed hosted credential {what}: {path}",
        action=AgentAction.REAUTHENTICATE,
    )


def read_managed_sign_in(state_dir: Path) -> ManagedSignIn | None:
    """The sign-in in the managed credential file, ``None`` without one; either layout reads."""

    path = managed_credentials_path(state_dir)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _invalid(path, "is not readable") from exc
    if info.st_size > _MAX_CREDENTIAL_BYTES:
        raise _invalid(path, f"is larger than {_MAX_CREDENTIAL_BYTES} bytes")
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise _invalid(path, "must not be group- or world-accessible (chmod 600)")
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(path, "is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise _invalid(path, "is not a JSON object")
    layout = payload.get("version", 1)
    if layout not in (1, _LAYOUT) or isinstance(layout, bool):
        raise _invalid(path, "has a layout this client does not know; run `lc auth login`")
    access = _validate_token(payload.get("access_token"), "access token")
    refresh = _validate_token(payload.get("refresh_token"), "refresh token")
    raw = payload.get("servers", {}) if layout == _LAYOUT else {}
    if not isinstance(raw, dict) or len(raw) > _MAX_SERVERS:
        raise _invalid(path, f"does not map at most {_MAX_SERVERS} Workspaces servers to their tokens")
    servers: dict[str, str] = {}
    for origin, token in raw.items():
        if not _is_origin(origin):
            raise _invalid(path, "names a Workspaces server that is not an origin")
        servers[origin] = "" if token == "" else _validate_token(token, "Workspaces server access token")
    return ManagedSignIn(access_token=access, refresh_token=refresh, servers=servers)


def read_managed_credentials(state_dir: Path) -> tuple[str, str]:
    """Return the LemonCrow API's ``(access_token, refresh_token)`` from the managed credential file."""

    sign_in = read_managed_sign_in(state_dir)
    return ("", "") if sign_in is None else (sign_in.access_token, sign_in.refresh_token)


def _write_sign_in(state_dir: Path, sign_in: ManagedSignIn) -> None:
    """Atomically replace the managed credential file with *sign_in*, mode 0600."""

    access = _validate_token(sign_in.access_token, "access token")
    refresh = _validate_token(sign_in.refresh_token, "refresh token")
    if len(sign_in.servers) > _MAX_SERVERS:
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            f"one sign-in names at most {_MAX_SERVERS} Workspaces servers",
            action=AgentAction.FIX_REQUEST,
        )
    servers: dict[str, str] = {}
    for origin, token in sorted(sign_in.servers.items()):
        if not _is_origin(origin):
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                "a Workspaces server of a sign-in is named by its origin",
                action=AgentAction.FIX_REQUEST,
            )
        servers[origin] = "" if token == "" else _validate_token(token, "Workspaces server access token")
    path = managed_credentials_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = (
        json.dumps(
            {"version": _LAYOUT, "access_token": access, "refresh_token": refresh, "servers": servers},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_managed_credentials(
    state_dir: Path, *, access_token: str, refresh_token: str, servers: Iterable[str] = ()
) -> None:
    """Atomically replace the managed credential with a new sign-in, mode 0600.

    *servers* are the Workspaces server origins the sign-in named besides the LemonCrow API; each gets its own access
    token when first asked for (:func:`workspaces_access`).
    """

    _write_sign_in(
        state_dir,
        ManagedSignIn(access_token=access_token, refresh_token=refresh_token, servers=dict.fromkeys(servers, "")),
    )


def store_new_sign_in(state_dir: Path, *, access_token: str, refresh_token: str, servers: Iterable[str] = ()) -> None:
    """:func:`write_managed_credentials` under the rotation lock, so no refresh in flight writes over a new sign-in."""

    with _process_refresh_lock(state_dir):
        write_managed_credentials(state_dir, access_token=access_token, refresh_token=refresh_token, servers=servers)


def clear_managed_credentials(state_dir: Path) -> None:
    path = managed_credentials_path(state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    try:
        path.parent.rmdir()
    except OSError:
        pass


class RefreshingHttpTransport(HttpTransport):
    """LemonCrow transport that refreshes its Authward bearer on HTTP 401.

    LemonCrow discovery names the trusted Authward issuer. Refresh then goes
    directly to Authward, never through LemonCrow's legacy ``/v1/auth/refresh``
    broker. The process/file locks still serialize refresh-token rotation.
    """

    __slots__ = ("_access_token", "_legacy_state_dir", "_refresh_lock", "_refresh_token", "_state_dir")

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float,
        state_dir: Path,
        access_token: str,
        refresh_token: str,
        legacy_state_dir: Path | None = None,
    ) -> None:
        super().__init__(url, timeout_s=timeout_s)
        self._state_dir = state_dir
        self._legacy_state_dir = legacy_state_dir
        self._access_token = _validate_token(access_token, "access token")
        self._refresh_token = _validate_token(refresh_token, "refresh token")
        self._refresh_lock = threading.Lock()

    def _current_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        out = dict(headers or {})
        if "Authorization" in out:
            out["Authorization"] = f"Bearer {self._access_token}"
        return out

    @staticmethod
    def _bearer_value(headers: Mapping[str, str]) -> str:
        scheme, _, token = headers.get("Authorization", "").partition(" ")
        return token if scheme.lower() == "bearer" else ""

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Response:
        request_headers = self._current_headers(headers)
        sent_access = self._bearer_value(request_headers)
        response = HttpTransport.request(
            self,
            method,
            path,
            body=body,
            headers=request_headers,
            timeout_s=timeout_s,
        )
        if response.status != 401 or not sent_access:
            return response

        with self._refresh_lock:
            try:
                access = refresh_managed_access(
                    self.url,
                    state_dir=self._state_dir,
                    legacy_state_dir=self._legacy_state_dir,
                    stale_access=sent_access,
                    timeout_s=self._timeout_s if timeout_s is None else timeout_s,
                    connect_timeout_s=self._timeout_s,
                )
            except ClientError as exc:
                if exc.server_code == "invalid_grant":
                    self._access_token = ""
                    self._refresh_token = ""
                raise
            if not access:
                return response
            self._access_token = access
            return HttpTransport.request(
                self,
                method,
                path,
                body=body,
                headers=self._current_headers(headers),
                timeout_s=timeout_s,
            )


def _locked_sign_in(state_dir: Path, legacy_state_dir: Path | None) -> ManagedSignIn | None:
    """The sign-in to rotate, read with the rotation lock held; one under the legacy shared root is imported first."""

    sign_in = read_managed_sign_in(state_dir)
    if sign_in is None and legacy_state_dir is not None:
        sign_in = read_managed_sign_in(legacy_state_dir)
        if sign_in is not None:
            _write_sign_in(state_dir, sign_in)
    return sign_in


def _refuse_legacy_session(sign_in: ManagedSignIn) -> None:
    if sign_in.access_token.startswith("lcs_") or sign_in.refresh_token.startswith("lcr_"):
        raise ClientError(
            ErrorCode.UNAUTHENTICATED,
            "this managed credential is a legacy LemonCrow session; run `lc auth login` to refresh your hosted sign-in",
            action=AgentAction.REAUTHENTICATE,
        )


def _rotate(
    url: str,
    sign_in: ManagedSignIn,
    *,
    resource: str,
    state_dir: Path,
    legacy_state_dir: Path | None,
    timeout_s: float,
    connect_timeout_s: float | None,
) -> tuple[str, str]:
    """One refresh-token request naming *resource* (``""``: the LemonCrow API); ``invalid_grant`` ends the sign-in."""

    authward = discover_authward(
        HttpTransport(url, timeout_s=connect_timeout_s if connect_timeout_s is not None else timeout_s),
        timeout_s=timeout_s,
    )
    try:
        if resource:
            access, refresh = authward.refresh(sign_in.refresh_token, resource=resource)
        else:
            access, refresh = authward.refresh(sign_in.refresh_token)
    except ClientError as exc:
        if exc.server_code == "invalid_grant":
            clear_managed_credentials(state_dir)
            if legacy_state_dir is not None:
                clear_managed_credentials(legacy_state_dir)
        raise
    return _validate_token(access, "access token"), _validate_token(refresh, "refresh token")


def refresh_managed_access(
    url: str,
    *,
    state_dir: Path,
    legacy_state_dir: Path | None = None,
    stale_access: str,
    timeout_s: float,
    connect_timeout_s: float | None = None,
) -> str:
    """An API access token to use instead of *stale_access* (refused, or about to run out); ``""`` without a sign-in.

    *url* is the hosted LemonCrow server that names the trusted Authward issuer. The rotation is serialized across
    processes: when another one already rotated the pair, its access token is returned and nothing is refreshed.
    Refresh tokens are one-time credentials, so the sign-in is re-read only after taking the process lock. During the
    Core-state migration a login may still live under the legacy shared root; it is imported into *state_dir* under
    the same lock, so concurrent clients converge on one sign-in before any rotates it. A refresh the issuer answers
    with ``invalid_grant`` clears the stored sign-in and raises. The Workspaces servers' tokens are kept.
    """

    with _process_refresh_lock(state_dir):
        sign_in = _locked_sign_in(state_dir, legacy_state_dir)
        if sign_in is None:
            return ""
        if stale_access != sign_in.access_token:
            return sign_in.access_token
        _refuse_legacy_session(sign_in)
        access, refresh = _rotate(
            url,
            sign_in,
            resource="",
            state_dir=state_dir,
            legacy_state_dir=legacy_state_dir,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
        )
        _write_sign_in(state_dir, replace(sign_in, access_token=access, refresh_token=refresh))
        return access


def _not_covered(origin: str) -> ClientError:
    return ClientError(
        ErrorCode.UNAUTHENTICATED,
        f"your LemonCrow sign-in does not cover the Workspaces server {origin}; run `lc auth login --server {origin}`",
        action=AgentAction.REAUTHENTICATE,
        server_code=NOT_COVERED,
    )


def workspaces_access(
    url: str,
    *,
    state_dir: Path,
    legacy_state_dir: Path | None = None,
    origin: str,
    refresh_within_s: float,
    timeout_s: float,
    connect_timeout_s: float | None = None,
) -> str:
    """The sign-in's access token for the Workspaces server at *origin*; ``""`` without a sign-in.

    A token with less than *refresh_within_s* left (or none yet) is replaced first by a refresh-token request that
    names *origin* alone, under the same cross-process lock as every other rotation, so only the newest refresh token is
    ever sent and the rotated one is stored with the new access token. *url* is the hosted LemonCrow server that names
    the trusted Authward issuer. A sign-in that did not name *origin* raises :class:`ClientError` with ``server_code``
    :data:`NOT_COVERED` and the command that fixes it; Authward is not asked.
    """

    if not _is_origin(origin):
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            "a Workspaces server's sign-in is asked for by its origin",
            action=AgentAction.FIX_REQUEST,
        )
    with _process_refresh_lock(state_dir):
        sign_in = _locked_sign_in(state_dir, legacy_state_dir)
        if sign_in is None:
            return ""
        if origin not in sign_in.servers:
            raise _not_covered(origin)
        held = sign_in.servers[origin]
        if held and not _expires_within(held, refresh_within_s):
            return held
        _refuse_legacy_session(sign_in)
        try:
            access, refresh = _rotate(
                url,
                sign_in,
                resource=origin,
                state_dir=state_dir,
                legacy_state_dir=legacy_state_dir,
                timeout_s=timeout_s,
                connect_timeout_s=connect_timeout_s,
            )
        except ClientError as exc:
            # Authward did not use the refresh token up: the sign-in stands for every other resource it named.
            if exc.server_code == "invalid_target":
                raise _not_covered(origin) from None
            raise
        _write_sign_in(state_dir, replace(sign_in, refresh_token=refresh, servers={**sign_in.servers, origin: access}))
        return access
