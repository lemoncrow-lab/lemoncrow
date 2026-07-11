"""Host-aware (context tokens, model) probe for live sessions.

Each probe tail-reads its host's session file and returns the most recent
turn's live context size plus the model that produced it - per-turn ground
truth feeding savings pricing, avoided-call credit, and context nudges. Unknown
hosts and missing sessions return ``(0, "")``; callers must treat that as
unknown and skip pricing rather than synthesize a value.

Extension point: implement a ``(session_id) -> tuple[int, str]`` probe that
reuses the ``session_parsers`` usage extractors for the host's line format,
then register it in ``_PROBES``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TAIL_BYTES = 65536
_USAGE_KEYS = ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens")


def host_context_state(host: str, session_id: str) -> tuple[int, str]:
    """Return measured (context tokens, model) for *host*'s live session."""
    probe = _PROBES.get((host or "").strip().lower())
    if probe is None or not session_id:
        return 0, ""
    try:
        return probe(session_id)
    except Exception:  # noqa: BLE001 - probes are best-effort
        logger.debug("context probe failed for host=%s", host, exc_info=True)
        return 0, ""


def _claude_probe(session_id: str) -> tuple[int, str]:
    from lemoncrow.core.capabilities.savings_summary import transcript_context_state

    return transcript_context_state(session_id)


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - _TAIL_BYTES))
        return fh.read().decode("utf-8", errors="replace").splitlines()


