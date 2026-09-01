"""Edit write-scope resolution (public commodity leaf).

``_claude_additional_dirs`` resolves the directories writes may touch beyond the
workspace root -- Claude Code's ``additionalDirectories`` setting plus the
``LEMONCROW_ADDITIONAL_DIRS`` env var. Shared by the bash and edit tools; a true
leaf (stdlib only) so both import it without a back-dependency on ``mcp_server``.

Extracted verbatim from ``mcp_server.py`` (behaviour-preserving); ``mcp_server``
re-exports these names for backward compatibility.

This module governs a WRITE boundary. Only knobs whose documented purpose is
"widen what edits may touch" may feed it -- notably ``LEMONCROW_ADDITIONAL_DIRS``
(``mcp.additional_edit_dirs``). Retrieval/indexing knobs have their own env vars
and must never be read here.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# Claude Code writes user-scoped overrides (its ``/permissions`` UI, ``--add-dir``)
# to ``settings.local.json`` next to ``settings.json``, at both the home and the
# workspace level. All four are read; a missing file simply contributes nothing.
_SETTINGS_FILENAMES = ("settings.json", "settings.local.json")

_CLAUDE_ADDITIONAL_DIRS_CACHE: dict[tuple[str, str, tuple[int, ...]], list[Path]] = {}

# (path, mtime_ns) pairs already reported as malformed, so a broken settings file
# is named once per revision instead of on every edit call.
_MALFORMED_SETTINGS_WARNED: set[tuple[str, int]] = set()


def _settings_files(workspace_root: Path) -> list[Path]:
    """Settings files consulted for ``additionalDirectories``, home first."""
    claude_dirs = (Path.home() / ".claude", workspace_root / ".claude")
    return [d / name for d in claude_dirs for name in _SETTINGS_FILENAMES]


def _settings_mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


def _split_dir_list(raw: str) -> Iterator[str]:
    """Yield non-empty, whitespace-stripped entries of an ``os.pathsep`` list.

    ``os.pathsep`` is the ONLY separator. A comma is a legal character in a
    directory name, so treating it as a separator both denied the user the
    directory they named and silently granted one they did not: ``/srv/data,backup``
    became ``/srv/data`` (writable, never asked for) plus the fragment ``backup``.
    Commas were only ever accepted because ``retrieval.additional_dirs`` shared
    this env var and was documented with commas; it now owns
    ``LEMONCROW_RETRIEVAL_ADDITIONAL_DIRS``, so the reason is gone.
    """
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            yield part


def _warn_malformed(path: Path, mtime: int, problem: str) -> None:
    """Warn once per (file, revision) that a settings file could not be used."""
    marker = (str(path), mtime)
    if marker in _MALFORMED_SETTINGS_WARNED:
        return
    if len(_MALFORMED_SETTINGS_WARNED) > 64:
        _MALFORMED_SETTINGS_WARNED.clear()
    _MALFORMED_SETTINGS_WARNED.add(marker)
    logger.warning(
        "ignoring %s while resolving edit-gate additionalDirectories: %s; "
        "directories listed there will NOT be writable until it is fixed",
        path,
        problem,
    )


def _is_blanket_grant(resolved: Path) -> bool:
    """True if *resolved* is a filesystem root or the user's whole home directory.

    Neither is an allow-list entry anyone means: they hand the edit gate every
    file the process can reach. A cloned repo can ship a
    ``.claude/settings.local.json`` asking for exactly that, and it is read with
    no prompt, so the grant is refused here rather than honored.
    """
    if resolved.parent == resolved:  # "/" (or a drive root on Windows)
        return True
    try:
        return resolved == Path.home().resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve_grant(raw: str, source: str) -> Path | None:
    """Resolve one raw entry to an absolute directory, or ``None`` if unusable.

    ``~`` is expanded first, then the entry must be absolute: a relative entry is
    NOT resolved against this process's working directory, which is an
    implementation detail the user never chose and which would turn a stray
    fragment into a writable path inside the workspace (or, via ``..``, outside
    it).
    """
    try:
        candidate = Path(raw).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("ignoring edit-gate additionalDirectories entry %r from %s: %s", raw, source, exc)
        return None
    if not candidate.is_absolute():
        logger.warning(
            "ignoring edit-gate additionalDirectories entry %r from %s: not an absolute "
            "path (relative entries are not resolved against the server's working "
            "directory); list the full path instead",
            raw,
            source,
        )
        return None
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        logger.warning("ignoring edit-gate additionalDirectories entry %r from %s: %s", raw, source, exc)
        return None
    if _is_blanket_grant(resolved):
        logger.warning(
            "refusing edit-gate additionalDirectories entry %r from %s: it resolves to %s, "
            "granting writes to a filesystem root or your entire home directory; "
            "list a specific subdirectory instead",
            raw,
            source,
            resolved,
        )
        return None
    return resolved


def _settings_additional_dirs(path: Path, mtime: int) -> list[str]:
    """Raw ``additionalDirectories`` entries declared by one settings file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Absent/unreadable is the common case (most users have no settings.local.json).
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        _warn_malformed(path, mtime, f"invalid JSON ({exc})")
        return []
    if not isinstance(data, dict):
        _warn_malformed(path, mtime, "top level is not a JSON object")
        return []

    raw_dirs: list[str] = []
    permissions = data.get("permissions")
    sources: list[tuple[str, object]] = [("additionalDirectories", data.get("additionalDirectories"))]
    if isinstance(permissions, dict):
        sources.append(("permissions.additionalDirectories", permissions.get("additionalDirectories")))
    for label, value in sources:
        if value is None:
            continue
        if not isinstance(value, list):
            _warn_malformed(path, mtime, f"{label} is not a list")
            continue
        raw_dirs += [entry for entry in value if isinstance(entry, str) and entry.strip()]
    return raw_dirs


