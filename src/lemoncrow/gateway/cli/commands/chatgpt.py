"""``lc chatgpt`` — expose LemonCrow's MCP transport to a ChatGPT connector.

ChatGPT's custom MCP connector (Developer Mode) speaks OAuth 2.1 or no-auth, so
``serve`` runs the OAuth shim from ``mcp_oauth.py`` (or the open transport with
``--no-auth``) behind loopback and prints the pairing code + the ChatGPT setup
steps the operator needs. By default a cloudflared *quick tunnel* is
auto-launched (downloading cloudflared on first use if needed) to provide the
public https URL ChatGPT requires; ``--no-tunnel`` opts out for operators
running their own named tunnel / ngrok. ``client`` mints a stable user-defined
OAuth client ID for ChatGPT's "Enter a client ID" connector field.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

import click

# How long to wait for cloudflared to print its quick-tunnel URL before giving
# up and falling back to manual instructions. Tunnel establishment is normally
# a couple of seconds; 30s covers a slow first-run edge download.
_TUNNEL_URL_TIMEOUT_SECONDS = 30.0

_CLOUDFLARED_INSTALL_URL = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
_CLOUDFLARED_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

# ChatGPT's connector OAuth redirect endpoints (both current and legacy hosts),
# used as the default redirect_uris for a user-defined client (`lc chatgpt client`).
_CHATGPT_REDIRECT_URIS = (
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chat.openai.com/connector_platform_oauth_redirect",
)

# The quick-tunnel hostname is <random-words>.trycloudflare.com. cloudflared's
# stderr also mentions its control-plane host (api.trycloudflare.com, e.g. in
# quota/failure lines) and docs links on other domains — those must not be
# mistaken for the tunnel URL, so the api host is filtered out explicitly.
_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CONTROL_PLANE_URL = "https://api.trycloudflare.com"


def _extract_tunnel_url(line: str) -> str | None:
    """Pull the quick-tunnel URL out of one cloudflared stderr line.

    The URL is printed inside an ASCII box::

        ... INF |  https://<random-words>.trycloudflare.com          |

    Pure function (no I/O) so the parsing is unit-testable without cloudflared.
    Returns ``None`` for lines without a tunnel URL, including control-plane
    noise like ``failed to request quick Tunnel: ... api.trycloudflare.com``.
    """
    match = _TUNNEL_URL_RE.search(line)
    if match is None:
        return None
    url = match.group(0)
    if url == _CONTROL_PLANE_URL:
        return None
    return url


def _pairing_code_log_slug(pairing_code: str) -> str:
    """Non-reversible, filesystem-safe identifier for the request-log filename.

    Hashed rather than sanitized-and-kept: the pairing code is the one secret
    gating shell access, and a filename sits in a listable directory (and can
    end up in a screenshot, `ls`, or a shared support bundle) far more easily
    than terminal output the operator already controls. SHA-256 hex,
    truncated to 16 chars — same convention this codebase used for the
    (since-removed) per-session Mcp-Session-Id file hash. Deterministic: the
    same pairing code always maps to the same file, so the exact path is
    still knowable and printable before the server starts.
    """
    return hashlib.sha256(pairing_code.encode("utf-8")).hexdigest()[:16]


# ── cloudflared binary resolution / auto-install ──────────────────────────────
def _cloudflared_asset_name(system: str, machine: str) -> str | None:
    """Map ``platform.system()``/``platform.machine()`` to a release asset name.

    Linux assets are bare static binaries (``cloudflared-linux-<arch>``); darwin
    ships only as ``.tgz`` archives (``cloudflared-darwin-<arch>.tgz`` holding a
    single ``cloudflared`` binary) which the installer extracts. Pure function
    for testability; returns ``None`` on unsupported platforms so the caller
    falls back to the manual install link.
    """
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine.lower())
    if arch is None:
        return None
    system_lower = system.lower()
    if system_lower == "linux":
        return f"cloudflared-linux-{arch}"
    if system_lower == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return None


def _managed_binary_path() -> Path:
    """Where the auto-installed cloudflared lives: ``<store_root>/chatgpt/bin``.

    ``<store_root>`` is ``default_store_root()`` (``~/.lemoncrow``, or
    ``$LEMONCROW_ROOT`` when set), with ``chatgpt/`` as this feature's peer
    subdirectory — same root as the OAuth state and request logs, instead of
    the old ``$XDG_DATA_HOME/lemoncrow/bin``. A binary already downloaded at
    the old XDG path is left there (not migrated); a fresh copy is downloaded
    here on next use.
    """
    from lemoncrow.core.foundation.paths import default_store_root

    return default_store_root() / "chatgpt" / "bin" / "cloudflared"


def _resolve_cloudflared() -> str | None:
    """Find a usable cloudflared: PATH first (operator-managed wins), then ours."""
    found = shutil.which("cloudflared")
    if found is not None:
        return found
    managed = _managed_binary_path()
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    return None


def _extract_tgz_member(archive: Path, dest: Path) -> None:
    """Pull the single ``cloudflared`` binary out of a darwin ``.tgz`` release."""
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile("cloudflared")
        if member is None:
            raise tarfile.TarError("no 'cloudflared' member in archive")
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".cloudflared.", suffix=".bin")
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(member, out)
        os.replace(tmp, dest)


def _verify_cloudflared(dest: Path) -> str | None:
    """Trust the download only after it executes: one ``--version`` probe."""
    try:
        probe = subprocess.run([str(dest), "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        probe = None
    if probe is None or probe.returncode != 0:
        click.echo("  Downloaded cloudflared failed its --version check; removing it.", err=True)
        with contextlib.suppress(OSError):
            dest.unlink()
        return None
    version = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else "cloudflared"
    click.secho(f"  ✓ installed {version} → {dest}", fg="green")
    return str(dest)


def _download_cloudflared(dest: Path) -> str | None:
    """Download the latest cloudflared release to ``dest`` (0755, atomic).

    Quick tunnels need no Cloudflare account and cloudflared is a single static
    binary on GitHub releases, so a one-shot download is all the "install" there
    is. Streams to a temp file in the target dir, then ``os.replace`` — a
    dropped connection never leaves a half-written binary in place. Returns the
    installed path, or ``None`` after printing the reason (caller aborts with
    the manual install link).
    """
    asset = _cloudflared_asset_name(platform.system(), platform.machine())
    if asset is None:
        click.echo(
            f"  No cloudflared release asset for this platform ({platform.system()}/{platform.machine()}).",
            err=True,
        )
        return None
    url = f"{_CLOUDFLARED_RELEASE_BASE}/{asset}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".cloudflared.", suffix=".part")
    try:
        click.secho(f"  Downloading {url}", dim=True)
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=60) as resp:
            if resp.status != 200:
                raise urllib.error.URLError(f"HTTP {resp.status}")
            received = 0
            while chunk := resp.read(1024 * 1024):
                out.write(chunk)
                received += len(chunk)
                click.echo(f"\r  … {received / 1_048_576:.1f} MB", nl=False)
        click.echo("")
        if asset.endswith(".tgz"):
            _extract_tgz_member(Path(tmp), dest)
        else:
            os.replace(tmp, dest)
        os.chmod(dest, 0o755)
    except (urllib.error.URLError, OSError, TimeoutError, tarfile.TarError) as exc:
        click.echo("")
        click.echo(f"  Download failed: {exc}", err=True)
        with contextlib.suppress(OSError):
            dest.unlink()
        return None
    finally:
        # Gone already when os.replace promoted it; suppress covers that.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return _verify_cloudflared(dest)


def _abort_missing_cloudflared(port: int) -> NoReturn:
    """Print the manual install path and exit — shared by every failure branch."""
    click.echo(f"  Install it:  {_CLOUDFLARED_INSTALL_URL}", err=True)
    click.echo("  Then run `uv run lemoncrow chatgpt serve` again.", err=True)
    click.echo("  (Or use --no-tunnel and expose the port yourself:", err=True)
    click.echo(f"     cloudflared tunnel --url http://localhost:{port} )", err=True)
    raise SystemExit(1)


def _install_cloudflared_interactive(port: int) -> str:
    """Offer to auto-download cloudflared; return the binary path or exit(1)."""
    try:
        accepted = click.confirm(
            "cloudflared not found. Download it now (~60MB, no Cloudflare account needed)?",
            default=True,
        )
    except click.Abort:
        # Non-interactive stdin (pipes, CI) cannot answer the prompt: treat as
        # declined and fall through to the manual instructions.
        click.echo("")
        accepted = False
    if not accepted:
        _abort_missing_cloudflared(port)
    installed = _download_cloudflared(_managed_binary_path())
    if installed is None:
        _abort_missing_cloudflared(port)
    return installed


# ── Tunnel launch ─────────────────────────────────────────────────────────────
def _start_tunnel(
    binary: str, port: int, timeout: float = _TUNNEL_URL_TIMEOUT_SECONDS
) -> tuple[subprocess.Popen[str], str | None]:
    """Launch a cloudflared quick tunnel for ``localhost:port``.

    Returns ``(proc, url)`` — ``url`` is ``None`` when no quick-tunnel URL
    appeared within ``timeout``. cloudflared prints the URL on **stderr**. A
    daemon thread keeps draining stderr for the life of the process — first to
    capture the URL, then discarding everything after it, because an undrained
    pipe eventually fills and blocks cloudflared. The caller owns ``proc`` and
    must terminate it.
    """
    proc: subprocess.Popen[str] = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    url_found = threading.Event()
    captured: list[str] = []

    def _drain_stderr() -> None:
        stderr = proc.stderr
        if stderr is None:  # pragma: no cover — PIPE guarantees a stream
            return
        for line in stderr:
            if not url_found.is_set():
                url = _extract_tunnel_url(line)
                if url is not None:
                    captured.append(url)
                    url_found.set()
            # Past the URL: keep reading and discard, forever.

    threading.Thread(target=_drain_stderr, daemon=True, name="cloudflared-stderr-drain").start()
    if url_found.wait(timeout):
        return proc, captured[0]
    return proc, None


# ── CLI ───────────────────────────────────────────────────────────────────────
@click.group("chatgpt", context_settings={"help_option_names": ["-h", "--help"]})
def chatgpt_group() -> None:
    """Connect ChatGPT (Developer Mode custom MCP connector) to LemonCrow."""


@chatgpt_group.command("serve")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Local port to bind. Default: an available port is chosen automatically "
    "(so multiple projects/servers can run at once). Pass a fixed port for a stable local URL.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address (loopback-only; reach it through a tunnel, never bind publicly).",
)
@click.option(
    "--pairing-code",
    default=None,
    help="Pairing code the browser OAuth page asks for (generated if omitted).",
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Delete persisted OAuth state first (revokes all tokens and clients).",
)
@click.option(
    "--tunnel/--no-tunnel",
    default=True,
    show_default=True,
    help="Auto-launch a cloudflared quick tunnel (--no-tunnel to manage your own).",
)
@click.option(
    "--no-auth",
    is_flag=True,
    default=False,
    help="Serve /mcp with NO authentication (ChatGPT's 'No authentication' option). "
    "Anyone with the URL gets full tool access — prefer OAuth.",
)
@click.option(
    "--persistent",
    is_flag=True,
    default=False,
    help="Stable MCP URL that survives restarts, backed by a real Cloudflare named "
    "tunnel (requires a domain you manage in Cloudflare DNS; one-time `cloudflared "
    "tunnel login` browser step). Mutually exclusive with --no-tunnel.",
)
@click.option(
    "--hostname",
    default=None,
    help="Public hostname to bind for --persistent, e.g. mcp.example.com (a domain you "
    "manage in Cloudflare). Required on the first --persistent run; read from persisted "
    "state on later runs.",
)
@click.option(
    "--reset-tunnel",
    is_flag=True,
    default=False,
    help="Clear the persisted --persistent tunnel state first, so it can be "
    "reconfigured with a different hostname. Does NOT delete the Cloudflare-side "
    "tunnel — that's `cloudflared tunnel delete` yourself.",
)
def chatgpt_serve_cmd(
    port: int | None,
    host: str,
    pairing_code: str | None,
    reset: bool,
    tunnel: bool,
    no_auth: bool,
    persistent: bool,
    hostname: str | None,
    reset_tunnel: bool,
) -> None:
    """Serve the OAuth-protected MCP endpoint for a ChatGPT connector.

    By default a cloudflared quick tunnel is launched automatically (offering a
    one-time download of cloudflared if missing) and the public MCP server URL
    is printed.

    \b
    1. In ChatGPT:  Settings -> Connectors -> Advanced settings ->
                    enable Developer mode -> Create. Set the printed MCP server
                    URL (https://<tunnel-host>/mcp), Authentication: OAuth.
    2. Approve the browser OAuth page with the pairing code below.

    With --no-tunnel, expose the port yourself (named cloudflared tunnel, ngrok).
    With --no-auth, /mcp is served completely open (URL = the only secret).
    With --persistent --hostname mcp.example.com, get a stable URL that survives
    restarts instead of a rotating quick-tunnel one (first run only; --hostname
    isn't needed again once configured).
    """
    import uvicorn

    from lemoncrow.gateway.adapters.mcp_oauth import (
        create_protected_mcp_app,
        default_state_path,
        reset_state,
    )
    from lemoncrow.gateway.cli.commands._persistent_tunnel import (
        TunnelSetupError,
        default_tunnel_state_path,
        load_tunnel_state,
        reset_tunnel_state,
        setup_persistent_tunnel,
    )

    if no_auth and (pairing_code is not None or reset):
        raise click.UsageError("--no-auth cannot be combined with --pairing-code or --reset")
    if persistent and not tunnel:
        raise click.UsageError("--persistent cannot be combined with --no-tunnel (--persistent IS a tunnel mode)")
    if reset_tunnel and not persistent:
        raise click.UsageError("--reset-tunnel requires --persistent")

    # Bind the real listening socket now (not just pick a number): with no
    # --port, the OS assigns a free ephemeral port, so multiple `chatgpt
    # serve` instances (e.g. one per project) never collide on a fixed
    # default. Binding a socket directly (vs. probe-close-rebind) avoids any
    # race with another process grabbing the port in between; uvicorn takes
    # this same socket at the very end via `Server.run(sockets=[sock])`, so
    # it never re-binds. Resolved before tunnel setup/banner printing since
    # both need to know the actual port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port or 0))
    except OSError as exc:
        raise click.ClickException(f"could not bind {host}:{port if port is not None else '(auto)'} — {exc}") from exc
    port = sock.getsockname()[1]

    tunnel_state_path = default_tunnel_state_path()
    existing_tunnel_state = None
    resolved_hostname: str | None = None
    if persistent:
        if reset_tunnel:
            removed = reset_tunnel_state(tunnel_state_path)
            click.echo(
                "  Reset persistent-tunnel state "
                f"({'removed ' + str(tunnel_state_path) if removed else 'nothing to remove'})."
            )
        else:
            existing_tunnel_state = load_tunnel_state(tunnel_state_path)
        if existing_tunnel_state is None and hostname is None:
            raise click.UsageError("first --persistent run needs --hostname <your-domain-in-cloudflare>")
        if existing_tunnel_state is not None and hostname is not None and hostname != existing_tunnel_state.hostname:
            raise click.UsageError(
                f"a persistent tunnel is already configured for hostname {existing_tunnel_state.hostname!r}; "
                "pass --reset-tunnel first to reconfigure with a different hostname."
            )
        resolved_hostname = hostname or (existing_tunnel_state.hostname if existing_tunnel_state else None)

    code: str | None = None
    if no_auth:
        from lemoncrow.gateway.adapters.mcp_http import create_mcp_http_app

        app = create_mcp_http_app()
    else:
        code = pairing_code or secrets.token_urlsafe(9)
        state_path = default_state_path()
        if reset:
            removed = reset_state(state_path)
            click.echo(f"  Reset OAuth state ({'removed ' + str(state_path) if removed else 'nothing to remove'}).")
        app = create_protected_mcp_app(pairing_code=code, state_path=state_path)

    from lemoncrow.gateway.cli.commands._request_log import (
        RequestLogMiddleware,
        dated_log_dir,
        default_log_dir,
        ensure_log_file,
    )

    # Request logging is always on: when the connector misbehaves the operator
    # needs the actual MCP traffic, and by the time they know they need it the
    # request is gone. Credentials are redacted before anything hits the file,
    # and bodies go to the file only — never this console. One concrete file,
    # never keyed on the MCP session id (that would split one connector's
    # traffic across several files for no real benefit on a single-user local
    # machine) and never the raw pairing code (hashed via
    # _pairing_code_log_slug — a filename sits in a listable directory, so the
    # one secret gating shell access must not leak through it). The hash is
    # deterministic, so the exact path is still known before the server even
    # starts, which is what makes the exact-path banner below possible — no
    # glob, no "ls to find it" hedge.
    log_slug = _pairing_code_log_slug(code) if code is not None else "no-auth"
    log_path = ensure_log_file(dated_log_dir(default_log_dir()) / f"{log_slug}.jsonl")
    app.add_middleware(RequestLogMiddleware, log_path=log_path)

    tunnel_proc: subprocess.Popen[str] | None = None
    tunnel_url: str | None = None
    if persistent:
        assert resolved_hostname is not None  # guaranteed by the validation above
        binary = _resolve_cloudflared()
        if binary is None:
            binary = _install_cloudflared_interactive(port)
        try:
            tunnel_proc = setup_persistent_tunnel(
                port=port,
                hostname=resolved_hostname,
                existing_state=existing_tunnel_state,
                state_path=tunnel_state_path,
                binary=binary,
                narrate=lambda msg: click.secho(f"  {msg}", dim=True),
            )
        except TunnelSetupError as exc:
            click.echo(f"  ✗ {exc}", err=True)
            raise SystemExit(1) from exc
        tunnel_url = f"https://{resolved_hostname}"
        click.secho("  ✓ persistent tunnel up", fg="green")
    elif tunnel:
        binary = _resolve_cloudflared()
        if binary is None:
            binary = _install_cloudflared_interactive(port)
        click.secho("  Starting cloudflared quick tunnel…", dim=True)
        tunnel_proc, tunnel_url = _start_tunnel(binary, port)
        if tunnel_url is None:
            click.secho(
                f"  ⚠  cloudflared started but printed no tunnel URL within "
                f"{int(_TUNNEL_URL_TIMEOUT_SECONDS)}s — check its logs; "
                "falling back to the manual steps below.",
                fg="yellow",
                err=True,
            )
        else:
            click.secho("  ✓ tunnel up", fg="green")

    auth_value = "None (no auth)" if no_auth else "OAuth"
    rule = "─" * 64
    click.echo("")
    click.echo(f"  {rule}")
    click.secho(
        "  LemonCrow → ChatGPT connector " + ("(NO AUTH)" if no_auth else "(OAuth 2.1)"),
        fg="cyan",
        bold=True,
    )
    click.echo(f"  {rule}")
    if code is not None:
        click.echo(click.style("  Pairing code:  ", dim=True) + click.style(code, fg="yellow", bold=True))
    click.echo(click.style("  Local server:  ", dim=True) + f"http://{host}:{port}/mcp")
    click.echo(click.style("  Request log:   ", dim=True) + click.style(str(log_path), fg="magenta"))
    click.secho("  View it live in a second terminal (keeps this output clean):", dim=True)
    click.secho(f"      tail -f {log_path} | jq .", fg="cyan", bold=True)
    click.echo("")
    if tunnel_url is not None:
        # THE value the user pastes into ChatGPT — make it the loudest line.
        click.echo(
            click.style("  MCP server URL for ChatGPT:  ", dim=True)
            + click.style(f"{tunnel_url}/mcp", fg="green", bold=True)
        )
        click.echo("")
        click.echo(click.style("  1.", bold=True) + " ChatGPT → Settings → Plugins → Browse Plugins →")
        click.echo("     Next to search click + → Create.")
        click.echo("       Name: LC-<project> (so you can refer it per project)")
        click.echo("       MCP server URL:  " + click.style(f"{tunnel_url}/mcp", fg="green"))
        click.echo(f"       Authentication:  {auth_value}")
        click.echo("       Create.")
        if not no_auth:
            click.echo(click.style("  2.", bold=True) + " Approve the browser OAuth page with the pairing code above.")
        click.echo("")
        if persistent:
            click.secho("  Note: stable — this URL does not change across restarts.", dim=True)
        else:
            click.secho("  Note: this quick-tunnel URL rotates on every restart — re-add the", dim=True)
            click.secho("  connector each time, or use a named tunnel with --no-tunnel.", dim=True)
    else:
        click.echo(click.style("  1.", bold=True) + " Expose it through a tunnel (in another terminal):")
        click.echo(f"       cloudflared tunnel --url http://localhost:{port}")
        click.echo(f"       # or:  ngrok http {port}")
        click.echo(click.style("  2.", bold=True) + " ChatGPT → Settings → Plugins → Browse Plugins →")
        click.echo("     Next to search click + → Create.")
        click.echo("       Name: LC-<project> (so you can refer it per project)")
        click.echo("       MCP server URL:  https://<tunnel-host>/mcp")
        click.echo(f"       Authentication:  {auth_value}")
        click.echo("        Create.")
        if not no_auth:
            click.echo(click.style("  3.", bold=True) + " Approve the browser OAuth page with the pairing code above.")
    click.echo("")
    if no_auth:
        click.secho("  ⚠  NO AUTHENTICATION: anyone who learns the tunnel URL gets", fg="red", bold=True)
        click.secho("     unauthenticated shell-grade access to this machine. The URL", fg="red", bold=True)
        click.secho("     is the only secret — prefer OAuth mode (omit --no-auth).", fg="red", bold=True)
    else:
        click.echo(
            click.style("  ⚠  ", fg="red", bold=True)
            + click.style("This exposes shell-grade tool access to this machine over the", fg="yellow")
        )
        click.echo(click.style("     tunnel. Only share the pairing code with yourself; stop the", fg="yellow"))
        click.echo(click.style("     server (Ctrl-C) when you are done.", fg="yellow"))
    click.echo(f"  {rule}")
    click.echo("")

    try:
        config = uvicorn.Config(app, log_level="info", timeout_keep_alive=30)
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        # Ctrl-C lands here via KeyboardInterrupt out of Server.run: take the
        # tunnel down with us so no stray cloudflared keeps the URL alive.
        if tunnel_proc is not None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()
        sock.close()


@chatgpt_group.command("client")
@click.option(
    "--redirect-uri",
    "redirect_uris",
    multiple=True,
    help="OAuth redirect URI to register (repeatable; default: ChatGPT's connector redirects).",
)
def chatgpt_client_cmd(redirect_uris: tuple[str, ...]) -> None:
    """Print a stable OAuth client ID for ChatGPT's "Enter a client ID" field.

    ChatGPT's connector form can use a user-supplied OAuth client instead of
    dynamic registration. This mints one in the same state store `serve` uses
    (idempotent: re-running prints the same ID) so it survives restarts.
    """
    from lemoncrow.gateway.adapters.mcp_oauth import (
        _is_allowed_redirect_uri,
        default_state_path,
        ensure_user_client,
    )

    uris = list(redirect_uris) if redirect_uris else list(_CHATGPT_REDIRECT_URIS)
    for uri in uris:
        if not _is_allowed_redirect_uri(uri):
            raise click.UsageError(f"redirect_uri must be https (or http loopback): {uri}")
    record = ensure_user_client(default_state_path(), uris)

    click.echo("")
    click.echo(
        click.style("  Client ID:      ", dim=True) + click.style(str(record["client_id"]), fg="green", bold=True)
    )
    click.echo(click.style("  Client secret:  ", dim=True) + "leave empty (public client, PKCE)")
    click.echo("")
    click.echo("  Paste the client ID into ChatGPT's connector form → Advanced /")
    click.echo('  OAuth client section ("Enter a client ID"). Registered redirect URIs:')
    for uri in record["redirect_uris"]:
        click.echo(f"    - {uri}")
    click.echo("")
