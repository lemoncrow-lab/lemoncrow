"""Droid session importer for Atelier."""

from __future__ import annotations

import json
import re
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

_SYSTEM_REMINDER_RE = re.compile(r"^<system-reminder>", re.IGNORECASE)


def _factory_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    return Path.home() / ".factory"


def find_droid_sessions(root: Path | None = None) -> list[Path]:
    sessions_root = _factory_root(root) / "sessions"
    if not sessions_root.is_dir():
        return []
    sessions: list[Path] = []
    for subdir in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
        sessions.extend(sorted(subdir.glob("*.jsonl")))
    return sessions


def _strip_model_prefix(raw: str) -> str:
    return (
        raw.replace("custom:", "")
        .replace("[Proxy]", "")
        .replace("[proxy]", "")
        .rstrip("-0123456789")
        .strip("-")
        .strip()
        or "unknown"
    )


class DroidImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        from atelier.gateway.hosts.session_parsers._common import import_paths_with_progress

        return import_paths_with_progress(
            "droid", list(find_droid_sessions(root)), lambda p: self.import_session(p, force=force)
        )

    def import_session(self, session_file: Path, *, force: bool = False) -> str | None:
        raw_source = session_file.read_text(encoding="utf-8")
        settings_path = session_file.with_suffix(".settings.json")
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            settings = {}

        entries: list[dict[str, Any]] = []
        for line in raw_source.splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        session_start = next((entry for entry in entries if entry.get("type") == "session_start"), {})
        session_id = str(session_start.get("id") or session_file.stem)
        title = str(session_start.get("title") or "") or None
        timestamp = str(session_start.get("timestamp") or "") or None
        model = _strip_model_prefix(str(settings.get("model") or "unknown"))

        assistant_payloads: list[tuple[str | None, list[str], list[dict[str, Any]]]] = []
        current_user = ""
        events: list[dict[str, Any]] = [
            make_session_line(
                session_id,
                timestamp=timestamp,
                cwd=str(session_start.get("cwd") or "") or None,
                title=title,
            )
        ]

        for index, entry in enumerate(entries):
            if entry.get("type") != "message":
                continue
            message = entry.get("message") or {}
            role = str(message.get("role") or "")
            content = message.get("content") or []

            if role == "user":
                texts = [
                    str(block.get("text") or "").strip()
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                non_system = [text for text in texts if text and not _SYSTEM_REMINDER_RE.match(text)]
                if non_system:
                    current_user = " ".join(non_system)[:500]
                    events.append(
                        make_user_message(
                            current_user,
                            timestamp=str(entry.get("timestamp") or "") or None,
                            message_id=f"u-{index}",
                        )
                    )
                continue

            if role != "assistant":
                continue

            texts = [
                str(block.get("text") or "").strip()
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
            ]
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "unknown")
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                command = str(arguments.get("command") or "").strip()
                if command:
                    arguments = {**arguments, "command": command.splitlines()[0]}
                tool_calls.append(make_tool_call(name, arguments))
            if texts or tool_calls:
                assistant_payloads.append((str(entry.get("timestamp") or "") or None, texts, tool_calls))

        token_usage = settings.get("tokenUsage") or {}
        call_count = max(1, len(assistant_payloads))
        totals = {
            "input": int(token_usage.get("inputTokens", 0) or 0),
            "output": int(token_usage.get("outputTokens", 0) or 0),
            "cache_write": int(token_usage.get("cacheCreationTokens", 0) or 0),
            "cache_read": int(token_usage.get("cacheReadTokens", 0) or 0),
            "thinking": int(token_usage.get("thinkingTokens", 0) or 0),
        }

        def portion(total: int, idx: int) -> int:
            per = total // call_count
            if idx == call_count - 1:
                return total - per * (call_count - 1)
            return per

        for idx, (call_timestamp, texts, tool_calls) in enumerate(assistant_payloads):
            events.append(
                make_assistant_message(
                    model=model,
                    input_tokens=portion(totals["input"], idx),
                    output_tokens=portion(totals["output"], idx),
                    cache_read=portion(totals["cache_read"], idx),
                    cache_write=portion(totals["cache_write"], idx),
                    thinking_tokens=portion(totals["thinking"], idx),
                    texts=texts,
                    tool_calls=tool_calls,
                    timestamp=call_timestamp,
                    message_id=f"a-{idx}",
                )
            )

        raw_content = build_normalized_jsonl(events)
        source_mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source="droid",
            session_id=session_id,
            relative_path=session_file.name,
            content_path=f"raw/droid/{session_file.name}",
            raw_content=raw_content,
            source_mtime=source_mtime,
            force=force,
            task=title,
        )
