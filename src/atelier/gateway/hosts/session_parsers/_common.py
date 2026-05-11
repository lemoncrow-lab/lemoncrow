"""Shared helpers for normalized host session imports."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
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

logger = logging.getLogger(__name__)

_COMMAND_TOOL_NAMES = {
    "bash",
    "exec",
    "execute",
    "execute_command",
    "run_command",
    "run_shell_command",
    "developer__shell",
    "computercontroller__shell",
}
_FILE_TOOL_NAMES = {
    "edit",
    "write",
    "create",
    "multiedit",
    "write_file",
    "edit_file",
    "replace",
    "patch",
    "apply_patch",
    "developer__text_editor",
    "developer__write_file",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_id(value: str) -> str:
    return value.replace("/", "-").replace("\\", "-")


def parse_datetime(value: Any, *, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp < 1e12:
            stamp *= 1000
        return datetime.fromtimestamp(stamp / 1000, tz=UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            if stripped.isdigit():
                return parse_datetime(int(stripped), default=default)
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                logger.warning(
                    "Suppressed exception at _common.py:76",
                    exc_info=True,
                )
    return default or utcnow()


def char_tokens(text: str, *, chars_per_token: int = 4) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + chars_per_token - 1) // chars_per_token)


def unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def extract_command_names(command: str) -> list[str]:
    if not command.strip():
        return []
    first_line = command.splitlines()[0].strip()
    if not first_line:
        return []
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", first_line)
    names: list[str] = []
    for part in parts:
        tokens = [token for token in part.strip().split() if token]
        while tokens and ("=" in tokens[0] or tokens[0] in {"env", "sudo"}):
            tokens.pop(0)
        if not tokens:
            continue
        names.append(Path(tokens[0]).name)
    return unique_strings(names)


def make_session_line(
    session_id: str,
    *,
    timestamp: str | None = None,
    cwd: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    line: dict[str, Any] = {"type": "session", "id": session_id}
    if timestamp:
        line["timestamp"] = timestamp
    if cwd:
        line["cwd"] = cwd
    if title:
        line["title"] = title
    return line


def make_user_message(text: str, *, timestamp: str | None = None, message_id: str | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {
        "type": "message",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    if timestamp:
        line["timestamp"] = timestamp
    if message_id:
        line["id"] = message_id
    return line


def make_tool_call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "toolCall", "name": name, "arguments": arguments or {}}


def make_assistant_message(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    timestamp: str | None = None,
    cache_read: int = 0,
    cache_write: int = 0,
    thinking_tokens: int = 0,
    texts: Iterable[str] = (),
    tool_calls: Iterable[dict[str, Any]] = (),
    thinking_texts: Iterable[str] = (),
    message_id: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for text in texts:
        if text:
            content.append({"type": "text", "text": text})
    for text in thinking_texts:
        if text:
            content.append({"type": "reasoning", "text": text})
    content.extend(tool_calls)
    line: dict[str, Any] = {
        "type": "message",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input": input_tokens,
                "output": output_tokens,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "thinking": thinking_tokens,
            },
            "content": content,
        },
    }
    if timestamp:
        line["timestamp"] = timestamp
    if message_id:
        line["id"] = message_id
    return line


def build_normalized_jsonl(events: Iterable[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events if event)


def _tool_args_hash(args: dict[str, Any] | None) -> str:
    payload = json.dumps(args or {}, sort_keys=True, default=str, ensure_ascii=False)
    return sha256_text(payload)


def _is_command_tool(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered in _COMMAND_TOOL_NAMES or lowered.endswith("shell") or lowered.endswith("command")


def _is_file_tool(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered in _FILE_TOOL_NAMES or lowered.endswith("edit") or lowered.endswith("write")


def _build_trace_from_normalized_content(
    *,
    source: str,
    session_id: str,
    raw_content: str,
    artifact: RawArtifact,
    task: str | None,
    source_mtime: datetime | None,
) -> Trace:
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    total_thinking = 0
    user_prompt_tokens = 0
    model_seen = ""
    created_at = source_mtime or utcnow()
    task_text = task or ""
    first_user_text = ""
    reasoning: list[str] = []
    files_touched: dict[str, FileEditRecord] = {}
    commands_run: list[CommandRecord] = []
    tools_called: dict[str, int] = {}
    tool_args: dict[str, dict[str, Any] | None] = {}
    tool_input_tokens: dict[str, int] = {}
    tool_output_tokens: dict[str, int] = {}

    for line in raw_content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "session":
            created_at = parse_datetime(event.get("timestamp"), default=created_at)
            if not task_text:
                title = str(event.get("title") or "").strip()
                if title:
                    task_text = title
            continue

        if event_type != "message":
            continue

        message = event.get("message") or {}
        role = str(message.get("role") or "")
        timestamp = parse_datetime(event.get("timestamp"), default=created_at)
        if timestamp < created_at:
            created_at = timestamp

        content = message.get("content") or []
        if role == "user":
            text_parts = [
                str(block.get("text") or "").strip()
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            combined = " ".join(part for part in text_parts if part).strip()
            if combined:
                user_prompt_tokens += char_tokens(combined)
                if not first_user_text:
                    first_user_text = combined[:200]
                    if not task_text and not first_user_text.startswith("/"):
                        task_text = first_user_text
            continue

        if role != "assistant":
            continue

        usage = message.get("usage") or {}
        input_tokens = int(usage.get("input", 0) or 0)
        output_tokens = int(usage.get("output", 0) or 0)
        cache_read = int(usage.get("cacheRead", 0) or 0)
        cache_write = int(usage.get("cacheWrite", 0) or 0)
        thinking_tokens = int(usage.get("thinking", 0) or 0)

        total_input += input_tokens
        total_output += output_tokens
        total_cache_read += cache_read
        total_cache_write += cache_write
        total_thinking += thinking_tokens

        model = str(message.get("model") or model_seen)
        if model:
            model_seen = model

        tool_blocks = [
            block for block in content if isinstance(block, dict) and block.get("type") in {"toolCall", "tool_use"}
        ]
        reasoning.extend(
            str(block.get("text") or "").strip()
            for block in content
            if isinstance(block, dict) and block.get("type") in {"reasoning", "thinking"} and block.get("text")
        )

        if tool_blocks:
            distributed_input = (input_tokens + cache_read + cache_write) // len(tool_blocks)
            distributed_output = (output_tokens + thinking_tokens) // len(tool_blocks)
            for block in tool_blocks:
                name = str(block.get("name") or "unknown")
                arguments = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
                tools_called[name] = tools_called.get(name, 0) + 1
                tool_args.setdefault(name, arguments or None)
                tool_input_tokens[name] = tool_input_tokens.get(name, 0) + distributed_output
                tool_output_tokens[name] = tool_output_tokens.get(name, 0) + distributed_input

                if _is_file_tool(name):
                    path = str(
                        arguments.get("file_path") or arguments.get("path") or arguments.get("target_file") or ""
                    ).strip()
                    if path:
                        files_touched[path] = FileEditRecord(
                            path=path,
                            diff=str(
                                arguments.get("diff")
                                or arguments.get("patch")
                                or arguments.get("content")
                                or arguments.get("new_string")
                                or ""
                            )[:4096],
                        )

                if _is_command_tool(name):
                    command = str(arguments.get("command") or "").strip()
                    if command:
                        commands_run.append(CommandRecord(command=command[:4096]))

    if not task_text:
        task_text = f"untitled {source} session"

    trace = Trace(
        id=artifact.id,
        session_id=session_id,
        agent="atelier:code",
        host=source,
        domain="coding",
        task=task_text,
        status="success",
        files_touched=list(files_touched.values()),
        tools_called=[
            ToolCall(
                name=name,
                args_hash=_tool_args_hash(tool_args.get(name)),
                count=count,
                args=tool_args.get(name),
                input_tokens=tool_input_tokens.get(name, 0),
                output_tokens=tool_output_tokens.get(name, 0),
            )
            for name, count in sorted(tools_called.items())
        ],
        commands_run=commands_run,
        errors_seen=[],
        validation_results=[],
        raw_artifact_ids=[artifact.id],
        reasoning=[item for item in reasoning if item][:32],
        input_tokens=total_input,
        user_prompt_tokens=user_prompt_tokens,
        output_tokens=total_output,
        thinking_tokens=total_thinking,
        cached_input_tokens=total_cache_read,
        cache_creation_input_tokens=total_cache_write,
        model=model_seen,
        created_at=created_at,
    )
    return trace


_SIZE_LIMIT_BYTES = 500 * 1024 * 1024  # 500 MB


def import_paths_with_progress(
    source: str,
    paths: list[Path],
    import_fn: Any,
    *,
    size_limit: int = _SIZE_LIMIT_BYTES,
) -> list[str]:
    """Iterate *paths*, print Gemini-style progress, call *import_fn(path)* for each."""
    total = len(paths)
    print(f"[atelier] {source}: discovering sessions (found {total})")
    imported: list[str] = []
    for i, path in enumerate(paths):
        try:
            size = path.stat().st_size
            if size > size_limit:
                print(f"[atelier] {source}: skipping massive session {path.name} ({size / 1e6:.1f}MB)")
                continue
            if i % 10 == 0 and i > 0:
                print(f"[atelier] {source}: importing {i}/{total}...")
            sid = import_fn(path)
            if sid:
                imported.append(sid)
        except Exception as exc:
            import traceback as _tb

            _tb.print_exc()
            print(f"[atelier] skipping {source} session {path.name}: {exc}")
    return imported


def record_normalized_session(
    store: ReasoningStore,
    *,
    source: str,
    session_id: str,
    relative_path: str,
    content_path: str,
    raw_content: str,
    source_mtime: datetime | None,
    force: bool = False,
    task: str | None = None,
) -> str | None:
    artifact_id = f"{source}-{sanitize_id(session_id)}"
    if not force and source_mtime is not None:
        existing = store.get_raw_artifact(artifact_id)
        if existing and existing.source_file_mtime and source_mtime <= existing.source_file_mtime:
            return None

    redacted = redact(raw_content)
    artifact = RawArtifact(
        id=artifact_id,
        source=source,
        source_session_id=session_id,
        kind="session.jsonl",
        relative_path=relative_path,
        content_path=content_path,
        sha256_original=sha256_text(raw_content),
        sha256_redacted=sha256_text(redacted),
        byte_count_original=len(raw_content.encode("utf-8")),
        byte_count_redacted=len(redacted.encode("utf-8")),
        created_at=utcnow(),
        source_file_mtime=source_mtime,
    )
    store.record_raw_artifact(artifact, redacted)

    trace = _build_trace_from_normalized_content(
        source=source,
        session_id=session_id,
        raw_content=raw_content,
        artifact=artifact,
        task=task,
        source_mtime=source_mtime,
    )
    store.record_trace(trace, write_json=False)
    return trace.id
