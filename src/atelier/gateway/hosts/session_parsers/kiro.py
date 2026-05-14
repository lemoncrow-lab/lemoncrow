"""Kiro session importer for Atelier."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import (
    build_normalized_jsonl,
    char_tokens,
    make_assistant_message,
    make_session_line,
    make_tool_call,
    make_user_message,
    record_normalized_session,
)

logger = logging.getLogger(__name__)

_TOOL_RE = re.compile(r"<tool_use>\s*<name>([^<]+)</name>", re.IGNORECASE)


def _agent_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    return Path.home() / ".config" / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent"


def _workspace_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    return Path.home() / ".config" / "Kiro" / "User" / "workspaceStorage"


def _resolve_project_name(workspace_root: Path, hash_name: str) -> str:
    workspace_json = workspace_root / hash_name / "workspace.json"
    if workspace_json.is_file():
        try:
            data = json.loads(workspace_json.read_text(encoding="utf-8"))
            folder = str(data.get("folder") or "")
            if folder:
                cleaned = folder.replace("file://", "")
                return Path(cleaned).name or hash_name
        except json.JSONDecodeError:
            logger.warning(
                "Suppressed exception at kiro.py:47",
                exc_info=True,
            )
    return hash_name


def find_kiro_sessions(agent_root: Path | None = None, workspace_root: Path | None = None) -> list[tuple[Path, str]]:
    actual_agent_root = _agent_root(agent_root)
    actual_workspace_root = _workspace_root(workspace_root)
    if not actual_agent_root.is_dir():
        return []
    sessions: list[tuple[Path, str]] = []
    for workspace_dir in sorted(path for path in actual_agent_root.iterdir() if path.is_dir()):
        project = _resolve_project_name(actual_workspace_root, workspace_dir.name)
        for chat_file in sorted(workspace_dir.glob("*.chat")):
            sessions.append((chat_file, project))
    return sessions


class KiroImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(
        self,
        agent_root: Path | None = None,
        *,
        workspace_root: Path | None = None,
        force: bool = False,
    ) -> list[str]:
        all_sessions = list(find_kiro_sessions(agent_root, workspace_root))
        total = len(all_sessions)
        print(f"[atelier] kiro: discovering sessions (found {total})")
        imported: list[str] = []
        for i, (chat_file, project) in enumerate(all_sessions):
            if i % 10 == 0 and i > 0:
                print(f"[atelier] kiro: importing {i}/{total}...")
            trace_id = self.import_session(chat_file, project=project, force=force)
            if trace_id:
                imported.append(trace_id)
        return imported

    def import_session(self, chat_file: Path, *, project: str, force: bool = False) -> str | None:
        data = json.loads(chat_file.read_text(encoding="utf-8"))
        metadata = data.get("metadata") or {}
        session_id = str(metadata.get("workflowId") or data.get("executionId") or chat_file.stem)
        model = str(metadata.get("modelId") or "kiro-auto")
        if model == "auto":
            model = "kiro-auto"
        timestamp = datetime.fromtimestamp(chat_file.stat().st_mtime, tz=UTC).isoformat()
        if metadata.get("startTime"):
            with contextlib.suppress(TypeError, ValueError):
                timestamp = datetime.fromtimestamp(int(metadata["startTime"]) / 1000, tz=UTC).isoformat()

        events: list[dict[str, Any]] = [make_session_line(session_id, timestamp=timestamp, title=project)]
        pending_user = ""
        for idx, message in enumerate(data.get("chat") or []):
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role == "human":
                if content.startswith("<identity>"):
                    continue
                pending_user = content.strip()[:500]
                if pending_user:
                    events.append(make_user_message(pending_user, timestamp=timestamp, message_id=f"u-{idx}"))
                continue
            if role != "bot":
                continue
            body = content.strip()
            if not body:
                continue
            tool_calls = [make_tool_call(match.group(1).strip()) for match in _TOOL_RE.finditer(body)]
            events.append(
                make_assistant_message(
                    model=model,
                    input_tokens=char_tokens(pending_user),
                    output_tokens=char_tokens(body),
                    texts=[body],
                    tool_calls=tool_calls,
                    timestamp=timestamp,
                    message_id=f"a-{idx}",
                )
            )
            pending_user = ""

        raw_content = build_normalized_jsonl(events)
        source_mtime = datetime.fromtimestamp(chat_file.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source="kiro",
            session_id=session_id,
            relative_path=chat_file.name,
            content_path=f"raw/kiro/{chat_file.name}",
            raw_content=raw_content,
            source_mtime=source_mtime,
            force=force,
            task=project,
        )