def _find_usage(obj: Any, depth: int = 0) -> dict[str, Any] | None:
    """Locate the first dict carrying usage-token keys, in any wrapper format."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    if any(key in obj for key in _USAGE_KEYS):
        return obj
    for value in obj.values():
        found = _find_usage(value, depth + 1)
        if found is not None:
            return found
    return None


def _opencode_probe(session_id: str, db_path: Path | None = None) -> tuple[int, str]:
    path = db_path or (Path.home() / ".local/share/opencode/opencode.db")
    if not path.exists():
        return 0, ""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """
                SELECT p.data, m.data
                FROM part p
                JOIN message m ON p.message_id = m.id
                WHERE p.session_id = ?
                  AND json_extract(p.data, '$.type') = 'step-finish'
                ORDER BY p.time_created DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, ""
    if row is None:
        return 0, ""
    try:
        part = json.loads(row[0] or "{}")
        message = json.loads(row[1] or "{}")
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        ctx = int(tokens.get("input", 0) or 0) + int(cache.get("read", 0) or 0) + int(cache.get("write", 0) or 0)
        model_id = str(message.get("modelID") or message.get("model") or "")
        provider_id = str(message.get("providerID") or "")
        model = f"{provider_id}/{model_id}" if provider_id and model_id else model_id
        return (ctx, model) if ctx > 0 else (0, "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0, ""


def _codex_probe(session_id: str, root: Path | None = None) -> tuple[int, str]:
    from .session_parsers._session_parser import _extract_codex_usage, _extract_model_id
    from .session_parsers.codex import find_codex_sessions

    candidates = [p for p in find_codex_sessions(root) if session_id in p.stem]
    if not candidates:
        return 0, ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        lines = _tail_lines(newest)
    except OSError:
        return 0, ""
    best = 0
    best_model = ""
    current_model = ""
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue  # first tail line may be partial
        # Codex carries the model on `turn_context` entries that precede the
        # usage entries, so track it across lines rather than per-entry.
        model = _extract_model_id(entry)
        if model:
            current_model = model
        usage = _find_usage(entry)
        if usage is None:
            continue
        input_tokens, _out, _reasoning, _, cached, cache_write = _extract_codex_usage(usage)
        # OpenAI-style Codex usage is cumulative billing data: input_tokens
        # already includes cached_input_tokens, while the Codex UI's live
        # "used" value is the uncached remainder. Split-cache hosts report
        # cached reads separately, so those still add to the live window.
        if cached <= input_tokens:
            ctx = max(0, input_tokens - cached) + cache_write
        else:
            ctx = input_tokens + cached + cache_write
        if ctx > 0:
            best = ctx
            best_model = current_model
    return (best, best_model) if best > 0 else (0, "")


def _cursor_probe(session_id: str, db_path: Path | None = None) -> tuple[int, str]:
    """Newest Cursor bubble's (context tokens, model) for one composer session.

    Cursor stores every chat bubble in the shared ``state.vscdb`` sqlite under
    ``bubbleId:<composerId>:<bubbleId>`` keys (see session_parsers/cursor.py).
    ``tokenCount`` is omitted on effectively all bubbles (subscription
    product); when absent this correctly returns ``(0, "")`` — unknown, never
    estimated.
    """
    from lemoncrow.gateway.hosts.session_parsers.cursor import _normalize_model, find_cursor_db

    path = db_path or find_cursor_db()
    if path is None or not Path(path).exists():
        return 0, ""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT "
                "json_extract(value, '$.tokenCount.inputTokens'), "
                "COALESCE("
                "  json_extract(value, '$.tokenCount.cacheReadTokens'),"
                "  json_extract(value, '$.tokenCount.cachedInputTokens'),"
                "  json_extract(value, '$.tokenCount.cache_read_input_tokens'), 0), "
                "COALESCE("
                "  json_extract(value, '$.tokenCount.cacheWriteTokens'),"
                "  json_extract(value, '$.tokenCount.cacheCreationInputTokens'),"
                "  json_extract(value, '$.tokenCount.cache_creation_input_tokens'), 0), "
                "json_extract(value, '$.modelInfo.modelName') "
                "FROM cursorDiskKV WHERE key LIKE ? ORDER BY ROWID DESC LIMIT 50",
                (f"bubbleId:{session_id}:%",),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, ""
    for input_tokens, cache_read, cache_write, model in rows:
        ctx = int(input_tokens or 0) + int(cache_read or 0) + int(cache_write or 0)
        if ctx > 0:
            return ctx, _normalize_model(model)
    return 0, ""


def _copilot_probe(session_id: str, root: Path | None = None) -> tuple[int, str]:
    """Latest context-bearing usage from a Copilot session's events.jsonl.

    Copilot's ``assistant.message`` events carry only ``outputTokens``; the
    full input/cache breakdown appears on ``session.compaction_complete``
    (mid-session) and ``session.shutdown`` (end) events — see
    session_parsers/copilot.py. The newest such event's
    input + cacheRead + cacheWrite is the best measured context signal;
    sessions that never compacted return ``(0, "")`` (unknown, not estimated).
    """
    from lemoncrow.gateway.hosts.session_parsers.copilot import find_copilot_sessions

    session_dir = next((p for p in find_copilot_sessions(root) if p.name == session_id), None)
    if session_dir is None:
        return 0, ""
    try:
        lines = _tail_lines(session_dir / "events.jsonl")
    except OSError:
        return 0, ""
    best = 0
    best_model = ""
    current_model = ""
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue  # first tail line may be partial
        data = entry.get("data") or {}
        if not isinstance(data, dict):
            continue
        etype = str(entry.get("type") or "")
        model = str(data.get("model") or "").strip()
        if model:
            current_model = model
        usage: dict[str, Any] | None = None
        if etype == "session.compaction_complete":
            compaction = data.get("compactionTokensUsed")
            if isinstance(compaction, dict):
                usage = compaction
        elif etype == "session.shutdown":
            metrics = data.get("modelMetrics")
            if isinstance(metrics, dict):
                for mname, mdata in metrics.items():
                    m_usage = (mdata or {}).get("usage") if isinstance(mdata, dict) else None
                    if isinstance(m_usage, dict):
                        ctx = (
                            int(m_usage.get("inputTokens", 0) or 0)
                            + int(m_usage.get("cacheReadTokens", 0) or 0)
                            + int(m_usage.get("cacheWriteTokens", 0) or 0)
                        )
                        if ctx > 0:
                            best = ctx
                            best_model = str(mname)
                continue
        if usage is None:
            continue
        ctx = (
            int(usage.get("inputTokens", 0) or 0)
            + int(usage.get("cacheReadTokens", 0) or 0)
            + int(usage.get("cacheWriteTokens", 0) or 0)
        )
        if ctx > 0:
            best = ctx
            best_model = str(usage.get("model") or "").strip() or current_model
    return (best, best_model) if best > 0 else (0, "")


_PROBES: dict[str, Callable[[str], tuple[int, str]]] = {
    "claude": _claude_probe,
    "codex": _codex_probe,
    "opencode": _opencode_probe,
    "cursor": _cursor_probe,
    "copilot": _copilot_probe,
}
