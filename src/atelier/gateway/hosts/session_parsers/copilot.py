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
import re
import traceback as _traceback
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import (
    _SIZE_LIMIT_BYTES,
    make_llm_usage_entry,
    summarize_usage_entries,
)

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


def _is_placeholder_model(value: Any) -> bool:
    model = _text_from_value(value)
    return not model or model == "auto"


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


_TRANSCRIPT_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:[\\/]|/)[^\s\"'`<>()\[\]{}]+)")
_MAX_TRANSCRIPT_PARENT_DELTA = timedelta(hours=8)


def _iter_nested_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_nested_strings(nested)
        return
    if isinstance(value, list):
        for nested in value:
            yield from _iter_nested_strings(nested)


def _normalize_match_path(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    if not normalized:
        return "/"
    if normalized.startswith("/"):
        return "/" + "/".join(part for part in normalized.split("/") if part)
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].upper()
        tail = "/" + "/".join(part for part in normalized[2:].split("/") if part)
        return f"{drive}:{tail}"
    return normalized


def _extract_absolute_paths_from_text(text: str) -> set[str]:
    paths: set[str] = set()
    for match in _TRANSCRIPT_PATH_RE.finditer(text):
        candidate = match.group("path").rstrip('.,:;)]}"')
        if not candidate:
            continue
        paths.add(_normalize_match_path(candidate))
    return paths


def _extract_transcript_linkage(redacted_events: str) -> tuple[set[str], datetime | None]:
    transcript_paths: set[str] = set()
    start_time: datetime | None = None

    for line in redacted_events.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if start_time is None and event.get("type") == "session.start":
            payload = event.get("data") or {}
            ts = payload.get("startTime") or event.get("timestamp")
            if ts:
                start_time = _parse_workspace_dt(ts)

        for text in _iter_nested_strings(event):
            transcript_paths.update(_extract_absolute_paths_from_text(text))

    return transcript_paths, start_time


