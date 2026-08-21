"""Named-tunnel (Cloudflare) backend for ``lc mcp serve --persistent``.

The default ``mcp serve`` tunnel is a cloudflared *quick tunnel*: zero
setup, but the hostname rotates on every restart, so the operator has to
re-point the connector each time. ``--persistent`` trades that
convenience for a stable ``https://<hostname>/mcp`` URL backed by a real
Cloudflare *named* tunnel, which requires a one-time ``cloudflared tunnel
login`` (the operator must already manage a domain in Cloudflare DNS).

Every cloudflared subcommand/flag this module shells out to was verified
against the actually-installed binary's own ``--help`` output (cloudflared
2026.6.0) rather than assumed from memory:

- ``cloudflared tunnel login`` — no args; prints a login URL and blocks until
  the browser flow completes; creates ``~/.cloudflared/`` immediately
  (confirmed empirically: the directory appears before the browser step even
  finishes). The artifact it writes, ``cert.pem``, is what
  ``--origincert``'s own ``--help`` text calls "the certificate generated for
  your origin when you run cloudflared login".
- ``cloudflared tunnel create NAME`` — positional ``NAME``; on success prints
  (verified via the binary's embedded format strings) ``Created tunnel %s
  with id %s`` and ``Tunnel credentials written to %v.`` — both parsed
  directly from stdout rather than re-deriving cloudflared's own credentials
  path convention.
- ``cloudflared tunnel list --name NAME -o json`` — ``list``'s ``SUBCOMMAND
  OPTIONS`` include ``--output/-o {json,yaml}`` and ``--name/-n`` (exact-name
  filter), used here to look up a tunnel that already exists under our name
  rather than depending on parsing a server-generated (and unverifiable
  without a live account) "already exists" error message from ``create``.
- ``cloudflared tunnel route dns [--overwrite-dns] TUNNEL HOSTNAME`` —
  positional tunnel ref + hostname; ``--overwrite-dns/-f`` (verified on
  ``route dns --help``) is passed so re-pointing a hostname that currently
  CNAMEs to a different tunnel succeeds instead of failing on an existing
  record.
- ``cloudflared tunnel --no-autoupdate run --credentials-file PATH --url
  URL TUNNEL`` — ``run`` supports ``--url`` directly (confirmed on ``tunnel
  run --help``), so no ingress ``config.yml`` is needed for this single-service
  case. Automatic updates are disabled because the LemonCrow supervisor owns
  the connector lifecycle and restarts it deliberately.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from lemoncrow.core.foundation.paths import default_store_root

# One tunnel PER HOSTNAME, not per machine. Two `--persistent` servers
# sharing a tunnel id are treated by Cloudflare as replicas of one tunnel:
# edge traffic is load-balanced across them, so project A's requests land on
# project B's local port. Everything per-connector (tunnel name, state file,
# OAuth store) is therefore keyed off the hostname.

# Pre-isolation layout: one shared tunnel of exactly this name, one shared
# state file. Migrated to the per-hostname layout by `migrate_legacy_state`.
LEGACY_TUNNEL_NAME = "lemoncrow-chatgpt"
_LEGACY_STATE_FILENAME = "state.json"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def hostname_slug(hostname: str) -> str:
    """``lc-lc.beseam.com`` → ``lc-lc-beseam-com`` — the per-hostname key for
    state and OAuth filenames. Only hostnames differing purely in punctuation
    (``a_b.com`` vs ``a-b.com``) could collide, which no real pair of
    connector hostnames does."""
    return _SLUG_RE.sub("-", hostname.strip().lower()).strip("-") or "default"


def tunnel_name_for(hostname: str) -> str:
    """The tunnel is named after the subdomain label alone — ``lc-lc`` for
    ``lc-lc.beseam.com`` — so `cloudflared tunnel list` reads like the
    connector list. Labels repeat across zones (``lc-lc.a.com`` vs
    ``lc-lc.b.com``), so the caller must check no other configured hostname
    already claims the name before creating/reusing it — sharing one tunnel
    is exactly the replica bug this isolation exists to prevent."""
    return hostname_slug(hostname.strip().split(".")[0])


class TunnelSetupError(RuntimeError):
    """A --persistent setup step failed in a way that isn't cleanly
    recoverable — the caller should print this message to stderr and exit 1
    rather than half-starting the server."""


@dataclass(frozen=True)
class TunnelState:
    """Everything persisted between ``--persistent`` runs. Never includes
    the tunnel's Cloudflare-side secret — that lives only in the credentials
    JSON file cloudflared itself wrote, whose path we merely record."""

    tunnel_name: str
    tunnel_id: str
    hostname: str
    credentials_path: str

    def to_json(self) -> dict[str, str]:
        return asdict(self)


def default_tunnel_state_dir() -> Path:
    """``<store_root>/chatgpt/tunnel`` — peer of ``chatgpt/oauth.json`` and
    ``chatgpt/sessions/`` under the same LemonCrow store root."""
    return default_store_root() / "chatgpt" / "tunnel"


def tunnel_state_path_for(hostname: str) -> Path:
    """``<store_root>/chatgpt/tunnel/<hostname-slug>.json`` — one file per
    configured connector hostname, so a second project never overwrites the
    first project's tunnel reference."""
    return default_tunnel_state_dir() / f"{hostname_slug(hostname)}.json"


