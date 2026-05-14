"""Claude Code session importer for Atelier.

Converts ~/.claude/projects/<workspace-slug>/<session-uuid>.jsonl
into redacted RawArtifacts + curated Atelier Traces.

Session layout::

    ~/.claude/projects/
        -home-pankaj-Projects-leanchain-atelier/
            00463f2c-c1c9-4f70-8919-48226e641627.jsonl

"""

from __future__ import annotations

import hashlib
import json
import re
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

_FILE_TOOLS = {
    "read",
    "edit",
    "write",
    "multiedit",
    "read_file",
    "replace",
    "apply_patch",
    "view",
    "Edit",
    "Write",
    "MultiEdit",
}
_SYSTEM_PREFIXES_CLAUDE = (
    "I have been initialized",
    "Environment context:",
    "<environment_context>",
    "<permissions instructions>",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return _utcnow()


def find_claude_sessions(root: Path | None = None) -> Iterator[tuple[str, Path]]:
    """Yield (workspace_slug, jsonl_path) for all Claude sessions."""
    if root is None:
        root = Path("~/.claude/projects").expanduser()
    if not root.is_dir():
        return
    for project_dir in sorted(root.iterdir()):
        if project_dir.is_dir():
            for p in project_dir.glob("*.jsonl"):
                yield project_dir.name, p


def _extract_user_text(content: Any) -> str:
    if isinstance(content, str):
        text = content.strip()
        if any(text.startswith(p) for p in _SYSTEM_PREFIXES_CLAUDE):
            return ""
        # Claude Code often wraps the main task in <task> tags
        xml_match = re.search(r"<(task|prompt|request|question)[^>]*>(.*?)</\1>", text, re.IGNORECASE | re.DOTALL)
        if xml_match:
            return xml_match.group(2).strip()
        return text

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and not any(text.startswith(p) for p in _SYSTEM_PREFIXES_CLAUDE):
                    xml_match = re.search(
                        r"<(task|prompt|request|question)[^>]*>(.*?)</\1>",
                        text,
                        re.IGNORECASE | re.DOTALL,
                    )
                    if xml_match:
                        parts.append(xml_match.group(2).strip())
                    else:
                        parts.append(text)
        return "\n\n".join(parts)
    return ""


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text") or ""
    return ""


def _tool_result_streams(content: Any, is_error: bool = False) -> tuple[str, str]:
    text = _tool_result_text(content)
    if is_error:
        return "", text
    return text, ""


def _infer_file_edit_diff(tool_name: str, inp: dict[str, Any], result_text: str | None = None) -> str:
    # Very basic diff inference for Claude's Write/Edit tools
    if tool_name == "Write":
        return f"+ {inp.get('content', '')[:1000]}"
    if tool_name == "Edit":
        return f"Editing {inp.get('file_path')}"
    return ""


class ClaudeImporter:
    """Claude Code session importer."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        """Import all Claude sessions. Returns IDs of successfully imported traces."""
        if root is None:
            root = Path("~/.claude/projects").expanduser()
        if not root.is_dir():
            return []

        all_sessions = [
            (project_dir.name, jsonl_path)
            for project_dir in sorted(root.iterdir())
            if project_dir.is_dir()
            for jsonl_path in sorted(project_dir.glob("*.jsonl"))
        ]
        total = len(all_sessions)
        print(f"[atelier] claude: discovering sessions (found {total})")

        imported_ids = []
        for i, (workspace_slug, jsonl_path) in enumerate(all_sessions):
            try:
                size = jsonl_path.stat().st_size
                if size > _SIZE_LIMIT_BYTES:
                    print(f"[atelier] claude: skipping massive session {jsonl_path.name} ({size / 1e6:.1f}MB)")
                    continue
                if i % 10 == 0 and i > 0:
                    print(f"[atelier] claude: importing {i}/{total}...")
                sid = self.import_session(workspace_slug, jsonl_path, force=force)
                if sid:
                    imported_ids.append(sid)
            except Exception as exc:
                _traceback.print_exc()
                print(f"[atelier] skipping claude session {jsonl_path.name}: {exc}")
        return imported_ids

    def import_session(self, workspace_slug: str, jsonl_path: Path, *, force: bool = False) -> str | None:
        """Import a Claude session and its subagents. Returns the session ID on success."""
        session_id = jsonl_path.stem
        project_dir = jsonl_path.parent

        artifact_id = f"claude-{workspace_slug}-{session_id}"
        file_mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
        if not force:
            existing = self.store.get_raw_artifact(artifact_id)
            if existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
                return None

        # Find ALL jsonl files related to this session (including subagents)
        all_files = [jsonl_path]
        subagent_dir = project_dir / session_id / "subagents"
        if subagent_dir.is_dir():
            all_files.extend(sorted(subagent_dir.glob("*.jsonl")))

        artifact_ids: list[str] = []
        model_seen = ""
        user_prompt_tokens = 0

        processed_msg_ids: set[str] = set()
        assistant_usage_entries: dict[str, Any] = {}
        orphan_usage_entries: list[Any] = []
        pending_tool_uses: dict[str, dict[str, Any]] = {}
        file_index_by_tool_use_id: dict[str, int] = {}
        command_index_by_tool_use_id: dict[str, int] = {}
        files_touched: list[str | FileEditRecord] = []
        errors_seen: set[str] = set()
        commands_run: list[str | CommandRecord] = []
        reasoning_snippets: list[str] = []
        task = "untitled claude session"
        title = ""
        created_at: datetime = _utcnow()
        first_ts_set = False

        for f_path in all_files:
            try:
                raw_content = f_path.read_text(encoding="utf-8")
            except OSError:
                continue
            redacted = redact(raw_content)

            rel_path = f_path.relative_to(project_dir)
            art = RawArtifact(
                id=f"claude-{workspace_slug}-{f_path.stem}",
                source="claude",
                source_session_id=session_id,
                kind="session.jsonl",
                relative_path=str(rel_path),
                content_path=f"raw/claude/{workspace_slug}/{rel_path}",
                sha256_original=_sha256(raw_content),
                sha256_redacted=_sha256(redacted),
                byte_count_original=len(raw_content.encode("utf-8")),
                byte_count_redacted=len(redacted.encode("utf-8")),
                created_at=_utcnow(),
                source_file_mtime=datetime.fromtimestamp(f_path.stat().st_mtime, tz=UTC),
                source_path=str(f_path),
            )
            self.store.record_raw_artifact(art, redacted)
            artifact_ids.append(art.id)

            tool_args: dict[str, dict[str, Any] | None] = {}
            tool_results: dict[str, str] = {}
            tools_called: dict[str, int] = {}
            tool_in_tokens: dict[str, int] = {}
            tool_out_tokens: dict[str, int] = {}

            for line in redacted.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ev_type = ev.get("type", "")
                ts_str = ev.get("timestamp", "")
                msg = ev.get("message") or {}
                msg_id = str(msg.get("id") or ev.get("uuid") or ev.get("id") or "")

                if ts_str and not first_ts_set:
                    created_at = _parse_ts(ts_str)
                    first_ts_set = True

                if ev_type == "ai-title":
                    t = ev.get("aiTitle") or ev.get("title", "")
                    if t:
                        title = str(t)
                elif ev_type == "last-prompt":
                    lp = str(ev.get("lastPrompt", "")).strip()
                    if lp and task == "untitled claude session" and not lp.startswith("<") and len(lp) > 5:
                        task = lp[:200]
                elif ev_type == "user":
                    if ev.get("isMeta"):
                        continue
                    content = msg.get("content", "")
                    text_ext = _extract_user_text(content)
                    if msg_id and msg_id not in processed_msg_ids:
                        if text_ext:
                            user_prompt_tokens += max(1, len(text_ext) // 4)
                        processed_msg_ids.add(msg_id)
                    if (
                        task == "untitled claude session"
                        and text_ext
                        and (not text_ext.startswith("<") and not text_ext.startswith("/") and len(text_ext) > 5)
                    ):
                        task = text_ext[:200]
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "tool_result":
                                continue
                            tid = str(block.get("tool_use_id") or "")
                            if not tid:
                                continue
                            pending = pending_tool_uses.get(tid) or {}
                            name = str(pending.get("name") or block.get("name") or "unknown")
                            res_txt = _tool_result_text(block.get("content"))
                            if res_txt:
                                tool_results[name] = res_txt[:200]
                            if name == "Bash":
                                idx = command_index_by_tool_use_id.get(tid)
                                if idx is not None:
                                    stdout, stderr = _tool_result_streams(
                                        block.get("content"), bool(block.get("is_error"))
                                    )
                                    commands_run[idx] = CommandRecord(
                                        command=str((pending.get("input") or {}).get("command") or "")[:200],
                                        exit_code=block.get("exit_code"),
                                        stdout=stdout,
                                        stderr=stderr,
                                    )
                            elif name in {"Write", "Edit", "MultiEdit"}:
                                idx = file_index_by_tool_use_id.get(tid)
                                if idx is not None:
                                    inp = pending.get("input") or {}
                                    path = str(inp.get("file_path") or inp.get("path") or "")
                                    diff = _infer_file_edit_diff(name, inp, res_txt)
                                    if diff:
                                        files_touched[idx] = FileEditRecord(path=path, diff=diff[:4096], event="edit")
                elif ev_type == "assistant":
                    usage = msg.get("usage", {}) or {}
                    m = msg.get("model") or ev.get("model")
                    if m:
                        model_seen = str(m)
                    usage_entry = make_llm_usage_entry(
                        model=str(m or model_seen),
                        input_tokens=int(usage.get("input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0),
                        cached_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                        thinking_tokens=int(usage.get("thinking_tokens", 0) or 0),
                        source_type="claude.assistant",
                        source_id=msg_id,
                        created_at=_parse_ts(ev.get("timestamp")) if ev.get("timestamp") else None,
                    )
                    if usage_entry is not None:
                        if msg_id:
                            assistant_usage_entries[msg_id] = usage_entry
                        else:
                            orphan_usage_entries.append(usage_entry)
                    content = msg.get("content") or []
                    calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                    if calls:
                        in_t = int(usage.get("input_tokens", 0) or 0)
                        out_t = int(usage.get("output_tokens", 0) or 0)
                        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                        cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
                        eff_in = in_t + cr + cw
                        dist_in = eff_in // len(calls)
                        dist_out = out_t // len(calls)
                    else:
                        dist_in, dist_out = 0, 0
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "thinking":
                            think = block.get("thinking", "")
                            if think:
                                reasoning_snippets.append(str(think)[:500])
                        if bt != "tool_use":
                            continue
                        name = str(block.get("name", "unknown"))
                        tools_called[name] = tools_called.get(name, 0) + 1
                        tool_in_tokens[name] = tool_in_tokens.get(name, 0) + dist_out
                        tool_out_tokens[name] = tool_out_tokens.get(name, 0) + dist_in
                        inp = block.get("input") or {}
                        if not isinstance(inp, dict):
                            inp = {}
                        tool_args[name] = inp or tool_args.get(name)
                        tid = str(block.get("id") or "")
                        if tid:
                            pending_tool_uses[tid] = {"name": name, "input": inp}
                        if name in _FILE_TOOLS:
                            fp = inp.get("file_path") or inp.get("path")
                            if fp:
                                fp_str = str(fp)
                                if name in {"Write", "Edit", "MultiEdit"}:
                                    diff = _infer_file_edit_diff(name, inp)
                                    if diff:
                                        files_touched.append(
                                            FileEditRecord(path=fp_str, diff=diff[:4096], event="edit")
                                        )
                                    else:
                                        files_touched.append(fp_str)
                                else:
                                    files_touched.append(fp_str)
                                if tid:
                                    file_index_by_tool_use_id[tid] = len(files_touched) - 1
                        elif name == "Bash":
                            cmd = str(inp.get("command") or "").strip()
                            if cmd:
                                commands_run.append(cmd[:200])
                                if tid:
                                    command_index_by_tool_use_id[tid] = len(commands_run) - 1

        usage_summary = summarize_usage_entries(
            [*assistant_usage_entries.values(), *orphan_usage_entries],
            fallback_model=model_seen,
        )

        if task == "untitled claude session" and title:
            task = title

        trace = Trace(
            id=artifact_id,
            session_id=session_id,
            agent="atelier:code",
            host="claude",
            domain="coding",
            task=task,
            status="success",
            files_touched=files_touched,
            tools_called=[
                ToolCall(
                    name=n,
                    args_hash="",
                    count=c,
                    args=tool_args.get(n),
                    input_tokens=tool_in_tokens.get(n, 0),
                    output_tokens=tool_out_tokens.get(n, 0),
                )
                for n, c in tools_called.items()
            ],
            commands_run=commands_run,
            errors_seen=sorted(errors_seen),
            validation_results=[],
            raw_artifact_ids=artifact_ids,
            reasoning=reasoning_snippets,
            input_tokens=usage_summary["input_tokens"],
            user_prompt_tokens=user_prompt_tokens,
            cached_input_tokens=usage_summary["cached_input_tokens"],
            cache_creation_input_tokens=usage_summary["cache_creation_input_tokens"],
            model=usage_summary["model"],
            usage_entries=usage_summary["usage_entries"],
            model_usages=usage_summary["model_usages"],
            output_tokens=usage_summary["output_tokens"],
            thinking_tokens=usage_summary["thinking_tokens"],
            created_at=created_at,
        )
        self.store.record_trace(trace, write_json=False)
        return trace.id
