"""Shared helpers for normalized host session imports."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.pricing import is_placeholder_model, usage_cost_usd
from lemoncrow.core.foundation.models import (
    CommandRecord,
    FileEditRecord,
    ModelUsage,
    RawArtifact,
    ToolCall,
    Trace,
    UsageEntry,
)
from lemoncrow.core.foundation.redaction import redact
from lemoncrow.infra.storage.bundle import StoreBundle
from lemoncrow.pro.capabilities.prompt_compilation.tokens import approx_tokens

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

# System-message prefixes marking host-injected content (permissions
# banners, environment context, IDE-injected commands) rather than real user
# input. Shared by claude.py's Trace user-text extraction and
# _session_parser.py's turn rendering so the two lists can't drift apart.
_SYSTEM_PREFIXES_CLAUDE = (
    "I have been initialized",
    "Environment context:",
    "<environment_context>",
    "<permissions instructions>",
    "<local-command",
    "<ide_",
    "<command-",
    "<thinking>",
)

# Claude Code and the normalized hosts wrap the real task in an XML-ish
# envelope. Shared by claude.py and _session_parser.py so the two copies
# can't drift; group(2) is the wrapped text.
#
# Both quantifiers are bounded for the same reason redaction.py bounds its
# own (#38): an unbounded ``[^>]*``/``.*?`` before a required closing
# literal makes the engine walk to end-of-string once per "<task" opener, so
# a multi-MB tool-output blob that happens to contain such openers (and no
# close tag) costs O(n) per occurrence. 64KB is a generous wrapped-task size
# and matches the window used for ``<think>`` blocks in redaction.py.
_TASK_WRAPPER_RE = re.compile(
    r"<(task|prompt|request|question)[^>]{0,256}>(.{0,65536}?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Necessary condition for _TASK_WRAPPER_RE to match anywhere, checked first
# because it is a single O(n) C-level scan. Bounding alone leaves a brutal
# constant -- ~13s/MB on text densely seeded with unmatched "<task>" openers,
# since each opener still pays a 64KB window scan. Skipping outright when no
# closing tag exists is exact (a match requires one), not heuristic.
_TASK_WRAPPER_CLOSE_RE = re.compile(r"</(?:task|prompt|request|question)>", re.IGNORECASE)


def extract_task_wrapper(text: str) -> str | None:
    """Return the text wrapped in ``<task>``/``<prompt>``/``<request>``/
    ``<question>`` tags, or ``None`` when *text* carries no such wrapper.
    """
    if _TASK_WRAPPER_CLOSE_RE.search(text) is None:
        return None
    match = _TASK_WRAPPER_RE.search(text)
    return match.group(2).strip() if match else None


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
        try:
            stamp = float(value)
            if stamp < 1e12:
                stamp *= 1000
            return datetime.fromtimestamp(stamp / 1000, tz=UTC)
        except (OSError, ValueError, OverflowError, TypeError):
            logger.warning(
                "Suppressed out-of-range numeric timestamp at _common.py",
                exc_info=True,
            )
            return default or utcnow()
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


# Fast char-only token estimate for import gating; canonical home is
# prompt_compilation.tokens.approx_tokens. Kept importable under this name.
char_tokens = approx_tokens


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
    reasoning_output_tokens: int = 0,
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
                "reasoningOutput": min(reasoning_output_tokens, output_tokens),
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


def make_llm_usage_entry(
    *,
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    thinking_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    source_type: str = "",
    source_id: str = "",
    created_at: datetime | None = None,
) -> UsageEntry | None:
    model_id = str(model or "").strip()
    usage_values = {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "reasoning_output_tokens": min(
            int(reasoning_output_tokens or 0),
            int(output_tokens or 0),
        ),
        "thinking_tokens": int(thinking_tokens or 0),
        "cached_input_tokens": int(cached_input_tokens or 0),
        "cache_creation_input_tokens": int(cache_creation_input_tokens or 0),
    }
    if not model_id and not any(usage_values.values()):
        return None
    cost_usd = usage_cost_usd(
        model_id or "_default",
        input_tokens=usage_values["input_tokens"],
        output_tokens=usage_values["output_tokens"],
        cache_read_tokens=usage_values["cached_input_tokens"],
        cache_write_tokens=usage_values["cache_creation_input_tokens"],
        thinking_tokens=usage_values["thinking_tokens"],
    )
    return UsageEntry(
        kind="llm",
        model=model_id,
        input_tokens=usage_values["input_tokens"],
        output_tokens=usage_values["output_tokens"],
        reasoning_output_tokens=usage_values["reasoning_output_tokens"],
        thinking_tokens=usage_values["thinking_tokens"],
        cached_input_tokens=usage_values["cached_input_tokens"],
        cache_creation_input_tokens=usage_values["cache_creation_input_tokens"],
        cost_usd=cost_usd,
        source_type=source_type,
        source_id=source_id,
        created_at=created_at,
    )


def _normalized_event_identity(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or event.get("_type") or "")
    event_id = str(event.get("id") or event.get("event_id") or event.get("eventId") or "").strip()
    if not event_id:
        message = event.get("message") or event.get("data") or {}
        if isinstance(message, dict):
            event_id = str(message.get("id") or message.get("messageId") or message.get("message_id") or "").strip()
    return f"{event_type}:{event_id}" if event_id else ""


def make_tool_usage_entry(
    *,
    tool_name: str,
    cost_usd: float,
    source_type: str = "",
    source_id: str = "",
    created_at: datetime | None = None,
) -> UsageEntry | None:
    tool = str(tool_name or "").strip()
    cost = float(cost_usd or 0.0)
    if not tool and cost == 0.0:
        return None
    return UsageEntry(
        kind="tool",
        tool_name=tool,
        cost_usd=cost,
        source_type=source_type,
        source_id=source_id,
        created_at=created_at,
    )


def summarize_usage_entries(
    entries: Iterable[UsageEntry],
    *,
    fallback_model: str = "",
) -> dict[str, Any]:
    usage_entries = [entry for entry in entries if isinstance(entry, UsageEntry)]
    aggregated: dict[str, dict[str, int]] = {}
    total_input = 0
    total_output = 0
    total_reasoning_output = 0
    total_thinking = 0
    total_cache_read = 0
    total_cache_write = 0
    unique_models: set[str] = set()

    for entry in usage_entries:
        if entry.kind != "llm":
            continue
        total_input += int(entry.input_tokens or 0)
        total_output += int(entry.output_tokens or 0)
        total_reasoning_output += int(entry.reasoning_output_tokens or 0)
        total_thinking += int(entry.thinking_tokens or 0)
        total_cache_read += int(entry.cached_input_tokens or 0)
        total_cache_write += int(entry.cache_creation_input_tokens or 0)

        model_id = str(entry.model or "").strip()
        # Placeholder ids like "<synthetic>" should not influence the trace's
        # single resolved model — they're noise that would otherwise either
        # become the displayed model or force us to bucket as multi-model.
        if model_id and not is_placeholder_model(model_id):
            unique_models.add(model_id)
        if not model_id and not any(
            (
                entry.input_tokens,
                entry.output_tokens,
                entry.reasoning_output_tokens,
                entry.thinking_tokens,
                entry.cached_input_tokens,
                entry.cache_creation_input_tokens,
            )
        ):
            continue

        bucket = aggregated.setdefault(
            model_id,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "thinking_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )
        bucket["input_tokens"] += int(entry.input_tokens or 0)
        bucket["output_tokens"] += int(entry.output_tokens or 0)
        bucket["reasoning_output_tokens"] += int(entry.reasoning_output_tokens or 0)
        bucket["thinking_tokens"] += int(entry.thinking_tokens or 0)
        bucket["cached_input_tokens"] += int(entry.cached_input_tokens or 0)
        bucket["cache_creation_input_tokens"] += int(entry.cache_creation_input_tokens or 0)

    model_usages = [ModelUsage(model=model, **usage) for model, usage in aggregated.items()]
    single_model = next(iter(unique_models)) if len(unique_models) == 1 else ""
    if not single_model and not unique_models:
        candidate = str(fallback_model or "").strip()
        single_model = "" if is_placeholder_model(candidate) else candidate

    return {
        "usage_entries": usage_entries,
        "model_usages": model_usages,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "reasoning_output_tokens": min(total_reasoning_output, total_output),
        "thinking_tokens": total_thinking,
        "cached_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_write,
        "model": single_model,
    }


def build_normalized_jsonl(events: Iterable[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events if event)


def infer_time_bounds(
    raw_content: str,
    *,
    default: datetime | None = None,
) -> tuple[datetime, datetime]:
    started_at = default or utcnow()
    ended_at = started_at
    seen = False

    for line in raw_content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        timestamp = event.get("timestamp") or event.get("created_at") or event.get("updated_at")
        if not timestamp:
            continue
        parsed = parse_datetime(timestamp, default=started_at)
        if not seen:
            started_at = parsed
            ended_at = parsed
            seen = True
            continue
        if parsed < started_at:
            started_at = parsed
        if parsed > ended_at:
            ended_at = parsed

    return started_at, ended_at


def persist_imported_run_snapshot(
    store: StoreBundle,
    trace: Trace,
    *,
    started_at: datetime,
    ended_at: datetime,
) -> Path:
    from lemoncrow.core.foundation.paths import session_dir

    run_dir = session_dir(store.history.root, trace.host or "claude", trace.session_id or trace.id)
    run_dir.mkdir(parents=True, exist_ok=True)

    calls: list[dict[str, Any]] = []
    total_cost_usd = 0.0
    for entry in trace.usage_entries:
        if entry.kind != "llm":
            continue
        model_id = str(entry.model or trace.model or "_default")
        cost_usd = float(entry.cost_usd or 0.0)
        if cost_usd <= 0:
            cost_usd = usage_cost_usd(
                model_id,
                input_tokens=int(entry.input_tokens or 0),
                output_tokens=int(entry.output_tokens or 0),
                cache_read_tokens=int(entry.cached_input_tokens or 0),
                cache_write_tokens=int(entry.cache_creation_input_tokens or 0),
                thinking_tokens=int(entry.thinking_tokens or 0),
            )
        calls.append(
            {
                "operation": entry.source_type or "imported.message",
                "model": model_id,
                "input_tokens": int(entry.input_tokens or 0),
                "output_tokens": int(entry.output_tokens or 0),
                "cache_read_tokens": int(entry.cached_input_tokens or 0),
                "cache_write_tokens": int(entry.cache_creation_input_tokens or 0),
                "thinking_tokens": int(entry.thinking_tokens or 0),
                "cost_usd": round(cost_usd, 8),
                "at": (
                    entry.created_at.isoformat() if isinstance(entry.created_at, datetime) else ended_at.isoformat()
                ),
                "source_id": entry.source_id,
            }
        )
        total_cost_usd += cost_usd

    payload = {
        "session_id": trace.session_id or trace.id,
        "agent": trace.agent,
        "task": trace.task,
        "domain": trace.domain,
        "status": "done" if trace.status != "failed" else "failed",
        "tool_call_count": sum(int(tool.count or 0) for tool in trace.tools_called),
        "created_at": started_at.isoformat(),
        "updated_at": ended_at.isoformat(),
        "agent_settings": dict(trace.agent_settings),
        "skills": list(trace.skills),
        "telemetry": dict(trace.telemetry),
        "raw_artifact_ids": list(trace.raw_artifact_ids),
        "cost": {
            "calls": calls,
            "total_cost_usd": round(total_cost_usd, 6),
            "total_input_tokens": sum(int(call["input_tokens"]) for call in calls),
            "total_output_tokens": sum(int(call["output_tokens"]) for call in calls),
            "total_cache_read_tokens": sum(int(call["cache_read_tokens"]) for call in calls),
            "total_cache_write_tokens": sum(int(call["cache_write_tokens"]) for call in calls),
        },
        "events": [],
    }

    path = run_dir / "run.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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
    usage_entries: list[UsageEntry] = []
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
    agent_settings: dict[str, Any] = {}
    skills: list[str] = []
    synthetic_count = 0
    provider_id = ""
    seen_event_ids: set[str] = set()
    previous_unidentified_event = ""

    for line in raw_content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        event_identity = _normalized_event_identity(event)
        if event_identity:
            if event_identity in seen_event_ids:
                continue
            seen_event_ids.add(event_identity)
            previous_unidentified_event = ""
        elif stripped == previous_unidentified_event:
            continue
        else:
            previous_unidentified_event = stripped

        event_type = event.get("type") or event.get("_type")
        if event_type == "session":
            created_at = parse_datetime(event.get("timestamp"), default=created_at)
            if not task_text:
                title = str(event.get("title") or "").strip()
                if title:
                    task_text = redact(title)

            # Extract settings if available
            if "settings" in event:
                agent_settings.update(event["settings"])
            if "skills" in event:
                skills.extend(event["skills"])
            continue

        if event_type == "message":
            data = event.get("data") or {}
            if data.get("synthetic"):
                synthetic_count += 1
            if not provider_id:
                provider_id = data.get("providerID") or data.get("provider") or ""

        if event_type != "message":
            continue

        message = event.get("message") or event.get("data") or {}
        role = str(message.get("role") or "")

        # Scrape for skills/settings in system messages or context
        if role == "user" or role == "system":
            texts_to_scan = []
            for part in message.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts_to_scan.append(part.get("text") or "")
            if message.get("text"):
                texts_to_scan.append(message["text"])

            for txt in texts_to_scan:
                # Look for MCP servers/skills
                if "active mcp servers" in txt.lower():
                    matches = re.findall(r"- ([\w.-]+)", txt)
                    skills.extend(matches)
                if "agent settings" in txt.lower():
                    # Bounded quantifiers (#38): greedy ``\w+`` immediately
                    # before a required ``:`` re-scans the whole run at every
                    # start offset, so a long colon-free word run in a big
                    # blob is O(n^2). Keys/values this long aren't settings.
                    matches = re.findall(r"(\w{1,64}):\s*(.{1,4096})", txt)
                    for k, v in matches:
                        agent_settings[k] = v.strip()
            if role == "system":
                continue

        message = event.get("message") or event.get("data") or {}
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
                        task_text = redact(first_user_text)
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

        usage_entry = make_llm_usage_entry(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            cached_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
            source_type="normalized.message",
            source_id=str(event.get("id") or ""),
            created_at=timestamp,
        )
        if usage_entry is not None:
            usage_entries.append(usage_entry)

        tool_blocks = [
            block for block in content if isinstance(block, dict) and block.get("type") in {"toolCall", "tool_use"}
        ]
        reasoning.extend(
            str(block.get("text") or block.get("thinking") or "").strip()
            for block in content
            if isinstance(block, dict)
            and block.get("type") in {"reasoning", "thinking"}
            and (block.get("text") or block.get("thinking"))
        )

        if tool_blocks:
            distributed_input = (input_tokens + cache_read + cache_write) // len(tool_blocks)
            distributed_output = (output_tokens + thinking_tokens) // len(tool_blocks)
            for block in tool_blocks:
                name = str(block.get("name") or "unknown")
                arguments = block.get("arguments")
                arguments_dict = arguments if isinstance(arguments, dict) else {}
                tools_called[name] = tools_called.get(name, 0) + 1
                tool_args.setdefault(name, arguments_dict or None)
                tool_input_tokens[name] = tool_input_tokens.get(name, 0) + distributed_output
                tool_output_tokens[name] = tool_output_tokens.get(name, 0) + distributed_input

                if _is_file_tool(name):
                    path = str(
                        arguments_dict.get("file_path")
                        or arguments_dict.get("path")
                        or arguments_dict.get("target_file")
                        or ""
                    ).strip()
                    if path:
                        files_touched[path] = FileEditRecord(
                            path=path,
                            diff=redact(
                                str(
                                    arguments_dict.get("diff")
                                    or arguments_dict.get("patch")
                                    or arguments_dict.get("content")
                                    or arguments_dict.get("new_string")
                                    or ""
                                )[:4096]
                            ),
                        )

                if _is_command_tool(name):
                    command = str(arguments_dict.get("command") or "").strip()
                    if command:
                        commands_run.append(CommandRecord(command=redact(command[:4096])))

    if not task_text:
        task_text = f"untitled {source} session"

    usage_summary = summarize_usage_entries(usage_entries, fallback_model=model_seen)

    telemetry: dict[str, Any] = {
        "synthetic_messages_count": synthetic_count,
    }
    if provider_id:
        telemetry["provider_id"] = provider_id

    trace = Trace(
        id=artifact.id,
        session_id=session_id,
        agent="lemoncrow:code",
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
        commands_run=list(commands_run),
        errors_seen=[],
        validation_results=[],
        raw_artifact_ids=[artifact.id],
        reasoning=[item for item in reasoning if item][:32],
        input_tokens=usage_summary["input_tokens"],
        user_prompt_tokens=user_prompt_tokens,
        output_tokens=usage_summary["output_tokens"],
        thinking_tokens=usage_summary["thinking_tokens"],
        cached_input_tokens=usage_summary["cached_input_tokens"],
        cache_creation_input_tokens=usage_summary["cache_creation_input_tokens"],
        model=usage_summary["model"],
        usage_entries=usage_summary["usage_entries"],
        model_usages=usage_summary["model_usages"],
        agent_settings=agent_settings,
        skills=unique_strings(skills),
        telemetry=telemetry,
        created_at=created_at,
        transcript_path=artifact.source_path,
    )
    return trace


_SIZE_LIMIT_BYTES = 500 * 1024 * 1024  # 500 MB

# Cap for sessions serialized in memory by the SQLite-backed importers
# (opencode/lemoncode/cursor). Those never touch a file, so ``_SIZE_LIMIT_BYTES``
# above -- a *file* skip enforced only by import_paths_with_progress -- never
# applies to them, leaving redaction + JSONL parsing unbounded (#38). 32MB is
# far above any real session (the pathological #38 report was ~20MB of inlined
# tool output) while capping worst-case redaction work at a couple of seconds.
_MAX_SERIALIZED_SESSION_BYTES = 32 * 1024 * 1024  # 32 MB

# Cap for a *single* serialized record. The #38 payload was one ~20MB inlined
# tool output on one line, so the whole-session cap alone would drop the entire
# remainder of the session -- including every ``step-finish`` token record,
# permanently zeroing that session's token/cost accounting (the ``time_updated``
# dedup means a re-import never revisits it). Eliding the oversized *payload*
# inside the record instead keeps the record valid and every later record intact.
_MAX_SERIALIZED_RECORD_BYTES = 1024 * 1024  # 1 MB

# Floor for the longest single string value kept inside a record that blew the
# per-record cap, used when the derived budget (see _record_byte_budget) leaves
# no room for more. 512 chars still identifies what the payload was.
_MIN_RECORD_STRING_CHARS = 512

# Marker appended in place of the dropped tail. It is a syntactically valid
# JSONL record with an ``_type`` no parser dispatches on, so truncation stays
# visible in the stored RawArtifact without adding a bogus turn or a
# malformed line to the event stream.
_TRUNCATION_MARKER_TYPE = "lemoncrow.truncated"

# Key stamped onto a record whose payload was elided. Parsers read known keys
# off each event, so an extra one is inert -- but it makes the loss visible to
# anyone reading the stored artifact.
_RECORD_TRUNCATED_KEY = "_truncated"


@dataclass(frozen=True)
class SerializedSession:
    """A serialized JSONL session plus honest metadata about the *original*.

    ``sha256_original`` / ``byte_count_original`` always describe the bytes as
    they were before any capping, so a RawArtifact built from them keeps saying
    what the source actually held; ``text`` is what is safe to store and parse.
    """

    text: str
    sha256_original: str
    byte_count_original: int
    truncated: bool


def _elide_long_strings(value: Any, max_chars: int, state: dict[str, int]) -> Any:
    """Recursively replace string values longer than *max_chars* with a stub."""
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        dropped = len(value) - max_chars
        state["elided"] += dropped
        return f"{value[:max_chars]}...[lemoncrow: elided {dropped} chars]"
    if isinstance(value, dict):
        return {key: _elide_long_strings(item, max_chars, state) for key, item in value.items()}
    if isinstance(value, list):
        return [_elide_long_strings(item, max_chars, state) for item in value]
    return value


def _record_byte_budget() -> int:
    """Per-record byte budget, resolved at call time so both caps stay overridable.

    Never more than an eighth of the whole-session cap: a record allowed to eat
    the entire session budget defeats the point of capping records first, and
    deriving one from the other means they cannot be configured into that state.
    """
    return min(_MAX_SERIALIZED_RECORD_BYTES, max(1024, _MAX_SERIALIZED_SESSION_BYTES // 8))


def serialize_capped_record(
    record: dict[str, Any],
    *,
    max_bytes: int | None = None,
) -> tuple[str, str]:
    """Serialize *record* as one JSONL line, eliding oversized string payloads.

    Returns ``(line, original_line)`` -- the same object twice when nothing was
    elided, so callers can test identity. ``original_line`` lets the caller hash
    and size the true original while retaining only the capped text.
    """
    max_bytes = _record_byte_budget() if max_bytes is None else max_bytes
    line = json.dumps(record, ensure_ascii=False)
    original_bytes = len(line.encode("utf-8"))
    if original_bytes <= max_bytes:
        return line, line

    # A quarter of the record budget per string leaves room for the record's
    # other fields and the marker; never below _MIN_RECORD_STRING_CHARS, so a
    # tiny budget still yields an identifiable payload head.
    max_string_chars = max(_MIN_RECORD_STRING_CHARS, max_bytes // 4)
    state = {"elided": 0}
    capped: dict[str, Any] = {key: _elide_long_strings(value, max_string_chars, state) for key, value in record.items()}
    if not state["elided"]:
        # Oversized but not because of any single string (many small fields);
        # nothing safe to drop, so keep the record whole.
        return line, line
    capped[_RECORD_TRUNCATED_KEY] = {
        "original_byte_count": original_bytes,
        "elided_chars": state["elided"],
    }
    return json.dumps(capped, ensure_ascii=False), line


def truncate_serialized_session(
    text: str,
    *,
    source: str,
    session_id: str,
    limit: int | None = None,
) -> SerializedSession:
    """Cap an in-memory serialized JSONL session at *limit* bytes.

    Backstop only: prefer :func:`serialize_capped_record` to cap the oversized
    *payload* inside a record, which preserves every later record. This drops
    the tail wholesale and is what remains when a session is huge in aggregate.

    Truncates on a line boundary and appends a marker line recording the
    original size, so downstream ``json.loads`` per line only ever sees whole
    records. When even the *first* record exceeds *limit* there is no line
    boundary to cut on, and the head is dropped entirely rather than emitting a
    byte-truncated fragment that fails to parse. ``limit=None`` resolves
    ``_MAX_SERIALIZED_SESSION_BYTES`` at call time rather than at def time, so
    the cap stays overridable.
    """
    limit = _MAX_SERIALIZED_SESSION_BYTES if limit is None else limit
    encoded = text.encode("utf-8")
    original = len(encoded)
    digest = sha256_text(text)
    if original <= limit:
        return SerializedSession(text=text, sha256_original=digest, byte_count_original=original, truncated=False)

    head = encoded[:limit].decode("utf-8", errors="ignore")
    cut = head.rfind("\n")
    # cut == -1 means the first record alone blows the cap: any head we kept
    # would be a mid-record fragment that fails json.loads. Keep none of it.
    head = head[:cut] if cut != -1 else ""
    marker = json.dumps(
        {
            "_type": _TRUNCATION_MARKER_TYPE,
            "source": source,
            "session_id": session_id,
            "limit_bytes": limit,
            "original_byte_count": original,
        },
        ensure_ascii=False,
    )
    logger.warning(
        "%s: truncating oversized session %s (%.1fMB > %.1fMB limit)",
        source,
        session_id,
        original / 1e6,
        limit / 1e6,
    )
    return SerializedSession(
        text=f"{head}\n{marker}" if head else marker,
        sha256_original=digest,
        byte_count_original=original,
        truncated=True,
    )


def import_paths_with_progress(
    source: str,
    paths: list[Path],
    import_fn: Any,
    size_limit: int = _SIZE_LIMIT_BYTES,
) -> list[str]:
    """Iterate *paths*, print Gemini-style progress, call *import_fn(path)* for each."""
    total = len(paths)
    logger.info("%s: discovering sessions (found %d)", source, total)
    imported: list[str] = []
    for i, path in enumerate(paths):
        try:
            size = path.stat().st_size
            if size > size_limit:
                logger.warning(
                    "%s: skipping massive session %s (%.1fMB)",
                    source,
                    path.name,
                    size / 1e6,
                )
                continue
            if i % 10 == 0 and i > 0:
                logger.info("%s: importing %d/%d...", source, i, total)
            sid = import_fn(path)
            if sid:
                imported.append(sid)
        except Exception:
            logger.exception("skipping %s session %s", source, path.name)
    return imported


_SIZE_LIMIT_BYTES = 500 * 1024 * 1024  # 500 MB


_SNAPSHOT_MAX_BYTES = 256 * 1024  # 256 KB — skip large / generated files


def snapshot_edited_files(
    store: StoreBundle,
    files_touched: list[FileEditRecord],
    *,
    session_id: str,
    source: str,
) -> int:
    """Read current on-disk content for each edited file and persist as a
    ``file.snapshot`` RawArtifact linked to *session_id*.

    Best-effort: missing, binary, or oversized files are silently skipped.
    Returns the number of files successfully snapshotted.
    """
    seen: set[str] = set()
    saved = 0
    for rec in files_touched:
        fpath = str(rec.path or "").strip()
        if not fpath or fpath in seen:
            continue
        seen.add(fpath)

        p = Path(fpath)
        try:
            if not p.is_file():
                continue
        except (PermissionError, OSError):
            logger.debug("Skipping inaccessible file: %s", fpath)
            continue

        try:
            size = p.stat().st_size
            if size > _SNAPSHOT_MAX_BYTES:
                continue
            raw_bytes = p.read_bytes()
            # Skip binary files
            try:
                file_content = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
        except OSError:
            continue

        snap_id = f"snap-{sanitize_id(session_id)}-{sha256_text(fpath)[:12]}"

        # Skip if already stored (idempotent re-imports)
        if store.history.get_raw_artifact(snap_id) is not None:
            saved += 1
            continue

        artifact = RawArtifact(
            id=snap_id,
            source=source,
            source_session_id=session_id,
            kind="file.snapshot",
            relative_path=fpath,
            content_path=(f"raw/{sanitize_id(source)}/snapshots/{sanitize_id(session_id)}/{sha256_text(fpath)}.txt"),
            sha256_original=sha256_text(file_content),
            sha256_redacted=sha256_text(file_content),
            byte_count_original=len(raw_bytes),
            byte_count_redacted=len(raw_bytes),
            created_at=utcnow(),
            source_file_mtime=None,
        )
        try:
            store.history.record_raw_artifact(artifact, file_content)
            saved += 1
        except Exception:
            logger.warning("snapshot_edited_files: failed to save %s", fpath, exc_info=True)

    return saved


def record_normalized_session(
    store: StoreBundle,
    *,
    source: str,
    session_id: str,
    relative_path: str,
    content_path: str,
    raw_content: str | SerializedSession,
    source_mtime: datetime | None,
    force: bool = False,
    task: str | None = None,
    trace_content: str | None = None,
    source_path: str | None = None,
) -> str | None:
    # A SerializedSession carries the pre-truncation hash/size, so a capped
    # session's artifact still reports what the source actually held. A plain
    # str is its own original.
    serialized = (
        raw_content
        if isinstance(raw_content, SerializedSession)
        else SerializedSession(
            text=raw_content,
            sha256_original=sha256_text(raw_content),
            byte_count_original=len(raw_content.encode("utf-8")),
            truncated=False,
        )
    )
    text = serialized.text
    artifact_id = f"{source}-{sanitize_id(session_id)}"
    if not force and source_mtime is not None:
        existing = store.history.get_raw_artifact(artifact_id)
        if existing and existing.source_file_mtime and source_mtime <= existing.source_file_mtime:
            return None

    redacted = redact(text)
    artifact = RawArtifact(
        id=artifact_id,
        source=source,
        source_session_id=session_id,
        kind="session.jsonl",
        relative_path=relative_path,
        content_path=content_path,
        sha256_original=serialized.sha256_original,
        sha256_redacted=sha256_text(redacted),
        byte_count_original=serialized.byte_count_original,
        byte_count_redacted=len(redacted.encode("utf-8")),
        created_at=utcnow(),
        source_file_mtime=source_mtime,
        source_path=source_path,
    )
    store.history.record_raw_artifact(artifact, redacted)

    normalized_content = trace_content if trace_content is not None else text
    trace = _build_trace_from_normalized_content(
        source=source,
        session_id=session_id,
        raw_content=normalized_content,
        artifact=artifact,
        task=task,
        source_mtime=source_mtime,
    )
    store.history.record_trace(trace, write_json=False)
    started_at, ended_at = infer_time_bounds(normalized_content, default=source_mtime)
    persist_imported_run_snapshot(store, trace, started_at=started_at, ended_at=ended_at)

    # Best-effort: snapshot current on-disk state of every edited file
    if trace.files_touched:
        file_recs = [r for r in trace.files_touched if isinstance(r, FileEditRecord)]
        if file_recs:
            snapshot_edited_files(store, file_recs, session_id=session_id, source=source)

    return trace.id
