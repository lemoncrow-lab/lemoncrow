"""Edit write-scope resolution (public commodity leaf).

``_claude_additional_dirs`` resolves the directories writes may touch beyond the
workspace root -- Claude Code's ``additionalDirectories`` setting plus the
``LEMONCROW_ADDITIONAL_DIRS`` env var. Shared by the bash and edit tools; a true
leaf (stdlib only) so both import it without a back-dependency on ``mcp_server``.

Extracted verbatim from ``mcp_server.py`` (behaviour-preserving); ``mcp_server``
re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_CLAUDE_ADDITIONAL_DIRS_CACHE: dict[tuple[str, str, int, int], list[Path]] = {}


def _claude_additional_dirs(workspace_root: Path) -> list[Path]:
    """Extra directories allowed for edits beyond *workspace_root*.

    Merges two sources in order:
    1. ``LEMONCROW_ADDITIONAL_DIRS`` -- colon-separated env var (highest priority).
    2. ``additionalDirectories`` array in ``~/.claude/settings.json`` and
       ``<workspace>/.claude/settings.json`` (mirrors what Claude Code's
       ``--add-dir`` flag persists).

    Read-only tools (grep/search/read) already accept any absolute path, so
    this only affects write operations (edit, batch-edit).
    """
    home_settings = Path.home() / ".claude" / "settings.json"
    ws_settings = workspace_root / ".claude" / "settings.json"
    env_raw = os.environ.get("LEMONCROW_ADDITIONAL_DIRS", "").strip()

    def _settings_mtime(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    # Memoize on the inputs' mtimes: this runs on every edit call, but the env
    # var and the two settings files change rarely, so a stat-keyed cache avoids
    # re-reading + JSON-parsing both files (and re-resolving entries) per edit.
    cache_key = (str(workspace_root), env_raw, _settings_mtime(home_settings), _settings_mtime(ws_settings))
    cached = _CLAUDE_ADDITIONAL_DIRS_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    dirs: list[Path] = []
    for raw in env_raw.split(":"):
        raw = raw.strip()
        if raw:
            try:
                dirs.append(Path(raw).expanduser().resolve())
            except (OSError, ValueError):
                pass

    for sp in (home_settings, ws_settings):
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            for raw in data.get("additionalDirectories", []):
                if isinstance(raw, str) and raw.strip():
                    try:
                        dirs.append(Path(raw).expanduser().resolve())
                    except (OSError, ValueError):
                        pass
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    if len(_CLAUDE_ADDITIONAL_DIRS_CACHE) > 16:
        _CLAUDE_ADDITIONAL_DIRS_CACHE.clear()
    _CLAUDE_ADDITIONAL_DIRS_CACHE[cache_key] = dirs
    return list(dirs)
