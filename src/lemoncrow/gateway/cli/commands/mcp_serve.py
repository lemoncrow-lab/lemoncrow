"""``lc mcp serve`` — expose LemonCrow's MCP transport to any remote MCP client.

Nothing here is vendor-specific: ``serve`` publishes the standard
streamable-HTTP MCP transport at ``/mcp``, protected by the OAuth 2.1 shim in
``mcp_oauth.py`` (or open, with ``--no-auth``). Any client that accepts a remote
MCP server URL — ChatGPT connectors, Claude connectors, Cursor, VS Code, and
other MCP hosts — connects to the same URL. By default a cloudflared *quick
tunnel* is auto-launched (downloading cloudflared on first use if needed) to
provide the public https URL remote clients require; ``--no-tunnel`` opts out
for operators running their own named tunnel / ngrok. ``client`` mints a stable
user-defined OAuth client ID for clients that ask for one instead of doing
dynamic registration (ChatGPT's "Enter a client ID" field).

``lc chatgpt`` stays registered as a hidden deprecated alias of this group.
On-disk state still lives under ``<store_root>/chatgpt/`` — the directory name
is kept so already-paired connectors survive the rename.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click

if TYPE_CHECKING:
    import uvicorn

    from lemoncrow.gateway.cli.commands._mcp_service import ServiceInfo
    from lemoncrow.gateway.cli.commands._persistent_tunnel import TunnelState

# How long to wait for cloudflared to print its quick-tunnel URL before giving
# up and falling back to manual instructions. Tunnel establishment is normally
# a couple of seconds; 30s covers a slow first-run edge download.
_TUNNEL_URL_TIMEOUT_SECONDS = 30.0

_CLOUDFLARED_INSTALL_URL = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
_CLOUDFLARED_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"

# Connector OAuth redirect endpoints of the hosted chat clients that ask for a
# user-defined client ID instead of doing dynamic registration. Used as the
# default redirect_uris for `lc mcp client`; override with --redirect-uri for
# any other host. Current and legacy/forward-compat hostnames are both
# registered per vendor — an unregistered redirect_uri fails the handshake, and
# a spare entry costs nothing since each is still an exact-match allowlist.
#
# Not covered here (deliberately): ChatGPT now mints a *per-app* callback
# (https://chatgpt.com/connector/oauth/<callback_id>) shown in its app-management
# UI. It cannot be predicted, so pass it with --redirect-uri when ChatGPT shows
# one instead of the legacy shared endpoint below.
_CONNECTOR_REDIRECT_URIS = (
    # Claude — one callback shared by web, Desktop and mobile.
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    # ChatGPT — legacy shared endpoint, still honoured for existing apps.
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chat.openai.com/connector_platform_oauth_redirect",
)

# Back-compat alias for the pre-rename name.
_CHATGPT_REDIRECT_URIS = _CONNECTOR_REDIRECT_URIS

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
    click.echo("  Then run `uv run lemoncrow mcp serve` again.", err=True)
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


def _watch_tunnel_process(proc: subprocess.Popen[str], server: uvicorn.Server, shutting_down: threading.Event) -> None:
    """Blocking watchdog: stop ``server`` the moment ``proc`` dies on its own.

    cloudflared can crash or wedge independently of the local uvicorn server —
    an edge disconnect it fails to recover from, an OOM kill, a stale process —
    and nothing else here ever notices: the server keeps answering *locally*
    while the public tunnel silently 502s every request. Run this in a daemon
    thread; ``proc.wait()`` blocks until the child actually exits, so this
    costs nothing while the tunnel is healthy. On exit, ``shutting_down``
    distinguishes the two ways ``wait()`` can return: deliberate shutdown
    (the ``finally`` block below sets it before tearing the tunnel down
    itself — nothing to do here) versus an unannounced death, where setting
    ``server.should_exit`` is uvicorn's documented way to stop the server
    programmatically from another thread — the same cooperative path a
    SIGTERM/Ctrl-C takes, without this thread signalling the whole process
    itself. Under ``--persistent`` that exit is exactly what the
    systemd/launchd unit's ``Restart=always`` (see ``_mcp_service.py``) is
    watching for, so the whole unit — including a fresh cloudflared — comes
    back up instead of the tunnel staying dead until someone notices and
    restarts by hand.
    """
    proc.wait()
    if shutting_down.is_set():
        return  # our own finally block killed it as part of a normal stop
    click.secho(
        f"  ✗ tunnel process exited unexpectedly (code {proc.returncode}) — "
        "stopping so the supervisor can bring up a fresh tunnel.",
        fg="red",
        err=True,
    )
    server.should_exit = True


# ── CLI ───────────────────────────────────────────────────────────────────────
# Where to paste the URL in the clients people actually ask about. Not a
# capability list — the endpoint is plain MCP, so anything that accepts a
# remote server URL works; these are just the two menus that are hard to find.
_CLIENT_HINTS = (
    ("ChatGPT", "Settings → Plugins → Browse Plugins → (next to search) + → Create"),
    ("Claude", "Settings → Connectors → Add custom connector"),
    ("Cursor / VS Code / Zed / …", "add a remote (streamable-HTTP) MCP server"),
)


def _echo_client_hints() -> None:
    for name, where in _CLIENT_HINTS:
        click.echo(f"       {name}:  " + click.style(where, dim=True))


def _handoff_to_service(
    *,
    hostname: str,
    slug: str,
    tunnel_state_path: Path,
    existing_tunnel_state: TunnelState | None,
    sock: socket.socket,
    port: int,
    explicit_port: bool,
    host: str,
    no_auth: bool,
    code: str | None,
) -> None:
    """Install/refresh this hostname's service, start it, and print the banner.

    The interactive half of ``--persistent`` (cloudflared browser login, tunnel
    create, DNS route) runs *here*, in the operator's terminal, so that the
    supervised process only ever has to do the silent half. The reserved socket
    is released before the service starts — with an explicit ``--port`` the unit
    binds that exact port, and holding it here would make the unit crash-loop.
    """
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.gateway.cli.commands._mcp_service import (
        ServiceError,
        management_hints,
        register_persistent_service,
    )
    from lemoncrow.gateway.cli.commands._persistent_tunnel import (
        TunnelSetupError,
        provision_persistent_tunnel,
    )

    binary = _resolve_cloudflared()
    if binary is None:
        binary = _install_cloudflared_interactive(port)
    try:
        provision_persistent_tunnel(
            hostname=hostname,
            existing_state=existing_tunnel_state,
            state_path=tunnel_state_path,
            binary=binary,
            narrate=lambda msg: click.secho(f"  {msg}", dim=True),
        )
    except TunnelSetupError as exc:
        click.echo(f"  ✗ {exc}", err=True)
        raise SystemExit(1) from exc

    serve_args = ["mcp", "serve", "--persistent", "--hostname", hostname, "--foreground"]
    if explicit_port:
        serve_args += ["--port", str(port)]
    if host != "127.0.0.1":
        serve_args += ["--host", host]
    if no_auth:
        serve_args.append("--no-auth")

    sock.close()
    workspace = Path.cwd()
    try:
        unit = register_persistent_service(
            hostname=hostname,
            slug=slug,
            workspace=workspace,
            serve_args=serve_args,
            root=default_store_root(),
            narrate=lambda msg: click.secho(f"  {msg}", dim=True),
        )
    except ServiceError as exc:
        raise click.ClickException(
            f"{exc}\n  (run the same command with --foreground to serve in this terminal instead)"
        ) from exc

    rule = "─" * 64
    click.echo("")
    click.echo(f"  {rule}")
    click.secho("  LemonCrow remote MCP server " + ("(NO AUTH)" if no_auth else "(OAuth 2.1)"), fg="cyan", bold=True)
    click.echo(f"  {rule}")
    click.secho("  ✓ running as a background service — starts again on reboot", fg="green")
    if code is not None:
        click.echo(click.style("  Pairing code:    ", dim=True) + click.style(code, fg="yellow", bold=True))
        click.secho("                   stays the same across restarts (--new-pairing-code changes it)", dim=True)
    click.echo(
        click.style("  MCP server URL:  ", dim=True) + click.style(f"https://{hostname}/mcp", fg="green", bold=True)
    )
    click.echo(click.style("  Authentication:  ", dim=True) + ("None (no auth)" if no_auth else "OAuth"))
    click.echo(click.style("  Service:         ", dim=True) + unit)
    click.echo(click.style("  Workspace:       ", dim=True) + str(workspace))
    click.echo("")
    click.echo(click.style("  1.", bold=True) + " Add it as a remote MCP server in any client that takes a URL:")
    _echo_client_hints()
    if not no_auth:
        click.echo(click.style("  2.", bold=True) + " Approve the browser OAuth page with the pairing code above.")
    click.echo("")
    click.secho("  Manage it:", dim=True)
    for hint in management_hints(unit):
        click.secho(f"      {hint}", fg="cyan")
    click.echo("")
    if no_auth:
        click.secho("  ⚠  NO AUTHENTICATION: anyone who learns the URL gets", fg="red", bold=True)
        click.secho("     unauthenticated shell-grade access to this machine.", fg="red", bold=True)
    else:
        click.echo(
            click.style("  ⚠  ", fg="red", bold=True)
            + click.style("This exposes shell-grade tool access to this machine over the", fg="yellow")
        )
        click.echo(click.style("     tunnel, now permanently. Only share the pairing code with", fg="yellow"))
        click.echo(click.style("     yourself; stop the service when you are done.", fg="yellow"))
    click.echo(f"  {rule}")
    click.echo("")


@click.command("serve")
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
    help="One-off pairing code for this run only (not persisted). Omit to use "
    "the stable code stored for this server, minted on first run.",
)
@click.option(
    "--new-pairing-code",
    is_flag=True,
    default=False,
    help="Rotate the stored pairing code (use if the old one leaked). Every "
    "already-authorized client keeps working — only re-pairing needs the new code.",
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
    help="Serve /mcp with NO authentication (the client's 'No authentication' option). "
    "Anyone with the URL gets full tool access — prefer OAuth.",
)
@click.option(
    "--persistent",
    is_flag=True,
    default=False,
    help="Stable MCP URL that survives restarts, backed by a real Cloudflare named "
    "tunnel (requires a domain you manage in Cloudflare DNS; one-time `cloudflared "
    "tunnel login` browser step). Also registers the server as a boot-persistent "
    "background service for this directory, so it is always up (--foreground opts "
    "out). Mutually exclusive with --no-tunnel. Recommended: most clients have to "
    "be re-pointed every time a quick-tunnel URL rotates.",
)
@click.option(
    "--hostname",
    default=None,
    help="Public hostname to bind for --persistent, e.g. mcp.example.com (a domain you "
    "manage in Cloudflare). Each hostname gets its own tunnel, state and OAuth store, so "
    "several projects can serve at once. Required on the first --persistent run, and "
    "whenever more than one hostname is already configured.",
)
@click.option(
    "--reset-tunnel",
    is_flag=True,
    default=False,
    help="Clear this hostname's persisted tunnel state first, so it can be "
    "reconfigured from scratch. Does NOT delete the Cloudflare-side "
    "tunnel — that's `cloudflared tunnel delete` yourself.",
)
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help="Run the server in this terminal instead of registering it as a "
    "boot-persistent background service (--persistent only; this is what the "
    "installed service itself runs).",
)
def mcp_serve_cmd(
    port: int | None,
    host: str,
    pairing_code: str | None,
    new_pairing_code: bool,
    reset: bool,
    tunnel: bool,
    no_auth: bool,
    persistent: bool,
    hostname: str | None,
    reset_tunnel: bool,
    foreground: bool,
) -> None:
    """Publish this workspace as a remote MCP server for any chat app.

    Serves the standard streamable-HTTP MCP transport at ``/mcp`` behind OAuth
    2.1, so any MCP client that takes a server URL gets the same LemonCrow
    tools you use locally. By default a cloudflared quick tunnel is launched
    automatically (offering a one-time download of cloudflared if missing) and
    the public MCP server URL is printed.

    \b
    1. Paste the printed https://<host>/mcp URL into your client:
         ChatGPT  Settings -> Plugins -> Browse Plugins ->
                  (next to search) + -> Create
         Claude   Settings -> Connectors -> Add custom connector
         Cursor / VS Code / Zed / ...: add a remote (streamable-HTTP) server
       Authentication: OAuth.
    2. Approve the browser OAuth page with the pairing code below. That code
       is stored per server, so it survives restarts.

    With --no-tunnel, expose the port yourself (named cloudflared tunnel, ngrok).
    With --no-auth, /mcp is served completely open (URL = the only secret).
    With --persistent --hostname mcp.example.com, get a stable URL that survives
    restarts instead of a rotating quick-tunnel one (first run only; --hostname
    isn't needed again once configured). --persistent also installs the server
    as a user service (systemd/launchd) bound to the current directory and
    starts it: it stays up after you close the terminal and comes back on
    reboot, with the same URL and the same pairing code. Add --foreground to
    run it in this terminal instead.
    """
    import uvicorn

    from lemoncrow.gateway.adapters.mcp_oauth import (
        create_protected_mcp_app,
        default_pairing_path,
        default_state_path,
        load_or_create_pairing_code,
        reset_pairing_code,
        reset_state,
    )
    from lemoncrow.gateway.adapters.mcp_oauth import migrate_legacy_state as migrate_legacy_oauth_state
    from lemoncrow.gateway.cli.commands._mcp_service import is_supervised, supervisor_kind
    from lemoncrow.gateway.cli.commands._persistent_tunnel import (
        TunnelSetupError,
        hostname_slug,
        load_all_tunnel_states,
        load_tunnel_state,
        migrate_legacy_state,
        reset_tunnel_state,
        setup_persistent_tunnel,
        tunnel_name_for,
        tunnel_state_path_for,
    )

    if no_auth and (pairing_code is not None or reset or new_pairing_code):
        raise click.UsageError("--no-auth cannot be combined with --pairing-code, --new-pairing-code or --reset")
    if new_pairing_code and pairing_code is not None:
        raise click.UsageError("--new-pairing-code cannot be combined with --pairing-code (it sets the code itself)")
    if persistent and not tunnel:
        raise click.UsageError("--persistent cannot be combined with --no-tunnel (--persistent IS a tunnel mode)")
    if reset_tunnel and not persistent:
        raise click.UsageError("--reset-tunnel requires --persistent")

    # --persistent installs a boot-persistent service and hands the server to
    # it (see _mcp_service); the supervised process itself re-enters here with
    # --foreground. `is_supervised()` covers a unit installed before this flag
    # existed, so an old ExecStart can never restart-loop itself.
    register_service = persistent and not foreground and not is_supervised()
    if register_service and supervisor_kind() is None:
        # No usable supervisor (container, WSL without a user bus, unknown
        # platform): serve here rather than failing — the URL is still stable.
        click.secho(
            "  note: no systemd/launchd user session — serving in the foreground "
            "(this server stops when you close the terminal).",
            fg="yellow",
        )
        register_service = False
    if register_service and pairing_code is not None:
        raise click.UsageError(
            "--pairing-code is a one-off for this process only, so it cannot configure a "
            "background service — use --new-pairing-code to rotate the stored code, or add --foreground"
        )
    explicit_port = port is not None

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

    tunnel_state_path: Path | None = None
    existing_tunnel_state = None
    resolved_hostname: str | None = None
    if persistent:
        migrate_legacy_state()
        if hostname is not None:
            resolved_hostname = hostname
        else:
            # No --hostname: unambiguous only while exactly one connector is
            # configured. Silently picking one of several would resurrect the
            # very cross-project mix-up per-hostname state exists to prevent.
            configured = load_all_tunnel_states()
            if not configured:
                raise click.UsageError("first --persistent run needs --hostname <your-domain-in-cloudflare>")
            if len(configured) > 1:
                raise click.UsageError(
                    "several hostnames are configured — pass --hostname to pick one: "
                    + ", ".join(state.hostname for state in configured)
                )
            resolved_hostname = configured[0].hostname
        tunnel_state_path = tunnel_state_path_for(resolved_hostname)
        if reset_tunnel:
            removed = reset_tunnel_state(tunnel_state_path)
            click.echo(
                "  Reset persistent-tunnel state "
                f"({'removed ' + str(tunnel_state_path) if removed else 'nothing to remove'})."
            )
        else:
            existing_tunnel_state = load_tunnel_state(tunnel_state_path)
        if existing_tunnel_state is None:
            # Tunnel names are the bare subdomain label, so two zones sharing
            # a label would silently land both connectors on one tunnel —
            # Cloudflare would then load-balance their traffic together.
            claimed = tunnel_name_for(resolved_hostname)
            clash = next(
                (
                    state
                    for state in load_all_tunnel_states()
                    if state.hostname != resolved_hostname and state.tunnel_name == claimed
                ),
                None,
            )
            if clash is not None:
                raise click.UsageError(
                    f"tunnel name {claimed!r} is already used by {clash.hostname} — "
                    "pick a different subdomain label so each connector keeps its own tunnel"
                )

    code: str | None = None
    state_path: Path | None = None
    if not no_auth:
        # Per-hostname OAuth store under --persistent: every mutation flushes
        # the whole file, so two concurrently-serving projects on one store
        # would clobber each other's clients and token hashes (the other
        # connector starts 401-ing mid-session).
        oauth_scope = hostname_slug(resolved_hostname) if resolved_hostname else None
        if oauth_scope is not None:
            migrate_legacy_oauth_state(oauth_scope)
        state_path = default_state_path(oauth_scope)
        pairing_path = default_pairing_path(oauth_scope)
        if reset:
            removed = reset_state(state_path)
            reset_pairing_code(pairing_path)
            click.echo(f"  Reset OAuth state ({'removed ' + str(state_path) if removed else 'nothing to remove'}).")
        # Explicit --pairing-code stays a one-off override; otherwise the code
        # is read from (or minted into) the store, so restarting the server
        # does not invalidate the code the operator already knows.
        if pairing_code is not None:
            code = pairing_code
        else:
            code = load_or_create_pairing_code(pairing_path, rotate=new_pairing_code)
            if new_pairing_code:
                click.secho("  Rotated the stored pairing code.", fg="yellow")

    # ── boot-persistent handoff ───────────────────────────────────────────
    # Everything above is state the service and this process share (tunnel
    # reference, OAuth store, pairing code). What is left — binding the port,
    # running cloudflared, serving — belongs to the supervisor, so the server
    # comes back by itself after a reboot with the same URL and same code.
    if register_service:
        assert resolved_hostname is not None and tunnel_state_path is not None
        _handoff_to_service(
            hostname=resolved_hostname,
            slug=hostname_slug(resolved_hostname),
            tunnel_state_path=tunnel_state_path,
            existing_tunnel_state=existing_tunnel_state,
            sock=sock,
            port=port,
            explicit_port=explicit_port,
            host=host,
            no_auth=no_auth,
            code=code,
        )
        return

    if no_auth:
        from lemoncrow.gateway.adapters.mcp_http import create_mcp_http_app

        app = create_mcp_http_app()
    else:
        assert state_path is not None and code is not None
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
        # both guaranteed by the --persistent resolution above
        assert resolved_hostname is not None
        assert tunnel_state_path is not None
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
        "  LemonCrow remote MCP server " + ("(NO AUTH)" if no_auth else "(OAuth 2.1)"),
        fg="cyan",
        bold=True,
    )
    click.echo(f"  {rule}")
    if code is not None:
        click.echo(click.style("  Pairing code:  ", dim=True) + click.style(code, fg="yellow", bold=True))
        if pairing_code is None:
            click.secho(
                "                 stays the same across restarts (--new-pairing-code changes it)",
                dim=True,
            )
    click.echo(click.style("  Local server:  ", dim=True) + f"http://{host}:{port}/mcp")
    click.echo(click.style("  Request log:   ", dim=True) + click.style(str(log_path), fg="magenta"))
    click.secho("  View it live in a second terminal (keeps this output clean):", dim=True)
    click.secho(f"      tail -f {log_path} | jq .", fg="cyan", bold=True)
    click.echo("")
    if tunnel_url is not None:
        # THE value the user pastes into their client — make it the loudest line.
        click.echo(
            click.style("  MCP server URL:  ", dim=True) + click.style(f"{tunnel_url}/mcp", fg="green", bold=True)
        )
        click.echo(click.style("  Authentication:  ", dim=True) + auth_value)
        click.echo("")
        click.echo(click.style("  1.", bold=True) + " Add it as a remote MCP server in any client that takes a URL:")
        _echo_client_hints()
        click.echo("     Name it LC-<project> so you can tell projects apart.")
        if not no_auth:
            click.echo(click.style("  2.", bold=True) + " Approve the browser OAuth page with the pairing code above.")
        click.echo("")
        if persistent:
            click.secho("  Note: stable — this URL does not change across restarts.", dim=True)
        else:
            click.secho("  Note: this quick-tunnel URL rotates on every restart — re-point the", dim=True)
            click.secho("  client each time, or use --persistent for a stable URL.", dim=True)
    else:
        click.echo(click.style("  1.", bold=True) + " Expose it through a tunnel (in another terminal):")
        click.echo(f"       cloudflared tunnel --url http://localhost:{port}")
        click.echo(f"       # or:  ngrok http {port}")
        click.echo(click.style("  2.", bold=True) + " Add https://<tunnel-host>/mcp as a remote MCP server:")
        _echo_client_hints()
        click.echo(f"     Authentication:  {auth_value}")
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

    config = uvicorn.Config(app, log_level="info", timeout_keep_alive=30)
    server = uvicorn.Server(config)
    # Set the instant this shutdown is *ours* (finally block below), so the
    # watchdog can tell a deliberate stop from cloudflared actually dying —
    # see _watch_tunnel_process.
    shutting_down = threading.Event()
    if tunnel_proc is not None:
        threading.Thread(
            target=_watch_tunnel_process,
            args=(tunnel_proc, server, shutting_down),
            daemon=True,
            name="tunnel-watchdog",
        ).start()

    try:
        server.run(sockets=[sock])
    finally:
        # Ctrl-C lands here via KeyboardInterrupt out of Server.run: take the
        # tunnel down with us so no stray cloudflared keeps the URL alive.
        shutting_down.set()
        if tunnel_proc is not None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()
        sock.close()


@click.command("client")
@click.option(
    "--redirect-uri",
    "redirect_uris",
    multiple=True,
    help="OAuth redirect URI to register (repeatable; default: ChatGPT's connector redirects).",
)
@click.option(
    "--hostname",
    default=None,
    help="Mint the client in the OAuth store of this --persistent hostname (each "
    "hostname has its own store). Omit for the default/quick-tunnel store.",
)
def mcp_client_cmd(redirect_uris: tuple[str, ...], hostname: str | None) -> None:
    """Print a stable OAuth client ID for clients that ask for one.

    Most MCP clients register themselves dynamically and need nothing here.
    Some connector forms (ChatGPT's "Enter a client ID") want a user-supplied
    OAuth client instead. This mints one in the same state store `serve` uses
    (idempotent: re-running prints the same ID) so it survives restarts. Pass
    the same --hostname you serve that connector with, and --redirect-uri for
    any client whose callback is not in the built-in defaults.
    """
    from lemoncrow.gateway.adapters.mcp_oauth import (
        _is_allowed_redirect_uri,
        default_state_path,
        ensure_user_client,
    )
    from lemoncrow.gateway.cli.commands._persistent_tunnel import hostname_slug

    uris = list(redirect_uris) if redirect_uris else list(_CONNECTOR_REDIRECT_URIS)
    for uri in uris:
        if not _is_allowed_redirect_uri(uri):
            raise click.UsageError(f"redirect_uri must be https (or http loopback): {uri}")
    scope = hostname_slug(hostname) if hostname is not None else None
    record = ensure_user_client(default_state_path(scope), uris)

    click.echo("")
    click.echo(
        click.style("  Client ID:      ", dim=True) + click.style(str(record["client_id"]), fg="green", bold=True)
    )
    click.echo(click.style("  Client secret:  ", dim=True) + "leave empty (public client, PKCE)")
    click.echo("")
    click.echo("  Paste the client ID into your connector form → Advanced / OAuth")
    click.echo('  client section (in ChatGPT: "Enter a client ID"). Registered')
    click.echo("  redirect URIs:")
    for uri in record["redirect_uris"]:
        click.echo(f"    - {uri}")
    click.echo("")


# ── service management ──────────────────────────────────────────────────────────────
# `serve --persistent` installs the service; these drive the ones already
# installed, so restarting a connector (or rotating its pairing code) never
# means re-typing the full serve invocation from the right directory.
@click.group("service", context_settings={"help_option_names": ["-h", "--help"]})
def mcp_service_group() -> None:
    """Manage the boot-persistent MCP servers installed by `serve --persistent`."""


def _resolve_or_fail(selector: str | None) -> ServiceInfo:
    from lemoncrow.gateway.cli.commands._mcp_service import ServiceError, resolve_service

    try:
        return resolve_service(selector)
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc


def echo_persistent_servers(*, empty_hint: bool = True) -> int:
    """Render the installed always-on remote servers; returns how many there are.

    Shared by ``lc mcp list`` (the one inventory of everything MCP on this
    machine — stdio sessions, singleton daemons, and these) and ``lc mcp
    service list``, so the two can never drift into showing different things.
    """
    from lemoncrow.gateway.cli.commands._mcp_service import describe_services

    services = describe_services()
    click.echo("")
    click.echo(f"  Always-on remote MCP servers · {len(services)}")
    click.echo("  " + "─" * 70)
    if not services:
        if empty_hint:
            click.echo("  None installed. Publish one with: lc mcp serve --persistent --hostname <host>")
            click.echo("")
        return 0
    home = str(Path.home())
    for service in services:
        colour = {"active": "green", "failed": "red"}.get(service.state, "yellow")
        workspace = service.workspace
        if workspace.startswith(home):
            workspace = "~" + workspace[len(home) :]
        click.echo(
            "  "
            + click.style(f"{service.state:<9}", fg=colour, bold=True)
            + click.style(f"https://{service.hostname}/mcp", fg="green")
        )
        click.secho(f"            {service.name}  ({service.enabled})  {workspace}", dim=True)
    click.echo("")
    return len(services)


@mcp_service_group.command("list")
def mcp_service_list() -> None:
    """List installed MCP services, their hostname, workspace and run state."""
    echo_persistent_servers()


@mcp_service_group.command("start")
@click.argument("hostname", required=False)
def mcp_service_start(hostname: str | None) -> None:
    """Start one installed MCP service."""
    _control(hostname, "start")


@mcp_service_group.command("stop")
@click.argument("hostname", required=False)
def mcp_service_stop(hostname: str | None) -> None:
    """Stop one installed MCP service (it still starts again on reboot)."""
    _control(hostname, "stop")


@mcp_service_group.command("restart")
@click.argument("hostname", required=False)
@click.option(
    "--new-pairing-code",
    is_flag=True,
    default=False,
    help="Rotate this server's stored pairing code before restarting (already-"
    "authorized clients keep working; only re-pairing needs the new code).",
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Revoke this server's OAuth state (all clients and tokens) before "
    "restarting — every client has to pair again.",
)
def mcp_service_restart(hostname: str | None, new_pairing_code: bool, reset: bool) -> None:
    """Restart one installed MCP service, optionally rotating its credentials."""
    from lemoncrow.gateway.adapters.mcp_oauth import (
        default_pairing_path,
        default_state_path,
        load_or_create_pairing_code,
        reset_pairing_code,
        reset_state,
    )
    from lemoncrow.gateway.cli.commands._persistent_tunnel import hostname_slug

    service = _resolve_or_fail(hostname)
    scope = hostname_slug(service.hostname) if service.hostname else None
    if reset:
        removed = reset_state(default_state_path(scope))
        reset_pairing_code(default_pairing_path(scope))
        click.echo(f"  Reset OAuth state ({'removed' if removed else 'nothing to remove'}).")
    code: str | None = None
    if new_pairing_code or reset:
        code = load_or_create_pairing_code(default_pairing_path(scope), rotate=new_pairing_code)
    _control(hostname, "restart", service=service)
    if code is not None:
        click.echo(click.style("  Pairing code:  ", dim=True) + click.style(code, fg="yellow", bold=True))


@mcp_service_group.command("code")
@click.argument("hostname", required=False)
def mcp_service_code(hostname: str | None) -> None:
    """Print the stored pairing code of one installed MCP service."""
    from lemoncrow.gateway.adapters.mcp_oauth import default_pairing_path, load_or_create_pairing_code
    from lemoncrow.gateway.cli.commands._persistent_tunnel import hostname_slug

    service = _resolve_or_fail(hostname)
    scope = hostname_slug(service.hostname) if service.hostname else None
    click.echo(load_or_create_pairing_code(default_pairing_path(scope)))


@mcp_service_group.command("logs")
@click.argument("hostname", required=False)
@click.option("--lines", "-n", default=50, show_default=True, help="Recent lines to show.")
@click.option("--follow/--no-follow", "-f", default=True, show_default=True, help="Keep streaming.")
def mcp_service_logs(hostname: str | None, lines: int, follow: bool) -> None:
    """Tail one installed MCP service's logs."""
    from lemoncrow.gateway.cli.commands._mcp_service import log_command

    service = _resolve_or_fail(hostname)
    cmd = log_command(service, lines=lines, follow=follow)
    os.execvp(cmd[0], cmd)


@mcp_service_group.command("remove")
@click.argument("hostname", required=False)
def mcp_service_remove(hostname: str | None) -> None:
    """Stop and unregister one MCP service (tunnel and OAuth state are kept)."""
    from lemoncrow.gateway.cli.commands._mcp_service import ServiceError, remove_service

    service = _resolve_or_fail(hostname)
    try:
        remove_service(service)
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"  Removed {service.name}.", fg="green")
    click.secho(
        "  Its tunnel and OAuth store are untouched — `lc mcp serve --persistent "
        f"--hostname {service.hostname}` brings it back with the same pairing code.",
        dim=True,
    )


def _control(selector: str | None, action: str, service: ServiceInfo | None = None) -> None:
    from lemoncrow.gateway.cli.commands._mcp_service import ServiceError, control_service

    target = service if service is not None else _resolve_or_fail(selector)
    try:
        control_service(target, action)
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"  {action}ed {target.name}", fg="green")


# ── deprecated alias ────────────────────────────────────────────────────────────────
@click.group("chatgpt", context_settings={"help_option_names": ["-h", "--help"]})
def chatgpt_group() -> None:
    """Deprecated alias of ``lc mcp serve`` / ``lc mcp client``.

    The endpoint was never ChatGPT-specific — it is plain remote MCP. Kept so
    scripts and muscle memory from before the rename keep working.
    """
    click.secho("  note: `lc chatgpt` is now `lc mcp` — this alias still works.", fg="yellow", err=True)


chatgpt_group.add_command(mcp_serve_cmd)
chatgpt_group.add_command(mcp_client_cmd)
chatgpt_group.add_command(mcp_service_group)

# Pre-rename symbol names, kept for any out-of-tree importer.
chatgpt_serve_cmd = mcp_serve_cmd
chatgpt_client_cmd = mcp_client_cmd
