"""Hosted Authward CLI without pulling the full LemonCrow runtime into the client.

This module is intentionally standard-library only.  Product traffic still goes
through :mod:`lemoncrow_client.transport`; the browser launch is a user-invoked
convenience for RFC 8628 device authorization, not a LemonCrow background
process or network path.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import Any, TextIO
from urllib.parse import parse_qs, urlsplit

from .authward import AuthwardClient, discover_authward
from .browser import launch_browser_url
from .config import ClientConfig, load_config
from .credentials import (
    clear_managed_credentials,
    managed_credentials_path,
    read_managed_sign_in,
    store_new_sign_in,
)
from .errors import ClientError
from .transport import HttpTransport

__all__ = ["auth_main"]

_PENDING_REASONS = frozenset({"authorization_pending", "slow_down"})
#: :data:`lemoncrow_client.task_server.SERVER_ENV`, named here too: the Review client ships without that module.
_SERVER_ENV = "LEMONCROW_WORKSPACES_SERVER"


class _NotAServer(Exception):
    """A Workspaces server a sign-in cannot name; ``code`` is the exit status."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _config(product: str = "core") -> ClientConfig:
    return load_config(cwd=None, product=product)


def _authward(config: ClientConfig) -> AuthwardClient:
    transport = HttpTransport(config.url, timeout_s=config.request_timeout_s)
    return discover_authward(transport, timeout_s=config.request_timeout_s, product=config.product)


def _managed(config: ClientConfig) -> bool:
    managed_paths = {
        str(managed_credentials_path(config.account_state_dir)),
        str(managed_credentials_path(config.state_dir)),
    }
    return config.token_source in managed_paths or (not config.token and config.refresh_token_source in managed_paths)


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"LemonCrow auth response is missing {name}")
    return value