def load_all_tunnel_states() -> list[TunnelState]:
    """Every configured hostname's state, hostname-sorted. Unreadable files
    are skipped (same fail-open posture as `load_tunnel_state`). Used to let
    ``--persistent`` keep working without ``--hostname`` when exactly one
    connector is configured."""
    try:
        paths = sorted(default_tunnel_state_dir().glob("*.json"))
    except OSError:
        return []
    states = [state for path in paths if (state := load_tunnel_state(path)) is not None]
    return sorted(states, key=lambda state: state.hostname)


def migrate_legacy_state() -> None:
    """Move the pre-isolation shared ``tunnel/state.json`` onto its
    per-hostname path, keeping its recorded tunnel name/id so an
    already-configured connector keeps serving from the same tunnel. No-op
    when there's nothing (or nothing readable) to migrate."""
    legacy = default_tunnel_state_dir() / _LEGACY_STATE_FILENAME
    state = load_tunnel_state(legacy)
    if state is None:
        return
    target = tunnel_state_path_for(state.hostname)
    if not target.exists():
        save_tunnel_state(target, state)
    with contextlib.suppress(OSError):
        legacy.unlink()


def load_tunnel_state(path: Path) -> TunnelState | None:
    """Best-effort load: a missing, corrupt, or shape-mismatched file is
    treated as "no state yet" (fresh ``--persistent`` setup) rather than a
    hard error — the same fail-open posture as OAuth state loading."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return TunnelState(
            tunnel_name=str(raw["tunnel_name"]),
            tunnel_id=str(raw["tunnel_id"]),
            hostname=str(raw["hostname"]),
            credentials_path=str(raw["credentials_path"]),
        )
    except KeyError:
        return None


def save_tunnel_state(path: Path, state: TunnelState) -> None:
    """Atomic, 0600 write — same tempfile+rename pattern as the OAuth store
    (``mcp_oauth._OAuthStore._save``): a crash mid-write must never leave a
    truncated state file that breaks the next ``--persistent`` run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tunnel_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.to_json(), handle, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def reset_tunnel_state(path: Path) -> bool:
    """Delete the persisted state file. Deliberately does NOT touch the
    Cloudflare-side tunnel object (``cloudflared tunnel delete`` is the
    operator's to run themselves) — this only forgets our local reference,
    so the next ``--persistent`` run behaves like first-time setup. Returns
    True if a file was removed."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ── cloudflared login ────────────────────────────────────────────────────────
def default_cert_path() -> Path:
    """``~/.cloudflared/cert.pem`` — the artifact ``cloudflared tunnel login``
    writes (see module docstring for how this was verified)."""
    return Path.home() / ".cloudflared" / "cert.pem"


def is_logged_in(cert_path: Path | None = None) -> bool:
    return (cert_path or default_cert_path()).exists()


def run_cloudflared_login(binary: str) -> None:
    """Run ``cloudflared tunnel login`` as an interactive subprocess.

    Deliberately does not capture stdout/stderr: cloudflared prints a login
    URL and blocks until the browser flow completes, and the operator needs
    to see (and act on) that output directly, not have it swallowed.
    """
    result = subprocess.run([binary, "tunnel", "login"])
    if result.returncode != 0:
        raise TunnelSetupError(f"`cloudflared tunnel login` exited {result.returncode}")


# ── tunnel create / reuse ────────────────────────────────────────────────────
_CREATED_TUNNEL_RE = re.compile(r"Created tunnel \S+ with id (\S+)")
_CREDENTIALS_WRITTEN_RE = re.compile(r"Tunnel credentials written to (\S+)\.")


def find_existing_tunnel(binary: str, name: str) -> tuple[str, str] | None:
    """Look up a tunnel by exact name via ``cloudflared tunnel list --name
    NAME -o json`` (``list`` defaults to non-deleted tunnels only, so a
    previously-deleted tunnel of the same name is never matched here).

    Returns ``(tunnel_id, credentials_path)`` if found, else ``None``. Raises
    ``TunnelSetupError`` if the credentials file cloudflared would have
    written for this tunnel isn't present locally (e.g. the tunnel was
    created on a different machine) — that's not cleanly recoverable without
    operator action.
    """
    result = subprocess.run(
        [binary, "tunnel", "list", "--name", name, "-o", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TunnelSetupError(
            f"`cloudflared tunnel list --name {name}` failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    try:
        tunnels = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise TunnelSetupError(f"could not parse `cloudflared tunnel list` output: {exc}") from exc
    if not tunnels:
        return None
    tunnel = tunnels[0]
    # Field casing isn't pinned down without a live account to inspect real
    # output against — accept either, since Go's encoding/json commonly uses
    # exported-field capitalization absent explicit json tags.
    tunnel_id = str(tunnel.get("ID") or tunnel.get("id") or "")
    if not tunnel_id:
        raise TunnelSetupError(f"`cloudflared tunnel list` returned a tunnel with no id: {tunnel!r}")
    credentials_path = str(default_cert_path().parent / f"{tunnel_id}.json")
    if not Path(credentials_path).exists():
        raise TunnelSetupError(
            f"found existing tunnel {name!r} (id={tunnel_id}) but its credentials file "
            f"{credentials_path} is missing locally — run `cloudflared tunnel token "
            f"--cred-file {credentials_path} {tunnel_id}` to refetch it, or `--reset-tunnel` "
            "to configure a different tunnel."
        )
    return tunnel_id, credentials_path


def create_tunnel(binary: str, name: str) -> tuple[str, str]:
    """Run ``cloudflared tunnel create NAME``, parsing its own stdout for the
    tunnel id and credentials path it just wrote. Callers should check
    ``find_existing_tunnel`` first (see ``setup_persistent_tunnel``) so this
    only ever runs for a genuinely new name.
    """
    result = subprocess.run([binary, "tunnel", "create", name], capture_output=True, text=True)
    if result.returncode != 0:
        raise TunnelSetupError(
            f"`cloudflared tunnel create {name}` failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    combined = result.stdout + "\n" + result.stderr
    id_match = _CREATED_TUNNEL_RE.search(combined)
    cred_match = _CREDENTIALS_WRITTEN_RE.search(combined)
    if id_match is None or cred_match is None:
        raise TunnelSetupError(
            f"`cloudflared tunnel create {name}` exited 0 but its output didn't match the "
            f"expected format; got: {combined.strip()!r}"
        )
    return id_match.group(1), cred_match.group(1)


def route_dns(binary: str, tunnel_ref: str, hostname: str) -> None:
    """Run ``cloudflared tunnel route dns --overwrite-dns TUNNEL HOSTNAME``.

    ``--overwrite-dns`` makes this idempotent for the same tunnel AND able to
    re-point a hostname that currently CNAMEs elsewhere (e.g. off the old
    shared tunnel onto this connector's own). Without it an existing record
    is an error, and swallowing that error would leave the operator with a
    "tunnel up" banner for a hostname pointing at someone else's tunnel.
    """
    result = subprocess.run(
        [binary, "tunnel", "route", "dns", "--overwrite-dns", tunnel_ref, hostname],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    raise TunnelSetupError(
        f"`cloudflared tunnel route dns {tunnel_ref} {hostname}` failed "
        f"(exit {result.returncode}): {result.stderr.strip()}"
    )


def start_named_tunnel_process(binary: str, tunnel_ref: str, port: int, credentials_path: str) -> subprocess.Popen[str]:
    """Launch the long-running named-tunnel connector.

    ``cloudflared`` checks for updates every 24 hours by default and may restart
    itself. Disable that updater because LemonCrow's watchdog and the OS service
    supervisor own this child process's lifecycle. Inherit stdout/stderr so
    connector failures reach journald, the launchd log, or the foreground
    terminal instead of disappearing inside a drain thread.
    """
    return subprocess.Popen(
        [
            binary,
            "tunnel",
            "--no-autoupdate",
            "run",
            "--credentials-file",
            credentials_path,
            "--url",
            f"http://localhost:{port}",
            tunnel_ref,
        ],
        text=True,
    )


def provision_persistent_tunnel(
    *,
    hostname: str,
    existing_state: TunnelState | None,
    state_path: Path,
    binary: str,
    narrate: Callable[[str], None],
) -> TunnelState:
    """Resolve (creating if needed) the named tunnel for ``hostname``, without
    starting ``cloudflared tunnel run``.

    Split out of :func:`setup_persistent_tunnel` because the boot-persistent
    service path has to do the *interactive* half (browser login, tunnel
    create, DNS route) in the operator's own terminal and then hand the
    non-interactive half — running the tunnel — to systemd/launchd. Once this
    returns, the persisted state is complete, so every later start is silent.
    """
    if existing_state is not None:
        narrate(f"Using persisted tunnel {existing_state.tunnel_name!r} (id={existing_state.tunnel_id}).")
        return existing_state

    if not is_logged_in():
        narrate("Not logged in to Cloudflare — launching `cloudflared tunnel login`…")
        run_cloudflared_login(binary)
    else:
        narrate(f"Cloudflare login already present ({default_cert_path()}).")

    tunnel_name = tunnel_name_for(hostname)
    narrate(f"Looking for an existing tunnel named {tunnel_name!r}…")
    existing = find_existing_tunnel(binary, tunnel_name)
    if existing is not None:
        tunnel_id, credentials_path = existing
        narrate(f"Reusing existing tunnel {tunnel_name!r} (id={tunnel_id}).")
    else:
        narrate(f"Creating tunnel {tunnel_name!r}…")
        tunnel_id, credentials_path = create_tunnel(binary, tunnel_name)
        narrate(f"Created tunnel {tunnel_name!r} (id={tunnel_id}).")

    narrate(f"Routing DNS: {hostname} → {tunnel_name}…")
    route_dns(binary, tunnel_name, hostname)

    state = TunnelState(
        tunnel_name=tunnel_name, tunnel_id=tunnel_id, hostname=hostname, credentials_path=credentials_path
    )
    save_tunnel_state(state_path, state)
    return state


def setup_persistent_tunnel(
    *,
    port: int,
    hostname: str,
    existing_state: TunnelState | None,
    state_path: Path,
    binary: str,
    narrate: Callable[[str], None],
) -> subprocess.Popen[str]:
    """Full ``--persistent`` setup (or fast-path reuse), then launch
    ``cloudflared tunnel run``.

    Assumes the caller (``mcp_serve.py``) already resolved ``hostname`` and
    loaded ``existing_state`` from that hostname's own state file — this
    function only orchestrates cloudflared. ``narrate`` is called with short
    progress messages so the operator isn't staring at a silent hang during
    the interactive browser login step.
    """
    state = provision_persistent_tunnel(
        hostname=hostname,
        existing_state=existing_state,
        state_path=state_path,
        binary=binary,
        narrate=narrate,
    )
    return start_named_tunnel_process(binary, state.tunnel_id, port, state.credentials_path)