def _path_within_workspace(path: str, workspace_path: str) -> bool:
    normalized_path = _normalize_match_path(path).casefold()
    normalized_workspace = _normalize_match_path(workspace_path).casefold()
    return normalized_path == normalized_workspace or normalized_path.startswith(f"{normalized_workspace}/")


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def find_copilot_sessions(root: Path | None = None) -> Iterator[Path]:
    """Yield session directories that contain an events.jsonl file."""
    roots: list[Path] = []
    if root is not None:
        roots = [root]
    else:
        # 1. Standalone Copilot storage
        roots.append(Path("~/.copilot/session-state").expanduser())

        # 2. VSCode workspace storage (Linux, macOS, Windows)
        import os

        paths_to_check: list[Path] = [
            # Linux
            Path("~/.config/Code/User/workspaceStorage").expanduser(),
            Path("~/.config/Code - Insiders/User/workspaceStorage").expanduser(),
            # macOS
            Path("~/Library/Application Support/Code/User/workspaceStorage").expanduser(),
            Path("~/Library/Application Support/Code - Insiders/User/workspaceStorage").expanduser(),
        ]

        # Windows (using %APPDATA%)
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "github-copilot" / "session-state")
            paths_to_check.append(Path(appdata) / "Code" / "User" / "workspaceStorage")
            paths_to_check.append(Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage")

        for vscode_base in paths_to_check:
            if vscode_base.is_dir():
                # Each subdirectory in workspaceStorage is a workspace hash
                try:
                    for ws_dir in vscode_base.iterdir():
                        if ws_dir.is_dir():
                            chat_dir = ws_dir / "GitHub.copilot-chat"
                            if chat_dir.is_dir():
                                roots.append(chat_dir)
                except OSError:
                    continue

    for r in roots:
        if not r.is_dir():
            continue
        try:
            for p in sorted(r.iterdir()):
                if p.is_dir() and (p / "events.jsonl").exists():
                    yield p
        except OSError:
            continue


def find_copilot_transcript_files(root: Path | None = None) -> Iterator[Path]:
    """Yield individual transcript .jsonl files from VSCode Copilot-chat workspaceStorage."""
    if root is not None:
        if root.is_dir():
            yield from sorted(root.glob("*.jsonl"))
        return

    import os

    paths_to_check: list[Path] = [
        # Linux
        Path("~/.config/Code/User/workspaceStorage").expanduser(),
        Path("~/.config/Code - Insiders/User/workspaceStorage").expanduser(),
        # macOS
        Path("~/Library/Application Support/Code/User/workspaceStorage").expanduser(),
        Path("~/Library/Application Support/Code - Insiders/User/workspaceStorage").expanduser(),
    ]

    # Windows
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths_to_check.append(Path(appdata) / "Code" / "User" / "workspaceStorage")
        paths_to_check.append(Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage")

    for vscode_base in paths_to_check:
        if not vscode_base.is_dir():
            continue
        try:
            for ws in sorted(vscode_base.iterdir()):
                transcript_dir = ws / "GitHub.copilot-chat" / "transcripts"
                if transcript_dir.is_dir():
                    yield from sorted(transcript_dir.glob("*.jsonl"))
        except OSError:
            continue


def find_copilot_debug_log_dirs(root: Path | None = None) -> Iterator[Path]:
    """Yield per-session debug-log directories from VSCode Copilot Chat."""
    if root is not None:
        if root.is_dir():
            try:
                for sid_dir in sorted(root.iterdir()):
                    if sid_dir.is_dir() and (sid_dir / "main.jsonl").exists():
                        yield sid_dir
            except OSError:
                pass
        return

    import os

    paths_to_check: list[Path] = [
        # Linux
        Path("~/.config/Code/User/workspaceStorage").expanduser(),
        Path("~/.config/Code - Insiders/User/workspaceStorage").expanduser(),
        # macOS
        Path("~/Library/Application Support/Code/User/workspaceStorage").expanduser(),
        Path("~/Library/Application Support/Code - Insiders/User/workspaceStorage").expanduser(),
    ]

    # Windows
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths_to_check.append(Path(appdata) / "Code" / "User" / "workspaceStorage")
        paths_to_check.append(Path(appdata) / "Code - Insiders" / "User" / "workspaceStorage")

    for vscode_base in paths_to_check:
        if not vscode_base.is_dir():
            continue
        try:
            for ws in sorted(vscode_base.iterdir()):
                debug_root = ws / "GitHub.copilot-chat" / "debug-logs"
                if not debug_root.is_dir():
                    continue
                for sid_dir in sorted(debug_root.iterdir()):
                    if sid_dir.is_dir() and (sid_dir / "main.jsonl").exists():
                        yield sid_dir
        except OSError:
            continue


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

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        """Import all sessions under *root*. Returns the IDs of successfully imported sessions."""
        imported_ids = []
        skipped = 0
        all_sessions = list(find_copilot_sessions(root))
        all_transcripts = list(find_copilot_transcript_files(root))
        all_debug_logs = list(find_copilot_debug_log_dirs(root))
        total = len(all_sessions) + len(all_transcripts) + len(all_debug_logs)
        print(
            f"[atelier] copilot: found {len(all_sessions)} session directories, "
            f"{len(all_transcripts)} transcript files, {len(all_debug_logs)} debug-log directories"
        )

        processed = 0

        # Phase 1: Session Directories (the primary source)
        for session_dir in all_sessions:
            processed += 1
            if processed % 10 == 0:
                print(f"[atelier] copilot: importing {processed}/{total} (sessions)...")
            try:
                sid = self.import_session(session_dir, force=force)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] skipping session {session_dir.name}: {exc}")

        # Pre-index parent traces and workspaces to avoid O(N^2) lookups during transcript linking
        # This is a major optimization for large history imports.
        parent_index = self._build_parent_index()

        # Phase 2: Transcript Files (VSCode-specific chat history)
        for transcript_path in all_transcripts:
            processed += 1
            if processed % 10 == 0:
                print(f"[atelier] copilot: importing {processed}/{total} (transcripts)...")
            try:
                sid = self.import_transcript_file(transcript_path, force=force, parent_index=parent_index)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] skipping transcript {transcript_path.name}: {exc}")

        # Phase 3: Debug Log Directories (telemetry/token counts)
        for debug_log_dir in all_debug_logs:
            processed += 1
            if processed % 10 == 0:
                print(f"[atelier] copilot: importing {processed}/{total} (debug-logs)...")
            try:
                sid = self.import_debug_log_dir(debug_log_dir, force=force)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] skipping debug-log {debug_log_dir.name}: {exc}")

        # Phase 4: Reconciliation (link existing orphans)
        reconciled = self._reconcile_stored_transcripts(parent_index=parent_index)
        for sid in reconciled:
            if sid not in imported_ids:
                imported_ids.append(sid)

        if skipped > 0:
            print(f"[atelier] {skipped} copilot artifacts already imported (skipped by dedup)")
        return imported_ids

    def _build_parent_index(self) -> list[dict[str, Any]]:
        """Pre-index parent traces and their workspace roots for efficient transcript linking."""
        index = []
        # We need Trace objects for their created_at and session_id
        traces = {
            t.session_id: t
            for t in self.store.list_traces(host="copilot", limit=10_000)
            if t.session_id and not t.id.startswith("copilot-transcript-")
        }

        # We need workspace.yaml artifacts for their CWD
        artifacts = self.store.list_raw_artifacts(source="copilot", limit=10_000)
        for art in artifacts:
            if art.kind != "workspace.yaml":
                continue
            parent_trace = traces.get(art.source_session_id)
            if not parent_trace:
                continue

            try:
                content = self.store.read_raw_artifact_content(art)
                workspace_data = yaml.safe_load(content) or {}
                cwd = _text_from_value(workspace_data.get("cwd"))
                if cwd:
                    index.append(
                        {
                            "trace": parent_trace,
                            "cwd": cwd,
                            "normalized_cwd": _normalize_match_path(cwd).casefold(),
                        }
                    )
            except Exception:
                continue
        return index

    def _reconcile_stored_transcripts(self, parent_index: list[dict[str, Any]] | None = None) -> list[str]:
        imported_ids: list[str] = []
        artifacts = self.store.list_raw_artifacts(source="copilot", limit=10_000)
        p_index = parent_index if parent_index is not None else self._build_parent_index()

        for artifact in artifacts:
            if not artifact.content_path.startswith("raw/copilot/transcripts/"):
                continue

            session_id = artifact.source_session_id
            if not session_id:
                continue

            try:
                redacted_events = self.store.read_raw_artifact_content(artifact)
            except OSError:
                self.store.delete_trace(artifact.id)
                continue

            source_path = str(getattr(artifact, "source_path", "") or "").strip()
            source_exists = bool(source_path) and Path(source_path).exists()

            # Already joined in a previous import run — skip expensive re-materialization
            # only while the source transcript still exists on disk.
            if self.store.trace_exists(artifact.id) and source_exists:
                imported_ids.append(artifact.id)
                continue

            sid = self._materialize_transcript_trace(
                session_id=session_id,
                redacted_events=redacted_events,
                artifact_id=artifact.id,
                parent_index=p_index,
            )
            if sid:
                imported_ids.append(sid)

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

        compaction_count = 0
        shutdown_entries: list[Any] = []
        compaction_entries: list[Any] = []
        assistant_turn_entries: list[Any] = []
        tool_in_tokens: dict[str, int] = {}
        tool_out_tokens: dict[str, int] = {}
        tool_call_in_buffer: dict[str, dict[str, Any]] = {}
        fallback_model = ""
        user_prompt_tokens = 0
        assistant_output_tokens = 0
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

            if etype == "session.compaction_complete":
                compaction_count += 1

            if etype == "session.start":
                fallback_model = (ev.get("data") or {}).get("selectedModel") or fallback_model
                if start_time is None:
                    ts = (ev.get("data") or {}).get("startTime") or ev.get("timestamp")
                    if ts:
                        start_time = _parse_workspace_dt(ts)

            if etype == "session.model_change":
                fallback_model = (ev.get("data") or {}).get("newModel") or fallback_model

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
                message_model = data.get("model")
                if _is_placeholder_model(fallback_model) and not _is_placeholder_model(message_model):
                    fallback_model = _text_from_value(message_model)
                turn_model = (
                    _text_from_value(message_model) if not _is_placeholder_model(message_model) else fallback_model
                )
                reasoning = data.get("reasoningText") or data.get("reasoningOpaque") or ""
                if reasoning and len(str(reasoning)) > 10:
                    reasoning_snippets.append(str(reasoning)[:500])
                out_t = int(data.get("outputTokens", 0) or 0)
                assistant_output_tokens += out_t
                # Emit a per-turn LLM usage entry so the trace records *which* model
                # produced each turn's output. Copilot's assistant.message payload
                # has no input/cache fields, only outputTokens — that's a Copilot
                # limitation, not an Atelier one.
                if out_t > 0:
                    turn_entry = make_llm_usage_entry(
                        model=turn_model,
                        output_tokens=out_t,
                        source_type="copilot.assistant.message",
                        source_id=str(data.get("messageId") or ev.get("id") or ""),
                        created_at=_parse_workspace_dt(ev.get("timestamp")) if ev.get("timestamp") else None,
                    )
                    if turn_entry is not None:
                        assistant_turn_entries.append(turn_entry)
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
                if _is_placeholder_model(fallback_model) and not _is_placeholder_model(data.get("model")):
                    fallback_model = _text_from_value(data.get("model"))
                t_id = data.get("toolCallId")
                if t_id and t_id in tool_call_in_buffer:
                    buf = tool_call_in_buffer.pop(t_id)
                    metrics = (data.get("toolTelemetry") or {}).get("metrics") or {}
                    tool_out_t = int(metrics.get("resultForLlmLength", 0) or 0) // 4
                    tn = buf["name"] or "unknown"
                    tool_in_tokens[tn] = tool_in_tokens.get(tn, 0) + buf["in_t"]
                    tool_out_tokens[tn] = tool_out_tokens.get(tn, 0) + tool_out_t

            elif etype == "session.compaction_complete":
                # Each compaction is its own LLM call with full input/output/cache
                # metrics. It is NOT a duplicate of the assistant.message turns —
                # compaction is a separate request that summarises history.
                data = ev.get("data") or {}
                compaction = data.get("compactionTokensUsed") or {}
                if isinstance(compaction, dict):
                    cmodel = compaction.get("model") or fallback_model
                    if _is_placeholder_model(fallback_model) and not _is_placeholder_model(cmodel):
                        fallback_model = _text_from_value(cmodel)
                    compaction_entry = make_llm_usage_entry(
                        model=_text_from_value(cmodel) if not _is_placeholder_model(cmodel) else fallback_model,
                        input_tokens=int(compaction.get("inputTokens", 0) or 0),
                        output_tokens=int(compaction.get("outputTokens", 0) or 0),
                        cached_input_tokens=int(compaction.get("cacheReadTokens", 0) or 0),
                        cache_creation_input_tokens=int(compaction.get("cacheWriteTokens", 0) or 0),
                        thinking_tokens=int(compaction.get("reasoningTokens", 0) or 0),
                        source_type="copilot.session.compaction_complete",
                        source_id=str(ev.get("id") or ev.get("timestamp") or ""),
                        created_at=_parse_workspace_dt(ev.get("timestamp")) if ev.get("timestamp") else None,
                    )
                    if compaction_entry is not None:
                        compaction_entries.append(compaction_entry)

            elif etype == "session.shutdown":
                mm = (ev.get("data") or {}).get("modelMetrics") or {}
                if isinstance(mm, dict):
                    for mname, mdata in mm.items():
                        usage = (mdata or {}).get("usage") or {}
                        usage_entry = make_llm_usage_entry(
                            model=mname,
                            input_tokens=int(usage.get("inputTokens", 0) or 0),
                            output_tokens=int(usage.get("outputTokens", 0) or 0),
                            cached_input_tokens=int(usage.get("cacheReadTokens", 0) or 0),
                            cache_creation_input_tokens=int(usage.get("cacheWriteTokens", 0) or 0),
                            thinking_tokens=int(usage.get("reasoningTokens", 0) or 0),
                            source_type="copilot.session.shutdown",
                            source_id=str(ev.get("id") or ev.get("timestamp") or ""),
                            created_at=_parse_workspace_dt(ev.get("timestamp")) if ev.get("timestamp") else None,
                        )
                        if usage_entry is not None:
                            shutdown_entries.append(usage_entry)

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

        # Resolve which usage source to bill from. Order of preference:
        #   1. session.shutdown.modelMetrics — Copilot's authoritative cumulative
        #      totals (includes both assistant turns AND compactions). Use ALONE.
        #   2. compaction + per-turn assistant entries — independent layers, safe
        #      to sum together.
        #   3. char/4 fallback over user_prompt + assistant_output as a last resort
        #      so an analytic doesn't show $0 for a real session.
        if shutdown_entries:
            usage_entries = list(shutdown_entries)
        else:
            usage_entries = [*compaction_entries, *assistant_turn_entries]

        if not usage_entries and assistant_output_tokens > 0:
            fallback_entry = make_llm_usage_entry(
                model=fallback_model,
                input_tokens=user_prompt_tokens,
                output_tokens=assistant_output_tokens,
                source_type="copilot.assistant_fallback",
            )
            if fallback_entry is not None:
                usage_entries.append(fallback_entry)

        usage_summary = summarize_usage_entries(usage_entries, fallback_model=fallback_model)

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
            "input_tokens": usage_summary["input_tokens"],
            "output_tokens": usage_summary["output_tokens"],
            "cached_input_tokens": usage_summary["cached_input_tokens"],
            "cache_creation_input_tokens": usage_summary["cache_creation_input_tokens"],
            "thinking_tokens": usage_summary["thinking_tokens"],
            "model": usage_summary["model"],
            "usage_entries": usage_summary["usage_entries"],
            "model_usages": usage_summary["model_usages"],
            "user_prompt_tokens": user_prompt_tokens,
            "compaction_count": compaction_count,
        }

    def import_session(self, session_dir: Path, *, force: bool = False) -> str | None:
        """Import a single session directory. Returns trace ID on success."""
        session_id = session_dir.name

        # --- events ---
        events_path = session_dir / "events.jsonl"
        if not events_path.exists():
            return None

        # Size check for massive sessions
        try:
            size = events_path.stat().st_size
            if size > _SIZE_LIMIT_BYTES:
                print(f"[atelier] copilot: skipping massive session {session_id} ({size / 1e6:.1f}MB)")
                return None
        except OSError:
            pass

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
        workspace_cwd = _text_from_value(workspace_data.get("cwd")) or None

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

        telemetry = {
            "compaction_count": state["compaction_count"],
        }
        if machine_id := workspace_data.get("machine_id"):
            telemetry["machine_id"] = str(machine_id)
        if vscode_ver := workspace_data.get("vscode_version"):
            telemetry["vscode_version"] = str(vscode_ver)

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
            usage_entries=state["usage_entries"],
            model_usages=state["model_usages"],
            workspace_path=workspace_cwd,
            telemetry=telemetry,
            created_at=_parse_workspace_dt(workspace_data.get("created_at")),
        )
        self.store.record_trace(trace, write_json=False)
        return trace.id

    def _find_parent_trace_for_transcript(
        self,
        transcript_paths: set[str],
        transcript_started_at: datetime | None,
        parent_index: list[dict[str, Any]] | None = None,
    ) -> tuple[Trace, str] | None:
        if transcript_started_at is None or not transcript_paths:
            return None

        p_index = parent_index if parent_index is not None else self._build_parent_index()

        max_delta_seconds = _MAX_TRANSCRIPT_PARENT_DELTA.total_seconds()
        best_match: tuple[tuple[int, float], Trace, str] | None = None

        for entry in p_index:
            parent_trace = entry["trace"]
            workspace_cwd = entry["cwd"]
            normalized_cwd = entry["normalized_cwd"]

            if not any(_path_within_workspace(path, workspace_cwd) for path in transcript_paths):
                continue

            delta_seconds = abs((parent_trace.created_at - transcript_started_at).total_seconds())
            if delta_seconds > max_delta_seconds:
                continue

            score = (len(normalized_cwd), -delta_seconds)
            if best_match is None or score > best_match[0]:
                best_match = (score, parent_trace, workspace_cwd)

        if best_match is None:
            return None
        return best_match[1], best_match[2]

    def import_transcript_file(
        self, transcript_path: Path, *, force: bool = False, parent_index: list[dict[str, Any]] | None = None
    ) -> str | None:
        """Import a single VSCode Copilot transcript .jsonl file."""
        session_id = transcript_path.stem

        # Size check
        try:
            size = transcript_path.stat().st_size
            if size > _SIZE_LIMIT_BYTES:
                print(f"[atelier] copilot: skipping massive transcript {session_id} ({size / 1e6:.1f}MB)")
                return None
        except OSError:
            pass

        artifact_id = f"copilot-transcript-{session_id}"
        existing = self.store.get_raw_artifact(artifact_id)
        try:
            file_mtime = datetime.fromtimestamp(transcript_path.stat().st_mtime, tz=UTC)
        except OSError:
            file_mtime = _utcnow()
        redacted_events: str | None = None
        if not force and existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
            # File unchanged — skip join entirely if the trace is already linked.
            if self.store.trace_exists(artifact_id):
                return artifact_id
            try:
                redacted_events = self.store.read_raw_artifact_content(existing)
            except OSError:
                redacted_events = None

        if redacted_events is None:
            if not transcript_path.exists():
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

        return self._materialize_transcript_trace(
            session_id=session_id,
            redacted_events=redacted_events,
            artifact_id=artifact_id,
            parent_index=parent_index,
        )

    # ------------------------------------------------------------------
    # VSCode Copilot Chat debug-logs (per-LLM-call telemetry)
    # ------------------------------------------------------------------

    def import_debug_log_dir(self, debug_log_dir: Path, *, force: bool = False) -> str | None:
        """Import a single ``debug-logs/<sid>/`` directory as one Trace per UTC day.

        Concatenates every ``*.jsonl`` in the directory (main + subagent +
        title) and harvests ``type:"llm_request"`` events into UsageEntry
        records. Subagent / title files are tagged with distinct
        ``source_type`` values so consumers can tell them apart at analysis
        time.

        Long-running chats span multiple UTC days; this importer **partitions
        events by their UTC date** and emits one Trace per ``(session_id,
        date)`` pair so day-level dashboards bucket each event in the correct
        window. Without this split, every event in a multi-day chat would
        land in the day the session *started*, hiding today's activity.

        Returns the trace id of the most-recent day's trace, or ``None`` when
        the directory has no billable events at all.
        """
        session_id = debug_log_dir.name
        artifact_id = f"copilot-debug-log-{session_id}"
        main_path = debug_log_dir / "main.jsonl"
        if not main_path.exists():
            return None

        try:
            file_mtime = datetime.fromtimestamp(max(p.stat().st_mtime for p in debug_log_dir.glob("*.jsonl")), tz=UTC)
        except (OSError, ValueError):
            file_mtime = _utcnow()

        if not force:
            existing = self.store.get_raw_artifact(artifact_id)
            if existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
                return None

        # Concatenate every jsonl in the dir so a single raw artifact mirrors
        # the directory contents — keeps redaction + dedup simple.
        chunks: list[tuple[str, str]] = []  # (source_kind, content)
        for jsonl_path in sorted(debug_log_dir.glob("*.jsonl")):
            try:
                content = jsonl_path.read_text(encoding="utf-8")
            except OSError:
                continue
            chunks.append((jsonl_path.name, content))
        if not chunks:
            return None

        combined_raw = "\n".join(content for _, content in chunks)
        redacted = redact(combined_raw)
        raw_bytes = combined_raw.encode("utf-8")
        redacted_bytes = redacted.encode("utf-8")

        artifact = RawArtifact(
            id=artifact_id,
            source="copilot",
            source_session_id=session_id,
            kind="vscode-copilot-chat.debug-logs",
            relative_path=debug_log_dir.name,
            content_path=f"raw/copilot/vscode-debug-logs/{session_id}.jsonl",
            sha256_original=hashlib.sha256(raw_bytes).hexdigest(),
            sha256_redacted=hashlib.sha256(redacted_bytes).hexdigest(),
            byte_count_original=len(raw_bytes),
            byte_count_redacted=len(redacted_bytes),
            created_at=_utcnow(),
            source_file_mtime=file_mtime,
            source_path=str(debug_log_dir),
        )
        self.store.record_raw_artifact(artifact, redacted)

        # Parse llm_request events into UsageEntries, partitioned by UTC date
        # so a chat that spans multiple days bills correctly to each day.
        # Bucket key: ISO date string YYYY-MM-DD.
        from collections import defaultdict

        per_day: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "usage_entries": [],
                "tools_called": {},
                "user_prompt_tokens": 0,
                "first_ts": None,
                "task": "",
            }
        )
        for source_kind, content in chunks:
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_ms = ev.get("ts")
                ev_dt: datetime | None = None
                if isinstance(ts_ms, (int, float)) and ts_ms > 0:
                    ev_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
                day_key = ev_dt.date().isoformat() if ev_dt else "unknown"
                bucket = per_day[day_key]
                if ev_dt and (bucket["first_ts"] is None or ev_dt < bucket["first_ts"]):
                    bucket["first_ts"] = ev_dt
                etype = ev.get("type")
                if etype == "user_message":
                    text = _text_from_value((ev.get("attrs") or {}).get("text") or ev.get("name") or "")
                    if text:
                        bucket["user_prompt_tokens"] += max(1, len(text) // 4)
                        if not bucket["task"]:
                            bucket["task"] = text[:200]
                    continue
                if etype == "tool_call":
                    name = _text_from_value((ev.get("attrs") or {}).get("toolName") or ev.get("name") or "")
                    if name:
                        bucket["tools_called"][name] = bucket["tools_called"].get(name, 0) + 1
                    continue
                if etype != "llm_request":
                    continue
                attrs = ev.get("attrs") or {}
                raw_model = _text_from_value(attrs.get("model"))
                in_t = _int_or_none(attrs.get("inputTokens")) or 0
                out_t = _int_or_none(attrs.get("outputTokens")) or 0
                if in_t == 0 and out_t == 0 and not raw_model:
                    continue
                # GitHub Copilot is a subscription product ($19/mo, with the
                # underlying OpenAI/Anthropic calls included in the plan).
                # Billing the raw token counts at the upstream API's per-token
                # rate would massively overstate cost (>$200/day at gpt-5
                # rates). We namespace these models as ``copilot/<model>`` so
                # they price via dedicated zero-cost entries in pricing.py,
                # mirroring how ``opencode/big-pickle`` is handled. Users who
                # want a non-zero rate can call ``override_pricing``.
                model = f"copilot/{raw_model}" if raw_model else ""
                source_type = "copilot.vscode_chat.llm_request"
                if source_kind != "main.jsonl":
                    source_type = f"copilot.vscode_chat.{source_kind.replace('.jsonl', '')}.llm_request"
                entry = make_llm_usage_entry(
                    model=model,
                    input_tokens=in_t,
                    output_tokens=out_t,
                    source_type=source_type,
                    source_id=str(ev.get("spanId") or ev.get("ts") or ""),
                    created_at=ev_dt,
                )
                if entry is not None:
                    bucket["usage_entries"].append(entry)

        last_trace_id: str | None = None
        for day_key, bucket in sorted(per_day.items()):
            if not bucket["usage_entries"]:
                continue
            day_artifact_id = f"copilot-debug-log-{session_id}-{day_key}"
            usage_summary = summarize_usage_entries(bucket["usage_entries"])
            trace = Trace(
                id=day_artifact_id,
                session_id=session_id,
                agent="atelier:code",
                host="copilot",
                domain="coding",
                task=bucket["task"] or "vscode copilot chat session",
                status="success",
                files_touched=[],
                tools_called=[
                    ToolCall(name=n, args_hash="", count=c) for n, c in sorted(bucket["tools_called"].items())
                ],
                commands_run=[],
                errors_seen=[],
                validation_results=[],
                reasoning=[],
                raw_artifact_ids=[artifact_id],
                input_tokens=usage_summary["input_tokens"],
                user_prompt_tokens=bucket["user_prompt_tokens"],
                output_tokens=usage_summary["output_tokens"],
                thinking_tokens=usage_summary["thinking_tokens"],
                cached_input_tokens=usage_summary["cached_input_tokens"],
                cache_creation_input_tokens=usage_summary["cache_creation_input_tokens"],
                model=usage_summary["model"],
                usage_entries=usage_summary["usage_entries"],
                model_usages=usage_summary["model_usages"],
                created_at=bucket["first_ts"] or file_mtime,
            )
            self.store.record_trace(trace, write_json=False)
            last_trace_id = trace.id

        return last_trace_id

    def _materialize_transcript_trace(
        self,
        *,
        session_id: str,
        redacted_events: str,
        artifact_id: str,
        parent_index: list[dict[str, Any]] | None = None,
    ) -> str | None:
        transcript_paths, transcript_started_at = _extract_transcript_linkage(redacted_events)
        parent_match = self._find_parent_trace_for_transcript(
            transcript_paths, transcript_started_at, parent_index=parent_index
        )
        if parent_match is None:
            self.store.delete_trace(artifact_id)
            return None

        parent_trace, workspace_path = parent_match

        state = self._parse_events_to_trace_state(redacted_events)

        trace = Trace(
            id=artifact_id,
            session_id=parent_trace.session_id,
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
            input_tokens=0,
            user_prompt_tokens=0,
            output_tokens=0,
            thinking_tokens=0,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            model="",
            usage_entries=[],
            model_usages=[],
            workspace_path=workspace_path,
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
                    pass  # search patterns are not file edits — skip

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