def _device_browser_uri(payload: Mapping[str, Any], verification_uri: str, user_code: str) -> str:
    """Use the issuer\'s code-filled link only when it matches its base page."""
    complete = payload.get("verification_uri_complete")
    if not isinstance(complete, str) or not complete or len(complete) > 2048:
        return verification_uri
    try:
        base, target = urlsplit(verification_uri), urlsplit(complete)
        if (target.scheme, target.hostname, target.port, target.path) != (
            base.scheme,
            base.hostname,
            base.port,
            base.path,
        ):
            return verification_uri
        if target.username or target.password or target.fragment:
            return verification_uri
        if target.scheme != "https" and not (
            target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            return verification_uri
        if parse_qs(target.query).get("user_code") != [user_code]:
            return verification_uri
    except ValueError:
        return verification_uri
    return complete


def _error(exc: ClientError) -> str:
    return f"{exc.server_code}: {exc.message}"


_VERB_USAGE: Mapping[str, str] = {
    "login": (
        "usage: lemoncrow-client auth login [--server URL]... [--no-browser]\n"
        "Sign in to the configured Hosted LemonCrow deployment with Authward device authorization.\n"
        "  --server URL  also sign in to this LemonCrow-hosted Workspaces server (repeatable); the servers the\n"
        "                current sign-in covers, and LEMONCROW_WORKSPACES_SERVER, are named again\n"
        "  --no-browser  print the sign-in URL and code without opening a browser\n"
        "Local Review and Workspaces Lite need no login.\n"
    ),
    "status": "usage: lemoncrow-client auth status\nShow the configured hosted login state.\n",
    "logout": "usage: lemoncrow-client auth logout\nRevoke managed refresh credentials and sign out.\n",
}


def auth_main(arguments: Sequence[str], *, sink: TextIO, product: str = "core") -> int:
    """Run ``auth status|login|logout`` and return a process-style exit code."""

    if not arguments or arguments[0] in {"-h", "--help", "help"}:
        sink.write("usage: lemoncrow-client auth [status|login|logout]\n")
        sink.write("  login [--server URL]... [--no-browser]  sign in with Authward device authorization\n")
        sink.write("  status                show the configured hosted login state\n")
        sink.write("  logout                revoke managed refresh credentials and sign out\n")
        return 0

    verb, rest = arguments[0], list(arguments[1:])
    if verb in _VERB_USAGE and rest in (["-h"], ["--help"], ["help"]):
        sink.write(_VERB_USAGE[verb])
        return 0
    try:
        config = _config(product)
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
            try:
                current = read_managed_sign_in(config.account_state_dir)
            except ClientError:
                current = None
            for origin in current.servers if current is not None else ():
                sink.write(f"hosted: signed in to the Workspaces server {origin}\n")
        return 0
    if config.authenticated:
        sink.write(f"hosted: using operator-provided credentials for {config.url}\n")
        return 0
    sink.write(f"hosted: not signed in to {config.url}\n")
    return 0


def _workspaces_servers(config: ClientConfig, requested: Sequence[str]) -> list[str]:
    """The Workspaces server origins a new sign-in names: the current sign-in's, the server in
    ``LEMONCROW_WORKSPACES_SERVER`` when it is LemonCrow-hosted, and each ``--server``.

    A client without Workspaces (the Review client has no :mod:`lemoncrow_client.task_server`) names the current
    sign-in's servers again, so its sign-in does not drop them, and takes no ``--server``.
    """

    named: list[str] = []
    for state_dir in (config.account_state_dir, config.state_dir):
        try:
            current = read_managed_sign_in(state_dir)
        except ClientError:
            current = None
        if current is not None:
            named.extend(current.servers)
            break
    try:
        from .task_server import ServerError, account_resource
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.task_server":
            raise
        if requested:
            raise _NotAServer(
                "this LemonCrow client has no Workspaces: sign in to a Workspaces server with the Workspaces client "
                "(lemoncrow-runtime-client auth login --server URL)",
                2,
            ) from None
        return list(dict.fromkeys(named))
    configured = os.environ.get(_SERVER_ENV, "").strip()
    if configured:
        try:
            named.append(account_resource(configured))
        except ServerError:
            pass  # a server that takes the organization token, or a mistyped one `lc task` reports
    try:
        named.extend(account_resource(server) for server in requested)
    except ServerError as exc:
        raise _NotAServer(str(exc), exc.code) from None
    return list(dict.fromkeys(named))


def _login(config: ClientConfig, arguments: Sequence[str], sink: TextIO) -> int:
    no_browser = False
    requested: list[str] = []
    items = iter(arguments)
    for argument in items:
        option, equals, inline = argument.partition("=")
        if argument == "--no-browser":
            no_browser = True
        elif option == "--server":
            value = inline if equals else next(items, "")
            if not value.strip() or value.startswith("-"):
                sink.write("--server needs a Workspaces server URL\n")
                return 2
            requested.append(value)
        else:
            sink.write(f"unknown auth login option: {argument}\n")
            return 2

    if not config.hosted:
        sink.write("Local LemonCrow needs no login.\n")
        return 0

    try:
        servers = _workspaces_servers(config, requested)
    except _NotAServer as exc:
        sink.write(f"{exc}\n")
        return exc.code
    try:
        authward = _authward(config)
        if authward.contract.resource in servers:
            sink.write(f"{authward.contract.resource} is the LemonCrow API, not a Workspaces server\n")
            return 2
        authorization = authward.start_device(servers) if servers else authward.start_device()
        device_code = _required_string(authorization, "device_code")
        user_code = _required_string(authorization, "user_code")
        verification_uri = _required_string(authorization, "verification_uri")
        verification_uri = _device_browser_uri(authorization, verification_uri, user_code)
    except ClientError as exc:
        sink.write(_error(exc) + "\n")
        if servers and exc.server_code and exc.server_code != "invalid_client":
            sink.write(
                f"this sign-in also named {', '.join(servers)} (from --server, {_SERVER_ENV}, and the servers your "
                "current sign-in covers), and one that names a server LemonCrow's sign-in does not know is refused: "
                "run `lc auth logout`, then `lc auth login --server URL` for each server you use\n"
            )
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
        # The URL and code are already printed: a browser that cannot open is
        # not an authentication failure and must not discard the device code.
        launch_browser_url(verification_uri)

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
        store_new_sign_in(
            config.account_state_dir, access_token=access_token, refresh_token=refresh_token, servers=servers
        )
        sink.write(f"Signed in to {config.url}\n")
        for origin in servers:
            sink.write(f"Signed in to the Workspaces server {origin}\n")
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
        # Clear both locations so a legacy credential cannot reappear after the
        # new account-scoped file is removed.
        clear_managed_credentials(config.account_state_dir)
        clear_managed_credentials(config.state_dir)

    if error is not None:
        sink.write(f"local credentials removed, but Authward revocation failed: {_error(error)}\n")
        return 1
    sink.write("Signed out.\n")
    return 0
