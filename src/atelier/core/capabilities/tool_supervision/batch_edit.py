"""Deterministic batch-edit executor (WP-22).

Applies many mechanical edits across many files in one explicit call.
This is an *optional* Atelier augmentation; it does not intercept or replace
the host's native Edit/MultiEdit tools.

Safety protocol enforced here:
- Never delete files.
- Never operate outside the repo root.
- Atomic mode (default): snapshot all affected files before touching them;
  restore all if any single edit fails.
"""

from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

from .fuzzy_match import apply_fuzzy_replace

# --------------------------------------------------------------------------- #
# Public models (plain dicts to avoid Pydantic version coupling in callers)  #
# --------------------------------------------------------------------------- #

EditOp = Literal["replace", "insert_after", "replace_range"]


def _repo_root() -> Path:
    """Return the repo root as the process cwd.

    All paths are resolved relative to this.  A caller may pass absolute paths
    as long as they stay under the root.
    """
    return Path.cwd()


def _resolve_path(path: str, repo_root: Path) -> Path:
    """Resolve *path* to an absolute Path, enforcing it stays inside *repo_root*."""
    p = Path(path)
    if not p.is_absolute():
        p = repo_root / p
    p = p.resolve()
    try:
        p.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escape denied: {path!r} is outside the repo root") from exc
    return p


# --------------------------------------------------------------------------- #
# Individual edit operations                                                  #
# --------------------------------------------------------------------------- #


def _apply_replace(content: str, old_string: str, new_string: str) -> tuple[str, int, int]:
    """Replace the first occurrence of *old_string* with *new_string*.

    Returns ``(new_content, line_start, line_end)`` (1-indexed).
    Raises ``ValueError`` if *old_string* is not found.
    """
    idx = content.find(old_string)
    if idx == -1:
        raise ValueError("old_string not found in file")
    line_start = content[:idx].count("\n") + 1
    line_end = line_start + old_string.count("\n")
    new_content = content[:idx] + new_string + content[idx + len(old_string) :]
    return new_content, line_start, line_end


def _apply_insert_after(content: str, anchor: str, new_string: str) -> tuple[str, int, int]:
    """Insert *new_string* immediately after the first line containing *anchor*.

    Returns ``(new_content, line_start, line_end)`` (1-indexed).
    Raises ``ValueError`` if *anchor* is not found.
    """
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if anchor in line:
            insert_line = i + 1  # 0-indexed, insert after line i
            lines.insert(insert_line, new_string if new_string.endswith("\n") else new_string + "\n")
            return "".join(lines), insert_line + 1, insert_line + 1
    raise ValueError(f"anchor {anchor!r} not found in file")


def _apply_replace_range(content: str, line_start: int, line_end: int, new_string: str) -> tuple[str, int, int]:
    """Replace lines [line_start, line_end] (1-indexed, inclusive) with *new_string*.

    Raises ``ValueError`` if line numbers are out of range.
    """
    lines = content.splitlines(keepends=True)
    if line_start < 1 or line_end > len(lines) or line_start > line_end:
        raise ValueError(
            f"replace_range: line_start={line_start}, line_end={line_end} out of range "
            f"(file has {len(lines)} lines)"
        )
    replacement = new_string if new_string.endswith("\n") else new_string + "\n"
    new_lines = [*lines[: line_start - 1], replacement, *lines[line_end:]]
    return "".join(new_lines), line_start, line_end


# --------------------------------------------------------------------------- #
# Core executor                                                               #
# --------------------------------------------------------------------------- #


