"""Goose session importer for Atelier."""

from __future__ import annotations

import json
import logging
import sqlite3
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

logger = logging.getLogger(__name__)

_TOOL_NAME_MAP = {
    "developer__shell": "bash",
    "developer__text_editor": "edit",
    "developer__read_file": "read",
    "developer__write_file": "write",
    "developer__list_directory": "ls",
    "developer__search_files": "grep",
    "computercontroller__shell": "bash",
}


def _db_path(root: Path | None = None) -> Path:
    if root is not None:
        return root
    return Path.home() / ".local" / "share" / "goose" / "sessions" / "sessions.db"


def _resolved_db_path(root: Path | None = None) -> Path:
    if root is not None:
        return root
    if "GOOSE_PATH_ROOT" in __import__("os").environ:
        return Path(__import__("os").environ["GOOSE_PATH_ROOT"]) / "data" / "sessions" / "sessions.db"
    if "XDG_DATA_HOME" in __import__("os").environ:
        return Path(__import__("os").environ["XDG_DATA_HOME"]) / "goose" / "sessions" / "sessions.db"
    return _db_path(root)


def find_goose_db(root: Path | None = None) -> Path | None:
    db_path = _resolved_db_path(root)
    return db_path if db_path.is_file() else None


class GooseImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        db_path = find_goose_db(root)
        if db_path is None:
            return []
        imported: list[str] = []
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, working_dir, created_at, updated_at, accumulated_input_tokens, accumulated_output_tokens, model_config_json FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
            rows = [r for r in rows if r["accumulated_input_tokens"] or r["accumulated_output_tokens"]]
            print(f"[atelier] goose: discovering sessions (found {len(rows)})")
            for i, row in enumerate(rows):
                if i % 10 == 0 and i > 0:
                    print(f"[atelier] goose: importing {i}/{len(rows)}...")
                trace_id = self._import_session_row(conn, db_path, row, force=force)
                if trace_id:
                    imported.append(trace_id)
        return imported

    def _import_session_row(
        self, conn: sqlite3.Connection, db_path: Path, row: sqlite3.Row, *, force: bool
    ) -> str | None:
        session_id = str(row["id"])
        model = "unknown"
        raw_config = row["model_config_json"]
        if raw_config:
            try:
                config = json.loads(raw_config)
                model = str(config.get("model_name") or model)
            except json.JSONDecodeError:
                logger.warning(
                    "Suppressed exception at goose.py:87",
                    exc_info=True,
                )

        user_message = ""
        user_row = conn.execute(
            "SELECT content_json FROM messages WHERE session_id = ? AND role = 'user' ORDER BY created_timestamp ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        if user_row and user_row[0]:
            try:
                items = json.loads(user_row[0])
                for item in items if isinstance(items, list) else []:
                    if item.get("type") == "text":
                        user_message = str(item.get("text") or "")[:500]
                        break
            except json.JSONDecodeError:
                logger.warning(
                    "Suppressed exception at goose.py:102",
                    exc_info=True,
                )

        tool_calls: list[dict[str, Any]] = []
        assistant_rows = conn.execute(
            "SELECT content_json FROM messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ).fetchall()
        for assistant_row in assistant_rows:
            if not assistant_row[0]:
                continue
            try:
                items = json.loads(assistant_row[0])
            except json.JSONDecodeError:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if item.get("type") != "toolRequest":
                    continue
                tool_value = (
                    ((item.get("toolCall") or {}).get("value") or {}) if isinstance(item.get("toolCall"), dict) else {}
                )
                raw_name = str(tool_value.get("name") or "unknown")
                name = _TOOL_NAME_MAP.get(raw_name, raw_name.split("__")[-1])
                arguments = tool_value.get("arguments") if isinstance(tool_value.get("arguments"), dict) else {}
                tool_calls.append(make_tool_call(name, arguments))

        title = str(row["name"] or f"goose-{session_id}")
        timestamp = str(row["updated_at"] or row["created_at"] or "") or None
        events: list[dict[str, Any]] = [
            make_session_line(session_id, timestamp=timestamp, cwd=str(row["working_dir"] or "") or None, title=title)
        ]
        if user_message:
            events.append(make_user_message(user_message, timestamp=timestamp, message_id="u-0"))
        events.append(
            make_assistant_message(
                model=model,
                input_tokens=int(row["accumulated_input_tokens"] or 0),
                output_tokens=int(row["accumulated_output_tokens"] or 0),
                tool_calls=tool_calls,
                texts=[title],
                timestamp=timestamp,
                message_id="a-0",
            )
        )

        raw_content = build_normalized_jsonl(events)
        source_mtime = datetime.fromtimestamp(db_path.stat().st_mtime, tz=UTC)
        return record_normalized_session(
            self.store,
            source="goose",
            session_id=session_id,
            relative_path=f"{db_path.name}:{session_id}",
            content_path=f"raw/goose/{session_id}.jsonl",
            raw_content=raw_content,
            source_mtime=source_mtime,
            force=force,
            task=title,
        )
