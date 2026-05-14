"""Gemini CLI session importer for Atelier.

Converts ~/.gemini/tmp/atelier/chats/session-*.jsonl
into redacted RawArtifacts + curated Atelier Traces.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import traceback as _traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.foundation.models import (
    CommandRecord,
    FileEditRecord,
    RawArtifact,
    ToolCall,
    Trace,
)
from atelier.core.foundation.redaction import redact
from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import (
    _SIZE_LIMIT_BYTES,
    make_llm_usage_entry,
    summarize_usage_entries,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_gemini_sessions(root: Path | None = None) -> Iterator[Path]:
    if root is None:
        root = Path("~/.gemini/tmp").expanduser()
    if not root.is_dir():
        return

    # Two layouts coexist under ~/.gemini/tmp/<project>/chats/:
    #   1. Top-level session files:    chats/session-YYYY-MM-DDTHH-MM-<id>.jsonl
    #   2. Sub-agent / sub-session:    chats/<sessionId>/<subagent-id>.jsonl
    #      (kind="subagent" in the first record). These carry real LLM events
    #      with `tokens` blocks and were previously ignored, so atelier missed
    #      ~15-20M cache_read tokens per day on heavy gemini days.
    seen: set[Path] = set()
    for p in sorted(root.glob("**/chats/session-*.jsonl")):
        seen.add(p)
        yield p
    for p in sorted(root.glob("**/chats/*/*.jsonl")):
        if p in seen:
            continue
        # Skip if the parent dir is "chats" itself (already handled above) — the
        # pattern only matches one level deeper, so this is a sub-session file.
        yield p


def _gemini_event_key(event: dict[str, Any]) -> tuple[str, str] | None:
    event_id = str(event.get("id") or "").strip()
    timestamp = str(event.get("timestamp") or "").strip()
    if not event_id or not timestamp:
        return None
    return (event_id, timestamp)


def _token_total(tokens: dict[str, Any]) -> int:
    total = 0
    for key in ("input", "output", "cached", "thoughts", "tool", "total"):
        try:
            total += int(tokens.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _merge_gemini_event(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "tokens" and isinstance(value, dict):
            current = merged.get(key)
            if not isinstance(current, dict) or _token_total(value) >= _token_total(current):
                merged[key] = value
            continue

        if key in {"toolCalls", "thoughts", "content"} and isinstance(value, list):
            current = merged.get(key)
            if not isinstance(current, list) or len(value) >= len(current):
                merged[key] = value
            continue

        if value in (None, "", [], {}):
            continue

        current = merged.get(key)
        if current in (None, "", [], {}):
            merged[key] = value

    return merged


def _canonicalize_gemini_events(raw_content: str) -> list[dict[str, Any]]:
    seen_event_lines: set[str] = set()
    canonical_events: list[dict[str, Any]] = []
    event_indexes: dict[tuple[str, str], int] = {}

    for raw_line in raw_content.splitlines():
        line = raw_line.strip()
        if not line or line in seen_event_lines:
            continue
        seen_event_lines.add(line)

        try:
            event = json.loads(line)
        except Exception:
            continue

        key = _gemini_event_key(event)
        if key is None:
            canonical_events.append(event)
            continue

        existing_index = event_indexes.get(key)
        if existing_index is None:
            event_indexes[key] = len(canonical_events)
            canonical_events.append(event)
            continue

        canonical_events[existing_index] = _merge_gemini_event(canonical_events[existing_index], event)

    return canonical_events


class GeminiImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        """Import all sessions. Returns IDs of successfully imported sessions."""
        imported_ids = []
        skipped = 0
        all_sessions = list(find_gemini_sessions(root))
        total = len(all_sessions)

        print(f"[atelier] gemini: discovering sessions (found {total})")

        for i, jsonl_path in enumerate(all_sessions):
            try:
                # Performance safety: skip massive files (>50MB) for now
                if jsonl_path.stat().st_size > _SIZE_LIMIT_BYTES:
                    size_mb = jsonl_path.stat().st_size / 1e6
                    print(f"[atelier] gemini: skipping massive session {jsonl_path.name} ({size_mb:.1f}MB)")
                    continue

                if i % 10 == 0 and i > 0:
                    print(f"[atelier] gemini: importing {i}/{total}...")

                sid = self.import_session(jsonl_path, force=force)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] skipping gemini session {jsonl_path.name}: {exc}")
        return imported_ids

    def import_session(self, jsonl_path: Path, *, force: bool = False) -> str | None:
        """Import a single Gemini session JSONL file. Returns trace ID on success."""
        filename_session_id = jsonl_path.stem.replace("session-", "")
        artifact_id = f"gemini-{filename_session_id}"
        file_mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
        raw_content = jsonl_path.read_text(encoding="utf-8")
        raw_sha = _sha256(raw_content)

        if not force:
            existing = self.store.get_raw_artifact(artifact_id)
            if (
                existing
                and existing.sha256_original == raw_sha
                and existing.source_file_mtime
                and file_mtime <= existing.source_file_mtime
            ):
                return None

        redacted = redact(raw_content)

        # Extract internal sessionId if available
        actual_session_id = filename_session_id
        for line in raw_content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if "sessionId" in ev:
                    actual_session_id = str(ev["sessionId"])
                    break
            except Exception:
                continue

        artifact = RawArtifact(
            id=artifact_id,
            source="gemini",
            source_session_id=actual_session_id,
            kind="session.jsonl",
            relative_path=jsonl_path.name,
            content_path=f"raw/gemini/{jsonl_path.name}",
            sha256_original=raw_sha,
            sha256_redacted=_sha256(redacted),
            byte_count_original=len(raw_content.encode("utf-8")),
            byte_count_redacted=len(redacted.encode("utf-8")),
            created_at=_utcnow(),
            source_file_mtime=file_mtime,
            source_path=str(jsonl_path),
        )
        self.store.record_raw_artifact(artifact, redacted)

        tools_called: dict[str, int] = {}
        tool_args: dict[str, dict[str, Any] | None] = {}
        tool_in_tokens: dict[str, int] = {}
        tool_out_tokens: dict[str, int] = {}

        files_touched: list[str | FileEditRecord] = []
        commands_run: list[str | CommandRecord] = []
        reasoning_snippets: list[str] = []
        task = "untitled gemini session"
        created_at = file_mtime

        # Token aggregation. Gemini events expose tokens = {input, output, cached,
        # thoughts, tool, total} plus a top-level `model` field. `cached` is a
        # SUBSET of `input` (Gemini accounting). `thoughts` are reasoning tokens
        # tracked separately from `output`.
        total_in_tokens = 0
        total_out_tokens = 0
        total_thinking_tokens = 0
        total_cached = 0
        user_prompt_tokens = 0
        model_seen = ""
        usage_entries = []

        for ev in _canonicalize_gemini_events(raw_content):
            if "startTime" in ev and "sessionId" in ev:
                with contextlib.suppress(BaseException):
                    created_at = datetime.fromisoformat(ev["startTime"].replace("Z", "+00:00"))
                continue

            if ev.get("type") == "user":
                content = ev.get("content", [])
                if isinstance(content, list):
                    text = ""
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            text += part["text"]

                    if text:
                        user_prompt_tokens += max(1, len(text) // 4)
                        if task == "untitled gemini session" and not text.startswith("/"):
                            task = text[:200]

            m = ev.get("model")
            if m:
                model_seen = str(m)

            tokens = ev.get("tokens") or {}
            if tokens:
                in_t = int(tokens.get("input", 0) or 0)
                out_t = int(tokens.get("output", 0) or 0)
                thoughts_t = int(tokens.get("thoughts", 0) or 0)
                cached_t = int(tokens.get("cached", 0) or 0)
                billable_in_t = max(0, in_t - cached_t)

                usage_entry = make_llm_usage_entry(
                    model=str(m or model_seen or ""),
                    input_tokens=billable_in_t,
                    output_tokens=out_t,
                    thinking_tokens=thoughts_t,
                    cached_input_tokens=cached_t,
                    source_type="gemini.event",
                    source_id=str(ev.get("id") or ""),
                    created_at=created_at,
                )
                if usage_entry is not None:
                    usage_entries.append(usage_entry)

                total_in_tokens += billable_in_t
                total_out_tokens += out_t
                total_thinking_tokens += thoughts_t
                total_cached += cached_t

                calls = ev.get("toolCalls", []) or ev.get("tool_calls", [])
                if not calls and isinstance(ev.get("message"), dict):
                    calls = ev["message"].get("tool_calls", [])

                if calls:
                    dist_in = in_t // len(calls)
                    dist_out = out_t // len(calls)
                    for call in calls:
                        name = call.get("name", "unknown")
                        # Tool In = generated args (LLM Out), Tool Out = context window (LLM In)
                        tool_in_tokens[name] = tool_in_tokens.get(name, 0) + dist_out
                        tool_out_tokens[name] = tool_out_tokens.get(name, 0) + dist_in

            if "toolCalls" in ev:
                for call in ev["toolCalls"]:
                    name = call.get("name", "unknown")
                    tools_called[name] = tools_called.get(name, 0) + 1
                    args = call.get("args", {})
                    if name not in tool_args:
                        tool_args[name] = args
                    if name in ["write_file", "replace"]:
                        path = args.get("file_path") or args.get("path")
                        if path:
                            files_touched.append(
                                FileEditRecord(
                                    path=str(path),
                                    diff=str(args.get("content") or args.get("new_string") or "")[:4096],
                                )
                            )
                    elif name == "run_shell_command":
                        cmd = args.get("command")
                        if cmd:
                            commands_run.append(str(cmd)[:200])

            if "thoughts" in ev:
                for thought in ev["thoughts"]:
                    reasoning_snippets.append(thought.get("description", "")[:500])

        tools_list = []
        for n, c in tools_called.items():
            tools_list.append(
                ToolCall(
                    name=n,
                    args_hash="",
                    count=c,
                    args=tool_args.get(n),
                    input_tokens=tool_in_tokens.get(n, 0),
                    output_tokens=tool_out_tokens.get(n, 0),
                )
            )

        usage_summary = summarize_usage_entries(usage_entries, fallback_model=model_seen)

        trace = Trace(
            id=artifact_id,
            session_id=actual_session_id,
            agent="atelier:code",
            host="gemini",
            domain="coding",
            task=task,
            status="success",
            files_touched=files_touched,
            tools_called=tools_list,
            commands_run=commands_run,
            errors_seen=[],
            validation_results=[],
            raw_artifact_ids=[artifact.id],
            reasoning=reasoning_snippets,
            input_tokens=usage_summary["input_tokens"],
            user_prompt_tokens=user_prompt_tokens,
            cached_input_tokens=usage_summary["cached_input_tokens"],
            cache_creation_input_tokens=0,
            model=usage_summary["model"],
            usage_entries=usage_summary["usage_entries"],
            model_usages=usage_summary["model_usages"],
            output_tokens=usage_summary["output_tokens"],
            thinking_tokens=usage_summary["thinking_tokens"],
            created_at=created_at,
        )
        self.store.record_trace(trace, write_json=False)
        return trace.id
