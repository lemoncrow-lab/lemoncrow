"""Hosted Authward CLI without pulling the full LemonCrow runtime into the client.

This module is intentionally standard-library only.  Product traffic still goes
through :mod:`lemoncrow_client.transport`; the browser launch is a user-invoked
convenience for RFC 8628 device authorization, not a LemonCrow background
process or network path.
"""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from .authward import AuthwardClient, discover_authward
from .config import ClientConfig, load_config
from .credentials import clear_managed_credentials, managed_credentials_path, write_managed_credentials
from .errors import ClientError
from .transport import HttpTransport

__all__ = ["auth_main"]

_PENDING_REASONS = frozenset({"authorization_pending", "slow_down"})


def _config() -> ClientConfig:
    return load_config(cwd=None)


def _authward(config: ClientConfig) -> AuthwardClient:
    transport = HttpTransport(config.url, timeout_s=config.request_timeout_s)
    return discover_authward(transport, timeout_s=config.request_timeout_s)


def _managed(config: ClientConfig) -> bool:
    managed = str(managed_credentials_path(config.state_dir))
    return config.token_source == managed or (not config.token and config.refresh_token_source == managed)


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"LemonCrow auth response is missing {name}")
    return value


def _error(exc: ClientError) -> str:
    return f"{exc.server_code}: {exc.message}"


def auth_main(arguments: Sequence[str], *, sink: TextIO) -> int:
    """Run ``auth status|login|logout`` and return a process-style exit code."""

    if not arguments or arguments[0] in {"-h", "--help", "help"}:
        sink.write("usage: lemoncrow-client auth [status|login|logout]\n")
        sink.write("  login [--no-browser]  sign in with Authward device authorization\n")
        sink.write("  status                show the configured hosted login state\n")
        sink.write("  logout                revoke managed refresh credentials and sign out\n")
        return 0

    verb, rest = arguments[0], list(arguments[1:])
    try:
        config = _config()
    except ClientError as exc:
        sink.write(_error(exc) + "\n")
        return 2

    if verb == "status":
        if rest:
            sink.write("auth status takes no options\n")
            return 2
        return _status(config, sink)
    if verb == "login":
        return _login(config, rest, sink)
    if verb == "logout":
        if rest:
            sink.write("auth logout takes no options\n")
            return 2
        return _logout(config, sink)

    sink.write(f"unknown auth command: {verb}\n")
    return 2


def _status(config: ClientConfig, sink: TextIO) -> int:
    if not config.hosted:
        sink.write("local: no login required\n")
        return 0
    if _managed(config) and config.authenticated:
        if config.token.startswith("lcs_"):
            sink.write(
                f"hosted: legacy LemonCrow session for {config.url}; run `lc auth login` to migrate to Authward\n"
            )
        else:
            sink.write(f"hosted: signed in to {config.url} with Authward\n")
        return 0
    if config.authenticated:
        sink.write(f"hosted: using operator-provided credentials for {config.url}\n")
        return 0
    sink.write(f"hosted: not signed in to {config.url}\n")
    return 0


def _login(config: ClientConfig, arguments: Sequence[str], sink: TextIO) -> int:
    no_browser = False
    for argument in arguments:
        if argument == "--no-browser":
            no_browser = True
        else:
            sink.write(f"unknown auth login option: {argument}\n")
            return 2

    if not config.hosted:
        sink.write("Local LemonCrow needs no login.\n")
        return 0

    try:
        authward = _authward(config)
        authorization = authward.start_device()
        device_code = _required_string(authorization, "device_code")
        user_code = _required_string(authorization, "user_code")
        verification_uri = _required_string(authorization, "verification_uri")
    except ClientError as exc:
        sink.write(_error(exc) + "\n")
        return 1
    except (TypeError, ValueError) as exc:
        sink.write(str(exc) + "\n")
        return 1

    expires_in = max(1, int(authorization.get("expires_in") or 600))
    interval = max(1, int(authorization.get("interval") or 5))
    sink.write(f"Open {verification_uri}\n")
    sink.write(f"Code: {user_code}\n")
    sink.flush()
    if not no_browser:
        try:
            webbrowser.open(verification_uri)
        except Exception:
            # The URL and code are already printed. Browser-launch failure is
            # not an authentication failure and must not discard the device code.
            pass

    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            issued = authward.poll_device(device_code)
        except ClientError as exc:
            reason = str(exc.server_code or exc.details.get("reason") or "")
            if exc.retryable and reason in _PENDING_REASONS:
                if reason == "slow_down":
                    interval = min(60, interval + 5)
                continue
            sink.write(_error(exc) + "\n")
            return 1
        try:
            access_token = _required_string(issued, "access_token")
            refresh_token = _required_string(issued, "refresh_token")
        except ValueError as exc:
            sink.write(str(exc) + "\n")
            return 1
        write_managed_credentials(config.state_dir, access_token=access_token, refresh_token=refresh_token)
        sink.write(f"Signed in to {config.url}\n")
        return 0

    sink.write("device login expired before approval; run `lc auth login` again\n")
    return 1


def _logout(config: ClientConfig, sink: TextIO) -> int:
    if not config.hosted:
        sink.write("Local LemonCrow has no login session.\n")
        return 0
    if not _managed(config):
        if config.authenticated:
            sink.write("the active credential is operator-provided; unset it or remove its configured token file\n")
            return 1
        sink.write("Already signed out.\n")
        return 0

    access = config.token
    refresh = config.refresh_token
    error: ClientError | None = None
    try:
        if access.startswith("lcs_") or refresh.startswith("lcr_"):
            # Migration-only cleanup. New Authward credentials revoke directly
            # with Authward and never call a LemonCrow token lifecycle endpoint.
            pass
        elif refresh:
            _authward(config).revoke_refresh(refresh)
    except ClientError as exc:
        error = exc
    finally:
        clear_managed_credentials(config.state_dir)

    if error is not None:
        sink.write(f"local credentials removed, but Authward revocation failed: {_error(error)}\n")
        return 1
    sink.write("Signed out.\n")
    return 0
