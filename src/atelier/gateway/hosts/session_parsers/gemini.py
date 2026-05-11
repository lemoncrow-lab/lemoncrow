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
from atelier.core.foundation.store import ReasoningStore
from atelier.gateway.hosts.session_parsers._common import _SIZE_LIMIT_BYTES


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_gemini_sessions(root: Path | None = None) -> Iterator[Path]:
    if root is None:
        root = Path("~/.gemini/tmp").expanduser()
    if not root.is_dir():
        return

    # Discovery pattern: find all *.jsonl files that look like sessions
    # in any subproject's chats/ directory.
    # Pattern: ~/.gemini/tmp/*/chats/session-*.jsonl
    yield from sorted(root.glob("**/chats/session-*.jsonl"))


class GeminiImporter:
    def __init__(self, store: ReasoningStore) -> None:
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
                    print(
                        f"[atelier] gemini: skipping massive session {jsonl_path.name} ({jsonl_path.stat().st_size / 1e6:.1f}MB)"
                    )
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

        if not force:
            existing = self.store.get_raw_artifact(artifact_id)
            if existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
                return None

        raw_content = jsonl_path.read_text(encoding="utf-8")
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
            sha256_original=_sha256(raw_content),
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
        processed_ids: set[str] = set()

        for line in raw_content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue

            msg_id = str(ev.get("id") or "")
            if msg_id and msg_id in processed_ids:
                continue
            if msg_id:
                processed_ids.add(msg_id)

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

                total_in_tokens += in_t
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
            input_tokens=total_in_tokens - total_cached,
            user_prompt_tokens=user_prompt_tokens,
            cached_input_tokens=total_cached,
            cache_creation_input_tokens=0,
            model=model_seen,
            output_tokens=total_out_tokens,
            thinking_tokens=total_thinking_tokens,
            created_at=created_at,
        )
        self.store.record_trace(trace, write_json=False)
        return trace.id
