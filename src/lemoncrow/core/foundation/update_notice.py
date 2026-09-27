"""Opt-in daily "a LemonCrow update is available" notice (and optional auto-update).

Off unless the user opted in at install (or via ``lc settings``):
  cli.update_check  (LEMONCROW_UPDATE_CHECK)  check GitHub once a day and tell the user
  cli.auto_update   (LEMONCROW_AUTO_UPDATE)   also apply the update (implies the check)

State lives under the LemonCrow root:
  update_available.json  {"latest", "checked_at"}
  .update_claims/        atomic once-per-day claims for refresh/notices/auto-update
  update_badge           plain text: the pending version, absent when up to date
                         (the statusline scripts read it with ``cat`` -- no Python spawn)

The network call runs only in a detached child (``python -m ...update_notice``), never in
the thin client (which is audited to talk to the configured server only) and never in the
hook/CLI process itself. Stdlib only; every public function is fail-open.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

ENV_CHECK = "LEMONCROW_UPDATE_CHECK"
ENV_AUTO = "LEMONCROW_AUTO_UPDATE"
KEY_CHECK = "cli.update_check"
KEY_AUTO = "cli.auto_update"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_LATEST_URL = "https://api.github.com/repos/lemoncrow-lab/lemoncrow/releases/latest"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root) if root else Path.home() / ".lemoncrow"


def _flag(root: Path, env: str, key: str) -> bool:
    """env var wins, then ``plugin_settings.json``; unset means off (opt-in)."""
    raw = os.environ.get(env, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    with contextlib.suppress(OSError, ValueError):
        value = json.loads((root / "plugin_settings.json").read_text("utf-8")).get(key)
        return value is True or (isinstance(value, str) and value.strip().lower() in _TRUE)
    return False


def notice_enabled(root: Path | None = None) -> bool:
    """Daily check + notice on. Never for ``make dev`` source installs (``.dev_mode``)."""
    root = root or lemoncrow_root()
    if (root / ".dev_mode").exists():
        return False
    return _flag(root, ENV_CHECK, KEY_CHECK) or _flag(root, ENV_AUTO, KEY_AUTO)


def auto_update_enabled(root: Path | None = None) -> bool:
    root = root or lemoncrow_root()
    return notice_enabled(root) and _flag(root, ENV_AUTO, KEY_AUTO)


def installed_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("lemoncrow")
    except PackageNotFoundError:
        return None


def _key(version: str) -> tuple[int, ...] | None:
    parts = [int(m.group()) for chunk in version.split(".") if (m := re.match(r"\d+", chunk))]
    return tuple(parts) if parts else None


def _newer(latest: str | None, installed: str | None) -> bool:
    a, b = _key(latest or ""), _key(installed or "")
    return bool(a and b and a > b)


def _cache_path(root: Path) -> Path:
    return root / "update_available.json"


def read_cache(root: Path | None = None) -> dict[str, Any]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(_cache_path(root or lemoncrow_root()).read_text("utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_cache(root: Path, data: dict[str, Any]) -> None:
    _atomic_write(_cache_path(root), json.dumps(data, indent=2, sort_keys=True))


def pending(root: Path | None = None, installed: str | None = None) -> str | None:
    """The newer version to announce, or None (up to date / unknown / not opted in)."""
    root = root or lemoncrow_root()
    if not notice_enabled(root):
        return None
    latest = read_cache(root).get("latest")
    installed = installed or installed_version()
    return str(latest) if isinstance(latest, str) and _newer(latest, installed) else None


def sync_badge(root: Path | None = None, installed: str | None = None) -> None:
    """Make ``update_badge`` reflect ``pending()``: the version, or no file."""
    root = root or lemoncrow_root()
    badge = root / "update_badge"
    latest = pending(root, installed)
    with contextlib.suppress(OSError):
        if latest:
            _atomic_write(badge, latest)
        else:
            badge.unlink(missing_ok=True)


def clear(root: Path | None = None) -> None:
    """After a successful ``lc update``: drop the badge and the stale announcement."""
    root = root or lemoncrow_root()
    with contextlib.suppress(OSError):
        (root / "update_badge").unlink(missing_ok=True)
        cache = read_cache(root)
        cache.pop("latest", None)
        _write_cache(root, cache)


def _fetch_latest() -> str | None:
    req = urllib.request.Request(
        _LATEST_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "lemoncrow-update-check/1.0"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # nosec - fixed https GitHub API URL
        tag = str(json.load(resp).get("tag_name") or "")
    return tag.lstrip("v") or None


def refresh(
    root: Path | None = None,
    *,
    fetch: Callable[[], str | None] = _fetch_latest,
    now: float | None = None,
) -> str | None:
    """Fetch the latest release and cache it (network). Returns the latest version."""
    root = root or lemoncrow_root()
    if not notice_enabled(root):
        return None
    cache = read_cache(root)
    try:
        latest = fetch()
    except Exception:  # offline / rate limited: try again on the next day's check
        latest = None
    cache["checked_at"] = time.time() if now is None else now
    if latest and _key(latest):
        cache["latest"] = latest
    with contextlib.suppress(OSError):
        _write_cache(root, cache)
    sync_badge(root)
    return latest


def _is_stale(root: Path, now: float) -> bool:
    checked = read_cache(root).get("checked_at")
    return not isinstance(checked, int | float) or now - checked >= CHECK_INTERVAL_SECONDS


def _spawn(args: list[str], popen: Callable[..., Any]) -> None:
    popen(
        [sys.executable, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env={**os.environ, "LEMONCROW_NON_INTERACTIVE": "1"},
    )


def _claim_today(root: Path, slot: str, today: str) -> bool:
    """Atomically claim ``slot`` for ``today`` across concurrent processes."""
    safe_slot = re.sub(r"[^0-9A-Za-z_.-]", "_", slot)[:64] or "notice"
    claims = root / ".update_claims"
    try:
        claims.mkdir(parents=True, exist_ok=True)
        path = claims / f"{safe_slot}-{today}"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        # Bound state to roughly one file per slot: yesterday's claim no longer
        # participates once today's atomic create has succeeded.
        for old in claims.glob(f"{safe_slot}-*"):
            if old != path:
                with contextlib.suppress(OSError):
                    old.unlink()
        return True
    except FileExistsError:
        return False
    except OSError:
        # Fail-open for notification bookkeeping: never break a shell/hook just
        # because its state directory is temporarily unwritable.
        return False


def notice_for(
    surface: str,
    *,
    root: Path | None = None,
    installed: str | None = None,
    now: float | None = None,
    today: str | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> str | None:
    """The one-line message ``surface`` ("hook" | "cli") should show now, else None.

    At most once per calendar day per surface. Kicks a detached refresh when the
    cache is older than a day. With auto-update on, starts a detached ``lc update``
    (at most once a day, so a failing update is not retried in a loop).
    """
    try:
        root = root or lemoncrow_root()
        if not notice_enabled(root):
            # A previous opt-in may have left a badge behind. Opt-out must be
            # reflected immediately on the next CLI/hook invocation.
            sync_badge(root, installed)
            return None
        now = time.time() if now is None else now
        today = today or date.today().isoformat()
        if _is_stale(root, now) and _claim_today(root, "refresh", today):
            _spawn(["-m", "lemoncrow.core.foundation.update_notice"], popen)
        installed = installed or installed_version()
        sync_badge(root, installed)
        latest = pending(root, installed)
        if not latest:
            return None
        if auto_update_enabled(root) and _claim_today(root, "auto_attempt", today):
            _spawn(["-m", "lemoncrow.gateway.cli", "update"], popen)
            return (
                f"LemonCrow {installed} -> {latest}: updating in the background. Restart your session when it finishes."
            )
        if not _claim_today(root, surface, today):
            return None
        return (
            f"LemonCrow {latest} is available (you have {installed}). Run `lc update`. "
            f"Silence: `lc settings set {KEY_CHECK} false`."
        )
    except Exception:
        return None


if __name__ == "__main__":
    refresh()
