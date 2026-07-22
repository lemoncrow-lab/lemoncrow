"""Named-tunnel (Cloudflare) backend for ``lc chatgpt serve --persistent``.

The default ``chatgpt serve`` tunnel is a cloudflared *quick tunnel*: zero
setup, but the hostname rotates on every restart, so the operator has to
re-add the ChatGPT connector each time. ``--persistent`` trades that
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
- ``cloudflared tunnel route dns TUNNEL HOSTNAME`` — positional tunnel ref +
  hostname; idempotent-safe (tolerated) on an already-routed hostname.
- ``cloudflared tunnel run --credentials-file PATH --url URL TUNNEL`` — ``run``
  supports ``--url`` directly (confirmed on ``tunnel run --help``), so no
  ingress ``config.yml`` is needed for this single-service case.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from lemoncrow.core.foundation.paths import default_store_root

# Fixed name: one persistent tunnel per machine backs every `--persistent`
# invocation, reused across restarts (see `find_existing_tunnel`).
TUNNEL_NAME = "lemoncrow-chatgpt"

_STATE_FILENAME = "state.json"


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


def default_tunnel_state_path() -> Path:
    return default_tunnel_state_dir() / _STATE_FILENAME


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
    """Run ``cloudflared tunnel route dns TUNNEL HOSTNAME``, tolerating an
    already-routed hostname the same idempotent-safe way tunnel reuse is
    tolerated — routing the same hostname to the same tunnel twice is a
    no-op, not a failure."""
    result = subprocess.run(
        [binary, "tunnel", "route", "dns", tunnel_ref, hostname],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    combined = (result.stdout + result.stderr).lower()
    if "already exist" in combined or "already has" in combined or "already configured" in combined:
        return
    raise TunnelSetupError(
        f"`cloudflared tunnel route dns {tunnel_ref} {hostname}` failed "
        f"(exit {result.returncode}): {result.stderr.strip()}"
    )


def start_named_tunnel_process(binary: str, tunnel_ref: str, port: int, credentials_path: str) -> subprocess.Popen[str]:
    """Launch ``cloudflared tunnel run --credentials-file PATH --url URL
    TUNNEL`` for the given already-created/reused tunnel.

    Same lifecycle contract as the quick-tunnel ``_start_tunnel``: the caller
    owns ``proc`` and must terminate/wait/kill it. Diverges from
    ``_start_tunnel`` deliberately — there's no URL to scrape off stderr here
    (the hostname is already known), so stderr is drained and discarded from
    the start rather than watched for a match; sharing one helper for both
    would make each case harder to read for no real benefit.
    """
    proc: subprocess.Popen[str] = subprocess.Popen(
        [
            binary,
            "tunnel",
            "run",
            "--credentials-file",
            credentials_path,
            "--url",
            f"http://localhost:{port}",
            tunnel_ref,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    def _drain_stderr() -> None:
        stderr = proc.stderr
        if stderr is None:  # pragma: no cover — PIPE guarantees a stream
            return
        for _line in stderr:
            pass  # discard forever — an undrained pipe eventually blocks cloudflared

    threading.Thread(target=_drain_stderr, daemon=True, name="cloudflared-named-tunnel-stderr-drain").start()
    return proc


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

    Assumes the caller (``chatgpt.py``) already resolved and validated
    ``hostname`` (first-run-needs---hostname, hostname-mismatch-needs-reset)
    against ``existing_state`` — this function only orchestrates cloudflared.
    ``narrate`` is called with short progress messages so the operator isn't
    staring at a silent hang during the interactive browser login step.
    """
    if existing_state is not None:
        narrate(f"Using persisted tunnel {existing_state.tunnel_name!r} (id={existing_state.tunnel_id}).")
        return start_named_tunnel_process(binary, existing_state.tunnel_id, port, existing_state.credentials_path)

    if not is_logged_in():
        narrate("Not logged in to Cloudflare — launching `cloudflared tunnel login`…")
        run_cloudflared_login(binary)
    else:
        narrate(f"Cloudflare login already present ({default_cert_path()}).")

    narrate(f"Looking for an existing tunnel named {TUNNEL_NAME!r}…")
    existing = find_existing_tunnel(binary, TUNNEL_NAME)
    if existing is not None:
        tunnel_id, credentials_path = existing
        narrate(f"Reusing existing tunnel {TUNNEL_NAME!r} (id={tunnel_id}).")
    else:
        narrate(f"Creating tunnel {TUNNEL_NAME!r}…")
        tunnel_id, credentials_path = create_tunnel(binary, TUNNEL_NAME)
        narrate(f"Created tunnel {TUNNEL_NAME!r} (id={tunnel_id}).")

    narrate(f"Routing DNS: {hostname} → {TUNNEL_NAME}…")
    route_dns(binary, TUNNEL_NAME, hostname)

    save_tunnel_state(
        state_path,
        TunnelState(tunnel_name=TUNNEL_NAME, tunnel_id=tunnel_id, hostname=hostname, credentials_path=credentials_path),
    )
    return start_named_tunnel_process(binary, tunnel_id, port, credentials_path)
