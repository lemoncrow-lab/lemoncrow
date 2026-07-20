"""Hermes Agent session importer for LemonCrow.

Reads Nous Research hermes-agent's SQLite state store (``~/.hermes/state.db``,
WAL mode; schema documented at
https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage)
into redacted RawArtifacts + curated Traces via the shared normalized-session
path. Sessions carry authoritative token/billing totals on the ``sessions``
row (input/output/cache_read/cache_write/reasoning tokens + model); per-row
``messages`` provide prose, tool calls, and reasoning text.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.gateway.hosts.session_parsers._common import (
    build_normalized_jsonl,
    make_assistant_message,
    make_session_line,
    make_tool_call,
    make_user_message,
    record_normalized_session,
)
from lemoncrow.infra.storage.bundle import StoreBundle

logger = logging.getLogger(__name__)

_MAX_USER_TEXT = 500
_MAX_SESSION_AGE_DAYS = 5


def _hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    return Path(home).expanduser() if home else Path.home() / ".hermes"


def find_hermes_db(root: Path | None = None) -> Path | None:
    """Return the hermes state.db path, or None when absent.

    ``root`` overrides the full db path (tests / manual imports).
    """
    db_path = root if root is not None else _hermes_home() / "state.db"
    return db_path if db_path.is_file() else None


def _iso(ts: Any) -> str:
    """Unix-epoch float (hermes timestamps) to ISO-8601, \"\" when invalid."""
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _dt(ts: Any, default: datetime) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return default


def find_hermes_sessions(db_path: Path | None = None) -> list[dict[str, Any]]:
    resolved = db_path or find_hermes_db()
    if resolved is None or not Path(resolved).is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [
                dict(row)
                for row in conn.execute(
                    # last_active mirrors the docs' recent-sessions query: a
                    # live session keeps landing messages under an immutable
                    # started_at, so dedup/recency must key on message time.
                    "SELECT s.*, COALESCE("
                    "  (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id),"
                    "  s.ended_at, s.started_at"
                    ") AS last_active "
                    "FROM sessions s ORDER BY last_active DESC"
                ).fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("hermes: failed to read sessions from %s", resolved)
        return []


def _tool_calls_from_row(raw: Any) -> list[dict[str, Any]]:
    """Normalized toolCall events from a messages.tool_calls JSON string.

    Hermes stores OpenAI-format tool calls:
    ``[{"id": ..., "function": {"name": ..., "arguments": "<json>"}}]``.
    """
    if not raw:
        return []
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    if not isinstance(calls, list):
        return []
    out: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or call.get("name") or "").strip()
        if not name:
            continue
        raw_args = function.get("arguments")
        args: dict[str, Any] = {}
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                pass
        elif isinstance(raw_args, dict):
            args = raw_args
        out.append(make_tool_call(name, args))
    return out


def serialize_hermes_session(session_row: dict[str, Any], db_path: Path) -> str:
    """Serialize one hermes session into normalized JSONL.

    Module-level so recall indexing can reuse it without a StoreBundle.
    Per-message rows carry prose/tool-calls/reasoning but no per-call token
    split, so token accounting is emitted as ONE trailing usage rollup taken
    from the authoritative ``sessions`` row totals.
    """
    session_id = str(session_row.get("id") or "")
    events: list[dict[str, Any]] = [
        make_session_line(
            session_id,
            timestamp=_iso(session_row.get("started_at")) or None,
            title=str(session_row.get("title") or "") or None,
        )
    ]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, role, content, tool_calls, reasoning, reasoning_content, timestamp "
                "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("hermes: failed to read messages from %s", db_path)
        rows = []

    for row in rows:
        role = str(row["role"] or "")
        content = str(row["content"] or "").strip()
        timestamp = _iso(row["timestamp"]) or None
        message_id = f"m-{row['id']}"
        if role == "user":
            if content:
                events.append(make_user_message(content[:_MAX_USER_TEXT], timestamp=timestamp, message_id=message_id))
        elif role == "assistant":
            reasoning = str(row["reasoning"] or row["reasoning_content"] or "").strip()
            tool_calls = _tool_calls_from_row(row["tool_calls"])
            if not (content or reasoning or tool_calls):
                continue
            events.append(
                make_assistant_message(
                    # Per-message token split is not stored; the session-level
                    # rollup below carries the authoritative totals + model.
                    model="",
                    input_tokens=0,
                    output_tokens=0,
                    texts=[content] if content else [],
                    thinking_texts=[reasoning[:_MAX_USER_TEXT]] if reasoning else [],
                    tool_calls=tool_calls,
                    timestamp=timestamp,
                    message_id=message_id,
                )
            )
        # role == "tool" rows are tool RESULTS; the toolCall events above
        # already record the calls, and results add bulk without signal.

    def _tok(key: str) -> int:
        try:
            return int(session_row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    totals = {
        "input": _tok("input_tokens"),
        "output": _tok("output_tokens"),
        "cache_read": _tok("cache_read_tokens"),
        "cache_write": _tok("cache_write_tokens"),
        "reasoning": _tok("reasoning_tokens"),
    }
    if any(totals.values()):
        events.append(
            make_assistant_message(
                model=str(session_row.get("model") or ""),
                input_tokens=totals["input"],
                output_tokens=totals["output"],
                cache_read=totals["cache_read"],
                cache_write=totals["cache_write"],
                thinking_tokens=totals["reasoning"],
                timestamp=_iso(session_row.get("last_active") or session_row.get("ended_at")) or None,
                message_id=f"usage-{session_id}",
            )
        )
    return build_normalized_jsonl(events)


class HermesImporter:
    """Hermes Agent session importer (state.db -> normalized sessions)."""

    def __init__(self, store: StoreBundle) -> None:
        self.store = store

    def import_all(self, db_path: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        resolved = db_path or find_hermes_db()
        if resolved is None or not Path(resolved).is_file():
            return []
        sessions = find_hermes_sessions(resolved)
        if not force:
            cutoff = time.time() - (_MAX_SESSION_AGE_DAYS * 86400)
            before = len(sessions)
            sessions = [s for s in sessions if (s.get("last_active") or s.get("started_at") or 0) > cutoff]
            if before != len(sessions):
                logger.info(
                    "hermes: skipped %d sessions older than %d days", before - len(sessions), _MAX_SESSION_AGE_DAYS
                )
        total = len(sessions)
        if limit is not None:
            sessions = sessions[:limit]
        logger.info(
            "hermes: discovering sessions (found %d, processing top %s)",
            total,
            limit if limit is not None else "all",
        )
        imported: list[str] = []
        for i, session_row in enumerate(sessions):
            if i % 10 == 0 and i > 0:
                logger.info("hermes: importing %d/%d...", i, len(sessions))
            session_id = str(session_row.get("id") or "").strip()
            if not session_id:
                continue
            now = datetime.now(UTC)
            session_mtime = _dt(session_row.get("last_active") or session_row.get("started_at"), now)
            raw_content = serialize_hermes_session(session_row, Path(resolved))
            trace_id = record_normalized_session(
                self.store,
                source="hermes",
                session_id=session_id,
                relative_path=f"state.db:{session_id}",
                content_path=f"raw/hermes/{session_id}.jsonl",
                raw_content=raw_content,
                source_mtime=session_mtime,
                force=force,
                task=str(session_row.get("title") or "") or None,
            )
            if trace_id:
                imported.append(trace_id)
        return imported
