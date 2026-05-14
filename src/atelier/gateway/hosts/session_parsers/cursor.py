"""Cursor session importer for Atelier."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
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

_DEFAULT_MODEL = "claude-sonnet-4-5"
_PLACEHOLDER_MODELS = {"", "auto", "default", "composer-2"}


def _db_path(root: Path | None = None) -> Path:
    if root is not None:
        return root
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _workspace_storage_dir(db_path: Path) -> Path:
    return db_path.parent.parent / "workspaceStorage"


def _parse_composer_id(key: str) -> str | None:
    parts = key.split(":", 2)
    if len(parts) < 3:
        return None
    composer_id = parts[1].strip()
    if not composer_id or any(ch in composer_id for ch in "\r\n\x00"):
        return None
    return composer_id


def _normalize_model(value: Any) -> str:
    model = str(value or "").strip()
    if model in _PLACEHOLDER_MODELS:
        return _DEFAULT_MODEL
    return model


def _collect_rich_text(node: Any, parts: list[str]) -> None:
    if isinstance(node, dict):
        text = str(node.get("text") or "").strip()
        if text:
            parts.append(text)
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                _collect_rich_text(child, parts)
        for key, value in node.items():
            if key in {"text", "children"}:
                continue
            if isinstance(value, (dict, list)):
                _collect_rich_text(value, parts)
    elif isinstance(node, list):
        for child in node:
            _collect_rich_text(child, parts)


def _extract_row_text(text: Any, rich_text: Any) -> str:
    plain_text = str(text or "").strip()
    if plain_text:
        return plain_text
    rich_text_value = rich_text
    if isinstance(rich_text_value, str):
        try:
            rich_text_value = json.loads(rich_text_value)
        except json.JSONDecodeError:
            return ""
    parts: list[str] = []
    _collect_rich_text(rich_text_value, parts)
    return "\n".join(part for part in parts if part).strip()


def _project_map(db_path: Path) -> dict[str, str]:
    workspace_dir = _workspace_storage_dir(db_path)
    mapping: dict[str, str] = {}
    if not workspace_dir.is_dir():
        return mapping
    for directory in workspace_dir.iterdir():
        if not directory.is_dir():
            continue
        workspace_json = directory / "workspace.json"
        workspace_db = directory / "state.vscdb"
        if not (workspace_json.is_file() and workspace_db.is_file()):
            continue
        try:
            workspace_data = json.loads(workspace_json.read_text(encoding="utf-8"))
            folder = str(workspace_data.get("folder") or "")
        except json.JSONDecodeError:
            continue
        if not folder:
            continue
        project = Path(folder.replace("file://", "")).name or "cursor"
        try:
            with sqlite3.connect(workspace_db) as conn:
                row = conn.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
        except sqlite3.Error:
            continue
        if not row or not row[0]:
            continue
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            continue
        for composer in payload.get("allComposers") or []:
            composer_id = str(composer.get("composerId") or "").strip()
            if composer_id:
                mapping[composer_id] = project
    return mapping


def find_cursor_db(root: Path | None = None) -> Path | None:
    db_path = _db_path(root)
    return db_path if db_path.is_file() else None


class CursorImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        db_path = find_cursor_db(root)
        if db_path is None:
            return []
        imported: list[str] = []
        project_map = _project_map(db_path)
        groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"events": [], "project": "cursor"})
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Cursor's bubble schema only ships tokenCount.{inputTokens,outputTokens}
            # today; cache fields are pulled defensively so that if Cursor ever
            # starts populating them we don't silently keep reporting $0 on cache.
            rows = conn.execute(
                "SELECT key, "
                "json_extract(value, '$.tokenCount.inputTokens') AS input_tokens, "
                "json_extract(value, '$.tokenCount.outputTokens') AS output_tokens, "
                "COALESCE("
                "  json_extract(value, '$.tokenCount.cacheReadTokens'),"
                "  json_extract(value, '$.tokenCount.cachedInputTokens'),"
                "  json_extract(value, '$.tokenCount.cache_read_input_tokens')"
                ") AS cache_read_tokens, "
                "COALESCE("
                "  json_extract(value, '$.tokenCount.cacheWriteTokens'),"
                "  json_extract(value, '$.tokenCount.cacheCreationInputTokens'),"
                "  json_extract(value, '$.tokenCount.cache_creation_input_tokens')"
                ") AS cache_write_tokens, "
                "COALESCE("
                "  json_extract(value, '$.tokenCount.reasoningTokens'),"
                "  json_extract(value, '$.tokenCount.thinkingTokens')"
                ") AS thinking_tokens, "
                "json_extract(value, '$.modelInfo.modelName') AS model, "
                "json_extract(value, '$.createdAt') AS created_at, "
                "json_extract(value, '$.type') AS bubble_type, "
                "json_extract(value, '$.text') AS text, "
                "json_extract(value, '$.richText') AS rich_text, "
                "json_extract(value, '$.codeBlocks') AS code_blocks "
                "FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' ORDER BY ROWID ASC"
            ).fetchall()
        for row in rows:
            composer_id = _parse_composer_id(str(row["key"] or ""))
            if not composer_id:
                continue
            project = project_map.get(composer_id, "cursor")
            group = groups[composer_id]
            group["project"] = project
            text = _extract_row_text(row["text"], row["rich_text"])
            timestamp = str(row["created_at"] or datetime.fromtimestamp(db_path.stat().st_mtime, tz=UTC).isoformat())
            bubble_type = int(row["bubble_type"] or 0)
            if bubble_type == 1:
                if text:
                    group["events"].append(make_user_message(text[:500], timestamp=timestamp))
                continue
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            cache_read_tokens = int(row["cache_read_tokens"] or 0)
            cache_write_tokens = int(row["cache_write_tokens"] or 0)
            thinking_tokens = int(row["thinking_tokens"] or 0)
            if input_tokens == 0 and output_tokens == 0 and text:
                output_tokens = char_tokens(text)
            tool_calls: list[dict[str, Any]] = []
            try:
                code_blocks = json.loads(str(row["code_blocks"] or "[]"))
            except json.JSONDecodeError:
                code_blocks = []
            if isinstance(code_blocks, list):
                languages = {
                    str(block.get("languageId") or "")
                    for block in code_blocks
                    if isinstance(block, dict) and block.get("languageId") and block.get("languageId") != "plaintext"
                }
                for language in sorted(languages):
                    tool_calls.append(make_tool_call("cursor:edit", {"language": language}))
            group["events"].append(
                make_assistant_message(
                    model=_normalize_model(row["model"]),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read=cache_read_tokens,
                    cache_write=cache_write_tokens,
                    thinking_tokens=thinking_tokens,
                    texts=[text] if text else [],
                    tool_calls=tool_calls,
                    timestamp=timestamp,
                )
            )

        for composer_id, group in groups.items():
            events = [make_session_line(composer_id, title=str(group["project"]))]
            events.extend(group["events"])
            raw_content = build_normalized_jsonl(events)
            trace_id = record_normalized_session(
                self.store,
                source="cursor",
                session_id=composer_id,
                relative_path=f"{db_path.name}:{composer_id}",
                content_path=f"raw/cursor/{composer_id}.jsonl",
                raw_content=raw_content,
                source_mtime=datetime.fromtimestamp(db_path.stat().st_mtime, tz=UTC),
                force=force,
                task=str(group["project"]),
            )
            if trace_id:
                imported.append(trace_id)
        return imported
