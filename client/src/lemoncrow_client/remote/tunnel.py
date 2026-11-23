"""cloudflared for the remote MCP gateway: quick tunnels and the shared named tunnel.

The client never downloads cloudflared: it uses one on ``PATH``, or the copy an
older LemonCrow already placed at ``<store>/chatgpt/bin/cloudflared``.

Quick tunnel: ``cloudflared tunnel --url`` prints a rotating
``https://<words>.trycloudflare.com`` URL on stderr; no account is needed.

Shared named tunnel (``serve --persistent``): one Cloudflare tunnel,
``lemoncrow-mcp``, carries every connector hostname to the loopback gateway.
Its id and credentials path persist in ``<store>/mcp/tunnel.json``; the
Cloudflare-side secret stays in the credentials file cloudflared wrote.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from .state import write_private_json


def device_slug(hostname: str | None = None) -> str:
    """Sanitize a machine hostname for use in tunnel names."""
    if hostname is None:
        # The host name without importing socket, which this package keeps out (test_packaging_audit); "" if unknown.
        hostname = platform.node()
    raw = hostname.strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")


def default_tunnel_name(*, environment: Mapping[str, str] | None = None, hostname: str | None = None) -> str:
    """Device-unique tunnel name, e.g. lemoncrow-mcp-pankaj-rog.

    Cloudflare Tunnels are origin connection pools. When multiple machines share
    the same tunnel ID, Cloudflare load balances across them as identical replicas.
    Deriving the tunnel name from the local machine hostname ensures each device gets
    its own tunnel and never receives traffic meant for a different machine.
    """
    env = os.environ if environment is None else environment
    if configured := env.get("LEMONCROW_MCP_TUNNEL_NAME", "").strip():
        return configured
    slug = device_slug(hostname)
    return f"lemoncrow-mcp-{slug}" if slug else "lemoncrow-mcp"


SHARED_TUNNEL_NAME: Final[str] = "lemoncrow-mcp"
INSTALL_URL: Final[str] = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
QUICK_TUNNEL_TIMEOUT_S: Final[float] = 30.0

# The quick-tunnel host is <words>.trycloudflare.com; the control plane
# (api.trycloudflare.com) also appears in cloudflared's stderr and is not it.
_TUNNEL_URL_RE: Final[re.Pattern[str]] = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CONTROL_PLANE_URL: Final[str] = "https://api.trycloudflare.com"
_CREATED_TUNNEL_RE: Final[re.Pattern[str]] = re.compile(r"Created tunnel \S+ with id (\S+)")
_CREDENTIALS_WRITTEN_RE: Final[re.Pattern[str]] = re.compile(r"Tunnel credentials written to (\S+)\.")
# Cloudflared can exit successfully while silently appending its certificate's
# DNS zone to an unrelated FQDN (e.g. lc.olaryn.com.beseam.com).
_ROUTED_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:Added CNAME\s+|\bINF\s+)([a-z0-9.-]+)\s+" r"(?:which will route|is already configured)", re.IGNORECASE
)


class TunnelSetupError(RuntimeError):
    """A tunnel step failed; the caller reports it and starts nothing."""


def resolve_cloudflared(store_root: Path) -> str | None:
    """An operator-managed cloudflared on PATH, else the legacy managed copy."""

    found = shutil.which("cloudflared")
    if found is not None:
        return found
    managed = store_root / "chatgpt" / "bin" / "cloudflared"
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed)
    return None


def missing_cloudflared_help(port: int) -> str:
    return (
        "cloudflared is required for a public MCP URL and was not found.\n"
        f"  Install it:  {INSTALL_URL}\n"
        "  Or use --no-tunnel and expose the loopback port yourself:\n"
        f"     cloudflared tunnel --url http://localhost:{port}\n"
    )


# -- quick tunnel -------------------------------------------------------------- #


def extract_tunnel_url(line: str) -> str | None:
    match = _TUNNEL_URL_RE.search(line)
    if match is None or match.group(0) == _CONTROL_PLANE_URL:
        return None
    return match.group(0)


def start_quick_tunnel(
    binary: str, port: int, timeout: float = QUICK_TUNNEL_TIMEOUT_S
) -> tuple[subprocess.Popen[str], str | None]:
    """Launch a quick tunnel to ``localhost:port``; the caller owns the process.

    A drain thread reads stderr for the life of the process: first for the URL,
    then discarding, because an undrained pipe eventually blocks cloudflared.
    """

    process: subprocess.Popen[str] = subprocess.Popen(
        [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    found = threading.Event()
    captured: list[str] = []

    def drain() -> None:
        stream = process.stderr
        if stream is None:
            return
        for line in stream:
            if not found.is_set() and (url := extract_tunnel_url(line)) is not None:
                captured.append(url)
                found.set()

    threading.Thread(target=drain, name="cloudflared-stderr", daemon=True).start()
    return process, (captured[0] if found.wait(timeout) else None)


# -- shared named tunnel ----------------------------------------------------------- #


@dataclass(frozen=True)
class TunnelState:
    tunnel_name: str
    tunnel_id: str
    hostname: str
    credentials_path: str


def tunnel_state_path(store_root: Path) -> Path:
    return store_root / "mcp" / "tunnel.json"


def load_tunnel_state(path: Path) -> TunnelState | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TunnelState(
            tunnel_name=str(raw["tunnel_name"]),
            tunnel_id=str(raw["tunnel_id"]),
            hostname=str(raw["hostname"]),
            credentials_path=str(raw["credentials_path"]),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_tunnel_state(path: Path, state: TunnelState) -> None:
    write_private_json(path, asdict(state), prefix=".tunnel_state.")


def default_cert_path() -> Path:
    """What ``cloudflared tunnel login`` writes."""

    return Path.home() / ".cloudflared" / "cert.pem"


def _cloudflared(binary: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([binary, *arguments], capture_output=True, text=True, check=False)


def _ensure_tunnel_credentials(binary: str, tunnel_id: str, credentials: Path) -> None:
    """Recover the existing tunnel secret without replacing the tunnel or DNS."""
    if credentials.exists():
        return
    try:
        credentials.parent.mkdir(parents=True, exist_ok=True)
        # cloudflared may print the secret even with --cred-file. Capture it and
        # never include either output stream in an error or progress message.
        with tempfile.TemporaryDirectory(prefix=".tunnel-credentials-", dir=credentials.parent) as temporary:
            recovered = Path(temporary) / "credentials.json"
            result = _cloudflared(binary, "tunnel", "token", "--cred-file", str(recovered), tunnel_id)
            if result.returncode != 0:
                raise ValueError("credential retrieval failed")
            document = json.loads(recovered.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or document.get("TunnelID") != tunnel_id
                or not all(
                    isinstance(document.get(key), str) and document[key] for key in ("AccountTag", "TunnelSecret")
                )
            ):
                raise ValueError("invalid tunnel credentials")
            recovered.chmod(0o600)
            os.replace(recovered, credentials)
    except (OSError, ValueError):
        raise TunnelSetupError(
            f"Could not restore credentials for tunnel {tunnel_id!r}; "
            "run `cloudflared tunnel login` for its account and retry. "
            "The existing tunnel was preserved."
        ) from None


def find_existing_tunnel(binary: str, name: str) -> tuple[str, str] | None:
    result = _cloudflared(binary, "tunnel", "list", "--name", name, "-o", "json")
    if result.returncode != 0:
        raise TunnelSetupError(f"`cloudflared tunnel list --name {name}` failed: {result.stderr.strip()}")
    try:
        tunnels = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise TunnelSetupError(f"could not parse `cloudflared tunnel list` output: {exc}") from exc
    if not tunnels:
        return None
    tunnel_id = str(tunnels[0].get("ID") or tunnels[0].get("id") or "")
    if not tunnel_id:
        raise TunnelSetupError(f"`cloudflared tunnel list` returned a tunnel with no id: {tunnels[0]!r}")
    credentials = default_cert_path().parent / f"{tunnel_id}.json"
    _ensure_tunnel_credentials(binary, tunnel_id, credentials)
    return tunnel_id, str(credentials)


def create_tunnel(binary: str, name: str) -> tuple[str, str]:
    result = _cloudflared(binary, "tunnel", "create", name)
    if result.returncode != 0:
        raise TunnelSetupError(f"`cloudflared tunnel create {name}` failed: {result.stderr.strip()}")
    combined = f"{result.stdout}\n{result.stderr}"
    created = _CREATED_TUNNEL_RE.search(combined)
    written = _CREDENTIALS_WRITTEN_RE.search(combined)
    if created is None or written is None:
        raise TunnelSetupError(f"unexpected `cloudflared tunnel create` output: {combined.strip()!r}")
    return created.group(1), written.group(1)


def _certificate_dns_zone() -> str | None:
    """Best-effort DNS zone of cloudflared's certificate; keep the token private."""
    try:
        pem = default_cert_path().read_text(encoding="utf-8")
        encoded = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
        credentials = json.loads(base64.b64decode(encoded))
        request = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/zones/{credentials['zoneID']}",
            headers={"Authorization": f"Bearer {credentials['apiToken']}"},
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            zone = json.load(response).get("result", {}).get("name")
        return zone.lower().rstrip(".") if isinstance(zone, str) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def route_dns(binary: str, tunnel_ref: str, hostname: str) -> None:
    """Route the exact FQDN; never allow cloudflared to append another zone."""
    hostname = hostname.lower().rstrip(".")
    if zone := _certificate_dns_zone():
        if hostname != zone and not hostname.endswith(f".{zone}"):
            raise TunnelSetupError(
                f"cloudflared is authenticated to {zone!r}, not the DNS zone for {hostname!r}; "
                "no DNS record was created. Log in with `cloudflared tunnel login` "
                "for the correct DNS zone/account first."
            )
    result = _cloudflared(binary, "tunnel", "route", "dns", "--overwrite-dns", tunnel_ref, hostname)
    if result.returncode != 0:
        raise TunnelSetupError(
            f"`cloudflared tunnel route dns {tunnel_ref} {hostname}` failed: {result.stderr.strip()}"
        )
    output = f"{result.stdout}\n{result.stderr}"
    routed = _ROUTED_HOST_RE.search(output)
    if routed is not None and routed.group(1).lower().rstrip(".") != hostname.lower().rstrip("."):
        actual = routed.group(1)
        raise TunnelSetupError(
            f"Cloudflare routed {actual!r} instead of {hostname!r}. "
            "Your cloudflared origin certificate selected the wrong DNS zone. "
            f"Create a DNS CNAME for {hostname} in its actual Cloudflare zone, "
            f"pointing at {tunnel_ref}.cfargotunnel.com, or log in with "
            "`cloudflared tunnel login` using a certificate authorized for that zone. "
            "A tunnel in a different Cloudflare account may require moving/creating "
            "the tunnel in the hostname's account."
        )


