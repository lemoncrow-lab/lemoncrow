"""Persistence for the OAuth session (auth token, cached ``/api/auth/me`` user, base URL)."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from lemoncrow.core.foundation.paths import default_store_root

# ── OAuth auth token ("~/.lemoncrow/auth_token") ───────────────────────────────────

_AUTH_TOKEN_FILENAME = "auth_token"
AUTH_TOKEN_ENV_VAR = "LEMONCROW_AUTH_TOKEN"


def auth_token_path() -> Path:
    return default_store_root() / _AUTH_TOKEN_FILENAME


def load_auth_token() -> str | None:
    """Return the OAuth session token: env var wins, then the file."""
    env = os.environ.get(AUTH_TOKEN_ENV_VAR, "").strip()
    if env:
        return env
    path = auth_token_path()
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def save_auth_token(token: str) -> Path:
    """Persist OAuth session token with owner-only permissions."""
    path = auth_token_path()
    parent = path.parent
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token.strip() + "\n")
    finally:
        os.chmod(path, 0o600)
    clear_login_declined()
    return path


# ── Explicit opt-out (`lc init --no-login`) ────────────────────────────────────────

_LOGIN_DECLINED_FILENAME = "login_declined"


def login_declined_path() -> Path:
    return default_store_root() / _LOGIN_DECLINED_FILENAME


def mark_login_declined() -> None:
    """Record an explicit `lc init --no-login`.

    Suppresses the MCP server's seamless background browser login
    (``_try_seamless_login``) until the user activates explicitly via
    ``lc account login`` or ``lc init`` without ``--no-login`` -- cleared automatically
    by ``save_auth_token`` the moment either succeeds.
    """
    path = login_declined_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def is_login_declined() -> bool:
    return login_declined_path().exists()


def clear_login_declined() -> None:
    path = login_declined_path()
    if path.exists():
        path.unlink()


def delete_auth_token() -> bool:
    """Remove the OAuth session token file. Returns True if a file was deleted."""
    path = auth_token_path()
    if path.exists():
        path.unlink()
        return True
    return False


# ── Auth user cache ("~/.lemoncrow/auth_user.json") ────────────────────────────
# Persists the full /api/auth/me response so it survives process restarts.
# Refreshed every 6 h; each refresh also renews the server-side CLI token
# (rolling 24 h window on the server). Stale cache + unreachable server =>
# locked (fail-closed), retried hourly.

_AUTH_USER_FILENAME = "auth_user.json"
AUTH_USER_CACHE_TTL = 6 * 60 * 60  # seconds


def auth_user_path() -> Path:
    return default_store_root() / _AUTH_USER_FILENAME


def load_auth_user() -> dict[str, object] | None:
    """Return cached auth user data if fresh (< AUTH_USER_CACHE_TTL), else None."""
    import json
    import time

    path = auth_user_path()
    if not path.exists():
        return None
    try:
        data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        cached_at_raw = data.get("_cached_at", 0)
        cached_at = float(cached_at_raw) if isinstance(cached_at_raw, int | float) else 0.0
        if time.time() - cached_at > AUTH_USER_CACHE_TTL:
            return None  # stale
        return data
    except Exception:  # noqa: BLE001
        return None


def save_auth_user(data: dict[str, object]) -> None:
    """Persist full auth user response with a timestamp."""
    import json
    import time

    path = auth_user_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = {**data, "_cached_at": time.time()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:  # noqa: BLE001
        pass


def delete_auth_user() -> None:
    """Remove the cached auth user file."""
    path = auth_user_path()
    if path.exists():
        path.unlink()


# ── Auth base URL ("~/.lemoncrow/auth_base") ──────────────────────────────────

LEMONCROW_DEFAULT_BASE = "https://lemoncrow.com"


def auth_base_path() -> Path:
    return default_store_root() / "auth_base"


def load_auth_base() -> str:
    """Return the base URL for the auth server (default: production)."""
    path = auth_base_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip() or LEMONCROW_DEFAULT_BASE
    return LEMONCROW_DEFAULT_BASE


def save_auth_base(base: str) -> None:
    path = auth_base_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base.strip(), encoding="utf-8")


def delete_auth_base() -> None:
    path = auth_base_path()
    if path.exists():
        path.unlink()


# ── Stable device ID ("~/.lemoncrow/device_id") ─────────────────────────────
# Generated once, persists forever — survives logout and re-login.
# Used as a stable identifier for this machine across CLI sessions.


DEVICE_ID_ENV_VAR = "LEMONCROW_DEVICE_ID"
_DEVICE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{4,64}$")


def device_id_path() -> Path:
    return default_store_root() / "device_id"


def _generate_local_device_id() -> str:
    """A cryptographically-secure random 12-hex id — never machine-derived."""
    return uuid.uuid4().hex[:12]


def stable_machine_device_id() -> str:
    """A random, locally-generated installation id (NOT derived from hardware).

    Open-source runtime: this id is used only by the OPTIONAL hosted-account
    link (``lc account login``). It gates nothing, enforces no cap, and is never
    required. It is generated locally with a cryptographically secure random
    UUID — never from ``/etc/machine-id``, hostname, MAC address, disk serial,
    OS id, or any other machine property — and cached at
    ``~/.lemoncrow/device_id`` (0600). Reset it by deleting that file. The name
    is kept for backward-compatible imports; "machine" no longer implies
    hardware derivation. See docs/maintenance-mode-transition.md.
    """
    path = device_id_path()
    if path.exists():
        val = path.read_text(encoding="utf-8").strip()
        if val:
            return val
    device_id = _generate_local_device_id()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(device_id)
    except Exception:  # noqa: BLE001
        pass
    return device_id


def load_or_create_device_id() -> str:
    """Return the local installation id used by the optional hosted-account link.

    ``LEMONCROW_DEVICE_ID`` (validated) overrides it when set — useful for
    containers/CI that want a stable id across ephemeral filesystems. Otherwise a
    random, local id is used. This value gates nothing, enforces no cap, and is
    never derived from machine properties, so there is no longer any reason to
    tie the override to an auth token.
    """
    env_id = os.environ.get(DEVICE_ID_ENV_VAR, "").strip()
    if env_id and _DEVICE_ID_RE.fullmatch(env_id):
        return env_id
    return stable_machine_device_id()
