"""``lc mcp serve`` — expose LemonCrow's MCP transport to any remote MCP client.

Nothing here is vendor-specific: ``serve`` publishes the standard
streamable-HTTP MCP transport at ``/mcp``, protected by the OAuth 2.1 shim in
the standalone pairing flow in ``mcp_oauth.py`` (or open, with ``--no-auth``).
Hosted enterprise Remote MCP uses Authward instead. Any client that accepts a remote
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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click

if TYPE_CHECKING:
    import uvicorn

    from lemoncrow.gateway.cli.commands._persistent_tunnel import TunnelState
    from lemoncrow.gateway.mcp_connectors import ConnectorBinding

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


def _persistent_backend_port() -> int:
    """Port of the indexed local LemonCrow server used by the MCP gateway."""
    from urllib.parse import urlsplit

    from lemoncrow_client.config import load_config

    env = dict(os.environ)
    if env.get("LEMONCROW_ROOT") and not env.get("LEMONCROW_HOME"):
        env["LEMONCROW_HOME"] = env["LEMONCROW_ROOT"]
    config = load_config(env, cwd=Path.cwd())
    parsed = urlsplit(config.url)
    host = (parsed.hostname or "").strip("[]").lower()
    if config.install_mode != "local" or host not in {"127.0.0.1", "localhost", "::1"}:
        raise click.ClickException(
            "--persistent publishes a local thin-client gateway; configure local mode "
            "(LEMONCROW_URL=http://127.0.0.1:7420) instead of tunnelling a hosted server"
        )
    return int(parsed.port or (443 if parsed.scheme == "https" else 80))


def _persistent_origin_port() -> int:
    """Independent loopback port where persistent MCP terminates."""
    raw = os.environ.get("LEMONCROW_MCP_GATEWAY_PORT", "7421").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise click.ClickException("LEMONCROW_MCP_GATEWAY_PORT must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise click.ClickException("LEMONCROW_MCP_GATEWAY_PORT must be between 1 and 65535")
    return port


def _wait_gateway_ready(port: int, process: subprocess.Popen[str], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise click.ClickException(f"MCP gateway exited {process.returncode} before becoming ready")
        try:
            with urllib.request.urlopen(url, timeout=0.25) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    process.terminate()
    raise click.ClickException(f"MCP gateway did not become ready at {url}")


class _ThinHttpRuntime:
    """Per-HTTP-session wrapper around the canonical thin-client MCP server."""

    def __init__(self) -> None:
        from lemoncrow_client.config import load_config

        env = dict(os.environ)
        if env.get("LEMONCROW_ROOT") and not env.get("LEMONCROW_HOME"):
            env["LEMONCROW_HOME"] = env["LEMONCROW_ROOT"]
        self._config = load_config(env, cwd=Path.cwd())
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[Any, threading.Lock]] = {}

    def _entry(self, session_id: str | None) -> tuple[Any, threading.Lock]:
        key = session_id or "http-default"
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                from lemoncrow_client.mcpserver import McpServer

                entry = (McpServer(self._config), threading.Lock())
                self._sessions[key] = entry
            return entry

    def dispatch(
        self,
        request: dict[str, Any],
        session_id: str | None = None,
        host: str | None = None,
        bridge_id: str | None = None,
    ) -> dict[str, Any] | None:
        del host, bridge_id
        server, lock = self._entry(session_id)
        with lock:
            return server.handle(request)

    @staticmethod
    def tools() -> list[dict[str, Any]]:
        from lemoncrow_client.surface import tool_list

        return [dict(spec) for spec in tool_list()]

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for server, lock in sessions:
            with lock:
                server.close()


def _thin_http_runtime() -> _ThinHttpRuntime:
    return _ThinHttpRuntime()


def _register_connector(hostname: str) -> None:
    from lemoncrow.gateway.mcp_connectors import ConnectorBinding, save_connector

    try:
        save_connector(ConnectorBinding(hostname=hostname, workspace=str(Path.cwd().resolve())))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _handoff_to_service(
    *,
    hostname: str,
    tunnel_state: TunnelState,
    origin_port: int,
    backend_port: int,
    binary: str,
    code: str | None,
) -> None:
    """Install/restart the one shared cloudflared service and print the connector banner."""
    from lemoncrow.core.foundation.paths import default_store_root
    from lemoncrow.gateway.cli.commands._mcp_service import (
        ServiceError,
        management_hints,
        register_shared_tunnel_service,
    )

    try:
        unit = register_shared_tunnel_service(
            binary=binary,
            tunnel_ref=tunnel_state.tunnel_id,
            credentials_path=tunnel_state.credentials_path,
            origin_port=origin_port,
            backend_port=backend_port,
            root=default_store_root(),
            narrate=lambda msg: click.secho(f"  {msg}", dim=True),
        )
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc

    workspace = Path.cwd().resolve()
    rule = "─" * 64
    click.echo("")
    click.echo(f"  {rule}")
    click.secho("  LemonCrow remote MCP connector (OAuth 2.1)", fg="cyan", bold=True)
    click.echo(f"  {rule}")
    click.secho("  ✓ routed through the shared boot-persistent Cloudflare tunnel", fg="green")
    if code is not None:
        click.echo(click.style("  Pairing code:    ", dim=True) + click.style(code, fg="yellow", bold=True))
        click.secho("                   stays the same across restarts (--new-pairing-code changes it)", dim=True)
    click.echo(
        click.style("  MCP server URL:  ", dim=True) + click.style(f"https://{hostname}/mcp", fg="green", bold=True)
    )
    click.echo(click.style("  MCP gateway:      ", dim=True) + f"http://127.0.0.1:{origin_port}")
    click.echo(click.style("  Indexed backend:  ", dim=True) + f"http://127.0.0.1:{backend_port}")
    click.echo(click.style("  Tunnel service:   ", dim=True) + unit)
    click.echo(click.style("  Workspace:        ", dim=True) + str(workspace))
    click.echo("")
    click.echo(click.style("  1.", bold=True) + " Add it as a remote MCP server in any client that takes a URL:")
    _echo_client_hints()
    click.echo(click.style("  2.", bold=True) + " Approve the browser OAuth page with the pairing code above.")
    click.echo("")
    click.secho("  Manage the shared tunnel:", dim=True)
    for hint in management_hints(unit):
        click.secho(f"      {hint}", fg="cyan")
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
    isn't needed again once configured). --persistent registers this workspace
    on the configured LemonCrow server and installs only the named Cloudflare
    tunnel as a user service. The MCP execution runtime remains the one central
    LemonCrow server. Add --foreground to run only the tunnel in this terminal.
    """
    import uvicorn

    from lemoncrow.gateway.adapters.mcp_oauth import (
        create_protected_mcp_app,
        default_pairing_path,
        default_state_path,
        load_or_create_pairing_code,
        migrate_legacy_state,
        reset_pairing_code,
        reset_state,
    )
    from lemoncrow.gateway.cli.commands._mcp_service import supervisor_kind

    if no_auth and (pairing_code is not None or reset or new_pairing_code):
        raise click.UsageError("--no-auth cannot be combined with --pairing-code, --new-pairing-code or --reset")
    if new_pairing_code and pairing_code is not None:
        raise click.UsageError("--new-pairing-code cannot be combined with --pairing-code (it sets the code itself)")
    if persistent and not tunnel:
        raise click.UsageError("--persistent cannot be combined with --no-tunnel (--persistent IS a tunnel mode)")
    if persistent and no_auth:
        raise click.UsageError(
            "--persistent uses the central server OAuth surface and cannot be combined with --no-auth"
        )
    if persistent and (port is not None or host != "127.0.0.1"):
        click.secho(
            "  note: --port/--host are ignored in persistent mode; the tunnel targets the configured LemonCrow server",
            dim=True,
        )
    if reset_tunnel and not persistent:
        raise click.UsageError("--reset-tunnel requires --persistent")

    # Persistent mode installs one cloudflared unit directly. No LemonCrow CLI
    # process re-enters this command under systemd anymore.
    register_service = persistent and not foreground
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
    # Persistent mode terminates at an independent thin-client HTTP gateway.
    # That process owns client-side bash/edit execution and survives backend
    # server restarts; only indexed/intelligence calls cross to the backend.
    persistent_origin_port = _persistent_origin_port() if persistent else 0
    persistent_backend_port = _persistent_backend_port() if persistent else 0

    if persistent:
        from lemoncrow.gateway.adapters.mcp_oauth import migrate_legacy_state as migrate_legacy_oauth_state
        from lemoncrow.gateway.cli.commands._persistent_tunnel import (
            TunnelSetupError,
            hostname_slug,
            provision_shared_tunnel,
            start_named_tunnel_process,
        )
        from lemoncrow.gateway.mcp_connectors import load_all_connectors

        if hostname is not None:
            resolved_hostname = hostname
        else:
            configured = load_all_connectors()
            if not configured:
                raise click.UsageError("first --persistent run needs --hostname <your-domain-in-cloudflare>")
            if len(configured) > 1:
                raise click.UsageError(
                    "several connector hostnames are configured — pass --hostname to pick one: "
                    + ", ".join(binding.hostname for binding in configured)
                )
            resolved_hostname = configured[0].hostname

        _register_connector(resolved_hostname)
        oauth_scope = hostname_slug(resolved_hostname)
        migrate_legacy_oauth_state(oauth_scope)
        persistent_state_path = default_state_path(oauth_scope)
        pairing_path = default_pairing_path(oauth_scope)
        if reset:
            removed = reset_state(persistent_state_path)
            reset_pairing_code(pairing_path)
            click.echo(
                f"  Reset OAuth state "
                f"({'removed ' + str(persistent_state_path) if removed else 'nothing to remove'})."
            )
        persistent_code = load_or_create_pairing_code(pairing_path, rotate=new_pairing_code)
        if new_pairing_code:
            click.secho("  Rotated the stored pairing code.", fg="yellow")

        binary = _resolve_cloudflared()
        if binary is None:
            binary = _install_cloudflared_interactive(persistent_origin_port)
        try:
            tunnel_state = provision_shared_tunnel(
                hostname=resolved_hostname,
                binary=binary,
                narrate=lambda msg: click.secho(f"  {msg}", dim=True),
                reset=reset_tunnel,
            )
        except TunnelSetupError as exc:
            raise click.ClickException(str(exc)) from exc

        if register_service:
            _handoff_to_service(
                hostname=resolved_hostname,
                tunnel_state=tunnel_state,
                origin_port=persistent_origin_port,
                backend_port=persistent_backend_port,
                binary=binary,
                code=persistent_code,
            )
            return

        from lemoncrow.gateway.cli.commands._mcp_service import start_gateway_process

        gateway_proc = start_gateway_process(
            origin_port=persistent_origin_port,
            backend_port=persistent_backend_port,
        )
        try:
            _wait_gateway_ready(persistent_origin_port, gateway_proc)
            persistent_tunnel_proc = start_named_tunnel_process(
                binary, tunnel_state.tunnel_id, persistent_origin_port, tunnel_state.credentials_path
            )
            click.secho(
                f"  ✓ {resolved_hostname} → shared tunnel → thin-client gateway :{persistent_origin_port} → backend :{persistent_backend_port}",
                fg="green",
            )
            try:
                returncode = persistent_tunnel_proc.wait()
            except KeyboardInterrupt:
                persistent_tunnel_proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    persistent_tunnel_proc.wait(timeout=5)
                return
            if returncode:
                raise click.ClickException(f"cloudflared tunnel exited {returncode}")
            return
        finally:
            if gateway_proc.poll() is None:
                gateway_proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    gateway_proc.wait(timeout=5)

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

    code: str | None = None
    state_path: Path | None = None
    if not no_auth:
        # One-off/quick-tunnel mode keeps its historical shared OAuth store.
        # Persistent hostnames returned above and use hostname-scoped state on
        # the central server.
        migrate_legacy_state()
        state_path = default_state_path()
        pairing_path = default_pairing_path()
        if reset:
            removed = reset_state(state_path)
            reset_pairing_code(pairing_path)
            click.echo(f"  Reset OAuth state ({'removed ' + str(state_path) if removed else 'nothing to remove'}).")
        if pairing_code is not None:
            code = pairing_code
        else:
            code = load_or_create_pairing_code(pairing_path, rotate=new_pairing_code)
            if new_pairing_code:
                click.secho("  Rotated the stored pairing code.", fg="yellow")

    thin_runtime = _thin_http_runtime()
    if no_auth:
        from lemoncrow.gateway.adapters.mcp_http import create_mcp_http_app

        app = create_mcp_http_app(
            dispatch=thin_runtime.dispatch,
            tools_provider=thin_runtime.tools,
        )
    else:
        assert state_path is not None and code is not None
        app = create_protected_mcp_app(
            pairing_code=code,
            state_path=state_path,
            dispatch=thin_runtime.dispatch,
            tools_provider=thin_runtime.tools,
        )
    app.router.add_event_handler("shutdown", thin_runtime.close)
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
    if tunnel:
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
        migrate_legacy_state,
    )
    from lemoncrow.gateway.cli.commands._persistent_tunnel import hostname_slug

    uris = list(redirect_uris) if redirect_uris else list(_CONNECTOR_REDIRECT_URIS)
    for uri in uris:
        if not _is_allowed_redirect_uri(uri):
            raise click.UsageError(f"redirect_uri must be https (or http loopback): {uri}")
    scope = hostname_slug(hostname) if hostname is not None else None
    migrate_legacy_state(scope)
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
    """Manage persistent MCP connectors and their one shared tunnel."""