def apply_batch_edit(
    edits: list[dict[str, Any]],
    *,
    atomic: bool = True,
    backup_base: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Apply *edits* and return a result envelope.

    Parameters
    ----------
    edits:
        List of edit descriptors.  Each must have at least ``path`` and ``op``.
        Supported ops: ``replace``, ``insert_after``, ``replace_range``.
    atomic:
        If ``True`` (default), any failure causes all already-applied edits to
        be rolled back from the backup.
    backup_base:
        Directory under which per-call backups are written.  Defaults to
        ``.atelier/run/<session_id>/batch_edit_backup/`` relative to *repo_root*.
    repo_root:
        Repository root.  Defaults to the process cwd.

    Returns
    -------
    dict with keys ``applied``, ``failed``, ``rolled_back``.
    """
    if repo_root is None:
        repo_root = _repo_root()

    session_id = str(uuid.uuid4())
    if backup_base is None and session_id:
        from atelier.core.foundation.paths import default_store_root

        backup_base = default_store_root() / "backups" / "batch_edit" / session_id

    # Collect unique paths we will touch.
    paths_to_touch: list[Path] = []
    resolved_edits: list[tuple[Path, dict[str, Any]]] = []
    failed: list[dict[str, Any]] = []
    for edit in edits:
        raw_path = edit.get("path", "")
        try:
            resolved = _resolve_path(str(raw_path), repo_root)
        except Exception as exc:
            failed.append({"path": str(raw_path), "error": str(exc)})
            if atomic:
                return {"applied": [], "failed": failed, "rolled_back": True}
            continue
        paths_to_touch.append(resolved)
        resolved_edits.append((resolved, edit))

    # --- Snapshot phase ---------------------------------------------------- #
    if atomic:
        backup_base.mkdir(parents=True, exist_ok=True)
        backup_map: dict[Path, Path] = {}
        for p in paths_to_touch:
            if p.exists():
                backup_path = backup_base / p.name
                # Avoid collisions when two edits touch files with the same name.
                counter = 0
                while backup_path.exists():
                    counter += 1
                    backup_path = backup_base / f"{p.stem}_{counter}{p.suffix}"
                shutil.copy2(str(p), str(backup_path))
                backup_map[p] = backup_path

    # --- Apply phase ------------------------------------------------------- #
    applied: list[dict[str, Any]] = []
    rolled_back = False

    for resolved_path, edit in resolved_edits:
        op: str = edit.get("op", "")
        try:
            if not resolved_path.exists():
                raise FileNotFoundError(f"file not found: {resolved_path}")

            content = resolved_path.read_text(encoding="utf-8")

            if op == "replace":
                old_string = edit["old_string"]
                new_string = edit["new_string"]
                try:
                    new_content, ls, le = _apply_replace(content, old_string, new_string)
                except ValueError:
                    if not bool(edit.get("fuzzy", False)):
                        raise
                    new_content, ls, le = apply_fuzzy_replace(content, old_string, new_string)
            elif op == "insert_after":
                anchor = edit["anchor"]
                new_string = edit["new_string"]
                new_content, ls, le = _apply_insert_after(content, anchor, new_string)
            elif op == "replace_range":
                ls = int(edit["line_start"])
                le = int(edit["line_end"])
                new_string = edit["new_string"]
                new_content, ls, le = _apply_replace_range(content, ls, le, new_string)
            else:
                raise ValueError(f"unknown op: {op!r}")

            resolved_path.write_text(new_content, encoding="utf-8")
            applied.append(
                {
                    "path": str(edit.get("path", resolved_path)),
                    "hunks": [{"line_start": ls, "line_end": le}],
                }
            )

        except Exception as exc:
            failed.append({"path": str(edit.get("path", resolved_path)), "error": str(exc)})
            if atomic:
                # Roll back all applied edits from backup.
                _rollback(backup_map, backup_base)
                rolled_back = True
                return {
                    "applied": [],
                    "failed": failed,
                    "rolled_back": True,
                }

    # --- Cleanup phase (success) ------------------------------------------- #
    if atomic and not failed:
        with contextlib.suppress(Exception):
            shutil.rmtree(str(backup_base))

    return {
        "applied": applied,
        "failed": failed,
        "rolled_back": rolled_back,
    }


def _rollback(backup_map: dict[Path, Path], backup_base: Path) -> None:
    """Restore every backed-up file from *backup_map* and remove the backup dir."""
    for original, backup in backup_map.items():
        with contextlib.suppress(Exception):
            shutil.copy2(str(backup), str(original))
    with contextlib.suppress(Exception):
        shutil.rmtree(str(backup_base))
