"""OpenCode session importer for Atelier.

Converts ``~/.local/share/opencode/opencode.db`` sessions into redacted
RawArtifacts + curated Atelier Traces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import traceback as _traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    make_llm_usage_entry,
    snapshot_edited_files,
    summarize_usage_entries,
)

logger = logging.getLogger(__name__)


def _ms_to_dt(ms: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_opencode_sessions(db_path: Path | None = None) -> list[dict[str, Any]]:
    if db_path is None:
        db_path = Path.home() / ".local/share/opencode/opencode.db"

    if not db_path.exists():
        return []

    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM session ORDER BY time_created DESC").fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        _traceback.print_exc()
        return []


class OpenCodeImporter:
    """OpenCode session importer."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, db_path: Path | None = None, *, force: bool = False) -> list[str]:
        resolved_db_path = db_path or (Path.home() / ".local/share/opencode/opencode.db")
        if not resolved_db_path.exists():
            return []

        all_sessions = list(find_opencode_sessions(resolved_db_path))
        logger.info("[atelier] opencode: discovering sessions (found %d)", len(all_sessions))
        imported_ids = []
        for i, session_row in enumerate(all_sessions):
            if i % 10 == 0 and i > 0:
                logger.info("[atelier] opencode: importing %d/%d...", i, len(all_sessions))
            tid = self._import_session(session_row, resolved_db_path, force=force)
            if tid:
                imported_ids.append(tid)

        return imported_ids

    def _import_session(self, session_row: dict[str, Any], db_path: Path, *, force: bool = False) -> str | None:
        session_id: str = session_row["id"]
        artifact_id = f"opencode-{session_id}"
        existing = self.store.get_raw_artifact(artifact_id)
        session_mtime = _ms_to_dt(session_row.get("time_created"))

        if not force and existing and existing.source_file_mtime and session_mtime <= existing.source_file_mtime:
            return None

        raw_content = self._serialize_session(session_id, db_path)
        redacted = redact(raw_content)

        artifact = RawArtifact(
            id=artifact_id,
            source="opencode",
            source_session_id=session_id,
            kind="session.jsonl",
            relative_path=f"{session_id}.jsonl",
            content_path=f"raw/opencode/{session_id}.jsonl",
            sha256_original=_sha256(raw_content),
            sha256_redacted=_sha256(redacted),
            byte_count_original=len(raw_content.encode("utf-8")),
            byte_count_redacted=len(redacted.encode("utf-8")),
            created_at=_utcnow(),
            source_file_mtime=session_mtime,
            source_path=str(db_path),
        )
        self.store.record_raw_artifact(artifact, redacted)

        tools_called: dict[str, int] = {}
        tool_in_tokens: dict[str, int] = {}
        tool_out_tokens: dict[str, int] = {}
        files_touched: dict[str, FileEditRecord] = {}
        commands_run: list[str | CommandRecord] = []
        reasoning_snippets: list[str] = []

        def _compute_diff(fp: str, inp: dict[str, Any]) -> str:
            """Synthesize a diff from tool input arguments.

            Returns a unified diff for edit tools (old_string → new_string),
            or the raw content for write tools (consistent with _common.py).
            """
            raw_diff = inp.get("diff") or inp.get("patch")
            if raw_diff:
                return str(raw_diff).strip()
            old = str(inp.get("old_string") or "")
            new = str(inp.get("new_string") or "")
            if old or new:
                diff_lines = [f"--- a/{fp}", f"+++ b/{fp}"]
                for line in old.splitlines():
                    diff_lines.append(f"-{line}")
                for line in new.splitlines():
                    diff_lines.append(f"+{line}")
                return "\n".join(diff_lines)
            return str(inp.get("content") or "").strip()

        total_in = 0
        total_out = 0
        total_reason = 0
        total_cache_read = 0
        total_cache_write = 0
        model_seen = ""
        user_prompt_tokens = 0
        curr_tool_calls: list[tuple[str, dict[str, Any]]] = []
        usage_entries = []
        seen_event_ids: set[str] = set()
        previous_unidentified_event = ""

        for line in redacted.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue

            etype = ev.get("_type")
            event_id = str(ev.get("id") or "").strip()
            if event_id:
                event_identity = f"{etype}:{event_id}"
                if event_identity in seen_event_ids:
                    continue
                seen_event_ids.add(event_identity)
                previous_unidentified_event = ""
            elif line == previous_unidentified_event:
                continue
            else:
                previous_unidentified_event = line
            data = ev.get("data") or {}

            if etype == "message":
                if data.get("role") == "assistant":
                    mid = data.get("modelID") or data.get("model")
                    pid = data.get("providerID")
                    if mid:
                        model_seen = f"{pid}/{mid}" if pid else str(mid)

            elif etype == "part":
                role = ev.get("role")
                ptype = data.get("type")

                if role == "user" and ptype == "text" and not data.get("synthetic"):
                    # THIS IS ACTUAL HUMAN INPUT
                    txt = str(data.get("text", "")).strip()
                    if txt:
                        user_prompt_tokens += max(1, len(txt) // 4)

                if ptype == "tool":
                    tool_name = str(data.get("tool", "unknown"))
                    state_inp = (data.get("state") or {}).get("input") or {}
                    curr_tool_calls.append((tool_name, state_inp))
                    cmd = str(state_inp.get("command", "") or state_inp.get("cmd", "")).strip()
                    if cmd:
                        commands_run.append(cmd[:200])
                    # OpenCode tool inputs use camelCase filePath
                    fp = state_inp.get("filePath") or state_inp.get("file_path") or state_inp.get("path")
                    # Only record actual files (not bare directories)
                    if fp and "." in str(fp).rsplit("/", 1)[-1]:
                        fpath_str = str(fp)
                        if fpath_str not in files_touched:
                            diff_text = _compute_diff(fpath_str, state_inp)
                            files_touched[fpath_str] = FileEditRecord(
                                path=fpath_str,
                                diff=diff_text[:4096],
                                event="edit",
                            )
                elif ptype == "step-finish":
                    ts_tok = data.get("tokens") or {}
                    in_t = int(ts_tok.get("input", 0) or 0)
                    out_t = int(ts_tok.get("output", 0) or 0)
                    cache = ts_tok.get("cache") or {}
                    cache_r = int(cache.get("read", 0) or 0)
                    cache_w = int(cache.get("write", 0) or 0)

                    total_in += in_t
                    total_out += out_t
                    total_reason += int(ts_tok.get("reasoning", 0) or 0)
                    total_cache_read += cache_r
                    total_cache_write += cache_w

                    usage_entry = make_llm_usage_entry(
                        model=model_seen,
                        input_tokens=in_t,
                        output_tokens=out_t,
                        thinking_tokens=int(ts_tok.get("reasoning", 0) or 0),
                        cached_input_tokens=cache_r,
                        cache_creation_input_tokens=cache_w,
                        source_type="opencode.step_finish",
                        source_id=event_id or _sha256(line)[:16],
                        created_at=_ms_to_dt(ev.get("time_created")),
                    )
                    if usage_entry is not None:
                        usage_entries.append(usage_entry)

                    if curr_tool_calls:
                        dist_in = (in_t + cache_r + cache_w) // len(curr_tool_calls)
                        dist_out = out_t // len(curr_tool_calls)
                        for t_name, _t_args in curr_tool_calls:
                            tools_called[t_name] = tools_called.get(t_name, 0) + 1
                            tool_in_tokens[t_name] = tool_in_tokens.get(t_name, 0) + dist_out
                            tool_out_tokens[t_name] = tool_out_tokens.get(t_name, 0) + dist_in
                        curr_tool_calls = []

        usage_summary = summarize_usage_entries(usage_entries, fallback_model=model_seen)

        trace = Trace(
            id=artifact_id,
            session_id=session_id,
            agent="atelier:code",
            host="opencode",
            domain="coding",
            task=str(session_row.get("title") or "untitled opencode session"),
            status="success",
            files_touched=list(files_touched.values()),
            tools_called=[
                ToolCall(
                    name=n,
                    count=c,
                    args_hash="",
                    input_tokens=tool_in_tokens.get(n, 0),
                    output_tokens=tool_out_tokens.get(n, 0),
                )
                for n, c in tools_called.items()
            ],
            commands_run=cast(Any, commands_run),
            errors_seen=[],
            validation_results=[],
            raw_artifact_ids=[artifact.id],
            reasoning=reasoning_snippets,
            input_tokens=usage_summary["input_tokens"],
            user_prompt_tokens=user_prompt_tokens,
            output_tokens=usage_summary["output_tokens"],
            thinking_tokens=usage_summary["thinking_tokens"],
            cached_input_tokens=usage_summary["cached_input_tokens"],
            cache_creation_input_tokens=usage_summary["cache_creation_input_tokens"],
            model=usage_summary["model"],
            usage_entries=usage_summary["usage_entries"],
            model_usages=usage_summary["model_usages"],
            created_at=session_mtime,
        )
        self.store.record_trace(trace, write_json=False)

        # Best-effort: snapshot current on-disk state of every edited file
        if files_touched:
            snapshot_edited_files(self.store, list(files_touched.values()), session_id=session_id, source="opencode")

        return trace.id

    def _serialize_session(self, session_id: str, db_path: Path) -> str:
        lines: list[str] = []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row

            # Interleave messages and parts by time_created
            # We use a UNION to get a combined stream of events
            sql = """
                SELECT 'message' as etype, id, data, time_created, NULL as role
                FROM message
                WHERE session_id = ?
                UNION ALL
                SELECT 'part' as etype, p.id, p.data, p.time_created, json_extract(m.data, '$.role') as role
                FROM part p
                JOIN message m ON p.message_id = m.id
                WHERE p.session_id = ?
                ORDER BY time_created ASC
            """

            rows = conn.execute(sql, (session_id, session_id)).fetchall()
            for r in rows:
                if r["etype"] == "message":
                    lines.append(
                        json.dumps(
                            {
                                "_type": "message",
                                "id": r["id"],
                                "timestamp": r["time_created"],
                                "data": json.loads(r["data"] or "{}"),
                            },
                            ensure_ascii=False,
                        )
                    )
                else:
                    lines.append(
                        json.dumps(
                            {
                                "_type": "part",
                                "id": r["id"],
                                "role": r["role"],
                                "timestamp": r["time_created"],
                                "data": json.loads(r["data"] or "{}"),
                            },
                            ensure_ascii=False,
                        )
                    )

            conn.close()
        except Exception:
            _traceback.print_exc()
        return "\n".join(lines)