def _connector_or_fail(hostname: str | None) -> ConnectorBinding:
    from lemoncrow.gateway.mcp_connectors import load_all_connectors, load_connector_for_hostname

    if hostname:
        binding = load_connector_for_hostname(hostname)
        if binding is None:
            raise click.ClickException(f"no persistent MCP connector for {hostname!r}")
        return binding
    connectors = load_all_connectors()
    if not connectors:
        raise click.ClickException("no persistent MCP connectors configured")
    if len(connectors) > 1:
        raise click.ClickException(
            "several connector hostnames are configured — name one: "
            + ", ".join(binding.hostname for binding in connectors)
        )
    return connectors[0]


def echo_persistent_servers(*, empty_hint: bool = True) -> int:
    """Render persistent connector bindings plus the one shared tunnel state."""
    from lemoncrow.gateway.cli.commands._mcp_service import shared_tunnel_service_state
    from lemoncrow.gateway.mcp_connectors import load_all_connectors

    connectors = load_all_connectors()
    state, enabled, pid = shared_tunnel_service_state()
    click.echo("")
    click.echo(f"  Persistent MCP connectors · {len(connectors)}")
    click.echo("  " + "─" * 70)
    if not connectors:
        if empty_hint:
            click.echo("  None configured. Publish one with: lc mcp serve --persistent --hostname <host>")
            click.echo("")
        return 0
    colour = {"active": "green", "failed": "red"}.get(state, "yellow")
    click.echo(
        "  "
        + click.style(f"shared tunnel  {state}", fg=colour, bold=True)
        + click.style(f"  ({enabled})", dim=True)
        + (click.style(f"  pid={pid}", dim=True) if pid else "")
    )
    home = str(Path.home())
    for binding in connectors:
        workspace = binding.workspace
        if workspace.startswith(home):
            workspace = "~" + workspace[len(home) :]
        click.echo("    " + click.style(f"https://{binding.hostname}/mcp", fg="green"))
        click.secho(f"      {workspace}", dim=True)
    click.echo("")
    return len(connectors)


