"""Pi v3 JSONL session importer with deterministic active-branch selection."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.gateway.hosts.session_parsers._common import record_normalized_session
from lemoncrow.infra.storage.bundle import StoreBundle

logger = logging.getLogger(__name__)

_KNOWN_ENTRY_TYPES = {
    "session",
    "message",
    "model_change",
    "thinking_level_change",
    "compaction",
    "branch_summary",
    "custom",
    "custom_message",
    "label",
    "session_info",
}


def _candidate_roots(store_root: Path | None) -> list[Path]:
    roots: list[Path] = []
    explicit_session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if explicit_session_dir:
        roots.append(Path(explicit_session_dir).expanduser())
    else:
        config_dir = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
        roots.append((Path(config_dir).expanduser() if config_dir else Path.home() / ".pi" / "agent") / "sessions")
    if store_root is not None:
        roots.append(Path(store_root).expanduser() / "hosts" / "pi" / "sessions")
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        normalized = root.resolve(strict=False)
        if normalized not in seen:
            seen.add(normalized)
            result.append(root)
    return result


def find_pi_sessions(root: Path | None = None, *, store_root: Path | None = None) -> Iterator[Path]:
    roots = [Path(root).expanduser()] if root is not None else _candidate_roots(store_root)
    seen: set[Path] = set()
    for candidate in roots:
        if candidate.is_file() and candidate.suffix == ".jsonl":
            paths: Iterator[Path] = iter((candidate,))
        elif candidate.is_dir():
            paths = candidate.rglob("*.jsonl")
        else:
            continue
        for path in paths:
            resolved = path.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path


def _parse_lines(raw_content: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_line in raw_content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def pi_session_id(raw_content: str, fallback: str) -> str:
    for entry in _parse_lines(raw_content):
        if entry.get("type") == "session":
            session_id = str(entry.get("id") or "").strip()
            if session_id:
                return session_id
            break
    return fallback


def select_pi_active_branch(raw_content: str) -> str:
    """Return header + entries on the current leaf's parent chain.

    Pi persists all branches in one JSONL file. The current leaf is the final
    tree entry in append order; walking ``parentId`` back to null reproduces the
    active path deterministically without throwing away the original artifact.
    """
    entries = _parse_lines(raw_content)
    headers = [entry for entry in entries if entry.get("type") == "session"]
    tree_entries = [entry for entry in entries if str(entry.get("id") or "").strip()]
    if not tree_entries:
        return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in headers)

    by_id = {str(entry["id"]): entry for entry in tree_entries}
    current = str(tree_entries[-1]["id"])
    active_ids: set[str] = set()
    while current and current not in active_ids:
        entry = by_id.get(current)
        if entry is None:
            break
        active_ids.add(current)
        parent = entry.get("parentId")
        current = str(parent) if parent is not None else ""

    selected = [*headers, *(entry for entry in tree_entries if str(entry["id"]) in active_ids)]
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in selected)


def _session_name(active_content: str) -> str | None:
    name: str | None = None
    for entry in _parse_lines(active_content):
        if entry.get("type") == "session_info":
            value = str(entry.get("name") or "").strip()
            if value:
                name = value
    return name


def _warn_unknown_entries(path: Path, raw_content: str) -> None:
    unknown = sorted(
        {
            str(entry.get("type"))
            for entry in _parse_lines(raw_content)
            if entry.get("type") and entry.get("type") not in _KNOWN_ENTRY_TYPES
        }
    )
    if unknown:
        logger.warning("pi: preserving unknown session entries in %s: %s", path.name, ", ".join(unknown))


class PiImporter:
    """Import Pi JSONL while retaining the complete raw branching artifact."""

    def __init__(self, store: StoreBundle) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        stamped: list[tuple[float, Path]] = []
        for path in find_pi_sessions(root, store_root=self.store.root):
            try:
                stamped.append((path.stat().st_mtime, path))
            except OSError:
                continue
        paths = [path for _, path in sorted(stamped, key=lambda item: item[0], reverse=True)]
        if limit is not None:
            paths = paths[:limit]
        imported: list[str] = []
        for path in paths:
            try:
                trace_id = self.import_session(path, force=force)
            except Exception:
                logger.exception("skipping Pi session %s", path)
                continue
            if trace_id:
                imported.append(trace_id)
        return imported

    def import_session(self, jsonl_path: Path, *, force: bool = False) -> str | None:
        raw_content = jsonl_path.read_text(encoding="utf-8", errors="replace")
        _warn_unknown_entries(jsonl_path, raw_content)
        session_id = pi_session_id(raw_content, jsonl_path.stem)
        active_content = select_pi_active_branch(raw_content)
        source_mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source="pi",
            session_id=session_id,
            relative_path=jsonl_path.name,
            content_path=f"raw/pi/{jsonl_path.name}",
            raw_content=raw_content,
            trace_content=active_content,
            source_mtime=source_mtime,
            source_path=str(jsonl_path),
            force=force,
            task=_session_name(active_content),
        )


__all__ = ["PiImporter", "find_pi_sessions", "pi_session_id", "select_pi_active_branch"]
