"""Cursor Agent transcript importer for Atelier."""

from __future__ import annotations

import hashlib
import json
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

_USER_MARKER = re.compile(r"^\s*user:\s*", re.IGNORECASE)
_ASSISTANT_MARKER = re.compile(r"^\s*A:\s*")
_THINKING_MARKER = re.compile(r"^\s*\[Thinking\]\s*", re.IGNORECASE)
_TOOL_CALL_MARKER = re.compile(r"^\s*\[Tool call\]\s*(.+?)\s*$", re.IGNORECASE)
_TOOL_RESULT_MARKER = re.compile(r"^\s*\[Tool result\]\b", re.IGNORECASE)
_USER_QUERY_RE = re.compile(r"<user_query>([\s\S]*?)</user_query>", re.IGNORECASE)


def _cursor_root(root: Path | None = None) -> Path:
    return root or (Path.home() / ".cursor")


def _project_name_from_path(path: Path) -> str:
    try:
        project_id = path.parents[1].name
    except IndexError:
        project_id = path.parent.name
    stripped = project_id.lstrip("-")
    parts = [part for part in stripped.split("-") if part]
    return parts[-1] if parts else project_id


def find_cursor_agent_sessions(root: Path | None = None) -> list[tuple[Path, str]]:
    base = _cursor_root(root)
    projects_dir = base / "projects"
    if not projects_dir.is_dir():
        return []
    sessions: list[tuple[Path, str]] = []
    for transcript_dir in sorted(projects_dir.glob("*/agent-transcripts")):
        for transcript in sorted(transcript_dir.rglob("*")):
            if transcript.is_file() and transcript.suffix in {".txt", ".jsonl"}:
                sessions.append((transcript, _project_name_from_path(transcript)))
    return sessions


def _conversation_id(path: Path) -> str:
    stem = path.stem
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", stem, re.IGNORECASE):
        return stem
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def _extract_user_query(text: str) -> str:
    matches = _USER_QUERY_RE.findall(text)
    if matches:
        return " ".join(match.strip() for match in matches if match.strip())[:500]
    return text.strip()[:500]


def _parse_jsonl_transcript(raw: str) -> list[tuple[str, str, list[str], str]]:
    turns: list[tuple[str, str, list[str], str]] = []
    current_user = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = str(entry.get("role") or "")
        message = entry.get("message") or {}
        content = message.get("content") or []
        if role == "user":
            texts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            combined = " ".join(texts).strip()
            if combined:
                current_user = _extract_user_query(combined)
            continue
        if role != "assistant" or not current_user:
            continue
        texts = [
            str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        ]
        tools = [
            f"cursor:{str(block.get('name') or 'unknown').lower()}"
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        turns.append((current_user, "\n".join(texts).strip(), tools, ""))
        current_user = ""
    return turns


def _parse_text_transcript(raw: str) -> list[tuple[str, str, list[str], str]]:
    turns: list[tuple[str, str, list[str], str]] = []
    pending_users: list[str] = []
    active = "none"
    user_lines: list[str] = []
    assistant_lines: list[str] = []

    def flush_user() -> None:
        if not user_lines:
            return
        query = _extract_user_query("\n".join(user_lines))
        if query:
            pending_users.append(query)
        user_lines.clear()

    def flush_assistant() -> None:
        if not assistant_lines:
            return
        body_lines: list[str] = []
        reasoning_lines: list[str] = []
        tools: list[str] = []
        for line in assistant_lines:
            if _TOOL_RESULT_MARKER.match(line):
                continue
            thinking_match = _THINKING_MARKER.match(line)
            if thinking_match:
                reasoning_lines.append(line[thinking_match.end() :].strip())
                continue
            tool_match = _TOOL_CALL_MARKER.match(line)
            if tool_match:
                tools.append(f"cursor:{tool_match.group(1).strip().lower().replace(' ', '-')}")
                continue
            body_lines.append(line)
        if pending_users:
            turns.append(
                (pending_users.pop(0), "\n".join(body_lines).strip(), tools, "\n".join(reasoning_lines).strip())
            )
        assistant_lines.clear()

    for line in raw.splitlines():
        if _USER_MARKER.match(line):
            if active == "user":
                flush_user()
            elif active == "assistant":
                flush_assistant()
            active = "user"
            user_lines = [_USER_MARKER.sub("", line)]
            continue
        if _ASSISTANT_MARKER.match(line):
            if active == "user":
                flush_user()
            elif active == "assistant":
                flush_assistant()
            active = "assistant"
            assistant_lines = [_ASSISTANT_MARKER.sub("", line)]
            continue
        if active == "user":
            user_lines.append(line)
        elif active == "assistant":
            assistant_lines.append(line)

    if active == "user":
        flush_user()
    elif active == "assistant":
        flush_assistant()
    return turns


class CursorAgentImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        imported: list[str] = []
        for transcript, project in find_cursor_agent_sessions(root):
            trace_id = self.import_session(transcript, project=project, force=force)
            if trace_id:
                imported.append(trace_id)
        return imported

    def import_session(self, transcript: Path, *, project: str, force: bool = False) -> str | None:
        raw_source = transcript.read_text(encoding="utf-8")
        turns = (
            _parse_jsonl_transcript(raw_source) if transcript.suffix == ".jsonl" else _parse_text_transcript(raw_source)
        )
        session_id = _conversation_id(transcript)
        timestamp = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC).isoformat()
        events: list[dict[str, Any]] = [make_session_line(session_id, timestamp=timestamp, title=project)]
        for index, (user_text, assistant_text, tools, reasoning_text) in enumerate(turns):
            if user_text:
                events.append(make_user_message(user_text, timestamp=timestamp, message_id=f"u-{index}"))
            events.append(
                make_assistant_message(
                    model="cursor-agent-auto",
                    input_tokens=char_tokens(user_text),
                    output_tokens=char_tokens(assistant_text),
                    thinking_tokens=char_tokens(reasoning_text),
                    texts=[assistant_text] if assistant_text else [],
                    thinking_texts=[reasoning_text] if reasoning_text else [],
                    tool_calls=[make_tool_call(tool) for tool in tools],
                    timestamp=timestamp,
                    message_id=f"a-{index}",
                )
            )

        raw_content = build_normalized_jsonl(events)
        source_mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source="cursor-agent",
            session_id=session_id,
            relative_path=transcript.name,
            content_path=f"raw/cursor-agent/{transcript.name}",
            raw_content=raw_content,
            source_mtime=source_mtime,
            force=force,
            task=project,
        )