@mcp_service_group.command("list")
def mcp_service_list() -> None:
    """List persistent MCP connectors and the shared tunnel state."""
    echo_persistent_servers()


@mcp_service_group.command("repair")
def mcp_service_repair() -> None:
    """Restore the shared tunnel service from saved connector state."""
    from lemoncrow.gateway.cli.commands._mcp_service import ServiceError, repair_shared_tunnel_service

    try:
        unit = repair_shared_tunnel_service(narrate=lambda msg: click.secho(f"  {msg}", dim=True))
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    if unit is None:
        click.secho("  No persistent MCP connectors are configured; nothing to repair.", dim=True)
        return
    click.secho(f"  Repaired shared MCP tunnel ({unit}).", fg="green")


def _control_shared(action: str) -> None:
    from lemoncrow.gateway.cli.commands._mcp_service import ServiceError, control_shared_tunnel_service

    try:
        control_shared_tunnel_service(action)
    except ServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"  {action}ed shared MCP tunnel", fg="green")


@mcp_service_group.command("start")
def mcp_service_start() -> None:
    """Start the shared persistent MCP tunnel."""
    _control_shared("start")


@mcp_service_group.command("stop")
def mcp_service_stop() -> None:
    """Stop the shared persistent MCP tunnel (it remains enabled for reboot)."""
    _control_shared("stop")


