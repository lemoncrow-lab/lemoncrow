"""Cline (saoudrizwan.claude-dev VSCode extension) session importer for Atelier.

Reads tasks from:
  ~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/<id>/
    api_conversation_history.json  - full Anthropic API conversation
    task_metadata.json             - files in context, model usage

  ~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/state/taskHistory.json
    - per-task token totals (tokensIn/Out, cacheWrites/Reads, totalCost)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback as _traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from atelier.core.foundation.models import (
    FileEditRecord,
    RawArtifact,
    ToolCall,
    Trace,
)
from atelier.core.foundation.redaction import redact
from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import (
    make_llm_usage_entry,
    summarize_usage_entries,
)

logger = logging.getLogger(__name__)

_CLINE_ROOT = Path("~/.config/Code/User/globalStorage/saoudrizwan.claude-dev")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ms_to_dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _load_task_history(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "state" / "taskHistory.json"
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(entries, list):
            return {str(e["id"]): e for e in entries if "id" in e}
    except (OSError, json.JSONDecodeError, KeyError):
        logger.warning(
            "Suppressed exception at cline.py:50",
            exc_info=True,
        )
    return {}


def find_cline_tasks(root: Path | None = None) -> list[Path]:
    """Return task directories sorted chronologically by task ID (Unix ms timestamp)."""
    if root is None:
        root = _CLINE_ROOT.expanduser()
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        return []
    return sorted(
        [d for d in tasks_dir.iterdir() if d.is_dir() and (d / "api_conversation_history.json").exists()],
        key=lambda p: p.name,
    )


def _extract_tools_from_history(history: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "unknown")
                    counts[name] = counts.get(name, 0) + 1
        elif isinstance(content, str):
            # Old XML-in-text format
            for m in re.finditer(r"<([a-z_]+)>\n", content):
                name = m.group(1)
                if name not in ("thinking", "answer", "task", "response", "result"):
                    counts[name] = counts.get(name, 0) + 1
    return counts


def _extract_commands_from_history(history: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") in ("execute_command", "run_command", "bash", "shell"):
                inp = block.get("input") or {}
                cmd = inp.get("command") or inp.get("cmd") or ""
                if cmd:
                    commands.append(str(cmd)[:200])
    return commands


def _extract_task_text(history_entry: dict[str, Any], history: list[dict[str, Any]]) -> str:
    task = str(history_entry.get("task") or "").strip()
    if task:
        return task[:200]
    for msg in history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content") or []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = str(block.get("text") or "")
                m = re.search(r"<task>\s*(.*?)\s*</task>", text, re.DOTALL)
                if m:
                    return m.group(1)[:200]
                if len(text) > 5:
                    return text[:200]
        break
    return "untitled cline task"


class ClineImporter:
    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        if root is None:
            root = _CLINE_ROOT.expanduser()
        task_dirs = find_cline_tasks(root)
        if not task_dirs:
            return []

        task_history = _load_task_history(root)
        total = len(task_dirs)
        print(f"[atelier] cline: discovering tasks (found {total})")

        imported_ids: list[str] = []
        skipped = 0
        for i, task_dir in enumerate(task_dirs):
            try:
                if i % 10 == 0 and i > 0:
                    print(f"[atelier] cline: importing {i}/{total}...")
                history_entry = task_history.get(task_dir.name, {})
                sid = self.import_task(task_dir, history_entry, force=force)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] cline: skipping task {task_dir.name}: {exc}")

        if skipped > 0:
            print(f"[atelier] cline: {skipped} tasks already imported (skipped by dedup)")
        return imported_ids

    def import_task(self, task_dir: Path, history_entry: dict[str, Any], *, force: bool = False) -> str | None:
        task_id = task_dir.name
        api_history_path = task_dir / "api_conversation_history.json"
        if not api_history_path.exists():
            return None

        artifact_id = f"cline-{task_id}-api-history"
        existing = self.store.get_raw_artifact(artifact_id)
        try:
            file_mtime = datetime.fromtimestamp(api_history_path.stat().st_mtime, tz=UTC)
        except OSError:
            file_mtime = _utcnow()
        if not force and existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
            return None

        api_history_raw = api_history_path.read_text(encoding="utf-8")
        redacted_history = redact(api_history_raw)
        raw_bytes = api_history_raw.encode("utf-8")
        redacted_bytes = redacted_history.encode("utf-8")

        artifact = RawArtifact(
            id=artifact_id,
            source="cline",
            source_session_id=task_id,
            kind="api_conversation_history.json",
            relative_path="api_conversation_history.json",
            content_path=f"raw/cline/{task_id}/api_conversation_history.json",
            sha256_original=hashlib.sha256(raw_bytes).hexdigest(),
            sha256_redacted=hashlib.sha256(redacted_bytes).hexdigest(),
            byte_count_original=len(raw_bytes),
            byte_count_redacted=len(redacted_bytes),
            created_at=_utcnow(),
            source_file_mtime=file_mtime,
            source_path=str(api_history_path),
        )
        self.store.record_raw_artifact(artifact, redacted_history)

        try:
            history: list[dict[str, Any]] = json.loads(redacted_history)
        except json.JSONDecodeError:
            history = []

        metadata_path = task_dir / "task_metadata.json"
        metadata: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError):
                logger.warning(
                    "Suppressed exception at cline.py:212",
                    exc_info=True,
                )

        tools_called_counts = _extract_tools_from_history(history)
        commands_run = _extract_commands_from_history(history)

        files_touched: list[FileEditRecord | str] = []
        for fc in metadata.get("files_in_context") or []:
            path = fc.get("path")
            if not path:
                continue
            if fc.get("cline_edit_date"):
                files_touched.append(FileEditRecord(path=str(path), diff="", event="edit"))
            else:
                files_touched.append(str(path))

        model_usage = metadata.get("model_usage") or []
        model = str(model_usage[-1].get("model_id") or "") if model_usage else ""

        task_text = _extract_task_text(history_entry, history)

        input_tokens = int(history_entry.get("tokensIn") or 0)
        output_tokens = int(history_entry.get("tokensOut") or 0)
        cached_input_tokens = int(history_entry.get("cacheReads") or 0)
        cache_creation_input_tokens = int(history_entry.get("cacheWrites") or 0)
        usage_entry = make_llm_usage_entry(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            source_type="cline.task_history",
            source_id=str(task_id),
        )
        usage_summary = summarize_usage_entries([usage_entry] if usage_entry is not None else [], fallback_model=model)

        try:
            created_at = _ms_to_dt(int(task_id))
        except (ValueError, OSError):
            created_at = _utcnow()

        trace = Trace(
            id=f"cline-{task_id}",
            session_id=task_id,
            agent="atelier:code",
            host="cline",
            domain="coding",
            task=task_text,
            status="success",
            files_touched=files_touched,
            tools_called=[
                ToolCall(
                    name=name,
                    args_hash="",
                    count=count,
                    input_tokens=0,
                    output_tokens=0,
                )
                for name, count in tools_called_counts.items()
            ],
            commands_run=cast(Any, commands_run),
            errors_seen=[],
            validation_results=[],
            reasoning=[],
            raw_artifact_ids=[artifact_id],
            input_tokens=usage_summary["input_tokens"],
            output_tokens=usage_summary["output_tokens"],
            cached_input_tokens=usage_summary["cached_input_tokens"],
            cache_creation_input_tokens=usage_summary["cache_creation_input_tokens"],
            thinking_tokens=0,
            model=usage_summary["model"],
            usage_entries=usage_summary["usage_entries"],
            model_usages=usage_summary["model_usages"],
            created_at=created_at,
        )
        self.store.record_trace(trace, write_json=False)
        return trace.id
