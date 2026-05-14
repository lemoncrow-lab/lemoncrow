"""Shared helpers for VS Code task-backed hosts like KiloCode and Roo Code."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import (
    build_normalized_jsonl,
    make_assistant_message,
    make_session_line,
    make_user_message,
    record_normalized_session,
)

_MODEL_TAG_RE = re.compile(r"<model>([^<]+)</model>")


def vscode_global_storage_path(extension_id: str) -> Path:
    return Path.home() / ".config" / "Code" / "User" / "globalStorage" / extension_id


def find_task_dirs(extension_id: str, root: Path | None = None) -> list[Path]:
    base = root or vscode_global_storage_path(extension_id)
    tasks_dir = base / "tasks"
    if not tasks_dir.is_dir():
        return []
    task_dirs: list[Path] = []
    for task_dir in sorted(path for path in tasks_dir.iterdir() if path.is_dir()):
        if (task_dir / "ui_messages.json").is_file():
            task_dirs.append(task_dir)
    return task_dirs


def extract_model(task_dir: Path) -> str:
    history_path = task_dir / "api_conversation_history.json"
    if not history_path.is_file():
        return "cline-auto"
    try:
        messages = json.loads(history_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "cline-auto"
    if not isinstance(messages, list):
        return "cline-auto"
    for message in messages:
        if message.get("role") != "user":
            continue
        for block in message.get("content") or []:
            match = _MODEL_TAG_RE.search(str(block.get("text") or ""))
            if match:
                raw = match.group(1)
                return raw.split("/")[-1]
    return "cline-auto"


def import_task_dir(
    store: ContextStore,
    *,
    host: str,
    extension_id: str,
    task_dir: Path,
    force: bool = False,
) -> str | None:
    ui_messages = json.loads((task_dir / "ui_messages.json").read_text(encoding="utf-8"))
    if not isinstance(ui_messages, list):
        return None
    model = extract_model(task_dir)
    task_id = task_dir.name
    user_message = ""
    for message in ui_messages:
        if message.get("type") == "say" and message.get("say") in {"user_feedback", "text"}:
            user_message = str(message.get("text") or "")[:500]
            break

    events: list[dict[str, Any]] = [make_session_line(task_id, title=task_id)]
    if user_message:
        events.append(make_user_message(user_message, message_id="u-0"))

    for index, message in enumerate(ui_messages):
        if message.get("type") != "say" or message.get("say") != "api_req_started":
            continue
        try:
            payload = json.loads(str(message.get("text") or "{}"))
        except json.JSONDecodeError:
            continue
        timestamp = None
        if message.get("ts"):
            try:
                timestamp = datetime.fromtimestamp(int(message["ts"]) / 1000, tz=UTC).isoformat()
            except (TypeError, ValueError):
                timestamp = None
        events.append(
            make_assistant_message(
                model=model,
                input_tokens=int(payload.get("tokensIn", 0) or 0),
                output_tokens=int(payload.get("tokensOut", 0) or 0),
                cache_read=int(payload.get("cacheReads", 0) or 0),
                cache_write=int(payload.get("cacheWrites", 0) or 0),
                texts=[f"{host} API request {index + 1}"],
                timestamp=timestamp,
                message_id=f"a-{index}",
            )
        )

    raw_content = build_normalized_jsonl(events)
    source_mtime = datetime.fromtimestamp(task_dir.stat().st_mtime, tz=UTC)
    return record_normalized_session(
        store,
        source=host,
        session_id=task_id,
        relative_path=task_id,
        content_path=f"raw/{host}/{task_id}.jsonl",
        raw_content=raw_content,
        source_mtime=source_mtime,
        force=force,
        task=user_message or task_id,
    )
