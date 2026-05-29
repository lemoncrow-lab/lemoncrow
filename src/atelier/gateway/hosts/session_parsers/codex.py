"""Codex session importer for Atelier.

Converts ~/.codex/sessions/<year>/<month>/<day>/<rollout-ts-uuid>.jsonl
into redacted RawArtifacts + curated Atelier Traces.

Session layout::

    ~/.codex/sessions/
        2026/
            04/
                30/
                    rollout-2026-04-30T12-58-46-019ddee8-....jsonl

Codex JSONL comes in two formats depending on CLI version:

**Format A - event_msg wrapper** (VSCode extension / older CLI):

- ``{"type":"session_meta","payload":{"id":"...","cwd":"...","timestamp":"..."}}``
- ``{"type":"event_msg","payload":{"type":"user_message","message":"..."}}``
- ``{"type":"event_msg","payload":{"type":"exec_command_end","command":[...],...}}``
- ``{"type":"event_msg","payload":{"type":"patch_apply_end","changes":{path:diff},...}}``
- ``{"type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"..."}}``

**Format B - flat** (CLI TUI / newer builds, no event_msg wrapper):

- ``{"id":"...","timestamp":"...","instructions":"..."}``  ← session meta, no "type"
- ``{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}``
- ``{"type":"function_call","name":"apply_patch","arguments":"..."}``
- ``{"type":"function_call","name":"exec_command","arguments":"{\\"cmd\\":\\"...\\",...}"}``
- ``{"type":"function_call_output","call_id":"...","output":"..."}``

Lookup path::

    agent → curated Trace (fast, retrieval-friendly)
    human → RawArtifact content (full redacted JSONL for audit)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
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
    _SIZE_LIMIT_BYTES,
    make_llm_usage_entry,
    summarize_usage_entries,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _codex_event_identity(ev: dict[str, Any]) -> str:
    ev_type = str(ev.get("type") or ev.get("_type") or "")
    payload_raw = ev.get("payload")
    payload: dict[str, Any] = payload_raw if isinstance(payload_raw, dict) else {}
    payload_type = str(payload.get("type") or "")
    namespace = f"{ev_type}:{payload_type}" if payload_type else ev_type
    for candidate in (ev, payload):
        for key in ("id", "event_id", "eventId", "uuid", "message_id", "messageId", "call_id", "callId"):
            value = str(candidate.get(key) or "").strip()
            if value:
                return f"{namespace}:{value}"
    return ""


def _codex_event_source_id(ev: dict[str, Any], raw_line: str) -> str:
    identity = _codex_event_identity(ev)
    if identity:
        return identity.rsplit(":", 1)[-1]
    return _sha256(raw_line)[:16]


def _parse_ts(val: Any) -> datetime:
    if not val:
        return _utcnow()
    try:
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(float(val), tz=UTC)
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, OSError):
        return _utcnow()


def _command_str(cmd: Any) -> str:
    """Normalise a Codex command field (list or str) to a readable string.

    Format A: command is a list like ["/usr/bin/zsh", "-lc", "<actual cmd>"].
    The last element is the human-readable command.
    """
    if isinstance(cmd, list):
        return str(cmd[-1]) if cmd else ""
    return str(cmd)


def _int_or_zero(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _extract_flat_usage(usage: Any) -> tuple[int, int, int, int, int]:
    """Return (input, output, thinking, cached_read, cache_write) for flat Codex usage payloads."""
    if not isinstance(usage, dict):
        return (0, 0, 0, 0, 0)

    input_tokens = _int_or_zero(
        usage.get("input_tokens") or usage.get("inputTokens") or usage.get("prompt_tokens") or usage.get("promptTokens")
    )
    output_tokens = _int_or_zero(
        usage.get("output_tokens")
        or usage.get("outputTokens")
        or usage.get("completion_tokens")
        or usage.get("completionTokens")
    )
    # Codex/OpenAI reports reasoning as a subset of output tokens, so mapping it
    # to thinking_tokens would make Atelier price the same tokens twice.
    thinking_tokens = 0
    cached_tokens = _int_or_zero(
        usage.get("cached_input_tokens")
        or usage.get("cachedInputTokens")
        or usage.get("cache_read_tokens")
        or usage.get("cacheReadTokens")
    )
    if cached_tokens == 0:
        input_details = usage.get("input_tokens_details") or usage.get("inputTokensDetails")
        if isinstance(input_details, dict):
            cached_tokens = _int_or_zero(
                input_details.get("cached_tokens")
                or input_details.get("cachedTokens")
                or input_details.get("cache_read_tokens")
                or input_details.get("cacheReadTokens")
            )

    cache_write_tokens = _int_or_zero(
        usage.get("cache_creation_input_tokens")
        or usage.get("cacheCreationInputTokens")
        or usage.get("cache_write_tokens")
        or usage.get("cacheWriteTokens")
    )
    if cache_write_tokens == 0:
        input_details = usage.get("input_tokens_details") or usage.get("inputTokensDetails")
        if isinstance(input_details, dict):
            cache_write_tokens = _int_or_zero(
                input_details.get("cache_creation_input_tokens")
                or input_details.get("cacheCreationInputTokens")
                or input_details.get("cache_write_tokens")
                or input_details.get("cacheWriteTokens")
            )

    return (input_tokens, output_tokens, thinking_tokens, cached_tokens, cache_write_tokens)


# Prefixes that mark system-injected content blocks to skip for task extraction
_SYSTEM_CONTENT_PREFIXES = (
    "<user_instructions>",
    "<environment_context>",
    "<permissions instructions>",
    "<permissions_instructions>",
    "# AGENTS.md instructions",
    "AGENTS.md instructions",
    "<local-command",
    "<ide_",
    "<thinking>",
)

# Regex for "## My request for Codex:" style headers (Format A IDE context)
_REQUEST_HEADER_RE = re.compile(
    r"#+\s*(My request for Codex|My request|Request|Task|Prompt)[^:\n]*:\s*\n+(.+?)(?=\n#+\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_CAPTURE_HEADER_RE = re.compile(r"#+\s*(my request|request|task)[^:\n]*:", re.IGNORECASE)


def _get_clean_user_text(msg: str) -> str:
    """Extract only the human-typed portion of a Codex user message."""
    msg = msg.strip()
    if not msg:
        return ""

    # If it's short, assume it's just the user typing
    if len(msg) < 1000 and not msg.startswith("<"):
        return msg

    # Try to extract from "## My request for Codex:" header (Format A)
    md_match = _REQUEST_HEADER_RE.search(msg)
    if md_match:
        extracted = md_match.group(2).strip()
        # Cut off at known machine-context markers
        for marker in (
            "<INSTRUCTIONS>",
            "<environment_context>",
            "## Current Date:",
            "## Context",
            "```",
        ):
            if marker in extracted:
                extracted = extracted.split(marker)[0].strip()
        return extracted

    # If it starts with a known system prefix, it's a context dump.
    # We try to find if there's any human text hidden inside.
    if any(msg.startswith(p) for p in _SYSTEM_CONTENT_PREFIXES):
        # Search for the human marker anywhere in the block
        md_match = _REQUEST_HEADER_RE.search(msg)
        if md_match:
            return md_match.group(2).strip()
        return ""  # No human text found in this system block

    # Fallback: if it's huge and contains system-like markers, it's likely context.
    if len(msg) > 2000 and ("<INSTRUCTIONS>" in msg or "# AGENTS.md" in msg):
        return ""

    return msg


def _count_user_tokens(text: str) -> int:
    clean = _get_clean_user_text(text)
    if not clean:
        return 0
    return max(1, len(clean) // 4)


def _files_from_patch(patch_text: str) -> list[str]:
    """Extract file paths from a Codex apply_patch diff string.

    Looks for lines like:
        *** Update File: /absolute/path/to/file.py
        *** Add File: /absolute/path/to/new_file.py
        *** Delete File: /absolute/path/to/old_file.py
    """
    files: list[str] = []
    for ln in patch_text.splitlines():
        m = re.match(r"^\*\*\*\s+(?:Update|Add|Delete|Move|Rename)\s+File:\s+(.+)$", ln, re.IGNORECASE)
        if m:
            files.append(m.group(1).strip())
    return files


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def find_codex_sessions(root: Path | None = None) -> Iterator[Path]:
    """Yield every Codex session JSONL file under *root*."""
    if root is None:
        root = Path("~/.codex/sessions").expanduser()
    if not root.is_dir():
        return
    yield from sorted(root.rglob("*.jsonl"))


# ---------------------------------------------------------------------------
# Importer
# ---------------------------------------------------------------------------


class CodexImporter:
    """Loss-preserving importer for Codex sessions.

    Handles both the legacy event_msg-wrapped format (Format A) and the
    flat object format produced by the Codex TUI (Format B).

    For every ``.jsonl`` session file:

    1. Write a **redacted raw artifact** into
       ``<store_root>/raw/codex/<date_path>/<filename>``.
    2. Parse the *original* (pre-redaction) file into a compact ``Trace``
       so that task / command extraction is not impacted by redaction
       truncating chain-of-thought blocks.

    Nothing is thrown away beyond what Atelier's redactor strips.
    """

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def import_all(self, root: Path | None = None, *, force: bool = False) -> list[str]:
        """Import all sessions. Returns IDs of successfully imported sessions."""
        imported_ids = []
        skipped = 0
        all_sessions = list(find_codex_sessions(root))
        total = len(all_sessions)
        logger.info("[atelier] codex: discovering sessions (found %d)", total)
        for i, jsonl_path in enumerate(all_sessions):
            try:
                size = jsonl_path.stat().st_size
                if size > _SIZE_LIMIT_BYTES:
                    logger.warning(
                        "[atelier] codex: skipping massive session %s (%.1fMB)",
                        jsonl_path.name,
                        size / 1e6,
                    )
                    continue
                if i % 10 == 0 and i > 0:
                    logger.info("[atelier] codex: importing %d/%d...", i, total)
                sid = self.import_session(jsonl_path, force=force)
                if sid:
                    imported_ids.append(sid)
                else:
                    skipped += 1
            except Exception:
                logger.exception("[atelier] skipping codex session %s", jsonl_path.name)
        if skipped > 0:
            logger.info("[atelier] %d sessions already imported (skipped by dedup)", skipped)
        return imported_ids

    def import_session(self, jsonl_path: Path, *, force: bool = False) -> str | None:
        """Import a single Codex JSONL file. Returns trace ID on success."""
        if not force and self._is_unchanged(jsonl_path):
            return None

        return self._import_session_content(jsonl_path)

    def _is_unchanged(self, jsonl_path: Path) -> bool:
        artifact_id = f"codex-{self._session_id(jsonl_path)}"
        file_mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
        existing = self.store.get_raw_artifact(artifact_id)
        return bool(existing and existing.source_file_mtime and file_mtime <= existing.source_file_mtime)

    def _import_session_content(self, jsonl_path: Path) -> str:
        session_id = self._session_id(jsonl_path)
        artifact_id = f"codex-{session_id}"
        file_mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
        codex_root = Path("~/.codex/sessions").expanduser()
        try:
            rel = jsonl_path.relative_to(codex_root)
        except ValueError:
            rel = Path(jsonl_path.name)
        content_path = f"raw/codex/{rel}"

        raw_content = jsonl_path.read_text(encoding="utf-8")
        redacted = redact(raw_content)

        # ── Step 1: write redacted raw artifact ──────────────────────────────
        artifact = RawArtifact(
            id=artifact_id,
            source="codex",
            source_session_id=session_id,
            kind="session.jsonl",
            relative_path=jsonl_path.name,
            content_path=content_path,
            sha256_original=_sha256(raw_content),
            sha256_redacted=_sha256(redacted),
            byte_count_original=len(raw_content.encode("utf-8")),
            byte_count_redacted=len(redacted.encode("utf-8")),
            created_at=_utcnow(),
            source_file_mtime=file_mtime,
            source_path=str(jsonl_path),
        )

        # ── Step 2: detect format and build curated Trace ─────────────────────
        # Parse from RAW content (not redacted) so task extraction isn't
        # truncated by chain-of-thought redaction. Only the stored artifact
        # is redacted; the Trace fields carry only non-sensitive summaries.
        fmt = _detect_format(raw_content)
        if fmt == "flat":
            trace = self._parse_flat(session_id, raw_content, artifact.id)
        else:
            trace = self._parse_event_msg(session_id, raw_content, artifact.id)

        self.store.record_raw_artifact(artifact, redacted)
        # write_json=False: the raw JSONL is already stored as a RawArtifact;
        # there is no need to mirror the compact Trace JSON to disk too.
        self.store.record_trace(trace, write_json=False)
        return trace.id

    @staticmethod
    def _session_id(jsonl_path: Path) -> str:
        stem = jsonl_path.stem  # e.g. rollout-2026-04-30T12-58-46-019ddee8-...
        parts = stem.split("-")
        return "-".join(parts[-5:]) if len(parts) >= 5 else stem

    # ------------------------------------------------------------------
    # Format detection
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Format A: event_msg-wrapped (VSCode extension / older CLI)
    # ------------------------------------------------------------------

    def _parse_event_msg(self, session_id: str, raw_content: str, artifact_id: str) -> Trace:
        tools_called: dict[str, int] = {}
        tool_args: dict[str, dict[str, Any] | None] = {}
        tool_in_tokens: dict[str, int] = {}
        tool_out_tokens: dict[str, int] = {}
        # token_count events carry { info: { last_token_usage, total_token_usage } }.
        # last_token_usage is the per-turn delta (used for tool distribution).
        # total_token_usage is cumulative; we keep the FINAL value for session totals.
        # cached_input_tokens is a SUBSET of input_tokens in OpenAI accounting.
        # reasoning_output_tokens is a SUBSET of output_tokens.
        final_total_in = 0
        final_total_out = 0
        final_total_think = 0
        final_total_cached = 0
        model_seen = ""
        models_seen: set[str] = set()
        usage_entries = []
        curr_tool_calls: list[tuple[str, Any]] = []
        files_touched: set[str] = set()
        file_diffs: dict[str, str] = {}  # path → diff text
        commands_run: list[str | CommandRecord] = []
        reasoning_snippets: list[str] = []
        task = "untitled codex session"
        created_at = _utcnow()
        user_prompt_tokens = 0
        seen_event_ids: set[str] = set()
        previous_unidentified_event = ""

        for line in raw_content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_identity = _codex_event_identity(ev)
            if event_identity:
                if event_identity in seen_event_ids:
                    continue
                seen_event_ids.add(event_identity)
                previous_unidentified_event = ""
            elif line == previous_unidentified_event:
                continue
            else:
                previous_unidentified_event = line

            ev_type = ev.get("type", "")
            source_id = _codex_event_source_id(ev, line)

            if ev_type == "session_meta":
                payload = ev.get("payload") or {}
                ts = payload.get("timestamp")
                if ts:
                    created_at = _parse_ts(ts)
                m_meta = payload.get("model") or payload.get("model_id")
                if m_meta and not model_seen:
                    model_seen = str(m_meta)
                    models_seen.add(model_seen)

            elif ev_type == "turn_context":
                # turn_context.payload.model is the canonical model name for this turn.
                m_turn = (ev.get("payload") or {}).get("model")
                if m_turn:
                    model_seen = str(m_turn)
                    models_seen.add(model_seen)

            elif ev_type == "event_msg":
                payload = ev.get("payload") or {}
                ptype = payload.get("type", "")

                if ptype == "token_count":
                    info = payload.get("info") or {}
                    last = info.get("last_token_usage") if isinstance(info, dict) else None
                    last = last or {}
                    turn_in, turn_out, turn_think, turn_cached, turn_cache_write = _extract_flat_usage(last)
                    tot = info.get("total_token_usage") if isinstance(info, dict) else None
                    if isinstance(tot, dict):
                        final_total_in = int(tot.get("input_tokens", 0) or 0)
                        final_total_out = int(tot.get("output_tokens", 0) or 0)
                        final_total_think = 0
                        final_total_cached = int(tot.get("cached_input_tokens", 0) or 0)
                    usage_entry = make_llm_usage_entry(
                        model=model_seen,
                        input_tokens=max(turn_in - turn_cached, 0),
                        output_tokens=turn_out,
                        thinking_tokens=turn_think,
                        cached_input_tokens=turn_cached,
                        cache_creation_input_tokens=turn_cache_write,
                        source_type="codex.event_msg.token_count",
                        source_id=source_id,
                    )
                    if usage_entry is not None:
                        usage_entries.append(usage_entry)
                    if curr_tool_calls and (turn_in or turn_out):
                        dist_in = turn_in // len(curr_tool_calls)
                        dist_out = turn_out // len(curr_tool_calls)
                        for t_name, _args in curr_tool_calls:
                            tool_in_tokens[t_name] = tool_in_tokens.get(t_name, 0) + dist_out
                            tool_out_tokens[t_name] = tool_out_tokens.get(t_name, 0) + dist_in
                        curr_tool_calls = []

                elif ptype == "user_message":
                    text = str(payload.get("message", "")).strip()
                    if text:
                        user_prompt_tokens += _count_user_tokens(text)
                        extracted = _get_clean_user_text(text)[:200]
                        if extracted and task == "untitled codex session":
                            task = extracted

                elif ptype == "exec_command_end":
                    cmd = _command_str(payload.get("command", ""))
                    if cmd:
                        exit_code = payload.get("exit_code")
                        stdout = str(payload.get("stdout") or "")[:1024]
                        stderr = str(payload.get("stderr") or "")[:1024]
                        commands_run.append(
                            CommandRecord(
                                command=cmd[:200],
                                exit_code=exit_code,
                                stdout=stdout,
                                stderr=stderr,
                            )
                        )
                        tools_called["shell"] = tools_called.get("shell", 0) + 1

                elif ptype == "patch_apply_end":
                    # changes = {absolute_path: {"type":"update","unified_diff":"..."}}
                    changes: dict[str, Any] = payload.get("changes") or {}
                    for fpath, change_data in changes.items():
                        files_touched.add(str(fpath))
                        diff_text = ""
                        if isinstance(change_data, dict):
                            diff_text = str(change_data.get("unified_diff") or "")[:4096]
                        if diff_text:
                            file_diffs[str(fpath)] = diff_text
                    if changes:
                        tools_called["patch"] = tools_called.get("patch", 0) + 1

                elif ptype == "mcp_tool_call_end":
                    invocation = payload.get("invocation") or {}
                    tool_name = invocation.get("tool", "mcp")
                    tools_called[tool_name] = tools_called.get(tool_name, 0) + 1

            elif ev_type == "response_item":
                # Newer CLI still wraps in event_msg but also emits response_item
                payload = ev.get("payload") or {}
                ptype = payload.get("type", "")

                # Extract reasoning content from reasoning response items
                if ptype == "reasoning":
                    reasoning_text = str(payload.get("summary") or payload.get("text") or "")
                    if reasoning_text:
                        reasoning_snippets.append(reasoning_text[:500])

                if ptype == "function_call":
                    name = payload.get("name", "")
                    tools_called[name] = tools_called.get(name, 0) + 1
                    args_str = payload.get("arguments", "{}")
                    try:
                        args: dict[str, Any] = json.loads(args_str)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_args[name] = args
                    curr_tool_calls.append((name, args))
                    if name == "apply_patch":
                        patch_text = args.get("patch", "")
                        for fp in _files_from_patch(patch_text):
                            files_touched.add(fp)
                        if patch_text:
                            # Store the full patch as diff for each file
                            for fp in _files_from_patch(patch_text):
                                file_diffs[fp] = patch_text[:4096]
                    elif name in ("exec_command", "shell_command"):
                        cmd = str(args.get("cmd") or args.get("command") or "")
                        if cmd:
                            commands_run.append(cmd[:200])

                elif ptype == "custom_tool_call":
                    name = payload.get("name", "custom_tool")
                    tools_called[name] = tools_called.get(name, 0) + 1
                    curr_tool_calls.append((name, payload.get("input", "")))
                    if name == "apply_patch":
                        patch_text = str(payload.get("input", ""))
                        for fp in _files_from_patch(patch_text):
                            files_touched.add(fp)
                        if patch_text:
                            for fp in _files_from_patch(patch_text):
                                file_diffs[fp] = patch_text[:4096]

        # Build enriched files_touched
        files_enriched: list[str | FileEditRecord] = []
        for f in sorted(files_touched):
            if f in file_diffs:
                files_enriched.append(FileEditRecord(path=f, diff=file_diffs[f], event="edit"))
            else:
                files_enriched.append(f)

        if any((final_total_in, final_total_out, final_total_think, final_total_cached)) and len(models_seen) <= 1:
            usage_entries = []
            fallback_usage = make_llm_usage_entry(
                model=model_seen,
                input_tokens=max(final_total_in - final_total_cached, 0),
                output_tokens=final_total_out,
                thinking_tokens=final_total_think,
                cached_input_tokens=final_total_cached,
                source_type="codex.event_msg.total_token_usage",
            )
            if fallback_usage is not None:
                usage_entries.append(fallback_usage)
        elif not usage_entries and any((final_total_in, final_total_out, final_total_think, final_total_cached)):
            fallback_usage = make_llm_usage_entry(
                model=model_seen,
                input_tokens=max(final_total_in - final_total_cached, 0),
                output_tokens=final_total_out,
                thinking_tokens=final_total_think,
                cached_input_tokens=final_total_cached,
                source_type="codex.event_msg.total_token_usage",
            )
            if fallback_usage is not None:
                usage_entries.append(fallback_usage)

        usage_summary = summarize_usage_entries(usage_entries, fallback_model=model_seen)

        # ── Build Trace with reasoning ───────────────────────────────────────────────
        return Trace(
            id=f"codex-{session_id}",
            session_id=session_id,
            agent="atelier:code",
            host="codex",
            domain="coding",
            task=task,
            status="success",
            files_touched=cast(Any, files_enriched),
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
            commands_run=cast(Any, commands_run),
            errors_seen=[],
            validation_results=[],
            raw_artifact_ids=[artifact_id],
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
            created_at=created_at,
        )

    # ------------------------------------------------------------------
    # Format B: flat objects (Codex TUI / newer builds)
    # ------------------------------------------------------------------

    def _parse_flat(self, session_id: str, raw_content: str, artifact_id: str) -> Trace:
        tools_called: dict[str, int] = {}
        tool_args: dict[str, dict[str, Any] | None] = {}
        files_touched: set[str] = set()
        file_diffs: dict[str, str] = {}
        commands_run: list[str | CommandRecord] = []
        reasoning_snippets: list[str] = []
        task = "untitled codex session"
        created_at = _utcnow()
        first_ts_set = False
        user_prompt_tokens = 0
        model_seen = ""
        usage_entries = []
        seen_event_ids: set[str] = set()
        previous_unidentified_event = ""

        for line in raw_content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_identity = _codex_event_identity(ev)
            if event_identity:
                if event_identity in seen_event_ids:
                    continue
                seen_event_ids.add(event_identity)
                previous_unidentified_event = ""
            elif line == previous_unidentified_event:
                continue
            else:
                previous_unidentified_event = line

            ev_type = ev.get("type")
            source_id = _codex_event_source_id(ev, line)

            if not first_ts_set and ev.get("timestamp"):
                created_at = _parse_ts(ev.get("timestamp"))
                first_ts_set = True

            model_name = ev.get("model") or ev.get("model_id") or ev.get("modelId")
            if model_name:
                model_seen = str(model_name)

            # Extract reasoning block (Format B)
            if ev_type == "reasoning":
                reasoning_text = str(ev.get("summary") or ev.get("text") or "")
                if reasoning_text:
                    reasoning_snippets.append(reasoning_text[:500])

            # Session meta: flat object with no "type" field
            if ev_type is None and "id" in ev and "timestamp" in ev and not first_ts_set:
                created_at = _parse_ts(ev.get("timestamp"))
                first_ts_set = True
                continue

            if ev_type == "message":
                ts = ev.get("timestamp")
                if ts and not first_ts_set:
                    created_at = _parse_ts(ts)
                    first_ts_set = True

                if ev.get("role") == "assistant":
                    turn_in, turn_out, turn_think, turn_cached, turn_cache_write = _extract_flat_usage(ev.get("usage"))
                    usage_entry = make_llm_usage_entry(
                        model=model_seen,
                        input_tokens=max(turn_in - turn_cached, 0),
                        output_tokens=turn_out,
                        thinking_tokens=turn_think,
                        cached_input_tokens=turn_cached,
                        cache_creation_input_tokens=turn_cache_write,
                        source_type="codex.flat.message",
                        source_id=source_id,
                        created_at=_parse_ts(ev.get("timestamp")) if ev.get("timestamp") else None,
                    )
                    if usage_entry is not None:
                        usage_entries.append(usage_entry)

                if ev.get("role") == "user":
                    # Extract task from content blocks
                    for blk in ev.get("content") or []:
                        if not isinstance(blk, dict):
                            continue
                        btype = blk.get("type", "")
                        if btype not in ("input_text", "text"):
                            continue
                        text = str(blk.get("text", "")).strip()
                        if text:
                            user_prompt_tokens += _count_user_tokens(text)
                            extracted = _get_clean_user_text(text)[:200]
                            if extracted and task == "untitled codex session":
                                task = extracted
                                break

            elif ev_type == "function_call":
                name = str(ev.get("name") or ev.get("function", {}).get("name", "unknown"))
                tools_called[name] = tools_called.get(name, 0) + 1

                args_raw = ev.get("arguments", "{}")
                if isinstance(args_raw, dict):
                    args: dict[str, Any] = args_raw
                else:
                    try:
                        args = json.loads(str(args_raw))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                tool_args[name] = args

                if name == "apply_patch":
                    patch_text = str(args.get("patch", ""))
                    for fp in _files_from_patch(patch_text):
                        files_touched.add(fp)
                    if patch_text:
                        for fp in _files_from_patch(patch_text):
                            file_diffs[fp] = patch_text[:4096]
                elif name in ("exec_command", "shell_command"):
                    cmd = str(args.get("cmd") or args.get("command") or "")
                    if cmd:
                        commands_run.append(cmd[:200])

        # Build enriched files_touched
        files_enriched: list[str | FileEditRecord] = []
        for f in sorted(files_touched):
            if f in file_diffs:
                files_enriched.append(FileEditRecord(path=f, diff=file_diffs[f], event="edit"))
            else:
                files_enriched.append(f)

        usage_summary = summarize_usage_entries(usage_entries, fallback_model=model_seen)

        return Trace(
            id=f"codex-{session_id}",
            session_id=session_id,
            agent="atelier:code",
            host="codex",
            domain="coding",
            task=task,
            status="success",
            files_touched=cast(Any, files_enriched),
            tools_called=[
                ToolCall(name=n, args_hash="", count=c, args=tool_args.get(n)) for n, c in tools_called.items()
            ],
            commands_run=cast(Any, commands_run),
            errors_seen=[],
            validation_results=[],
            raw_artifact_ids=[artifact_id],
            reasoning=reasoning_snippets,
            input_tokens=usage_summary["input_tokens"],
            output_tokens=usage_summary["output_tokens"],
            thinking_tokens=usage_summary["thinking_tokens"],
            cached_input_tokens=usage_summary["cached_input_tokens"],
            cache_creation_input_tokens=usage_summary["cache_creation_input_tokens"],
            model=usage_summary["model"],
            usage_entries=usage_summary["usage_entries"],
            model_usages=usage_summary["model_usages"],
            user_prompt_tokens=user_prompt_tokens,
            created_at=created_at,
        )


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def _detect_format(raw_content: str) -> str:
    """Return 'event_msg' or 'flat' based on the first parseable event."""
    for line in raw_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev_type = ev.get("type")
        if ev_type == "session_meta":
            return "event_msg"
        if ev_type in ("message", "reasoning", "function_call", "function_call_output"):
            return "flat"
        if ev_type is None and "id" in ev and "timestamp" in ev:
            # Flat format: first line is session meta without "type"
            return "flat"
        # Unknown type — assume event_msg format
        return "event_msg"
    return "event_msg"
