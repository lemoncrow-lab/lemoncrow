"""The ``lc`` home screen's view of the current repository: is its Runtime Lite index built, and how big is it."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from lemoncrow.core.foundation.paths import detect_git_root
from lemoncrow.gateway.cli import style


def repository_lines() -> list[str]:
    """The current repository's Runtime Lite index at a glance: a few ``stat`` calls and one count."""
    repo = detect_git_root(Path.cwd())
    if repo is None:
        return []
    from lemoncrow_client.lite.index import LiteIndex, open_index_reader

    lines = ["", f"  {style.heading('This repository')}  {style.dim(style.home_relative(repo))}"]
    index = LiteIndex(repo).db_path
    try:
        # WAL mode: recent writes land in the -wal file first.
        updated = max(path.stat().st_mtime for path in (index, index.with_name(index.name + "-wal")) if path.exists())
        # A worktree overlay counts the files it inherits from its base too.
        connection = open_index_reader(index, timeout=0.2)
        try:
            (files,) = connection.execute("SELECT COUNT(*) FROM files").fetchone()
        finally:
            connection.close()
    except (OSError, ValueError, sqlite3.Error):
        lines.append(style.row("off", "Code index", "not built yet", width=12, indent=4))
        lines.append(style.next_step("Build it with", "lc rt index", indent=4))
        return lines
    when = style.ago(time.time() - updated)
    lines.append(
        style.row(
            "ok",
            "Code index",
            f"{style.plural(int(files), 'file')} {style.dim('· updated ' + when)}",
            width=12,
            indent=4,
        )
    )
    return lines


__all__ = ["repository_lines"]