def _claude_additional_dirs(workspace_root: Path) -> list[Path]:
    """Extra directories allowed for edits beyond *workspace_root*.

    Merges two sources in order:
    1. ``LEMONCROW_ADDITIONAL_DIRS`` -- ``os.pathsep``-separated env var (highest
       priority). Commas are not separators; see ``_split_dir_list``.
    2. ``additionalDirectories`` array in ``settings.json`` and
       ``settings.local.json`` under both ``~/.claude/`` and
       ``<workspace>/.claude/`` (mirrors what Claude Code's ``--add-dir`` flag and
       its ``/permissions`` UI persist), read from BOTH the legacy top-level key
       and the current ``permissions.additionalDirectories`` nested key --
       Claude Code itself writes the latter into ``settings.local.json``, so
       reading only the former, only from ``settings.json``, left real-world
       settings silently ignored.

    Every entry, from either source, must be absolute once ``~`` is expanded, and
    an entry resolving to a filesystem root or the whole home directory is
    refused (see ``_resolve_grant``); rejects are warned about and skipped
    without discarding the entries that are fine.

    Read-only tools (grep/search/read) already accept any absolute path, so
    this only affects write operations (edit, batch-edit).
    """
    settings_files = _settings_files(workspace_root)
    env_raw = os.environ.get("LEMONCROW_ADDITIONAL_DIRS", "").strip()
    mtimes = tuple(_settings_mtime(p) for p in settings_files)

    # Memoize on the inputs' mtimes: this runs on every edit call, but the env
    # var and the settings files change rarely, so a stat-keyed cache avoids
    # re-reading + JSON-parsing them (and re-resolving entries) per edit. A
    # missing file stats as 0, so creating or deleting one invalidates the key.
    cache_key = (str(workspace_root), env_raw, mtimes)
    cached = _CLAUDE_ADDITIONAL_DIRS_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    # (source, raw entry) pairs so a rejected grant can be warned about by name.
    raw_dirs: list[tuple[str, str]] = [("LEMONCROW_ADDITIONAL_DIRS", r) for r in _split_dir_list(env_raw)]
    for sp, mtime in zip(settings_files, mtimes, strict=True):
        raw_dirs += [(str(sp), r) for r in _settings_additional_dirs(sp, mtime)]

    dirs: list[Path] = []
    for source, raw in raw_dirs:
        resolved = _resolve_grant(raw, source)
        if resolved is not None:
            dirs.append(resolved)

    if len(_CLAUDE_ADDITIONAL_DIRS_CACHE) > 16:
        _CLAUDE_ADDITIONAL_DIRS_CACHE.clear()
    _CLAUDE_ADDITIONAL_DIRS_CACHE[cache_key] = dirs
    return list(dirs)
