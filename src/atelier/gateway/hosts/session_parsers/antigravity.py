"""Antigravity cache importer for Atelier."""

from __future__ import annotations

import json
import os
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


def _cache_path(root: Path | None = None) -> Path:
    if root is not None:
        return root / "antigravity-results.json" if root.is_dir() else root
    if "CODEBURN_CACHE_DIR" in os.environ:
        return Path(os.environ["CODEBURN_CACHE_DIR"]) / "antigravity-results.json"
    return Path.home() / ".cache" / "codeburn" / "antigravity-results.json"


class AntigravityImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        cache_path = _cache_path(root)
        if not cache_path.is_file():
            return []
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        cascades = payload.get("cascades") or {}
        # Sort cascades by timestamp (using the timestamp of the first call) descending
        sorted_cascades = sorted(
            cascades.items(),
            key=lambda item: (
                item[1]["calls"][0].get("timestamp")
                if isinstance(item[1], dict) and isinstance(item[1].get("calls"), list) and item[1]["calls"]
                else ""
            ),
            reverse=True,
        )
        if limit is not None:
            sorted_cascades = sorted_cascades[:limit]

        imported: list[str] = []
        for cascade_id, cascade in sorted_cascades:
            calls = cascade.get("calls") if isinstance(cascade, dict) else None
            if not isinstance(calls, list):
                continue
            trace_id = self._import_cascade(cache_path, str(cascade_id), calls, force=force)
            if trace_id:
                imported.append(trace_id)
        return imported

    def _import_cascade(
        self, cache_path: Path, cascade_id: str, calls: list[dict[str, Any]], *, force: bool
    ) -> str | None:
        if not calls:
            return None
        cache_mtime = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=UTC)
        first_timestamp = str(calls[0].get("timestamp") or cache_mtime.isoformat())
        # Per-cascade recency: the shared cache file's mtime bumps on every
        # cascade in the file, so keying dedup on it re-imports every
        # cascade whenever any one of them gets a new call. Use this
        # cascade's own newest call timestamp instead (parsed, not compared
        # as raw strings, since the source format isn't guaranteed to be a
        # lexicographically-sortable ISO string).
        session_mtime: datetime | None = None
        for call in calls:
            raw_ts = call.get("timestamp")
            if not raw_ts:
                continue
            parsed_ts = parse_datetime(raw_ts, default=cache_mtime)
            if session_mtime is None or parsed_ts > session_mtime:
                session_mtime = parsed_ts
        if session_mtime is None:
            session_mtime = cache_mtime
        events: list[dict[str, Any]] = [make_session_line(cascade_id, timestamp=first_timestamp, title="antigravity")]
        seen_call_ids: set[str] = set()
        previous_unidentified_call = ""
        turn_index = 0
        for call in calls:
            call_id = str(call.get("id") or call.get("callId") or call.get("messageId") or "").strip()
            if call_id:
                if call_id in seen_call_ids:
                    continue
                seen_call_ids.add(call_id)
                previous_unidentified_call = ""
            else:
                call_fingerprint = json.dumps(call, sort_keys=True, default=str, ensure_ascii=False)
                if call_fingerprint == previous_unidentified_call:
                    continue
                previous_unidentified_call = call_fingerprint
            user_message = str(call.get("userMessage") or "").strip()
            if user_message:
                events.append(
                    make_user_message(
                        user_message[:500],
                        timestamp=str(call.get("timestamp") or first_timestamp),
                        message_id=f"u-{turn_index}",
                    )
                )
            tools = []
            bash_commands = list(call.get("bashCommands") or [])
            for tool_index, tool_name in enumerate(call.get("tools") or []):
                arguments = {}
                if str(tool_name).strip().lower() == "bash" and bash_commands:
                    arguments = {"command": str(bash_commands[min(tool_index, len(bash_commands) - 1)])}
                tools.append(make_tool_call(str(tool_name), arguments))
            events.append(
                make_assistant_message(
                    model=str(call.get("model") or "antigravity-auto"),
                    input_tokens=int(call.get("inputTokens", 0) or 0),
                    output_tokens=int(call.get("outputTokens", 0) or 0),
                    cache_read=int(call.get("cacheReadInputTokens", 0) or 0),
                    cache_write=int(call.get("cacheCreationInputTokens", 0) or 0),
                    thinking_tokens=int(call.get("reasoningTokens", 0) or 0),
                    texts=[str(call.get("outputSummary") or "Antigravity response")],
                    tool_calls=tools,
                    timestamp=str(call.get("timestamp") or first_timestamp),
                    message_id=f"a-{turn_index}",
                )
            )
            turn_index += 1
        raw_content = build_normalized_jsonl(events)
        return record_normalized_session(
            self.store,
            source="antigravity",
            session_id=cascade_id,
            relative_path=f"{cache_path.name}:{cascade_id}",
            content_path=f"raw/antigravity/{cascade_id}.jsonl",
            raw_content=raw_content,
            source_mtime=session_mtime,
            force=force,
            task="antigravity",
        )
