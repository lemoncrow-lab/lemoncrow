"""Cursor session importer for LemonCrow."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lemoncrow.gateway.hosts.session_parsers._common import (
    build_normalized_jsonl,
    make_assistant_message,
    make_session_line,
    make_tool_call,
    make_user_message,
    parse_datetime,
    record_normalized_session,
)
from lemoncrow.infra.storage.bundle import StoreBundle

logger = logging.getLogger(__name__)

_PLACEHOLDER_MODELS = {"", "auto", "default", "composer-2"}
_MAX_SESSION_AGE_DAYS = 5
# Cursor's bubble schema only ever carries type 1 (user) and type 2
# (assistant) in practice. Anything else is treated as neither -- it must
# not be billed as a fabricated assistant turn (see import_all).
_ASSISTANT_BUBBLE_TYPES = {2}


def _cursor_agent_chats_dirs() -> list[Path]:
    """Candidate cursor-agent CLI chat-store roots (``~/.config/cursor/chats``).

    The CLI keeps conversations here (lowercase ``cursor``), distinct from the
    IDE's capital-C ``Cursor`` globalStorage. Honors ``XDG_CONFIG_HOME``.
    """
    dirs: list[Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.append(Path(xdg) / "cursor" / "chats")
    dirs.append(Path.home() / ".config" / "cursor" / "chats")
    dirs.append(Path.home() / "Library" / "Application Support" / "cursor" / "chats")
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)


def _map_cursor_tool(tool_name: str, part: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Map a Cursor tool-call/result part to a normalized ``(name, args)``.

    - Native Cursor tools (``Read``, ``Grep``, ``Edit``, ``codebase_search``, ...)
      pass through unchanged so the replay engine can spot wasteful grep /
      whole-file-read loops a single ``code_search`` would collapse.
    - MCP calls (``CallMcpTool`` -> ``lemoncrow``) are namespaced ``lc_<tool>`` so
      the engine treats them as already-optimized (never collapsible).
    - Discovery calls (``GetMcpTools``) are dropped -- not real work tool calls.
    """
    raw_args = part.get("args") or part.get("input") or part.get("arguments") or {}
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    if tool_name == "GetMcpTools":
        return None
    if tool_name == "CallMcpTool":
        inner = str(args.get("toolName") or "").strip()
        if not inner:
            return None
        server = str(args.get("server") or "").strip()
        inner_args_raw = args.get("arguments")
        inner_args: dict[str, Any] = inner_args_raw if isinstance(inner_args_raw, dict) else {}
        prefix = "lc_" if server == "lemoncrow" else (f"{server}_" if server else "")
        return f"{prefix}{inner}", inner_args
    return tool_name, args