@mcp_service_group.command("restart")
@click.argument("hostname", required=False)
@click.option(
    "--new-pairing-code",
    is_flag=True,
    default=False,
    help="Rotate one connector's pairing code before restarting the shared tunnel.",
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help="Revoke one connector's OAuth clients/tokens before restarting the shared tunnel.",
)
def mcp_service_restart(hostname: str | None, new_pairing_code: bool, reset: bool) -> None:
    """Restart the shared tunnel; optionally reset one hostname's OAuth state."""
    code: str | None = None
    if new_pairing_code or reset:
        from lemoncrow.gateway.adapters.mcp_oauth import (
            default_pairing_path,
            default_state_path,
            load_or_create_pairing_code,
            migrate_legacy_state,
            reset_pairing_code,
            reset_state,
        )
        from lemoncrow.gateway.cli.commands._persistent_tunnel import hostname_slug

        binding = _connector_or_fail(hostname)
        scope = hostname_slug(binding.hostname)
        migrate_legacy_state(scope)
        if reset:
            removed = reset_state(default_state_path(scope))
            reset_pairing_code(default_pairing_path(scope))
            click.echo(f"  Reset OAuth state for {binding.hostname} ({'removed' if removed else 'nothing to remove'}).")
        code = load_or_create_pairing_code(default_pairing_path(scope), rotate=new_pairing_code)
    _control_shared("restart")
    if code is not None:
        click.echo(click.style("  Pairing code:  ", dim=True) + click.style(code, fg="yellow", bold=True))


