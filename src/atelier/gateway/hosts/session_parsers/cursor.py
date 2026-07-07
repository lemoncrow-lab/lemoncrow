"""Cursor session importer for Atelier."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
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
    parse_datetime,
    record_normalized_session,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER_MODELS = {"", "auto", "default", "composer-2"}
# Cursor's bubble schema only ever carries type 1 (user) and type 2
# (assistant) in practice. Anything else is treated as neither -- it must
# not be billed as a fabricated assistant turn (see import_all).
_ASSISTANT_BUBBLE_TYPES = {2}


def _db_path(root: Path | None = None) -> Path:
    if root is not None:
        return root

    import os
    import sys

    # Linux
    linux_path = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    # macOS
    macos_path = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    # Windows
    appdata = os.environ.get("APPDATA")
    windows_path = Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb" if appdata else None

    if sys.platform == "darwin" and macos_path.exists():
        return macos_path
    if sys.platform == "win32" and windows_path and windows_path.exists():
        return windows_path
    if linux_path.exists():
        return linux_path

    # Fallback to linux/default if nothing found
    return linux_path


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


def _parse_bubble_id(key: str) -> str:
    parts = key.split(":", 2)
    if len(parts) >= 3 and parts[2].strip():
        return parts[2].strip()
    return key.strip()


def _normalize_model(value: Any) -> str:
    """Return a stable model id for a Cursor bubble.

    Cursor omits modelInfo.modelName on effectively all assistant bubbles
    observed in practice (it's a subscription product; the real per-request
    model/token accounting is never surfaced to the client). Placeholder
    values are namespaced under ``cursor/`` instead of being resolved to a
    real model id: resolving an unknown bubble to (previously) a real id
    like ``claude-sonnet-4-5`` let pricing.py bill it at real Anthropic
    per-token rates for usage Cursor never actually reported.
    """
    model = str(value or "").strip()
    if model in _PLACEHOLDER_MODELS:
        return f"cursor/{model or 'unknown'}"
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


def _project_map(db_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return (composer_id -> project name, composer_id -> workspace folder path)."""
    workspace_dir = _workspace_storage_dir(db_path)
    project_map: dict[str, str] = {}
    workspace_path_map: dict[str, str] = {}
    if not workspace_dir.is_dir():
        return project_map, workspace_path_map
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
        workspace_path = folder.replace("file://", "")
        project = Path(workspace_path).name or "cursor"
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
                project_map[composer_id] = project
                workspace_path_map[composer_id] = workspace_path
    return project_map, workspace_path_map


def find_cursor_db(root: Path | None = None) -> Path | None:
    db_path = _db_path(root)
    return db_path if db_path.is_file() else None


class CursorImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        db_path = find_cursor_db(root)
        if db_path is None:
            return []
        imported: list[str] = []
        project_map, workspace_path_map = _project_map(db_path)
        groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"events": [], "project": "cursor"})
        # Per-composer recency (newest bubble's createdAt), used both to rank
        # sessions for `limit` and as each session's dedup mtime -- the shared
        # db file mtime bumps on every bubble across every session, which
        # made a single new bubble anywhere re-import every session.
        composer_last_created: dict[str, str] = {}
        db_mtime = datetime.fromtimestamp(db_path.stat().st_mtime, tz=UTC)
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
                "FROM cursorDiskKV "
                "WHERE key LIKE 'bubbleId:%' "
                "  AND ROWID IN ("
                "    SELECT MAX(ROWID) FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' GROUP BY key"
                "  ) "
                "ORDER BY ROWID ASC"
            ).fetchall()
        for row in rows:
            row_key = str(row["key"] or "")
            composer_id = _parse_composer_id(row_key)
            if not composer_id:
                continue
            bubble_id = _parse_bubble_id(row_key)
            project = project_map.get(composer_id, "cursor")
            group = groups[composer_id]
            group["project"] = project
            text = _extract_row_text(row["text"], row["rich_text"])
            created_at_str = str(row["created_at"] or "")
            timestamp = created_at_str or db_mtime.isoformat()
            if created_at_str and created_at_str > composer_last_created.get(composer_id, ""):
                composer_last_created[composer_id] = created_at_str
            bubble_type = int(row["bubble_type"] or 0)
            if bubble_type == 1:
                if text:
                    group["events"].append(
                        make_user_message(text[:500], timestamp=timestamp, message_id=f"u-{bubble_id}")
                    )
                continue
            if bubble_type not in _ASSISTANT_BUBBLE_TYPES:
                # Unknown/non-assistant bubble type -- don't fabricate an
                # assistant usage entry (and its dollar cost) for it.
                continue
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            cache_read_tokens = int(row["cache_read_tokens"] or 0)
            cache_write_tokens = int(row["cache_write_tokens"] or 0)
            thinking_tokens = int(row["thinking_tokens"] or 0)
            # Cursor omits tokenCount on effectively all bubbles observed in
            # practice. Previously this fell back to a char/4 estimate of the
            # response text, which -- combined with _normalize_model rewriting
            # the (also-omitted) model to a real Anthropic id -- billed
            # fabricated tokens at real per-token rates. No estimate is
            # invented here; missing usage stays 0.
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
                    message_id=f"a-{bubble_id}",
                )
            )

        # Rank sessions by their newest bubble, newest first, and honor `limit`.
        newest_first = sorted(groups, key=lambda cid: composer_last_created.get(cid, ""), reverse=True)
        selected_ids = newest_first[:limit] if limit is not None else newest_first
        for composer_id in selected_ids:
            group = groups[composer_id]
            events = [make_session_line(composer_id, title=str(group["project"]))]
            events.extend(group["events"])
            raw_content = build_normalized_jsonl(events)
            last_created = composer_last_created.get(composer_id)
            session_mtime = parse_datetime(last_created, default=db_mtime) if last_created else db_mtime
            trace_id = record_normalized_session(
                self.store,
                source="cursor",
                session_id=composer_id,
                relative_path=f"{db_path.name}:{composer_id}",
                content_path=f"raw/cursor/{composer_id}.jsonl",
                raw_content=raw_content,
                source_mtime=session_mtime,
                force=force,
                task=str(group["project"]),
            )
            if trace_id:
                imported.append(trace_id)
                workspace_path = workspace_path_map.get(composer_id)
                if workspace_path:
                    trace = self.store.get_trace(trace_id)
                    if trace is not None and not trace.workspace_path:
                        trace.workspace_path = workspace_path
                        self.store.record_trace(trace, write_json=False)
        return imported