def _cursor_agent_events(store_db: Path) -> list[dict[str, Any]]:
    """Ordered conversation events from a cursor-agent ``store.db``.

    The CLI persists the conversation as content-addressed JSON blobs in a
    single ``blobs`` table; iterating by ROWID reproduces append (turn) order.
    Yields ``{"role": "user"/"assistant", "text": ...}`` for message text and
    ``{"role": "tool_call", "name": ..., "args": {...}}`` for each tool call
    (deduped by ``toolCallId``) so the replay/opportunity engine sees the native
    grep/read loops. Binary link blobs and the meta root are skipped. No
    per-message token usage is stored (Cursor hides it under privacy/ghost
    mode), so none is invented -- mirroring the IDE importer's no-fabrication rule.
    """
    events: list[dict[str, Any]] = []
    seen_tool_ids: set[str] = set()
    try:
        with sqlite3.connect(f"file:{store_db}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT data FROM blobs").fetchall()
    except sqlite3.Error:
        return events
    for (data,) in rows:
        if not isinstance(data, (bytes, bytearray)) or data[:1] != b"{":
            continue
        try:
            obj = json.loads(bytes(data))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict) or "role" not in obj:
            continue
        role = str(obj.get("role") or "")
        content = obj.get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for part in parts:
            if not isinstance(part, dict):
                continue
            tool_name = part.get("toolName")
            if tool_name:
                # A tool call surfaces as both a request part (assistant) and a
                # result part (tool); dedup so each real call counts once.
                tcid = str(part.get("toolCallId") or "").strip()
                dedup = tcid or f"{tool_name}:{json.dumps(part.get('args') or {}, sort_keys=True)[:120]}"
                if dedup in seen_tool_ids:
                    continue
                seen_tool_ids.add(dedup)
                mapped = _map_cursor_tool(str(tool_name), part)
                if mapped is not None:
                    events.append({"role": "tool_call", "name": mapped[0], "args": mapped[1]})
                continue
            text = str(part.get("text") or "").strip()
            if not text:
                continue
            if role == "user":
                events.append({"role": "user", "text": text})
            elif role == "assistant":
                events.append({"role": "assistant", "text": text})
    return events


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
    def __init__(self, store: StoreBundle) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        """Import Cursor IDE (state.vscdb) and cursor-agent CLI (chats/*/store.db).

        Both persist under the ``cursor`` host (shared config). ``root`` overrides
        only the IDE DB path (an explicit ``--path``); the cursor-agent CLI chat
        store lives at a fixed per-user location, so it is scanned only during
        default discovery (``root is None``). The CLI scan is best-effort and
        never raises -- a malformed chat store must not abort the IDE import.
        """
        imported: list[str] = []
        db_path = find_cursor_db(root)
        if db_path is not None:
            imported.extend(self._import_ide_db(db_path, force=force, limit=limit))
        if root is None:
            try:
                imported.extend(self._import_cursor_agent_chats(force=force, limit=limit))
            except Exception:
                logger.exception("cursor-agent CLI chat import failed (non-fatal)")
        return imported

    def _import_ide_db(self, db_path: Path, *, force: bool = False, limit: int | None = None) -> list[str]:
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
        if not force:
            cutoff = datetime.now(UTC) - timedelta(days=_MAX_SESSION_AGE_DAYS)
            before = len(newest_first)
            newest_first = [
                cid
                for cid in newest_first
                if parse_datetime(composer_last_created.get(cid, ""), default=db_mtime) >= cutoff
            ]
            if before != len(newest_first):
                logger.info(
                    "cursor: skipped %d sessions older than %d days", before - len(newest_first), _MAX_SESSION_AGE_DAYS
                )
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
                    trace = self.store.history.get_trace(trace_id)
                    if trace is not None and not trace.workspace_path:
                        trace.workspace_path = workspace_path
                        self.store.history.record_trace(trace, write_json=False)
        return imported

    def _import_cursor_agent_chats(self, *, force: bool = False, limit: int | None = None) -> list[str]:
        """Import cursor-agent CLI conversations from ``~/.config/cursor/chats``.

        Layout: ``chats/<projectHash>/<sessionUuid>/{meta.json, store.db}``.
        ``meta.json`` gives the workspace (``cwd``) and ms-epoch timestamps;
        ``store.db`` holds the transcript blobs. Sessions without a real
        conversation (``hasConversation`` false -- e.g. a bare ``/usage`` call)
        are skipped. Content only, no token/cost accounting (the CLI does not
        persist it), so these sessions price at $0 -- honest, not fabricated.
        """
        imported: list[str] = []
        discovered: list[tuple[Path, Path, dict[str, Any]]] = []
        for chats_dir in _cursor_agent_chats_dirs():
            if not chats_dir.is_dir():
                continue
            for project_dir in chats_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for session_path in project_dir.iterdir():
                    store_db = session_path / "store.db"
                    meta_path = session_path / "meta.json"
                    if not (store_db.is_file() and meta_path.is_file()):
                        continue
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not isinstance(meta, dict) or not meta.get("hasConversation"):
                        continue
                    discovered.append((session_path, store_db, meta))

        def _updated_ms(item: tuple[Path, Path, dict[str, Any]]) -> int:
            return int(item[2].get("updatedAtMs") or item[2].get("createdAtMs") or 0)

        discovered.sort(key=_updated_ms, reverse=True)
        if not force:
            cutoff_ms = (datetime.now(UTC) - timedelta(days=_MAX_SESSION_AGE_DAYS)).timestamp() * 1000
            before = len(discovered)
            discovered = [item for item in discovered if _updated_ms(item) >= cutoff_ms]
            if before != len(discovered):
                logger.info(
                    "cursor-agent: skipped %d CLI sessions older than %d days",
                    before - len(discovered),
                    _MAX_SESSION_AGE_DAYS,
                )
        if limit is not None:
            discovered = discovered[:limit]

        for session_path, store_db, meta in discovered:
            session_id = session_path.name
            cwd = str(meta.get("cwd") or "").strip()
            project = (Path(cwd).name or "cursor-agent") if cwd else "cursor-agent"
            updated_ms = _updated_ms((session_path, store_db, meta))
            created_ms = int(meta.get("createdAtMs") or updated_ms or 0)
            session_mtime = datetime.fromtimestamp(updated_ms / 1000, tz=UTC) if updated_ms else datetime.now(UTC)
            ts = (
                datetime.fromtimestamp(created_ms / 1000, tz=UTC).isoformat()
                if created_ms
                else session_mtime.isoformat()
            )
            events: list[dict[str, Any]] = [make_session_line(session_id, title=project)]

            def _assistant(**kw: Any) -> dict[str, Any]:
                # No model/token accounting is persisted; namespace the model so
                # pricing.py keeps it at $0 (like the IDE importer's
                # _normalize_model placeholder path).
                return make_assistant_message(
                    model="cursor/unknown",
                    input_tokens=0,
                    output_tokens=0,
                    cache_read=0,
                    cache_write=0,
                    thinking_tokens=0,
                    timestamp=ts,
                    **kw,
                )

            content_turns = 0
            for index, ev in enumerate(_cursor_agent_events(store_db)):
                role = ev.get("role")
                if role == "user":
                    text = str(ev.get("text") or "")
                    match = _USER_QUERY_RE.search(text)
                    clean = (match.group(1).strip() if match else text).strip()
                    # Drop the CLI's injected environment/preamble user turns;
                    # keep only the real user query.
                    if not clean or clean.startswith(("<user_info>", "<additional_data>", "<environment")):
                        continue
                    events.append(make_user_message(clean[:2000], timestamp=ts, message_id=f"u-{index}"))
                    content_turns += 1
                elif role == "assistant":
                    events.append(_assistant(texts=[str(ev.get("text") or "")[:4000]], message_id=f"a-{index}"))
                    content_turns += 1
                elif role == "tool_call":
                    # Emit as a one-tool-call assistant message so the normalized
                    # parser renders a tool_call turn the replay engine can read.
                    tool_call = make_tool_call(str(ev.get("name") or "tool"), ev.get("args") or {})
                    events.append(_assistant(tool_calls=[tool_call], message_id=f"t-{index}"))
                    content_turns += 1
            if content_turns == 0:
                continue  # transcript had no user/assistant/tool content worth recording
            raw_content = build_normalized_jsonl(events)
            trace_id = record_normalized_session(
                self.store,
                source="cursor",
                session_id=session_id,
                relative_path=f"cursor-agent:{session_id}",
                content_path=f"raw/cursor/{session_id}.jsonl",
                raw_content=raw_content,
                source_mtime=session_mtime,
                force=force,
                task=project,
            )
            if trace_id:
                imported.append(trace_id)
                if cwd:
                    trace = self.store.history.get_trace(trace_id)
                    if trace is not None and not trace.workspace_path:
                        trace.workspace_path = cwd
                        self.store.history.record_trace(trace, write_json=False)
        return imported