@mcp_service_group.command("code")
@click.argument("hostname", required=False)
def mcp_service_code(hostname: str | None) -> None:
    """Print the stored pairing code for one persistent connector."""
    from lemoncrow.gateway.adapters.mcp_oauth import (
        default_pairing_path,
        load_or_create_pairing_code,
        migrate_legacy_state,
    )
    from lemoncrow.gateway.cli.commands._persistent_tunnel import hostname_slug

    binding = _connector_or_fail(hostname)
    scope = hostname_slug(binding.hostname)
    migrate_legacy_state(scope)
    click.echo(load_or_create_pairing_code(default_pairing_path(scope)))


@mcp_service_group.command("logs")
@click.option("--lines", "-n", default=50, show_default=True, help="Recent lines to show.")
@click.option("--follow/--no-follow", "-f", default=True, show_default=True, help="Keep streaming.")
def mcp_service_logs(lines: int, follow: bool) -> None:
    """Tail the shared cloudflared tunnel logs."""
    from lemoncrow.gateway.cli.commands._mcp_service import shared_log_command

    cmd = shared_log_command(lines=lines, follow=follow)
    os.execvp(cmd[0], cmd)


@mcp_service_group.command("remove")
@click.argument("hostname", required=False)
def mcp_service_remove(hostname: str | None) -> None:
    """Remove one connector binding and its OAuth state."""
    from lemoncrow.gateway.adapters.mcp_oauth import (
        default_pairing_path,
        default_state_path,
        reset_pairing_code,
        reset_state,
    )
    from lemoncrow.gateway.cli.commands._mcp_service import remove_shared_tunnel_service
    from lemoncrow.gateway.cli.commands._persistent_tunnel import hostname_slug
    from lemoncrow.gateway.mcp_connectors import load_all_connectors, remove_connector

    binding = _connector_or_fail(hostname)
    scope = hostname_slug(binding.hostname)
    remove_connector(binding.hostname)
    reset_state(default_state_path(scope))
    reset_pairing_code(default_pairing_path(scope))
    click.secho(f"  Removed connector {binding.hostname}.", fg="green")
    if not load_all_connectors():
        if remove_shared_tunnel_service():
            click.secho("  Removed the shared tunnel service because no connectors remain.", fg="green")


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
