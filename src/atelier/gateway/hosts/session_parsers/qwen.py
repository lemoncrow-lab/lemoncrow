"""Qwen session importer for Atelier."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import (
    build_normalized_jsonl,
    make_assistant_message,
    make_session_line,
    make_tool_call,
    make_user_message,
    record_normalized_session,
)


def _projects_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    return Path.home() / ".qwen" / "projects"


def find_qwen_sessions(root: Path | None = None) -> list[Path]:
    projects_root = _projects_root(root)
    if not projects_root.is_dir():
        return []
    sessions: list[Path] = []
    for project_dir in sorted(path for path in projects_root.iterdir() if path.is_dir()):
        chats_dir = project_dir / "chats"
        if not chats_dir.is_dir():
            continue
        sessions.extend(sorted(chats_dir.glob("*.jsonl")))
    return sessions


class QwenImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        from atelier.gateway.hosts.session_parsers._common import import_paths_with_progress

        return import_paths_with_progress(
            "qwen", list(find_qwen_sessions(root)), lambda p: self.import_session(p, force=force)
        )

    def import_session(self, session_file: Path, *, force: bool = False) -> str | None:
        session_id = session_file.stem
        session_timestamp: str | None = None
        events: list[dict[str, Any]] = []

        for line in session_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = str(entry.get("sessionId") or session_id)
            session_timestamp = str(entry.get("timestamp") or session_timestamp or "") or session_timestamp
            entry_type = str(entry.get("type") or "")
            message = entry.get("message") or {}
            parts = message.get("parts") or []

            if entry_type == "user":
                texts = [
                    str(part.get("text") or "").strip()
                    for part in parts
                    if isinstance(part, dict) and part.get("text") and not part.get("thought")
                ]
                combined = " ".join(text for text in texts if text).strip()
                if combined:
                    events.append(
                        make_user_message(
                            combined[:500],
                            timestamp=str(entry.get("timestamp") or "") or None,
                            message_id=str(entry.get("uuid") or "") or None,
                        )
                    )
                continue

            if entry_type != "assistant":
                continue

            usage = entry.get("usageMetadata") or {}
            input_tokens = int(usage.get("promptTokenCount", 0) or 0)
            output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
            thinking_tokens = int(usage.get("thoughtsTokenCount", 0) or 0)
            cache_read = int(usage.get("cachedContentTokenCount", 0) or 0)
            tool_calls = [
                make_tool_call(
                    str(part.get("functionCall", {}).get("name") or "unknown"),
                    part.get("functionCall", {}).get("args") or {},
                )
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)
            ]
            texts = [
                str(part.get("text") or "").strip()
                for part in parts
                if isinstance(part, dict) and part.get("text") and not part.get("thought")
            ]
            thinking_texts = [
                str(part.get("text") or "").strip()
                for part in parts
                if isinstance(part, dict) and part.get("text") and part.get("thought")
            ]
            events.append(
                make_assistant_message(
                    model=str(entry.get("model") or "qwen-auto"),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read=cache_read,
                    thinking_tokens=thinking_tokens,
                    texts=texts,
                    thinking_texts=thinking_texts,
                    tool_calls=tool_calls,
                    timestamp=str(entry.get("timestamp") or "") or None,
                    message_id=str(entry.get("uuid") or "") or None,
                )
            )

        raw_content = build_normalized_jsonl([make_session_line(session_id, timestamp=session_timestamp), *events])
        source_mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source="qwen",
            session_id=session_id,
            relative_path=session_file.name,
            content_path=f"raw/qwen/{session_file.name}",
            raw_content=raw_content,
            source_mtime=source_mtime,
            force=force,
        )
