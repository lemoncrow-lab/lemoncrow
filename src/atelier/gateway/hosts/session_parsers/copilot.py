"""Copilot session-state importer for Atelier.

Converts ~/.copilot/session-state/ artifacts into:

- **Redacted RawArtifacts** — the full session files (events.jsonl,
  workspace.yaml) stored verbatim after Atelier redaction.  Nothing is
  thrown away except secrets/PII that the redactor strips.
- **Curated Atelier Traces** — compact, retrieval-friendly summaries linked
  back to the raw artifacts via ``raw_artifact_ids``.

Lookup path:
    agent → curated Trace (fast, context-window-friendly)
    human → RawArtifact content (full detail for audit / debugging)
"""

from __future__ import annotations

import hashlib
import json
import traceback as _traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from atelier.core.foundation.models import (
    CommandRecord,
    FileEditRecord,
    RawArtifact,
    ToolCall,
    Trace,
    ValidationResult,
)
from atelier.core.foundation.redaction import redact
from atelier.core.foundation.store import ReasoningStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _text_from_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value).strip()


def _extract_first_text(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    *,
    limit: int | None = None,
) -> str:
    for key in keys:
        if key not in payload:
            continue
        text = _text_from_value(payload.get(key))
        if text:
            return text[:limit] if limit is not None else text
    return ""


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_workspace_dt(val: Any) -> datetime:
    """Parse a workspace.yaml timestamp into a timezone-aware datetime."""
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            return _utcnow()
    else:
        return _utcnow()
    # yaml.safe_load may return naive datetimes — make tz-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def find_copilot_sessions(root: Path | None = None) -> Iterator[Path]:
    """Yield session directories that contain an events.jsonl file."""
    roots: list[Path]
    if root is not None:
        roots = [root]
    else:
        roots = [Path("~/.copilot/session-state").expanduser()]
        for vscode_base in [
            Path("~/.config/Code/User/workspaceStorage").expanduser(),
            Path("~/Library/Application Support/Code/User/workspaceStorage").expanduser(),
        ]:
            if vscode_base.is_dir():
                roots.extend(sorted(vscode_base.glob("*/GitHub.copilot-chat")))
    for r in roots:
        if not r.is_dir():
            continue
        for p in sorted(r.iterdir()):
            if p.is_dir() and (p / "events.jsonl").exists():
                yield p


