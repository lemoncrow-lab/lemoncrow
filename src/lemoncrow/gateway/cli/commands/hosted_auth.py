"""Hosted LemonCrow authentication directly against Authward."""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Mapping
from typing import Any

import click
from lemoncrow_client.authward import AuthwardClient, discover_authward
from lemoncrow_client.config import ClientConfig, load_config
from lemoncrow_client.credentials import (
    clear_managed_credentials,
    managed_credentials_path,
    write_managed_credentials,
)
from lemoncrow_client.errors import ClientError
from lemoncrow_client.transport import HttpTransport

_PENDING_REASONS = frozenset({"authorization_pending", "slow_down"})


def _config() -> ClientConfig:
    return load_config(cwd=None)


def _transport(config: ClientConfig) -> HttpTransport:
    return HttpTransport(config.url, timeout_s=config.request_timeout_s)


def _authward(config: ClientConfig) -> AuthwardClient:
    return discover_authward(_transport(config), timeout_s=config.request_timeout_s)


def _managed(config: ClientConfig) -> bool:
    managed = str(managed_credentials_path(config.state_dir))
    return config.token_source == managed or (not config.token and config.refresh_token_source == managed)


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise click.ClickException(f"LemonCrow auth response is missing {name}")
    return value


def _client_error(exc: ClientError) -> click.ClickException:
    return click.ClickException(f"{exc.server_code}: {exc.message}")


@click.group("auth", invoke_without_command=True)
@click.pass_context
def auth_group(ctx: click.Context) -> None:
    """Sign in to the configured hosted LemonCrow server."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(auth_status)


@auth_group.command("login")
@click.option("--no-browser", is_flag=True, help="Print the verification URL without opening it.")
def auth_login(no_browser: bool) -> None:
    """Sign in to hosted LemonCrow using the RFC 8628 device flow."""
    config = _config()
    if not config.hosted:
        click.echo("Local LemonCrow needs no login.")
        return

    try:
        authward = _authward(config)
        authorization = authward.start_device()
    except ClientError as exc:
        raise _client_error(exc) from exc

    device_code = _required_string(authorization, "device_code")
    user_code = _required_string(authorization, "user_code")
    verification_uri = _required_string(authorization, "verification_uri")
    verification_complete = authorization.get("verification_uri_complete")
    browser_url = (
        verification_complete if isinstance(verification_complete, str) and verification_complete else verification_uri
    )
    expires_in = int(authorization.get("expires_in") or 600)
    interval = max(1, int(authorization.get("interval") or 5))

    click.echo(f"Open {verification_uri}")
    click.echo(f"Code: {user_code}")
    if not no_browser:
        try:
            webbrowser.open(browser_url)
        except Exception:
            # The URL/code are already visible; a browser-launch failure is not
            # an authentication failure and must not discard the device code.
            pass

    deadline = time.monotonic() + max(1, expires_in)
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
            raise _client_error(exc) from exc

        access_token = _required_string(issued, "access_token")
        refresh_token = _required_string(issued, "refresh_token")
        write_managed_credentials(
            config.state_dir,
            access_token=access_token,
            refresh_token=refresh_token,
        )
        click.secho(f"Signed in to {config.url}", fg="green")
        return

    raise click.ClickException("device login expired before approval; run `lc auth login` again")


@auth_group.command("status")
def auth_status() -> None:
    """Show whether this installation has a hosted LemonCrow session."""
    config = _config()
    if not config.hosted:
        click.echo("local: no login required")
        return
    if _managed(config) and config.authenticated:
        if config.token.startswith("lcs_"):
            click.echo(
                f"hosted: legacy LemonCrow session for {config.url}; run `lc auth login` to refresh your sign-in"
            )
        else:
            click.echo(f"hosted: signed in to {config.url}")
        return
    if config.authenticated:
        click.echo(f"hosted: using operator-provided credentials for {config.url}")
        return
    click.echo(f"hosted: not signed in to {config.url}")


@auth_group.command("logout")
def auth_logout() -> None:
    """Revoke both managed Authward credentials and sign out locally."""
    config = _config()
    if not config.hosted:
        click.echo("Local LemonCrow has no login session.")
        return
    if not _managed(config):
        if config.authenticated:
            raise click.ClickException(
                "the active credential is operator-provided; unset it or remove its configured token file"
            )
        click.echo("Already signed out.")
        return

    access = config.token
    refresh = config.refresh_token
    errors: list[ClientError] = []
    try:
        if access.startswith("lcs_") or refresh.startswith("lcr_"):
            # Migration-only cleanup. New Authward credentials never call a
            # LemonCrow token lifecycle endpoint. The legacy server-side token
            # will age out under its short TTL after local removal.
            pass
        else:
            authward = _authward(config)
            # Revoke both halves of the credential pair. Refresh-token
            # revocation prevents extension; access-token revocation closes the
            # remaining short-lived replay window immediately.
            for token, revoke in (
                (access, authward.revoke_access),
                (refresh, authward.revoke_refresh),
            ):
                if not token:
                    continue
                try:
                    revoke(token)
                except ClientError as exc:
                    errors.append(exc)
    finally:
        clear_managed_credentials(config.state_dir)

    if errors:
        error = errors[0]
        raise click.ClickException(
            f"local credentials removed, but remote sign-out failed: {error.server_code}: {error.message}"
        ) from error
    click.secho("Signed out.", fg="green")


__all__ = ["auth_group"]
