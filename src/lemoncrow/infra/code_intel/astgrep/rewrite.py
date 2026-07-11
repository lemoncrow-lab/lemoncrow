"""Rewrite preview and apply helpers for ast-grep results."""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RewriteCandidate:
    """A single file rewrite proposed by ast-grep."""

    file_path: str
    before: str
    after: str


@dataclass(frozen=True)
class RewriteOutcome:
    """Rendered rewrite result for preview or apply flows."""

    diff: str
    files_changed: list[str]


def execute_rewrite(
    repo_root: Path,
    candidates: list[RewriteCandidate],
    *,
    dry_run: bool,
) -> RewriteOutcome:
    """Render diffs and optionally apply rewrites to disk."""

    diff_parts: list[str] = []
    files_changed: list[str] = []
    for candidate in candidates:
        if candidate.before == candidate.after:
            continue
        diff_parts.extend(
            difflib.unified_diff(
                candidate.before.splitlines(keepends=True),
                candidate.after.splitlines(keepends=True),
                fromfile=f"a/{candidate.file_path}",
                tofile=f"b/{candidate.file_path}",
            )
        )
        if not dry_run:
            target = (repo_root / candidate.file_path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(candidate.after)
                # Preserve the target's existing permissions: mkstemp creates the
                # temp file as 0o600 and os.replace swaps in that inode, which would
                # otherwise strip the executable bit and group/other access.
                with suppress(OSError):
                    os.chmod(tmp_name, stat.S_IMODE(target.stat().st_mode))
                os.replace(tmp_name, target)
            except BaseException:
                with suppress(OSError):
                    os.unlink(tmp_name)
                raise
        files_changed.append(candidate.file_path)
    return RewriteOutcome(diff="".join(diff_parts), files_changed=files_changed)


__all__ = ["RewriteCandidate", "RewriteOutcome", "execute_rewrite"]
