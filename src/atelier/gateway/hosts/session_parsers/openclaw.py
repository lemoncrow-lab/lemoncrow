"""OpenClaw session importer for Atelier."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import record_normalized_session

_OPENCLAW_ROOTS = (
    Path("~/.openclaw/agents").expanduser(),
    Path("~/.clawdbot/agents").expanduser(),
    Path("~/.moltbot/agents").expanduser(),
    Path("~/.moldbot/agents").expanduser(),
)


def find_openclaw_sessions(root: Path | None = None) -> list[Path]:
    candidates = [root] if root is not None else list(_OPENCLAW_ROOTS)
    sessions: list[Path] = []
    for base in candidates:
        if base is None or not base.is_dir():
            continue
        for agent_dir in sorted(path for path in base.iterdir() if path.is_dir()):
            sessions_dir = agent_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            sessions.extend(sorted(sessions_dir.glob("*.jsonl")))
    return sessions


class OpenClawImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        from atelier.gateway.hosts.session_parsers._common import import_paths_with_progress

        return import_paths_with_progress(
            "openclaw",
            list(find_openclaw_sessions(root)),
            lambda p: self.import_session(p, force=force),
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
            source="openclaw",
            session_id=session_id,
            relative_path=session_file.name,
            content_path=f"raw/openclaw/{session_file.name}",
            raw_content=raw_content,
            source_mtime=source_mtime,
            force=force,
        )
