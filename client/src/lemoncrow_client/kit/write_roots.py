"""Extra directories the client's ``edit`` and ``bash`` may write to.

The worktree is always writable. Anything else must be granted explicitly, either
by ``LEMONCROW_ADDITIONAL_DIRS`` (``os.pathsep``-separated) or by an
``additionalDirectories`` list in Claude Code's ``settings.json`` /
``settings.local.json`` under ``~/.claude/`` and ``<worktree>/.claude/``. Both the
legacy top-level key and ``permissions.additionalDirectories`` are read, because
Claude Code writes the nested one.

Every entry must be absolute once ``~`` is expanded. A grant that resolves to a
filesystem root or the whole home directory is refused: a cloned repository can
ship a ``.claude/settings.local.json`` that asks for exactly that.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

__all__ = ["additional_write_roots"]

logger = logging.getLogger(__name__)

_SETTINGS_FILENAMES = ("settings.json", "settings.local.json")
_CACHE: dict[tuple[str, str, str, tuple[int, ...]], list[Path]] = {}
_MALFORMED_WARNED: set[tuple[str, int]] = set()


def _home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME", "").strip()
    return Path(configured) if configured else Path.home()


def _settings_files(workspace_root: Path, home: Path) -> list[Path]:
    claude_dirs = (home / ".claude", workspace_root / ".claude")
    return [directory / name for directory in claude_dirs for name in _SETTINGS_FILENAMES]


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _split_dir_list(raw: str) -> Iterator[str]:
    """``os.pathsep`` is the only separator: a comma is legal in a directory name."""
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            yield part


def _warn_malformed(path: Path, mtime: int, problem: str) -> None:
    marker = (str(path), mtime)
    if marker in _MALFORMED_WARNED:
        return
    if len(_MALFORMED_WARNED) > 64:
        _MALFORMED_WARNED.clear()
    _MALFORMED_WARNED.add(marker)
    logger.warning(
        "ignoring %s while resolving additionalDirectories: %s; "
        "directories listed there will NOT be writable until it is fixed",
        path,
        problem,
    )


def _is_blanket_grant(resolved: Path, home: Path) -> bool:
    if resolved.parent == resolved:
        return True
    try:
        return resolved == home.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve_grant(raw: str, source: str, home: Path) -> Path | None:
    spelled = raw.strip()
    if spelled == "~" or spelled.startswith("~/"):
        candidate = home / spelled[2:]
    else:
        candidate = Path(spelled)
    if not candidate.is_absolute():
        logger.warning(
            "ignoring additionalDirectories entry %r from %s: not an absolute path; list the full path instead",
            raw,
            source,
        )
        return None
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        logger.warning("ignoring additionalDirectories entry %r from %s: %s", raw, source, exc)
        return None
    if _is_blanket_grant(resolved, home):
        logger.warning(
            "refusing additionalDirectories entry %r from %s: it resolves to %s, a filesystem root or your "
            "entire home directory; list a specific subdirectory instead",
            raw,
            source,
            resolved,
        )
        return None
    return resolved


def _settings_entries(path: Path, mtime: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        _warn_malformed(path, mtime, f"invalid JSON ({exc})")
        return []
    if not isinstance(data, dict):
        _warn_malformed(path, mtime, "top level is not a JSON object")
        return []

    permissions = data.get("permissions")
    sources: list[tuple[str, object]] = [("additionalDirectories", data.get("additionalDirectories"))]
    if isinstance(permissions, dict):
        sources.append(("permissions.additionalDirectories", permissions.get("additionalDirectories")))
    entries: list[str] = []
    for label, value in sources:
        if value is None:
            continue
        if not isinstance(value, list):
            _warn_malformed(path, mtime, f"{label} is not a list")
            continue
        entries += [entry for entry in value if isinstance(entry, str) and entry.strip()]
    return entries


def additional_write_roots(workspace_root: Path, environment: Mapping[str, str]) -> list[Path]:
    """Granted write roots beyond *workspace_root*, re-read when a settings file changes."""
    home = _home(environment)
    settings_files = _settings_files(workspace_root, home)
    env_raw = environment.get("LEMONCROW_ADDITIONAL_DIRS", "").strip()
    mtimes = tuple(_mtime(path) for path in settings_files)

    cache_key = (str(workspace_root), str(home), env_raw, mtimes)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    raw_entries: list[tuple[str, str]] = [("LEMONCROW_ADDITIONAL_DIRS", raw) for raw in _split_dir_list(env_raw)]
    for path, mtime in zip(settings_files, mtimes, strict=True):
        raw_entries += [(str(path), raw) for raw in _settings_entries(path, mtime)]

    roots = [root for source, raw in raw_entries if (root := _resolve_grant(raw, source, home)) is not None]

    if len(_CACHE) > 16:
        _CACHE.clear()
    _CACHE[cache_key] = roots
    return list(roots)