def find_copilot_transcript_files(root: Path | None = None) -> Iterator[Path]:
    """Yield individual transcript .jsonl files from VSCode Copilot-chat workspaceStorage."""
    if root is not None:
        if root.is_dir():
            yield from sorted(root.glob("*.jsonl"))
        return
    for vscode_base in [
        Path("~/.config/Code/User/workspaceStorage").expanduser(),
        Path("~/Library/Application Support/Code/User/workspaceStorage").expanduser(),
    ]:
        if not vscode_base.is_dir():
            continue
        for ws in sorted(vscode_base.iterdir()):
            transcript_dir = ws / "GitHub.copilot-chat" / "transcripts"
            if transcript_dir.is_dir():
                yield from sorted(transcript_dir.glob("*.jsonl"))


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class CopilotImporter:
    """Loss-preserving importer.

    For every Copilot session:
    1. Write **redacted raw artifacts** (events.jsonl + workspace.yaml) into
       ``<store_root>/raw/copilot/<session_id>/``.  The SHA-256 of both the
       original and the redacted form are recorded so you can verify nothing
       was silently lost.
    2. Parse the *redacted* events into a compact Atelier ``Trace`` whose
       ``raw_artifact_ids`` field links back to step 1.

    No data is discarded beyond what Atelier's redactor strips (secrets,
    API keys, PII).
    """

    def __init__(self, store: ReasoningStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        """Import all sessions under *root*. Returns the IDs of successfully imported sessions."""
        imported_ids = []
        skipped = 0
        all_sessions = list(find_copilot_sessions(root))
        all_transcripts = list(find_copilot_transcript_files())
        total = len(all_sessions) + len(all_transcripts)
        print(
            f"[atelier] copilot: discovering sessions (found {len(all_sessions)} directory, {len(all_transcripts)} transcript)"
        )
        for i, session_dir in enumerate(all_sessions):
            try:
                if i % 10 == 0 and i > 0:
                    print(f"[atelier] copilot: importing {i}/{total}...")
                sid = self.import_session(session_dir, force=force)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] skipping session {session_dir.name}: {exc}")
        for transcript_path in all_transcripts:
            try:
                sid = self.import_transcript_file(transcript_path, force=force)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] skipping transcript {transcript_path.name}: {exc}")
        if skipped > 0:
            print(f"[atelier] {skipped} sessions already imported (skipped by dedup)")
        return imported_ids

    def _parse_events_to_trace_state(self, redacted_events: str, initial_task: str = "") -> dict[str, Any]:
        """Parse event JSONL text and return accumulated state for building a Trace."""
        tools_called: dict[str, int] = {}
        tool_args: dict[str, dict[str, Any] | None] = {}
        tool_results: dict[str, str] = {}
        files_touched: dict[str, FileEditRecord | None] = {}
        errors_seen: set[str] = set()
        commands_run: list[str | CommandRecord] = []
        command_indices: dict[str, list[int]] = {}
        command_tools: dict[str, str] = {}
        validation_results: list[ValidationResult] = []
        reasoning_snippets: list[str] = []
        task = initial_task or "untitled copilot session"
        last_model_metrics: dict[str, dict[str, int]] = {}
        tool_in_tokens: dict[str, int] = {}
        tool_out_tokens: dict[str, int] = {}
        tool_call_in_buffer: dict[str, dict[str, Any]] = {}
        fallback_model = ""
        user_prompt_tokens = 0
        start_time: datetime | None = None

        for line in redacted_events.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = ev.get("type")

            if etype == "session.start":
                fallback_model = (ev.get("data") or {}).get("selectedModel") or fallback_model
                if start_time is None:
                    ts = (ev.get("data") or {}).get("startTime") or ev.get("timestamp")
                    if ts:
                        start_time = _parse_workspace_dt(ts)

            if etype == "user.message":
                data = ev.get("data") or {}
                content = data.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                if text:
                    user_prompt_tokens += max(1, len(text) // 4)
                    if task == "untitled copilot session" or (task.startswith("Read and follow") and len(text) > 20):
                        task = text[:200]

            if etype == "assistant.message":
                data = ev.get("data") or {}
                reasoning = data.get("reasoningText") or data.get("reasoningOpaque") or ""
                if reasoning and len(str(reasoning)) > 10:
                    reasoning_snippets.append(str(reasoning)[:500])
                out_t = int(data.get("outputTokens", 0) or 0)
                calls = data.get("toolRequests") or []
                if calls and out_t:
                    dist_out = out_t // len(calls)
                    for tool in calls:
                        t_id = tool.get("toolCallId")
                        t_name = tool.get("name")
                        if t_id:
                            tool_call_in_buffer[t_id] = {"in_t": dist_out, "name": t_name}
                        elif t_name:
                            tool_in_tokens[t_name] = tool_in_tokens.get(t_name, 0) + dist_out

            elif etype == "tool.execution_complete":
                data = ev.get("data") or {}
                if not fallback_model and "model" in data:
                    fallback_model = data["model"]
                t_id = data.get("toolCallId")
                if t_id and t_id in tool_call_in_buffer:
                    buf = tool_call_in_buffer.pop(t_id)
                    metrics = (data.get("toolTelemetry") or {}).get("metrics") or {}
                    tool_out_t = int(metrics.get("resultForLlmLength", 0) or 0) // 4
                    tn = buf["name"] or "unknown"
                    tool_in_tokens[tn] = tool_in_tokens.get(tn, 0) + buf["in_t"]
                    tool_out_tokens[tn] = tool_out_tokens.get(tn, 0) + tool_out_t

            elif etype == "session.shutdown":
                mm = (ev.get("data") or {}).get("modelMetrics") or {}
                if isinstance(mm, dict):
                    for mname, mdata in mm.items():
                        usage = (mdata or {}).get("usage") or {}
                        bucket = last_model_metrics.setdefault(
                            mname,
                            {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0},
                        )
                        bucket["in"] += int(usage.get("inputTokens", 0) or 0)
                        bucket["out"] += int(usage.get("outputTokens", 0) or 0)
                        bucket["cache_read"] += int(usage.get("cacheReadTokens", 0) or 0)
                        bucket["cache_write"] += int(usage.get("cacheWriteTokens", 0) or 0)
                        bucket["reasoning"] += int(usage.get("reasoningTokens", 0) or 0)

            self._process_event(
                ev,
                tools_called,
                tool_args,
                tool_results,
                files_touched,
                errors_seen,
                commands_run,
                command_indices,
                command_tools,
                validation_results,
                task,
            )

        for buf in tool_call_in_buffer.values():
            tn = buf["name"]
            if tn:
                tool_in_tokens[tn] = tool_in_tokens.get(tn, 0) + buf["in_t"]

        tot_in = tot_out = tot_cache_read = tot_cache_write = tot_reasoning = 0
        for bucket in last_model_metrics.values():
            tot_in += bucket["in"]
            tot_out += bucket["out"]
            tot_cache_read += bucket["cache_read"]
            tot_cache_write += bucket["cache_write"]
            tot_reasoning += bucket["reasoning"]

        primary_model = ""
        if last_model_metrics:
            primary_model = max(last_model_metrics.items(), key=lambda kv: kv[1]["out"])[0]
        if not primary_model:
            primary_model = fallback_model

        return {
            "task": task,
            "start_time": start_time or _utcnow(),
            "tools_called": tools_called,
            "tool_args": tool_args,
            "tool_results": tool_results,
            "files_touched": files_touched,
            "errors_seen": errors_seen,
            "commands_run": commands_run,
            "validation_results": validation_results,
            "reasoning_snippets": reasoning_snippets,
            "tool_in_tokens": tool_in_tokens,
            "tool_out_tokens": tool_out_tokens,
            "input_tokens": tot_in,
            "output_tokens": tot_out,
            "cached_input_tokens": tot_cache_read,
            "cache_creation_input_tokens": tot_cache_write,
            "thinking_tokens": tot_reasoning,
            "model": primary_model,
            "user_prompt_tokens": user_prompt_tokens,
        }

    def import_session(self, session_dir: Path, *, force: bool = False) -> str | None:
        """Import a single session directory. Returns trace ID on success."""
        session_id = session_dir.name

        # ── Timestamp-based dedup check ──────────────────────────────
        artifact_id = f"copilot-{session_id}-events-jsonl"
        existing = self.store.get_raw_artifact(artifact_id)
        try:
            file_mtime = datetime.fromtimestamp((session_dir / "events.jsonl").stat().st_mtime, tz=UTC)
        except OSError:
            file_mtime = _utcnow()
        if not force and existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
            return None  # unchanged, skip

        # --- workspace metadata ---
        workspace_path = session_dir / "workspace.yaml"
        if not workspace_path.exists():
            return None
        try:
            workspace_data: dict[str, Any] = yaml.safe_load(workspace_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None

        # --- events ---
        events_path = session_dir / "events.jsonl"
        if not events_path.exists():
            return None

        filename_session_id = session_id
        actual_session_id = str(workspace_data.get("mc_session_id") or filename_session_id)

        # ── Step 1: write redacted raw artifacts ─────────────────────────────
        artifact_ids: list[str] = []

        events_raw = events_path.read_text(encoding="utf-8")
        redacted_events = redact(events_raw)
        workspace_raw = workspace_path.read_text(encoding="utf-8")
        redacted_workspace = redact(workspace_raw)

        for filename, raw_content, redacted_content in (
            ("events.jsonl", events_raw, redacted_events),
            ("workspace.yaml", workspace_raw, redacted_workspace),
        ):
            kind = filename
            raw_bytes = raw_content.encode("utf-8")
            redacted_bytes = redacted_content.encode("utf-8")
            artifact = RawArtifact(
                id=f"copilot-{filename_session_id}-{kind.replace('.', '-')}",
                source="copilot",
                source_session_id=actual_session_id,
                kind=kind,
                relative_path=filename,
                content_path=f"raw/copilot/{filename_session_id}/{filename}",
                sha256_original=hashlib.sha256(raw_bytes).hexdigest(),
                sha256_redacted=hashlib.sha256(redacted_bytes).hexdigest(),
                byte_count_original=len(raw_bytes),
                byte_count_redacted=len(redacted_bytes),
                created_at=_utcnow(),
                source_file_mtime=file_mtime,
                source_path=str(session_dir / filename),
            )
            self.store.record_raw_artifact(artifact, redacted_content)
            artifact_ids.append(artifact.id)

        # ── Step 2: build curated Trace from redacted events ─────────────────
        state = self._parse_events_to_trace_state(
            redacted_events,
            initial_task=str(workspace_data.get("summary") or ""),
        )

        trace = Trace(
            id=f"copilot-{filename_session_id}",
            session_id=actual_session_id,
            agent="atelier:code",
            host="copilot",
            domain="coding",
            task=state["task"],
            status="success",
            files_touched=[r if r is not None else p for p, r in sorted(state["files_touched"].items())],
            tools_called=[
                ToolCall(
                    name=n,
                    args_hash="",
                    count=c,
                    args=state["tool_args"].get(n),
                    result_summary=state["tool_results"].get(n, ""),
                    input_tokens=state["tool_in_tokens"].get(n, 0),
                    output_tokens=state["tool_out_tokens"].get(n, 0),
                )
                for n, c in state["tools_called"].items()
            ],
            commands_run=state["commands_run"],
            errors_seen=sorted(state["errors_seen"]),
            validation_results=state["validation_results"],
            reasoning=state["reasoning_snippets"],
            raw_artifact_ids=artifact_ids,
            input_tokens=state["input_tokens"],
            user_prompt_tokens=state["user_prompt_tokens"],
            output_tokens=state["output_tokens"],
            thinking_tokens=state["thinking_tokens"],
            cached_input_tokens=state["cached_input_tokens"],
            cache_creation_input_tokens=state["cache_creation_input_tokens"],
            model=state["model"],
            created_at=_parse_workspace_dt(workspace_data.get("created_at")),
        )
        self.store.record_trace(trace, write_json=False)
        return trace.id

    def import_transcript_file(self, transcript_path: Path, *, force: bool = False) -> str | None:
        """Import a single VSCode Copilot transcript .jsonl file."""
        session_id = transcript_path.stem

        artifact_id = f"copilot-transcript-{session_id}"
        existing = self.store.get_raw_artifact(artifact_id)
        try:
            file_mtime = datetime.fromtimestamp(transcript_path.stat().st_mtime, tz=UTC)
        except OSError:
            file_mtime = _utcnow()
        if not force and existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
            return None

        events_raw = transcript_path.read_text(encoding="utf-8")
        redacted_events = redact(events_raw)
        raw_bytes = events_raw.encode("utf-8")
        redacted_bytes = redacted_events.encode("utf-8")

        artifact = RawArtifact(
            id=artifact_id,
            source="copilot",
            source_session_id=session_id,
            kind="events.jsonl",
            relative_path=transcript_path.name,
            content_path=f"raw/copilot/transcripts/{session_id}.jsonl",
            sha256_original=hashlib.sha256(raw_bytes).hexdigest(),
            sha256_redacted=hashlib.sha256(redacted_bytes).hexdigest(),
            byte_count_original=len(raw_bytes),
            byte_count_redacted=len(redacted_bytes),
            created_at=_utcnow(),
            source_file_mtime=file_mtime,
            source_path=str(transcript_path),
        )
        self.store.record_raw_artifact(artifact, redacted_events)

        state = self._parse_events_to_trace_state(redacted_events)

        trace = Trace(
            id=f"copilot-transcript-{session_id}",
            session_id=session_id,
            agent="atelier:code",
            host="copilot",
            domain="coding",
            task=state["task"],
            status="success",
            files_touched=[r if r is not None else p for p, r in sorted(state["files_touched"].items())],
            tools_called=[
                ToolCall(
                    name=n,
                    args_hash="",
                    count=c,
                    args=state["tool_args"].get(n),
                    result_summary=state["tool_results"].get(n, ""),
                    input_tokens=state["tool_in_tokens"].get(n, 0),
                    output_tokens=state["tool_out_tokens"].get(n, 0),
                )
                for n, c in state["tools_called"].items()
            ],
            commands_run=state["commands_run"],
            errors_seen=sorted(state["errors_seen"]),
            validation_results=state["validation_results"],
            reasoning=state["reasoning_snippets"],
            raw_artifact_ids=[artifact_id],
            input_tokens=state["input_tokens"],
            user_prompt_tokens=state["user_prompt_tokens"],
            output_tokens=state["output_tokens"],
            thinking_tokens=state["thinking_tokens"],
            cached_input_tokens=state["cached_input_tokens"],
            cache_creation_input_tokens=state["cache_creation_input_tokens"],
            model=state["model"],
            created_at=state["start_time"],
        )
        self.store.record_trace(trace, write_json=False)
        return trace.id

    # ------------------------------------------------------------------
    # Event parsing
    # ------------------------------------------------------------------

    def _process_event(
        self,
        ev: dict[str, Any],
        tools_called: dict[str, int],
        tool_args: dict[str, dict[str, Any] | None],
        tool_results: dict[str, str],
        files_touched: dict[str, FileEditRecord | None],
        errors_seen: set[str],
        commands_run: list[str | CommandRecord],
        command_indices: dict[str, list[int]],
        command_tools: dict[str, str],
        validation_results: list[ValidationResult],
        task: str,
    ) -> None:
        etype = ev.get("type", "")
        data: dict[str, Any] = ev.get("data") or {}

        # Copilot: tool.execution_start
        if etype == "tool.execution_start":
            name = data.get("toolName")
            if name:
                name = str(name)
                tools_called[name] = tools_called.get(name, 0) + 1

                args = _as_dict(data.get("arguments"))
                tool_args[name] = args or None

                # Extract files/commands from arguments
                if name in ("edit", "create", "create_thunk"):
                    path = args.get("path") or args.get("file_path") or args.get("filePath")
                    if path:
                        path_str = str(path)
                        diff_text = _extract_first_text(
                            args,
                            ("diff", "patch", "changes", "content", "contents", "input", "text"),
                            limit=4096,
                        )
                        files_touched[path_str] = FileEditRecord(
                            path=path_str,
                            diff=diff_text,
                            event="create" if name.startswith("create") else "edit",
                        )
                        tool_results[name] = (
                            diff_text[:200]
                            if diff_text
                            else _extract_first_text(args, ("path", "file_path", "filePath"), limit=200)
                        )
                elif name == "view":
                    path = args.get("path") or args.get("file_path") or args.get("filePath")
                    if path:
                        files_touched.setdefault(str(path), None)
                elif name in ("bash", "read_bash"):
                    cmd = _extract_first_text(args, ("command", "cmd"), limit=None)
                    if cmd:
                        display_cmd = cmd[:200]
                        idx = len(commands_run)
                        commands_run.append(display_cmd)
                        indices = command_indices.setdefault(cmd, [])
                        if cmd != display_cmd:
                            command_indices.setdefault(display_cmd, indices)
                        indices.append(idx)
                        command_tools[cmd] = name
                        command_tools[display_cmd] = name
                elif name in ("glob", "grep", "rg"):
                    pattern = _extract_first_text(args, ("pattern", "query"), limit=100)
                    if pattern:
                        files_touched.setdefault(f"{name}:{pattern}", None)

        elif etype == "tool_call":
            name = data.get("name")
            if name:
                name = str(name)
                tools_called[name] = tools_called.get(name, 0) + 1
                args = _as_dict(data.get("arguments") or data.get("input"))
                if args:
                    tool_args[name] = args
                result_summary = _extract_first_text(data, ("result_summary", "summary", "output", "result"), limit=200)
                if result_summary:
                    tool_results[name] = result_summary

        elif etype == "command_result":
            cmd = _extract_first_text(data, ("command", "cmd"), limit=None)
            if cmd:
                stdout = _extract_first_text(data, ("stdout", "output", "result"), limit=4096)
                stderr = _extract_first_text(data, ("stderr", "error", "err"), limit=4096)
                exit_code = data.get("exit_code")
                if exit_code is None:
                    exit_code = data.get("code")
                record = CommandRecord(
                    command=cmd,
                    exit_code=_int_or_none(exit_code),
                    stdout=stdout,
                    stderr=stderr,
                )
                command_match_indices = command_indices.get(cmd)
                if not command_match_indices:
                    command_match_indices = command_indices.get(cmd[:200])
                if command_match_indices:
                    idx = command_match_indices.pop(0)
                    commands_run[idx] = record
                    if not command_match_indices:
                        command_indices.pop(cmd, None)
                        command_indices.pop(cmd[:200], None)
                else:
                    commands_run.append(record)
                tool_name = command_tools.get(cmd) or command_tools.get(cmd[:200])
                if tool_name:
                    tool_results[tool_name] = _extract_first_text(
                        {"stdout": stdout, "stderr": stderr, "output": stdout},
                        ("stdout", "stderr", "output"),
                        limit=200,
                    )
            if not data.get("ok"):
                sig = data.get("error_signature")
                if sig:
                    errors_seen.add(str(sig))

        elif etype in ("file_edit", "file_revert"):
            path = data.get("path")
            if path:
                path_str = str(path)
                diff_text = _extract_first_text(
                    data,
                    (
                        "diff",
                        "patch",
                        "changes",
                        "content",
                        "contents",
                        "input",
                        "text",
                        "output",
                        "result",
                    ),
                    limit=4096,
                )
                files_touched[path_str] = FileEditRecord(
                    path=path_str,
                    diff=diff_text,
                    event="revert" if etype == "file_revert" else "edit",
                )

        elif etype == "test_result":
            name = data.get("test_id")
            if name:
                validation_results.append(
                    ValidationResult(
                        name=str(name),
                        passed=bool(data.get("passed")),
                        detail=str(data.get("detail") or ""),
                    )
                )