def provision_shared_tunnel(
    *,
    store_root: Path,
    hostname: str,
    binary: str,
    narrate: Callable[[str], None],
    tunnel_name: str | None = None,
    reset: bool = False,
    environment: Mapping[str, str] | None = None,
) -> TunnelState:
    """Ensure the device tunnel exists and route ``hostname`` to it."""

    path = tunnel_state_path(store_root)
    state = None if reset else load_tunnel_state(path)
    if state is None or not Path(state.credentials_path).exists():
        if not default_cert_path().exists():
            narrate("Not logged in to Cloudflare; running `cloudflared tunnel login`...")
            # Interactive on purpose: the operator must see and open the login URL.
            login = subprocess.run([binary, "tunnel", "login"], check=False)
            if login.returncode != 0:
                raise TunnelSetupError(f"`cloudflared tunnel login` exited {login.returncode}")
    if state is None:
        target_name = tunnel_name or default_tunnel_name(environment=environment)
        existing = find_existing_tunnel(binary, target_name)
        if existing is None:
            narrate(f"Creating shared tunnel {target_name!r}...")
            tunnel_id, credentials = create_tunnel(binary, target_name)
        else:
            tunnel_id, credentials = existing
            narrate(f"Reusing shared tunnel {target_name!r} (id={tunnel_id}).")
        state = TunnelState(target_name, tunnel_id, "*", credentials)
        save_tunnel_state(path, state)
    else:
        _ensure_tunnel_credentials(binary, state.tunnel_id, Path(state.credentials_path))
        narrate(f"Using shared tunnel {state.tunnel_name!r} (id={state.tunnel_id}).")
    narrate(f"Routing DNS: {hostname} -> {state.tunnel_name}...")
    route_dns(binary, state.tunnel_id, hostname)
    return state


def named_tunnel_command(*, binary: str, tunnel_ref: str, credentials_path: str, origin_port: int) -> list[str]:
    # A 2s grace period: the default 30s turns every gateway restart into an outage.
    return [
        binary,
        "tunnel",
        "--no-autoupdate",
        "--grace-period",
        "2s",
        "run",
        "--credentials-file",
        credentials_path,
        "--url",
        f"http://127.0.0.1:{origin_port}",
        tunnel_ref,
    ]


def start_named_tunnel(binary: str, state: TunnelState, origin_port: int) -> subprocess.Popen[str]:
    command = named_tunnel_command(
        binary=binary, tunnel_ref=state.tunnel_id, credentials_path=state.credentials_path, origin_port=origin_port
    )
    return subprocess.Popen(command, text=True)


__all__ = [
    "SHARED_TUNNEL_NAME",
    "TunnelSetupError",
    "TunnelState",
    "default_tunnel_name",
    "device_slug",
    "extract_tunnel_url",
    "load_tunnel_state",
    "missing_cloudflared_help",
    "named_tunnel_command",
    "provision_shared_tunnel",
    "resolve_cloudflared",
    "save_tunnel_state",
    "start_named_tunnel",
    "start_quick_tunnel",
    "tunnel_state_path",
]
