"""Pi and OMP session importers for Atelier."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import record_normalized_session


def _sessions_root(host: str, root: Path | None = None) -> Path:
    if root is not None:
        return root
    return Path(f"~/.{host}/agent/sessions").expanduser()


def _discover_sessions(host: str, root: Path | None = None) -> list[Path]:
    sessions_root = _sessions_root(host, root)
    if not sessions_root.is_dir():
        return []
    paths: list[Path] = []
    for project_dir in sorted(sessions_root.iterdir()):
        if not project_dir.is_dir():
            continue
        for session_file in sorted(project_dir.glob("*.jsonl")):
            try:
                first_line = next(
                    line for line in session_file.read_text(encoding="utf-8").splitlines() if line.strip()
                )
                first = json.loads(first_line)
            except (OSError, StopIteration, json.JSONDecodeError):
                continue
            if first.get("type") == "session":
                paths.append(session_file)
    return paths


class _BasePiImporter:
    def __init__(self, store: ContextStore, *, host: str) -> None:
        self.store = store
        self.host = host

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        from atelier.gateway.hosts.session_parsers._common import import_paths_with_progress

        return import_paths_with_progress(
            self.host, list(_discover_sessions(self.host, root)), lambda p: self.import_session(p, force=force)
        )

    def import_session(self, session_file: Path, *, force: bool = False) -> str | None:
        raw_content = session_file.read_text(encoding="utf-8")
        session_id = session_file.stem
        for line in raw_content.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "session" and entry.get("id"):
                session_id = str(entry["id"])
                break
        source_mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source=self.host,
            session_id=session_id,
            relative_path=session_file.name,
            content_path=f"raw/{self.host}/{session_file.name}",
            raw_content=raw_content,
            source_mtime=source_mtime,
            force=force,
        )


class PiImporter(_BasePiImporter):
    def __init__(self, store: ContextStore) -> None:
        super().__init__(store, host="pi")


class OmpImporter(_BasePiImporter):
    def __init__(self, store: ContextStore) -> None:
        super().__init__(store, host="omp")


def find_pi_sessions(root: Path | None = None) -> list[Path]:
    return _discover_sessions("pi", root)


def find_omp_sessions(root: Path | None = None) -> list[Path]:
    return _discover_sessions("omp", root)
