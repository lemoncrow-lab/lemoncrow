"""Managed Authward credentials and transparent access-token rotation.

The hosted CLI stores one Authward access/refresh pair under ``LEMONCROW_HOME``. The
pair is one JSON file so refresh-token rotation is persisted atomically: a
process crash can never leave a fresh access token beside a spent refresh
token.  Operator-provided credentials remain outside this module and retain
precedence in :mod:`lemoncrow_client.config`.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from .authward import discover_authward
from .errors import AgentAction, ClientError, ErrorCode
from .transport import HttpTransport, Response

__all__ = [
    "RefreshingHttpTransport",
    "clear_managed_credentials",
    "managed_credentials_path",
    "read_managed_credentials",
    "write_managed_credentials",
]

_MAX_CREDENTIAL_BYTES: Final[int] = 8192
_MAX_TOKEN_BYTES: Final[int] = 4096


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


def read_managed_credentials(state_dir: Path) -> tuple[str, str]:
    """Return ``(access_token, refresh_token)`` from the managed credential file."""

    path = managed_credentials_path(state_dir)
    try:
        info = path.stat()
    except FileNotFoundError:
        return "", ""
    except OSError as exc:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"managed hosted credential is not readable: {path}",
            action=AgentAction.REAUTHENTICATE,
        ) from exc
    if info.st_size > _MAX_CREDENTIAL_BYTES:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"managed hosted credential is larger than {_MAX_CREDENTIAL_BYTES} bytes: {path}",
            action=AgentAction.REAUTHENTICATE,
        )
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"managed hosted credential must not be group- or world-accessible (chmod 600): {path}",
            action=AgentAction.REAUTHENTICATE,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"managed hosted credential is not valid JSON: {path}",
            action=AgentAction.REAUTHENTICATE,
        ) from exc
    if not isinstance(payload, dict):
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            f"managed hosted credential is not a JSON object: {path}",
            action=AgentAction.REAUTHENTICATE,
        )
    return _validate_token(payload.get("access_token"), "access token"), _validate_token(
        payload.get("refresh_token"), "refresh token"
    )


def write_managed_credentials(state_dir: Path, *, access_token: str, refresh_token: str) -> None:
    """Atomically replace the managed hosted credential pair with mode 0600."""

    access = _validate_token(access_token, "access token")
    refresh = _validate_token(refresh_token, "refresh token")
    path = managed_credentials_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = (
        json.dumps(
            {"access_token": access, "refresh_token": refresh},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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

    __slots__ = ("_access_token", "_refresh_lock", "_refresh_token", "_state_dir")

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float,
        state_dir: Path,
        access_token: str,
        refresh_token: str,
    ) -> None:
        super().__init__(url, timeout_s=timeout_s)
        self._state_dir = state_dir
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

        with self._refresh_lock, _process_refresh_lock(self._state_dir):
            # Refresh tokens are one-time credentials. Re-read the atomic pair
            # only after taking the process lock: another CLI may have won the
            # race while this request was in flight. In that case adopt its pair
            # and retry instead of replaying the already-consumed refresh token.
            disk_access, disk_refresh = read_managed_credentials(self._state_dir)
            if not disk_access or not disk_refresh:
                return response
            self._access_token = disk_access
            self._refresh_token = disk_refresh
            if sent_access != self._access_token:
                return HttpTransport.request(
                    self,
                    method,
                    path,
                    body=body,
                    headers=self._current_headers(headers),
                    timeout_s=timeout_s,
                )

            if self._access_token.startswith("lcs_") or self._refresh_token.startswith("lcr_"):
                raise ClientError(
                    ErrorCode.UNAUTHENTICATED,
                    "this managed credential is a legacy LemonCrow session; run `lc auth login` to refresh your hosted sign-in",
                    action=AgentAction.REAUTHENTICATE,
                )

            authward = discover_authward(
                HttpTransport(self.url, timeout_s=self._timeout_s),
                timeout_s=self._timeout_s if timeout_s is None else timeout_s,
            )
            access, refresh = authward.refresh(self._refresh_token)
            access = _validate_token(access, "access token")
            refresh = _validate_token(refresh, "refresh token")
            write_managed_credentials(self._state_dir, access_token=access, refresh_token=refresh)
            self._access_token = access
            self._refresh_token = refresh

            return HttpTransport.request(
                self,
                method,
                path,
                body=body,
                headers=self._current_headers(headers),
                timeout_s=timeout_s,
            )
