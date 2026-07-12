"""Claude Code session importer for LemonCrow.

Converts ~/.claude/projects/<workspace-slug>/<session-uuid>.jsonl
into redacted RawArtifacts + curated LemonCrow Traces.

Session layout::

    ~/.claude/projects/
        -home-pankaj-Projects-leanchain-lemoncrow/
            00463f2c-c1c9-4f70-8919-48226e641627.jsonl

"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.pricing import is_placeholder_model
from lemoncrow.core.foundation.models import (
    CommandRecord,
    FileEditRecord,
    RawArtifact,
    ToolCall,
    Trace,
)
from lemoncrow.core.foundation.redaction import redact
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.gateway.hosts.session_parsers._common import (
    _SIZE_LIMIT_BYTES,
    _SYSTEM_PREFIXES_CLAUDE,
    make_llm_usage_entry,
    persist_imported_run_snapshot,
    snapshot_edited_files,
    summarize_usage_entries,
)

logger = logging.getLogger(__name__)

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
_SUBAGENT_TOOL_NAMES = {"agent", "task"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return _utcnow()


def _normalize_session_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    return text


def _extract_session_id_from_jsonl(raw_content: str) -> str | None:
    for line in raw_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        for key in ("sessionId", "session_id", "sessionUUID", "session_uuid"):
            if key in event:
                candidate = _normalize_session_id(event.get(key))
                if candidate:
                    return candidate
    return None


def find_claude_sessions(root: Path | None = None) -> Iterator[tuple[str, Path]]:
    """Yield (workspace_slug, jsonl_path) for all Claude sessions."""
    if root is not None:
        if not root.is_dir():
            return
        roots = [root]
    else:
        import os

        roots = [Path("~/.claude/projects").expanduser()]
        # macOS
        macos_root = Path("~/Library/Application Support/claude/projects").expanduser()
        if macos_root.is_dir():
            roots.append(macos_root)
        # Windows
        appdata = os.environ.get("APPDATA")
        if appdata:
            windows_root = Path(appdata) / "claude" / "projects"
            if windows_root.is_dir():
                roots.append(windows_root)

    for r in roots:
        if not r.is_dir():
            continue
        try:
            for project_dir in sorted(r.iterdir()):
                if project_dir.is_dir():
                    for p in project_dir.glob("*.jsonl"):
                        yield project_dir.name, p
        except OSError:
            continue


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
    # Smarter diff inference for Claude's Write/Edit tools
    if tool_name in {"Write", "write", "write_file"}:
        content = inp.get("content", "")
        if content:
            return f"+ {content[:4000]}"

    if result_text and "--- " in result_text and "+++ " in result_text:
        # If the tool result contains a unified diff, use it
        lines = result_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("--- "):
                return "\n".join(lines[i:])

    # Check input for patch/diff
    patch = inp.get("patch") or inp.get("diff")
    if patch:
        return str(patch)

    if tool_name in {"Edit", "EditFile", "replace"}:
        return f"Modified {inp.get('file_path') or inp.get('path')}"

    return ""


class ClaudeImporter:
    """Claude Code session importer."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False, limit: int | None = None) -> list[str]:
        """Import newest sessions under *root* up to limit per type."""

        # Helper to sort by mtime descending and take top N if limit is provided
        def get_newest(paths: list[tuple[str, Path]], n: int | None) -> list[tuple[str, Path]]:
            stamped: list[tuple[float, tuple[str, Path]]] = []
            for p in paths:
                try:
                    stamped.append((p[1].stat().st_mtime, p))
                except OSError:
                    continue
            sorted_paths = [p for _, p in sorted(stamped, key=lambda x: x[0], reverse=True)]
            return sorted_paths[:n] if n is not None else sorted_paths

        all_sessions = get_newest(list(find_claude_sessions(root)), limit)
        total = len(all_sessions)

        logger.info(
            "claude: discovered sessions (found %d, processing top %s)",
            total,
            limit if limit is not None else "all",
        )

        imported_ids = []
        for i, (workspace_slug, jsonl_path) in enumerate(all_sessions):
            try:
                size = jsonl_path.stat().st_size
                if size > _SIZE_LIMIT_BYTES:
                    logger.warning(
                        "claude: skipping massive session %s (%.1fMB)",
                        jsonl_path.name,
                        size / 1e6,
                    )
                    continue
                if i % 10 == 0 and i > 0:
                    logger.info("claude: importing %d/%d...", i, total)
                sid = self.import_session(workspace_slug, jsonl_path, force=force)
                if sid:
                    imported_ids.append(sid)
            except Exception:
                logger.exception("skipping claude session %s", jsonl_path.name)
        return imported_ids

    def import_session(self, workspace_slug: str, jsonl_path: Path, *, force: bool = False) -> str | None:
        """Import a Claude session and its subagents. Returns the session ID on success."""
        filename_session_id = jsonl_path.stem
        actual_session_id = filename_session_id
        project_dir = jsonl_path.parent

        artifact_id = f"claude-{workspace_slug}-{filename_session_id}"
        try:
            session_bytes = jsonl_path.stat().st_size
        except OSError:
            return None
        if session_bytes > _SIZE_LIMIT_BYTES:
            logger.warning(
                "claude: skipping oversized session %s (%.1fMB) to bound memory",
                jsonl_path.name,
                session_bytes / 1e6,
            )
            return None
        file_mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
        if not force:
            existing = self.store.get_raw_artifact(artifact_id)
            if existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime:
                return None

        try:
            root_raw_content = jsonl_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            root_raw_content = ""
        inferred_session_id = _extract_session_id_from_jsonl(root_raw_content)
        if inferred_session_id:
            actual_session_id = inferred_session_id

        # Find ALL jsonl files related to this session (including subagents)
        all_files = [jsonl_path]
        subagent_dir = project_dir / filename_session_id / "subagents"
        if subagent_dir.is_dir():
            all_files.extend(sorted(subagent_dir.glob("*.jsonl")))

        artifact_ids: list[str] = []
        pending_raw_artifacts: list[tuple[RawArtifact, str]] = []
        dropped_lines = 0
        model_seen = ""
        user_prompt_tokens = 0

        processed_msg_ids: set[str] = set()
        assistant_usage_entries: dict[str, Any] = {}
        orphan_usage_entries: list[Any] = []
        pending_tool_uses: dict[str, dict[str, Any]] = {}
        file_index_by_tool_use_id: dict[str, int] = {}
        command_index_by_tool_use_id: dict[str, int] = {}
        # Tool tallies must accumulate across ALL session files (main
        # transcript + subagents); per-file declarations would leave only the
        # last subagent's tools on the final trace.
        tool_args: dict[str, dict[str, Any] | None] = {}
        tool_results: dict[str, str] = {}
        tools_called: dict[str, int] = {}
        tool_in_tokens: dict[str, int] = {}
        tool_out_tokens: dict[str, int] = {}
        seen_tool_use_ids: set[str] = set()
        subagent_names: dict[str, int] = {}
        files_touched: list[str | FileEditRecord] = []
        errors_seen: set[str] = set()
        commands_run: list[str | CommandRecord] = []
        reasoning_snippets: list[str] = []
        task = "untitled claude session"
        title = ""
        created_at: datetime = _utcnow()
        updated_at: datetime = created_at
        first_ts_set = False
        agent_settings: dict[str, Any] = {}
        skills: list[str] = []
        latencies: list[float] = []
        ttfts: list[float] = []
        workspace_path: str | None = None

        for f_path in all_files:
            try:
                raw_content = f_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            redacted = redact(raw_content)

            rel_path = f_path.relative_to(project_dir)
            art = RawArtifact(
                id=f"claude-{workspace_slug}-{f_path.stem}",
                source="claude",
                source_session_id=actual_session_id,
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
            artifact_ids.append(art.id)
            pending_raw_artifacts.append((art, redacted))

            # Parse each line from the raw content. The whole-file redacted
            # text is already stored in the RawArtifact above; applying
            # redact() here before json.loads() would corrupt valid JSON because
            # the credential pattern (`\S[^\r\n]*`) consumes to end-of-line,
            # eating closing brackets and making the record unparseable. Instead
            # we parse raw and redact only the specific string values we extract
            # into Trace fields (task, title, reasoning, commands).
            for raw_line in raw_content.splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    dropped_lines += 1
                    continue

                ev_type = ev.get("type", "")
                ts_str = ev.get("timestamp", "")
                msg = ev.get("message") or {}
                msg_id = str(msg.get("id") or ev.get("uuid") or ev.get("id") or "")

                if workspace_path is None:
                    cwd = ev.get("cwd")
                    if cwd:
                        workspace_path = str(cwd)

                if ts_str and not first_ts_set:
                    created_at = _parse_ts(ts_str)
                    updated_at = created_at
                    first_ts_set = True
                elif ts_str:
                    parsed_ts = _parse_ts(ts_str)
                    if parsed_ts > updated_at:
                        updated_at = parsed_ts

                if ev_type == "ai-title":
                    t = ev.get("aiTitle") or ev.get("title", "")
                    if t:
                        title = redact(str(t))
                elif ev_type == "last-prompt":
                    lp = str(ev.get("lastPrompt", "")).strip()
                    if lp and task == "untitled claude session" and not lp.startswith("<") and len(lp) > 5:
                        task = redact(lp[:200])
                elif ev_type == "user":
                    if ev.get("isMeta"):
                        # Extract skills/settings from metadata injection
                        c_str = str(msg.get("content", ""))
                        if "active mcp servers" in c_str.lower():
                            matches = re.findall(r"- ([\w.-]+)", c_str)
                            skills.extend(matches)
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
                        task = redact(text_ext[:200])
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
                            if block.get("is_error") and res_txt:
                                errors_seen.add(redact(res_txt[:200]))
                            if name == "Bash":
                                idx = command_index_by_tool_use_id.get(tid)
                                if idx is not None:
                                    stdout, stderr = _tool_result_streams(
                                        block.get("content"), bool(block.get("is_error"))
                                    )
                                    commands_run[idx] = CommandRecord(
                                        command=redact(str((pending.get("input") or {}).get("command") or "")[:200]),
                                        exit_code=block.get("exit_code"),
                                        stdout=redact(stdout[:1024]),
                                        stderr=redact(stderr[:1024]),
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
                    # Capture performance telemetry
                    perf = ev.get("performance") or {}
                    latency = perf.get("total_latency_ms") or perf.get("latency_ms")
                    ttft = perf.get("time_to_first_token_ms") or perf.get("ttft_ms")
                    if latency:
                        latencies.append(float(latency))
                    if ttft:
                        ttfts.append(float(ttft))

                    usage = msg.get("usage", {}) or {}
                    m = msg.get("model") or ev.get("model")
                    is_synthetic_model = bool(m and is_placeholder_model(m))
                    # Claude Code emits "<synthetic>" for cached/injected replies that
                    # don't trigger a billable Anthropic request. Don't let that
                    # placeholder overwrite the last real model id we saw.
                    if m and not is_placeholder_model(m):
                        model_seen = str(m)
                    resolved_model = str(m) if (m and not is_placeholder_model(m)) else model_seen
                    if not is_synthetic_model:
                        usage_entry = make_llm_usage_entry(
                            model=resolved_model,
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
                        in_t = 0 if is_synthetic_model else int(usage.get("input_tokens", 0) or 0)
                        out_t = 0 if is_synthetic_model else int(usage.get("output_tokens", 0) or 0)
                        cr = 0 if is_synthetic_model else int(usage.get("cache_read_input_tokens", 0) or 0)
                        cw = 0 if is_synthetic_model else int(usage.get("cache_creation_input_tokens", 0) or 0)
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
                                reasoning_snippets.append(redact(str(think)[:500]))
                        if bt != "tool_use":
                            continue
                        name = str(block.get("name", "unknown"))
                        tid = str(block.get("id") or "")
                        if tid:
                            if tid in seen_tool_use_ids:
                                continue
                            seen_tool_use_ids.add(tid)
                        tools_called[name] = tools_called.get(name, 0) + 1
                        tool_in_tokens[name] = tool_in_tokens.get(name, 0) + dist_out
                        tool_out_tokens[name] = tool_out_tokens.get(name, 0) + dist_in
                        inp = block.get("input") or {}
                        if not isinstance(inp, dict):
                            inp = {}
                        tool_args[name] = inp or tool_args.get(name)
                        if tid:
                            pending_tool_uses[tid] = {"name": name, "input": inp}
                        if name.lower() in _SUBAGENT_TOOL_NAMES:
                            key = (
                                str(inp.get("agent_type") or inp.get("name") or "")
                                or str(inp.get("description") or "")[:24].strip()
                                or "agent"
                            )
                            subagent_names[key] = subagent_names.get(key, 0) + 1
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
                        if name == "Bash":
                            cmd = str(inp.get("command") or "").strip()
                            if cmd:
                                commands_run.append(redact(cmd[:200]))
                                if tid:
                                    command_index_by_tool_use_id[tid] = len(commands_run) - 1

        usage_summary = summarize_usage_entries(
            [*assistant_usage_entries.values(), *orphan_usage_entries],
            fallback_model=model_seen,
        )

        telemetry: dict[str, Any] = {}
        if latencies:
            telemetry["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1)
        if ttfts:
            telemetry["avg_ttft_ms"] = round(sum(ttfts) / len(ttfts), 1)
        if subagent_names:
            telemetry["subagent_names"] = subagent_names

        if task == "untitled claude session" and title:
            task = title

        trace = Trace(
            id=artifact_id,
            session_id=actual_session_id,
            agent="lc:code",
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
            agent_settings=agent_settings,
            skills=sorted(set(skills)),
            telemetry=telemetry,
            workspace_path=workspace_path,
            created_at=created_at,
        )
        if dropped_lines:
            logger.warning(
                "claude reader: dropped %d unparseable line(s) while importing session %s",
                dropped_lines,
                actual_session_id,
            )
        # Record raw artifacts only after the trace above parsed successfully.
        # Recording them first (as this used to) bumps source_file_mtime before
        # a mid-parse exception can be raised; on the next non-force import
        # the mtime-based dedup check then sees "unchanged" and skips the
        # session forever. Parse fully, then persist (matches codex.py).
        for art, redacted in pending_raw_artifacts:
            self.store.record_raw_artifact(art, redacted)
        self.store.record_trace(trace, write_json=False)
        persist_imported_run_snapshot(self.store, trace, started_at=created_at, ended_at=updated_at)

        # Best-effort: snapshot current on-disk state of every edited file
        ft = [r for r in files_touched if isinstance(r, FileEditRecord)]
        if ft:
            snapshot_edited_files(self.store, ft, session_id=actual_session_id, source="claude")

        return trace.id
