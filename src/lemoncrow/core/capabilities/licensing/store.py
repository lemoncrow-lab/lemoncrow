"""Persistence for the OAuth session (auth token, cached ``/api/auth/me`` user, base URL)."""

from __future__ import annotations

import hashlib
import os
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
    return path


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


def device_id_path() -> Path:
    return default_store_root() / "device_id"


def _read_os_machine_id() -> str | None:
    """Read the OS-provided stable machine identifier."""
    import subprocess
    import sys

    if sys.platform == "linux":
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                val = Path(path).read_text(encoding="utf-8").strip()
                if val:
                    return val
            except OSError:
                pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).decode()
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    parts = line.split('"')
                    if len(parts) >= 4:
                        return parts[-2]
        except Exception:  # noqa: BLE001
            pass
    elif sys.platform == "win32":
        try:
            import winreg  # type: ignore[import]

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
            val, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(val)
        except Exception:  # noqa: BLE001
            pass
    return None


def load_or_create_device_id() -> str:
    """Return a stable device ID for this machine.

    Derived from the OS machine ID (never changes across reboots or re-logins).
    Falls back to a locally-generated UUID cached in ~/.lemoncrow/device_id.
    """
    os_id = _read_os_machine_id()
    if os_id:
        return hashlib.sha256(os_id.encode()).hexdigest()[:12]

    # Fallback: persistent local UUID
    path = device_id_path()
    if path.exists():
        val = path.read_text(encoding="utf-8").strip()
        if val:
            return val
    device_id = uuid.uuid4().hex[:12]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(device_id)
    except Exception:  # noqa: BLE001
        pass
    return device_id
