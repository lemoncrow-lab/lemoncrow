"""Session parser for Atelier — converts raw JSONL session content into a
readable conversation timeline.

Public API::

    from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns
    turns = parse_session_turns(content, source="claude")

Supported sources: ``"claude"``, ``"codex"``, ``"opencode"``, ``"gemini"``, ``"copilot"``.
"""

from __future__ import annotations

import json
import mimetypes
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_NORMALIZED_SESSION_SOURCES = {
    "antigravity",
    "crush",
    "cursor",
    "cursor-agent",
    "droid",
    "goose",
    "kiro",
    "kilo-code",
    "omp",
    "openclaw",
    "pi",
    "qwen",
    "roo-code",
}

# Maximum number of turns to return
_MAX_TURNS = 1000

# System-message prefixes to skip in user blocks (Claude + Codex)
_SYSTEM_PREFIXES_CLAUDE = (
    "<local-command",
    "<ide_",
    "<command-",
    "<thinking>",
    "I have been initialized",
    "Environment context:",
)

_SYSTEM_PREFIXES_CODEX = (
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

_PASTED_CONTENT_TAG_RE = re.compile(r"<pasted_content\s+([^>]+?)\s*/?>", re.IGNORECASE)
_PASTED_CONTENT_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_MODEL_FIELD_KEYS = (
    "model",
    "modelId",
    "model_id",
    "modelName",
    "model_name",
    "assistantModel",
    "assistant_model",
    "currentModel",
    "current_model",
    "defaultModel",
    "default_model",
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_session_turns(content: str, source: str) -> list[dict[str, Any]]:
    """Parse raw JSONL *content* for *source* into a list of turn dicts."""
    if source == "claude":
        turns = _parse_claude(content)
    elif source == "codex":
        turns = _parse_codex(content)
    elif source == "opencode":
        turns = _parse_opencode(content)
    elif source == "gemini":
        turns = _parse_gemini(content)
    elif source == "copilot":
        turns = _parse_copilot(content)
    elif source in _NORMALIZED_SESSION_SOURCES:
        turns = _parse_normalized_session(content)
    else:
        turns = []

    return turns[:_MAX_TURNS]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _turn(
    kind: str,
    summary: str,
    content: str,
    at: str | Any | None = None,
    tokens: dict[str, int] | None = None,
    raw: dict[str, Any] | None = None,
    path: str | None = None,
    diff: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    # Normalise timestamp — accept ISO strings or ms-epoch integers (OpenCode)
    at_str: str | None = None
    if at:
        if isinstance(at, (int, float)):
            try:
                at_str = datetime.fromtimestamp(at / 1000, tz=UTC).isoformat()
            except (OSError, OverflowError, ValueError):
                at_str = str(at)
        else:
            at_str = str(at)

    turn: dict[str, Any] = {
        "kind": kind,
        "at": at_str,
        "summary": summary,
        "content": content,
        "tokens": tokens or {},
        "raw": raw,
    }
    model = extra.get("model") if "model" in extra else _extract_model_id(raw)
    if isinstance(model, str) and model.strip():
        turn["model"] = model.strip()
    # For file_edit turns, surface path + diff as top-level keys so the
    # frontend can render inline diffs without re-parsing the raw event.
    if kind == "file_edit":
        if path:
            turn["path"] = path
        if diff:
            turn["diff"] = diff
    for key, value in extra.items():
        if value is None:
            continue
        turn[key] = value
    return turn


def _normalize_turn_tokens(tokens: dict[str, Any] | None) -> dict[str, int]:
    payload = tokens if isinstance(tokens, dict) else {}
    return {
        "in": int(payload.get("in", 0) or 0),
        "out": int(payload.get("out", 0) or 0),
        "thinking": int(payload.get("thinking", 0) or 0),
        "cache_read": int(payload.get("cache_read", 0) or 0),
        "cache_write": int(payload.get("cache_write", 0) or 0),
    }


def _apply_tokens_once(
    turns: list[dict[str, Any]], tokens: dict[str, Any] | None
) -> list[dict[str, Any]]:
    normalized = _normalize_turn_tokens(tokens)
    if not turns or not any(normalized.values()):
        return turns

    preferred_kinds = (
        "agent_message",
        "thinking",
        "todo_write",
        "subagent_event",
        "file_edit",
        "shell_command",
        "tool_call",
        "user_message",
        "attachment",
    )
    target: dict[str, Any] | None = None
    for kind in preferred_kinds:
        target = next((turn for turn in turns if turn.get("kind") == kind), None)
        if target is not None:
            break
    if target is None:
        target = turns[0]

    for turn in turns:
        turn["tokens"] = normalized if turn is target else {}
    return turns


def _empty_usage_summary() -> dict[str, Any]:
    return {
        "started_model": None,
        "models_used": {},
        "total_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "usage_entries": [],
    }


def _record_usage_turn(
    summary: dict[str, Any],
    *,
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> None:
    model_id = str(model or "").strip()
    if model_id.lower() in {"unknown", "<synthetic>", "_default"}:
        model_id = ""

    token_values = (
        int(input_tokens or 0),
        int(output_tokens or 0),
        int(thinking_tokens or 0),
        int(cached_input_tokens or 0),
        int(cache_creation_input_tokens or 0),
    )
    if not model_id and not any(token_values):
        return

    if model_id:
        if summary["started_model"] is None:
            summary["started_model"] = model_id
        models_used = summary["models_used"]
        models_used[model_id] = int(models_used.get(model_id, 0)) + 1

    summary["total_turns"] += 1
    summary["input_tokens"] += token_values[0]
    summary["output_tokens"] += token_values[1]
    summary["thinking_tokens"] += token_values[2]
    summary["cached_input_tokens"] += token_values[3]
    summary["cache_creation_input_tokens"] += token_values[4]
    summary["usage_entries"].append(
        {
            "kind": "llm",
            "model": model_id,
            "input_tokens": token_values[0],
            "output_tokens": token_values[1],
            "thinking_tokens": token_values[2],
            "cached_input_tokens": token_values[3],
            "cache_creation_input_tokens": token_values[4],
        }
    )


def _merge_usage_summaries(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    if target["started_model"] is None and source.get("started_model"):
        target["started_model"] = source["started_model"]
    for key in (
        "total_turns",
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
    ):
        target[key] += int(source.get(key, 0) or 0)
    for model_id, count in dict(source.get("models_used") or {}).items():
        target["models_used"][model_id] = int(target["models_used"].get(model_id, 0)) + int(
            count or 0
        )
    target["usage_entries"].extend(list(source.get("usage_entries") or []))
    return target


def _extract_codex_usage(usage: Any) -> tuple[int, int, int, int, int]:
    if not isinstance(usage, dict):
        return (0, 0, 0, 0, 0)

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _int(
        usage.get("input_tokens")
        or usage.get("inputTokens")
        or usage.get("prompt_tokens")
        or usage.get("promptTokens")
    )
    output_tokens = _int(
        usage.get("output_tokens")
        or usage.get("outputTokens")
        or usage.get("completion_tokens")
        or usage.get("completionTokens")
    )
    cached_tokens = _int(
        usage.get("cached_input_tokens")
        or usage.get("cachedInputTokens")
        or usage.get("cache_read_tokens")
        or usage.get("cacheReadTokens")
    )
    input_details = usage.get("input_tokens_details") or usage.get("inputTokensDetails")
    if cached_tokens == 0 and isinstance(input_details, dict):
        cached_tokens = _int(
            input_details.get("cached_tokens")
            or input_details.get("cachedTokens")
            or input_details.get("cache_read_tokens")
            or input_details.get("cacheReadTokens")
        )

    cache_write_tokens = _int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cacheCreationInputTokens")
        or usage.get("cache_write_tokens")
        or usage.get("cacheWriteTokens")
    )
    if cache_write_tokens == 0 and isinstance(input_details, dict):
        cache_write_tokens = _int(
            input_details.get("cache_creation_input_tokens")
            or input_details.get("cacheCreationInputTokens")
            or input_details.get("cache_write_tokens")
            or input_details.get("cacheWriteTokens")
        )

    return (input_tokens, output_tokens, 0, cached_tokens, cache_write_tokens)


def _extract_model_id(value: Any, *, max_depth: int = 4) -> str | None:
    queue: list[tuple[Any, int]] = [(value, 0)]
    seen: set[int] = set()

    while queue:
        current, depth = queue.pop(0)
        if current is None or depth > max_depth:
            continue
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)

        if isinstance(current, dict):
            for key in _MODEL_FIELD_KEYS:
                raw_model = current.get(key)
                if isinstance(raw_model, str):
                    model = raw_model.strip()
                    if model and model.lower() not in {"unknown", "auto", "default"}:
                        return model
            if depth == max_depth:
                continue
            for child in current.values():
                if isinstance(child, (dict, list)):
                    queue.append((child, depth + 1))
            continue

        if isinstance(current, list):
            if depth == max_depth:
                continue
            for child in current:
                if isinstance(child, (dict, list)):
                    queue.append((child, depth + 1))

    return None


def _coerce_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            return json.loads(stripped)
        except Exception:
            return value
    return value


def _coerce_mapping(value: Any) -> dict[str, Any]:
    parsed = _coerce_jsonish(value)
    return parsed if isinstance(parsed, dict) else {}


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
    payload: dict[str, Any], keys: tuple[str, ...], *, limit: int | None = None
) -> str:
    for key in keys:
        if key not in payload:
            continue
        text = _text_from_value(payload.get(key))
        if text:
            return text[:limit] if limit is not None else text
    return ""


def _guess_mime_type(path: str | None) -> str | None:
    if not path:
        return None
    mime, _ = mimetypes.guess_type(path)
    return mime or None


def _extract_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        match = re.match(
            r"^\*\*\*\s+(?:Update|Add|Delete|Move)\s+File:\s+(.+)$", line, re.IGNORECASE
        )
        if match:
            paths.append(match.group(1).strip())
    return paths


def _normalize_todos(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    todos: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
            if text:
                todos.append({"content": text})
            continue
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("text") or item.get("title") or "").strip()
        if not content:
            continue
        todo: dict[str, Any] = {"content": content}
        for key in ("status", "priority", "id"):
            raw_value = item.get(key)
            if raw_value not in (None, ""):
                todo[key] = str(raw_value)
        todos.append(todo)
    return todos


def _extract_todos(value: Any) -> list[dict[str, Any]]:
    parsed = _coerce_jsonish(value)
    if isinstance(parsed, list):
        return _normalize_todos(parsed)
    if not isinstance(parsed, dict):
        return []

    metadata = parsed.get("metadata")
    metadata_todos = metadata.get("todos") if isinstance(metadata, dict) else None
    for candidate in (
        parsed.get("todos"),
        parsed.get("items"),
        metadata_todos,
        parsed.get("content"),
        parsed.get("output"),
    ):
        todos = _extract_todos(candidate) if isinstance(candidate, (dict, list, str)) else []
        if todos:
            return todos
    return []


def _make_attachment(
    *,
    attachment_type: str,
    path: str | None = None,
    display_name: str | None = None,
    content: str | None = None,
    size_label: str | None = None,
    line_count: int | None = None,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attachment: dict[str, Any] = {
        "type": attachment_type,
        "display_name": display_name
        or (Path(path).name if path else attachment_type.replace("_", " ")),
        "title": title or attachment_type.replace("_", " "),
    }
    if path:
        attachment["path"] = path
        mime_type = _guess_mime_type(path)
        if mime_type:
            attachment["mime_type"] = mime_type
    if content:
        attachment["content"] = content
    if size_label:
        attachment["size_label"] = size_label
    if line_count is not None:
        attachment["line_count"] = line_count
    if metadata:
        attachment["metadata"] = metadata
    return attachment


def _extract_pasted_content_attachment(text: str) -> dict[str, Any] | None:
    match = _PASTED_CONTENT_TAG_RE.search(text)
    if not match:
        return None
    attrs = dict(_PASTED_CONTENT_ATTR_RE.findall(match.group(1)))
    path = attrs.get("file")
    if not path:
        return None
    line_count: int | None = None
    raw_lines = attrs.get("lines")
    if raw_lines and raw_lines.isdigit():
        line_count = int(raw_lines)
    return _make_attachment(
        attachment_type="pasted_content",
        path=path,
        display_name=Path(path).name,
        size_label=attrs.get("size"),
        line_count=line_count,
        title="Pasted file",
    )


def extract_session_usage_summary(content: str, source: str) -> dict[str, Any]:
    if source == "claude":
        return _summarize_claude_usage(content)
    if source == "codex":
        return _summarize_codex_usage(content)
    if source == "copilot":
        return _summarize_copilot_usage(content)
    if source == "gemini":
        return _summarize_gemini_usage(content)
    if source == "opencode":
        return _summarize_opencode_usage(content)
    if source in _NORMALIZED_SESSION_SOURCES:
        return _usage_summary_from_turns(_parse_normalized_session(content))
    return _usage_summary_from_turns(parse_session_turns(content, source))


def _usage_summary_from_turns(turns: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _empty_usage_summary()
    for turn in turns:
        tokens = _normalize_turn_tokens(turn.get("tokens"))
        model_id = str(turn.get("model") or "").strip()
        if any(tokens.values()) or model_id:
            _record_usage_turn(
                summary,
                model=model_id,
                input_tokens=tokens["in"],
                output_tokens=tokens["out"],
                thinking_tokens=tokens["thinking"],
                cached_input_tokens=tokens["cache_read"],
                cache_creation_input_tokens=tokens["cache_write"],
            )
    return summary


def _summarize_claude_usage(content: str) -> dict[str, Any]:
    summary = _empty_usage_summary()
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        msg_id = str(msg.get("id") or ev.get("uuid") or ev.get("id") or "")
        if not msg_id:
            msg_id = f"assistant-{len(order)}"
        if msg_id not in merged:
            merged[msg_id] = {
                "model": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
            order.append(msg_id)
        usage = msg.get("usage") or {}
        record = merged[msg_id]
        record["model"] = str(msg.get("model") or record["model"] or "").strip()
        record["input_tokens"] = max(record["input_tokens"], int(usage.get("input_tokens", 0) or 0))
        record["output_tokens"] = max(
            record["output_tokens"], int(usage.get("output_tokens", 0) or 0)
        )
        record["cached_input_tokens"] = max(
            record["cached_input_tokens"],
            int(usage.get("cache_read_input_tokens", 0) or 0),
        )
        record["cache_creation_input_tokens"] = max(
            record["cache_creation_input_tokens"],
            int(usage.get("cache_creation_input_tokens", 0) or 0),
        )
    for msg_id in order:
        _record_usage_turn(summary, **merged[msg_id])
    return summary


def _summarize_codex_usage(content: str) -> dict[str, Any]:
    summary = _empty_usage_summary()
    current_model = ""
    saw_flat_usage = False
    legacy_totals: dict[str, dict[str, int | str]] = {}
    legacy_order: list[str] = []
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        et = ev.get("type")
        if et == "turn_context":
            payload = ev.get("payload") or {}
            model = str(payload.get("model") or "").strip()
            if model:
                current_model = model
            continue
        if et == "message" and str(ev.get("role") or "") == "assistant":
            model = str(ev.get("model") or current_model or "").strip()
            in_t, out_t, think_t, cached_t, cache_write_t = _extract_codex_usage(ev.get("usage"))
            if model or in_t or out_t or cached_t or cache_write_t:
                saw_flat_usage = True
                _record_usage_turn(
                    summary,
                    model=model,
                    input_tokens=max(in_t - cached_t, 0),
                    output_tokens=out_t,
                    thinking_tokens=think_t,
                    cached_input_tokens=cached_t,
                    cache_creation_input_tokens=cache_write_t,
                )
            continue
        if et != "event_msg" or saw_flat_usage:
            continue
        payload = ev.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        total_usage = info.get("total_token_usage") or {}
        model = current_model or str(payload.get("model") or "").strip()
        model_key = model or "_default"
        total_in, total_out, total_think, cached_total, cache_write_total = _extract_codex_usage(
            total_usage
        )
        if model_key not in legacy_totals:
            legacy_totals[model_key] = {
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
            legacy_order.append(model_key)
        bucket = legacy_totals[model_key]
        bucket["model"] = model
        bucket["input_tokens"] = max(int(bucket["input_tokens"]), max(total_in - cached_total, 0))
        bucket["output_tokens"] = max(int(bucket["output_tokens"]), total_out)
        bucket["thinking_tokens"] = max(int(bucket["thinking_tokens"]), total_think)
        bucket["cached_input_tokens"] = max(int(bucket["cached_input_tokens"]), cached_total)
        bucket["cache_creation_input_tokens"] = max(
            int(bucket["cache_creation_input_tokens"]), cache_write_total
        )
    if not saw_flat_usage:
        for model_key in legacy_order:
            bucket = legacy_totals[model_key]
            _record_usage_turn(
                summary,
                model=str(bucket["model"]),
                input_tokens=int(bucket["input_tokens"]),
                output_tokens=int(bucket["output_tokens"]),
                thinking_tokens=int(bucket["thinking_tokens"]),
                cached_input_tokens=int(bucket["cached_input_tokens"]),
                cache_creation_input_tokens=int(bucket["cache_creation_input_tokens"]),
            )
    return summary


def _summarize_copilot_usage(content: str) -> dict[str, Any]:
    fallback_summary = _empty_usage_summary()
    shutdown_summary = _empty_usage_summary()
    current_model = ""
    selected_model = ""
    assistant_turn_models: list[str] = []
    compaction_turns = 0
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        et = ev.get("type")
        data = ev.get("data") or {}
        if et == "session.start":
            selected = str(data.get("selectedModel") or "").strip()
            if selected and selected.lower() != "auto":
                selected_model = selected
                current_model = selected
            continue
        if et == "session.model_change":
            model = str(data.get("newModel") or "").strip()
            if model and model.lower() != "auto":
                current_model = model
            continue
        if et == "assistant.message":
            model = str(data.get("model") or current_model or selected_model or "").strip()
            if model and model.lower() == "auto":
                model = current_model or selected_model
            out_t = int(data.get("outputTokens", 0) or 0)
            if out_t > 0 or model:
                assistant_turn_models.append(model)
                _record_usage_turn(fallback_summary, model=model, output_tokens=out_t)
            continue
        if et == "session.compaction_complete":
            compaction = data.get("compactionTokensUsed") or {}
            if not isinstance(compaction, dict):
                continue
            model = str(compaction.get("model") or current_model or selected_model or "").strip()
            if model and model.lower() == "auto":
                model = current_model or selected_model
            _record_usage_turn(
                fallback_summary,
                model=model,
                input_tokens=int(compaction.get("inputTokens", 0) or 0),
                output_tokens=int(compaction.get("outputTokens", 0) or 0),
                thinking_tokens=int(compaction.get("reasoningTokens", 0) or 0),
                cached_input_tokens=int(compaction.get("cacheReadTokens", 0) or 0),
                cache_creation_input_tokens=int(compaction.get("cacheWriteTokens", 0) or 0),
            )
            compaction_turns += 1
            continue
        if et == "session.shutdown":
            metrics = data.get("modelMetrics") or {}
            if not isinstance(metrics, dict):
                continue
            for model, model_data in metrics.items():
                usage = (model_data or {}).get("usage") or {}
                _record_usage_turn(
                    shutdown_summary,
                    model=str(model).strip(),
                    input_tokens=int(usage.get("inputTokens", 0) or 0),
                    output_tokens=int(usage.get("outputTokens", 0) or 0),
                    thinking_tokens=int(usage.get("reasoningTokens", 0) or 0),
                    cached_input_tokens=int(usage.get("cacheReadTokens", 0) or 0),
                    cache_creation_input_tokens=int(usage.get("cacheWriteTokens", 0) or 0),
                )
    if shutdown_summary["total_turns"] > 0:
        if assistant_turn_models:
            shutdown_summary["total_turns"] = len(assistant_turn_models) + compaction_turns
            shutdown_summary["models_used"] = {}
            shutdown_summary["started_model"] = None
            for model in assistant_turn_models:
                model_id = str(model or "").strip()
                if not model_id:
                    continue
                if shutdown_summary["started_model"] is None:
                    shutdown_summary["started_model"] = model_id
                shutdown_summary["models_used"][model_id] = (
                    int(shutdown_summary["models_used"].get(model_id, 0)) + 1
                )
        return shutdown_summary
    return fallback_summary


def _summarize_gemini_usage(content: str) -> dict[str, Any]:
    summary = _empty_usage_summary()
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "gemini":
            continue
        mid = str(ev.get("id") or "")
        if not mid:
            continue
        if mid not in merged:
            merged[mid] = {
                "model": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
            order.append(mid)
        record = merged[mid]
        tokens = ev.get("tokens") or {}
        record["model"] = str(ev.get("model") or record["model"] or "").strip()
        record["input_tokens"] = max(record["input_tokens"], int(tokens.get("input", 0) or 0))
        record["output_tokens"] = max(record["output_tokens"], int(tokens.get("output", 0) or 0))
        record["thinking_tokens"] = max(
            record["thinking_tokens"], int(tokens.get("thoughts", 0) or 0)
        )
        record["cached_input_tokens"] = max(
            record["cached_input_tokens"], int(tokens.get("cached", 0) or 0)
        )
    for mid in order:
        _record_usage_turn(summary, **merged[mid])
    return summary


def _summarize_opencode_usage(content: str) -> dict[str, Any]:
    summary = _empty_usage_summary()
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("_type") != "message":
            continue
        data = ev.get("data") or {}
        if data.get("role") != "assistant":
            continue
        tokens = data.get("tokens") or {}
        cache = tokens.get("cache") or {}
        model_id = str(data.get("modelID") or data.get("model") or "").strip()
        provider_id = str(data.get("providerID") or "").strip()
        model = f"{provider_id}/{model_id}" if provider_id and model_id else model_id
        _record_usage_turn(
            summary,
            model=model,
            input_tokens=int(tokens.get("input", 0) or 0),
            output_tokens=int(tokens.get("output", 0) or 0),
            thinking_tokens=int(tokens.get("reasoning", 0) or 0),
            cached_input_tokens=int(cache.get("read", 0) or 0),
            cache_creation_input_tokens=int(cache.get("write", 0) or 0),
        )
    return summary


def _extract_text_from_claude_content(content: Any) -> str:
    """Extract plain text from a Claude user-message content field."""
    if isinstance(content, str):
        text = content.strip()

        # Handle User Commands (e.g. /plugin list)
        if text.startswith("<command-name>"):
            name_match = re.search(r"<command-name>(.*?)</command-name>", text)
            args_match = re.search(r"<command-args>(.*?)</command-args>", text)
            name = name_match.group(1).strip() if name_match else "unknown"
            args = args_match.group(1).strip() if args_match else ""
            return f"User ran command: {name} {args}".strip()

        if any(text.startswith(p) for p in _SYSTEM_PREFIXES_CLAUDE):
            return ""

        xml_match = re.search(r"<(task|prompt|request|question)[^>]*>(.*?)</\1>", text, re.I | re.S)
        if xml_match:
            return xml_match.group(2).strip()
        return text

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "").strip()
                if t and not any(t.startswith(p) for p in _SYSTEM_PREFIXES_CLAUDE):
                    xml_match = re.search(
                        r"<(task|prompt|request|question)[^>]*>(.*?)</\1>", t, re.I | re.S
                    )
                    parts.append(xml_match.group(2).strip() if xml_match else t)
        return "\n\n".join(parts)
    return ""


def _is_codex_system_message(text: str) -> bool:
    return any(text.strip().startswith(p) for p in _SYSTEM_PREFIXES_CODEX)


def _parse_normalized_session(content: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") != "message":
            continue
        message = event.get("message") or {}
        role = message.get("role")
        at = event.get("timestamp")
        usage = message.get("usage") or {}
        tokens = {
            "in": int(usage.get("input", 0) or 0),
            "out": int(usage.get("output", 0) or 0),
            "cache_read": int(usage.get("cacheRead", 0) or 0),
            "cache_write": int(usage.get("cacheWrite", 0) or 0),
        }
        blocks = message.get("content") or []

        if role == "user":
            texts = [
                str(block.get("text") or "").strip()
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            combined = "\n\n".join(text for text in texts if text).strip()
            if combined:
                turns.append(
                    _turn("user_message", combined[:80], combined, at=at, tokens=tokens, raw=event)
                )
            continue

        if role != "assistant":
            continue

        assistant_turns: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    assistant_turns.append(
                        _turn("agent_message", text[:80], text, at=at, tokens=tokens, raw=event)
                    )
            elif block_type in {"reasoning", "thinking"}:
                text = str(block.get("text") or "").strip()
                if text:
                    assistant_turns.append(
                        _turn("thinking", text[:80], text, at=at, tokens=tokens, raw=event)
                    )
            elif block_type in {"toolCall", "tool_use"}:
                name = str(block.get("name") or "unknown")
                _raw_args = block.get("arguments")
                arguments: dict[str, Any] = _raw_args if isinstance(_raw_args, dict) else {}
                command = str(arguments.get("command") or "").strip()
                path = str(
                    arguments.get("file_path")
                    or arguments.get("path")
                    or arguments.get("target_file")
                    or ""
                ).strip()
                lowered = name.lower()
                if command and (
                    "bash" in lowered
                    or lowered
                    in {"exec", "execute", "run_shell_command", "execute_command", "run_command"}
                ):
                    assistant_turns.append(
                        _turn(
                            "shell_command", command[:100], command, at=at, tokens=tokens, raw=event
                        )
                    )
                elif path and (
                    "edit" in lowered
                    or lowered in {"write", "create", "replace", "patch", "apply_patch"}
                ):
                    _raw_diff = arguments.get("diff") or arguments.get("patch")
                    if isinstance(_raw_diff, dict):
                        _raw_diff = _raw_diff.get("unified_diff") or None
                    if not _raw_diff and arguments.get("old_string") is not None:
                        old = str(arguments.get("old_string") or "")
                        new = str(arguments.get("new_string") or "")
                        _raw_diff = f"--- a/{path}\n+++ b/{path}\n"
                        for line in old.splitlines():
                            _raw_diff += f"-{line}\n"
                        for line in new.splitlines():
                            _raw_diff += f"+{line}\n"
                    _diff = str(_raw_diff).strip() if _raw_diff else None
                    content_text = _diff or str(
                        arguments.get("content")
                        or arguments.get("new_string")
                        or arguments.get("text")
                        or ""
                    )
                    assistant_turns.append(
                        _turn(
                            "file_edit",
                            f"{name}({path})",
                            content_text,
                            at=at,
                            tokens=tokens,
                            raw=event,
                            path=path or None,
                            diff=_diff,
                        )
                    )
                else:
                    assistant_turns.append(
                        _turn(
                            "tool_call",
                            f"{name}(...)" if arguments else name,
                            json.dumps(arguments, indent=2, ensure_ascii=False),
                            at=at,
                            tokens=tokens,
                            raw=event,
                        )
                    )
        turns.extend(_apply_tokens_once(assistant_turns, tokens))

    return turns


# ---------------------------------------------------------------------------
# Claude parser
# ---------------------------------------------------------------------------


def _parse_claude(content: str) -> list[dict[str, Any]]:
    """Parse Claude JSONL with message merging."""
    messages: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []

    # Store global metadata to yield if no messages exist
    meta: dict[str, Any] = {}

    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue

        et = ev.get("type", "")
        at = ev.get("timestamp")
        msg = ev.get("message") or {}
        msg_id = str(msg.get("id") or ev.get("uuid") or ev.get("id") or "")

        # Capture basic metadata
        if ev.get("sessionId") and not meta:
            meta = {"id": ev.get("sessionId"), "cwd": ev.get("cwd"), "at": at}

        if et in ("ai-title", "queue-operation", "progress", "last-prompt"):
            continue

        if et == "user":
            if ev.get("isMeta"):
                continue
            content_blocks = msg.get("content", "")
            text = _extract_text_from_claude_content(content_blocks)
            if text:
                if msg_id not in order:
                    order.append(msg_id)
                messages[msg_id] = [_turn("user_message", text[:80], text, at=at, raw=ev)]
            if isinstance(content_blocks, list):
                image_blocks = [
                    block
                    for block in content_blocks
                    if isinstance(block, dict) and block.get("type") == "image"
                ]
                if image_blocks:
                    image_turns = messages.setdefault(msg_id, [])
                    if msg_id not in order:
                        order.append(msg_id)
                    for block in image_blocks:
                        source = block.get("source") or {}
                        image_turns.append(
                            _turn(
                                "attachment",
                                "Attached image",
                                "",
                                at=at,
                                raw=ev,
                                attachments=[
                                    _make_attachment(
                                        attachment_type="image",
                                        display_name="Attached image",
                                        title="Attached image",
                                        metadata={
                                            "source_type": source.get("type"),
                                            "media_type": source.get("media_type"),
                                        },
                                    )
                                ],
                            )
                        )

        elif et == "assistant":
            if msg_id not in order:
                order.append(msg_id)
            usage = msg.get("usage") or {}
            tokens = {
                "in": usage.get("input_tokens", 0),
                "out": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_write": usage.get("cache_creation_input_tokens", 0),
            }

            blocks: list[dict[str, Any]] = []
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                if bt == "text":
                    t = block.get("text", "").strip()
                    if t:
                        blocks.append(
                            _turn("agent_message", t[:80], t, at=at, tokens=tokens, raw=ev)
                        )
                elif bt in ("thinking", "reasoning", "redacted"):
                    t = block.get("thinking") or block.get("text") or ""
                    if t:
                        blocks.append(_turn("thinking", t[:80], t, at=at, tokens=tokens, raw=ev))
                elif bt == "tool_use":
                    name = block.get("name", "unknown")
                    inp = block.get("input") or {}
                    lowered_name = str(name).lower()
                    todos = _extract_todos(inp)
                    if todos:
                        blocks.append(
                            _turn(
                                "todo_write",
                                f"{name} · {len(todos)} item{'s' if len(todos) != 1 else ''}",
                                "",
                                at=at,
                                tokens=tokens,
                                raw=ev,
                                tool_name=name,
                                arguments=inp,
                                todos=todos,
                            )
                        )
                        continue
                    if lowered_name in {"agent", "task"} or any(
                        key in inp for key in ("subagent_type", "agent", "description", "prompt")
                    ):
                        description = str(inp.get("description") or "").strip()
                        prompt = str(inp.get("prompt") or "").strip()
                        blocks.append(
                            _turn(
                                "subagent_event",
                                description[:80] or f"{name} subagent",
                                prompt or description,
                                at=at,
                                tokens=tokens,
                                raw=ev,
                                tool_name=name,
                                arguments=inp,
                                subagent_status="started",
                                subagent_name=str(
                                    inp.get("subagent_type") or inp.get("agent") or name
                                ),
                                subagent_description=description or None,
                            )
                        )
                        continue
                    kind = (
                        "file_edit"
                        if name in ("Edit", "Write", "MultiEdit")
                        else "shell_command"
                        if name == "Bash"
                        else "tool_call"
                    )

                    # High-fidelity extraction: use plain string for code/diffs
                    content_text = ""
                    file_path_str: str | None = None
                    diff_str: str | None = None
                    if kind == "file_edit":
                        file_path_str = (
                            str(inp.get("file_path") or inp.get("path") or "").strip() or None
                        )
                        raw_diff = inp.get("diff") or inp.get("patch")
                        # For Edit/MultiEdit: synthesise a mini unified diff from old/new strings
                        if not raw_diff and inp.get("old_string") is not None:
                            old = str(inp.get("old_string") or "")
                            new = str(inp.get("new_string") or "")
                            raw_diff = f"--- a/{file_path_str or 'file'}\n+++ b/{file_path_str or 'file'}\n"
                            for line in old.splitlines():
                                raw_diff += f"-{line}\n"
                            for line in new.splitlines():
                                raw_diff += f"+{line}\n"
                        diff_str = str(raw_diff).strip() if raw_diff else None
                        content_text = diff_str or str(
                            inp.get("content")
                            or inp.get("text")
                            or json.dumps(inp, indent=2, ensure_ascii=False)
                        )
                    elif kind == "shell_command":
                        content_text = str(inp.get("command") or "")
                    else:
                        content_text = json.dumps(inp, indent=2, ensure_ascii=False)

                    summary = (
                        f"{name}({file_path_str or ''})"
                        if kind == "file_edit"
                        else content_text[:100]
                        if kind == "shell_command"
                        else f"{name}(...)"
                    )
                    blocks.append(
                        _turn(
                            kind,
                            summary,
                            content_text,
                            at=at,
                            tokens=tokens,
                            raw=ev,
                            path=file_path_str,
                            diff=diff_str,
                        )
                    )

            if blocks:
                messages.setdefault(msg_id, []).extend(_apply_tokens_once(blocks, tokens))

        elif et == "attachment":
            attachment = ev.get("attachment") or {}
            attachment_type = str(attachment.get("type") or "").strip()
            if not attachment_type:
                continue
            visible_attachment_types = {
                "file",
                "directory",
                "edited_text_file",
                "already_read_file",
                "compact_file_reference",
                "diagnostics",
                "task_reminder",
                "todo_reminder",
                "command_permissions",
            }
            if attachment_type not in visible_attachment_types:
                continue
            aid = str(ev.get("uuid") or ev.get("id") or f"attachment-{len(order)}")
            if aid not in order:
                order.append(aid)

            file_info = None
            raw_content = attachment.get("content")
            if isinstance(raw_content, dict):
                file_info = (
                    raw_content.get("file") if isinstance(raw_content.get("file"), dict) else None
                )
            path = (
                str(
                    (file_info or {}).get("filePath")
                    or attachment.get("filename")
                    or attachment.get("displayPath")
                    or attachment.get("path")
                    or ""
                ).strip()
                or None
            )
            text_content = ""
            if isinstance(raw_content, str):
                text_content = raw_content
            elif file_info and isinstance(file_info.get("content"), str):
                text_content = str(file_info.get("content") or "")
            elif attachment_type == "diagnostics":
                text_content = json.dumps(
                    attachment.get("files") or attachment, indent=2, ensure_ascii=False
                )

            todos = _extract_todos(attachment.get("content") or attachment)
            if todos:
                messages[aid] = [
                    _turn(
                        "todo_write",
                        f"{attachment_type.replace('_', ' ')} · {len(todos)} item{'s' if len(todos) != 1 else ''}",
                        "",
                        at=at,
                        raw=ev,
                        todos=todos,
                        attachments=[
                            _make_attachment(
                                attachment_type=attachment_type,
                                path=path,
                                content=text_content or None,
                                title=attachment_type.replace("_", " "),
                                metadata=attachment,
                            )
                        ],
                    )
                ]
                continue

            messages[aid] = [
                _turn(
                    "attachment",
                    attachment_type.replace("_", " "),
                    text_content,
                    at=at,
                    raw=ev,
                    attachments=[
                        _make_attachment(
                            attachment_type=attachment_type,
                            path=path,
                            content=text_content or None,
                            line_count=int(
                                (file_info or {}).get("totalLines")
                                or (file_info or {}).get("numLines")
                                or 0
                            )
                            or None,
                            title=attachment_type.replace("_", " "),
                            metadata=attachment,
                        )
                    ],
                )
            ]

        elif et == "summary":
            text = str(ev.get("summary") or "").strip()
            if text:
                kind = "error" if "Error" in text else "agent_message"
                tid = f"summary-{hash(text)}"
                if tid not in order:
                    order.append(tid)
                messages[tid] = [_turn(kind, text[:80], text, at=at, raw=ev)]

    final = []
    for mid in order:
        final.extend(messages.get(mid, []))

    if not final and meta:
        final.append(
            _turn(
                "user_message",
                "Session Initialized",
                f"Metadata-only session: {json.dumps(meta)}",
                at=meta.get("at"),
            )
        )

    return final


# ---------------------------------------------------------------------------
# Codex parser
# ---------------------------------------------------------------------------


def _parse_codex(content: str) -> list[dict[str, Any]]:
    fmt = "event_msg"
    for line in content.splitlines():
        try:
            ev = json.loads(line)
            if ev.get("type") in ("message", "reasoning"):
                fmt = "flat"
                break
        except Exception:
            continue

    turns = _parse_codex_format_a(content) if fmt == "event_msg" else _parse_codex_format_b(content)

    if not turns:
        # Fallback to metadata turn for 100% reconstructability
        for line in content.splitlines():
            try:
                ev = json.loads(line)
                at = ev.get("timestamp")
                if ev.get("type") == "session_meta" or "instructions" in ev:
                    turns.append(
                        _turn(
                            "user_message",
                            "Session Initialized",
                            f"Session Metadata: {json.dumps(ev)}",
                            at=at,
                            raw=ev,
                        )
                    )
                    break
            except Exception:
                continue

    return turns


def _parse_codex_format_a(content: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    last_turn: dict[str, Any] | None = None
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        ev_type = ev.get("type")
        if ev_type not in {"event_msg", "response_item"}:
            continue
        payload = ev.get("payload") or {}
        pt = payload.get("type", "")
        at = ev.get("timestamp")

        info = payload.get("info") or {}
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if last and last_turn:
            curr = last_turn.setdefault("tokens", {})
            curr["in"] = curr.get("in", 0) + last.get("input_tokens", 0)
            curr["out"] = curr.get("out", 0) + last.get("output_tokens", 0)
            curr["thinking"] = curr.get("thinking", 0) + last.get("reasoning_output_tokens", 0)

        if pt == "user_message":
            msg = str(payload.get("message", "")).strip()
            if msg and not _is_codex_system_message(msg):
                last_turn = _turn("user_message", msg[:80], msg, at=at, raw=ev)
                turns.append(last_turn)
        elif pt == "agent_message":
            msg = str(payload.get("message", "")).strip()
            if msg:
                last_turn = _turn("agent_message", msg[:80], msg, at=at, raw=ev)
                turns.append(last_turn)
        elif pt == "message":
            role = str(payload.get("role") or "")
            blocks = payload.get("content") or []
            msg = "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict)
                and block.get("type") in {"input_text", "output_text", "text"}
            ).strip()
            if msg and not _is_codex_system_message(msg):
                last_turn = _turn(
                    "user_message" if role == "user" else "agent_message",
                    msg[:80],
                    msg,
                    at=at,
                    raw=ev,
                )
                turns.append(last_turn)
        elif pt == "exec_command_end":
            cmd = str(
                payload.get("command", "")[-1]
                if isinstance(payload.get("command"), list)
                else payload.get("command", "")
            )
            if cmd:
                last_turn = _turn("shell_command", cmd[:100], cmd, at=at, raw=ev)
                turns.append(last_turn)
        elif pt in (
            "file_edit",
            "file_write",
            "write_file",
            "edit_file",
            "patch_apply",
            "apply_patch",
            "file_create",
        ):
            _fpath = (
                str(
                    payload.get("path") or payload.get("file_path") or payload.get("filename") or ""
                ).strip()
                or None
            )
            _raw_diff = payload.get("diff") or payload.get("patch")
            if not _raw_diff and payload.get("old_string") is not None:
                old = str(payload.get("old_string") or "")
                new = str(payload.get("new_string") or "")
                _raw_diff = f"--- a/{_fpath or 'file'}\n+++ b/{_fpath or 'file'}\n"
                for _line in old.splitlines():
                    _raw_diff += f"-{_line}\n"
                for _line in new.splitlines():
                    _raw_diff += f"+{_line}\n"
            _diff = str(_raw_diff).strip() if _raw_diff else None
            content_text = _diff or str(
                payload.get("content")
                or payload.get("text")
                or json.dumps(payload, indent=2, ensure_ascii=False)
            )
            if content_text:
                last_turn = _turn(
                    "file_edit",
                    f"{pt}({_fpath or ''})",
                    content_text,
                    at=at,
                    raw=ev,
                    path=_fpath,
                    diff=_diff,
                )
                turns.append(last_turn)
        elif pt == "function_call":
            name = str(payload.get("name") or "unknown")
            args_raw = payload.get("arguments")
            args = _coerce_mapping(args_raw)
            todos = _extract_todos(args_raw)
            if todos:
                last_turn = _turn(
                    "todo_write",
                    f"{name} · {len(todos)} item{'s' if len(todos) != 1 else ''}",
                    "",
                    at=at,
                    raw=ev,
                    tool_name=name,
                    arguments=args or args_raw,
                    todos=todos,
                )
                turns.append(last_turn)
                continue
            if name.lower() in {"agent", "task"}:
                last_turn = _turn(
                    "subagent_event",
                    f"{name} subagent",
                    str(args.get("prompt") or args.get("description") or ""),
                    at=at,
                    raw=ev,
                    tool_name=name,
                    arguments=args or args_raw,
                    subagent_status="started",
                    subagent_name=str(args.get("subagent_type") or args.get("agent") or name),
                )
                turns.append(last_turn)
                continue
            if name == "exec_command":
                cmd = str(args.get("command") or args.get("cmd") or args_raw).strip()
                if cmd:
                    last_turn = _turn("shell_command", cmd[:100], cmd, at=at, raw=ev)
                    turns.append(last_turn)
                continue
            if name in {"apply_patch", "write_file", "edit_file"}:
                patch_text = str(args.get("patch") or args.get("diff") or args_raw).strip()
                patch_paths = _extract_patch_paths(patch_text)
                _fpath = (
                    patch_paths[0]
                    if patch_paths
                    else str(args.get("path") or args.get("file_path") or "").strip() or None
                )
                last_turn = _turn(
                    "file_edit",
                    f"{name}({_fpath or ''})",
                    patch_text
                    or json.dumps(args or {"raw": args_raw}, indent=2, ensure_ascii=False),
                    at=at,
                    raw=ev,
                    path=_fpath,
                    diff=patch_text or None,
                    tool_name=name,
                    arguments=args or args_raw,
                )
                turns.append(last_turn)
                continue
            last_turn = _turn(
                "tool_call",
                f"{name}(...)",
                json.dumps(args or {"raw": args_raw}, indent=2, ensure_ascii=False),
                at=at,
                raw=ev,
                tool_name=name,
                arguments=args or args_raw,
            )
            turns.append(last_turn)
    return turns


def _parse_codex_format_b(content: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        et = ev.get("type")
        at = ev.get("timestamp")
        if et == "reasoning":
            t = str(ev.get("summary") or ev.get("text") or "").strip()
            if t:
                turns.append(_turn("thinking", t[:80], t, at=at, raw=ev))
        elif et == "message":
            role = ev.get("role", "")
            in_t, out_t, think_t, cached_t, cache_write_t = _extract_codex_usage(ev.get("usage"))
            tokens = {
                "in": max(in_t - cached_t, 0),
                "out": out_t,
                "thinking": think_t,
                "cache_read": cached_t,
                "cache_write": cache_write_t,
            }
            msg = "".join(
                str(b.get("text", "")) for b in ev.get("content", []) if isinstance(b, dict)
            )
            if msg and not _is_codex_system_message(msg):
                turns.append(
                    _turn(
                        "user_message" if role == "user" else "agent_message",
                        msg[:80],
                        msg,
                        at=at,
                        tokens=tokens if role == "assistant" else None,
                        raw=ev,
                    )
                )
        elif et == "function_call":
            name = str(ev.get("name") or "unknown")
            args_raw = ev.get("arguments")
            args = _coerce_mapping(args_raw)
            todos = _extract_todos(args_raw)
            if todos:
                turns.append(
                    _turn(
                        "todo_write",
                        f"{name} · {len(todos)} item{'s' if len(todos) != 1 else ''}",
                        "",
                        at=at,
                        raw=ev,
                        tool_name=name,
                        arguments=args or args_raw,
                        todos=todos,
                    )
                )
                continue
            if name.lower() in {"agent", "task"}:
                turns.append(
                    _turn(
                        "subagent_event",
                        f"{name} subagent",
                        str(args.get("prompt") or args.get("description") or ""),
                        at=at,
                        raw=ev,
                        tool_name=name,
                        arguments=args or args_raw,
                        subagent_status="started",
                        subagent_name=str(args.get("subagent_type") or args.get("agent") or name),
                    )
                )
                continue

            kind = (
                "file_edit"
                if name in ("apply_patch", "write_file", "edit_file")
                else "shell_command"
                if name == "exec_command"
                else "tool_call"
            )

            if kind == "file_edit":
                _fpath = str(args.get("path") or args.get("file_path") or "").strip() or None
                _raw_diff = args.get("patch") or args.get("diff")
                if not _raw_diff and args.get("old_string") is not None:
                    old = str(args.get("old_string") or "")
                    new = str(args.get("new_string") or "")
                    _raw_diff = f"--- a/{_fpath or 'file'}\n+++ b/{_fpath or 'file'}\n"
                    for line in old.splitlines():
                        _raw_diff += f"-{line}\n"
                    for line in new.splitlines():
                        _raw_diff += f"+{line}\n"
                _diff = str(_raw_diff).strip() if _raw_diff else None
                content_text = _diff or str(
                    args.get("content")
                    or args.get("text")
                    or json.dumps(args, indent=2, ensure_ascii=False)
                )
                summary = f"{name}({_fpath or ''})"
            elif kind == "shell_command":
                _fpath = None
                _diff = None
                content_text = str(args.get("command") or args.get("cmd") or args_raw)
                summary = content_text[:100]
            else:
                _fpath = None
                _diff = None
                content_text = (
                    json.dumps(args, indent=2, ensure_ascii=False)
                    if isinstance(args, dict)
                    else str(args_raw)
                )
                summary = f"{name}(...)"

            turns.append(
                _turn(
                    kind,
                    summary,
                    content_text,
                    at=at,
                    raw=ev,
                    path=_fpath,
                    diff=_diff,
                    tool_name=name,
                    arguments=args or args_raw,
                )
            )
    return turns


# ---------------------------------------------------------------------------
# Gemini parser
# ---------------------------------------------------------------------------


def _parse_gemini(content: str) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        mid = str(ev.get("id") or "")
        et = ev.get("type")
        at = ev.get("timestamp")
        if not mid or et in ("$set", "session_start"):
            continue
        if mid not in merged:
            order.append(mid)
            merged[mid] = {
                "kind": "unknown",
                "content": "",
                "at": at,
                "tokens": {"in": 0, "out": 0, "thinking": 0},
                "raw": ev,
                "structured_turns": [],
            }

        t_raw = ev.get("tokens") or {}
        merged[mid]["tokens"]["in"] = max(merged[mid]["tokens"]["in"], t_raw.get("input", 0))
        merged[mid]["tokens"]["out"] = max(merged[mid]["tokens"]["out"], t_raw.get("output", 0))
        merged[mid]["tokens"]["thinking"] = max(
            merged[mid]["tokens"]["thinking"], t_raw.get("thoughts", 0)
        )

        if et == "user":
            merged[mid]["kind"] = "user_message"
            txt = "".join(p.get("text", "") for p in ev.get("content", []) if isinstance(p, dict))
            if txt:
                merged[mid]["content"] = txt
        elif et in ("gemini", "info"):
            merged[mid]["kind"] = "agent_message" if et == "gemini" else "user_message"
            thoughts = "\n".join(th.get("description", "") for th in ev.get("thoughts") or [])
            if thoughts:
                merged[mid]["thinking_content"] = thoughts
            c = ev.get("content")
            txt = (
                c
                if isinstance(c, str)
                else "".join(p.get("text", "") for p in (c or []) if isinstance(p, dict))
            )
            if txt:
                merged[mid]["content"] = txt
            tcalls = ev.get("toolCalls") or []
            if tcalls:
                _FILE_TOOL_NAMES = {
                    "write_file",
                    "edit_file",
                    "apply_patch",
                    "str_replace_editor",
                    "create_file",
                    "replace_file",
                    "patch_file",
                }
                for tc in tcalls:
                    name = tc.get("name", "unknown")
                    args = tc.get("args") or {}
                    todos = _extract_todos(args) or _extract_todos(tc.get("result"))
                    if todos:
                        merged[mid]["structured_turns"].append(
                            _turn(
                                "todo_write",
                                f"{name} · {len(todos)} item{'s' if len(todos) != 1 else ''}",
                                "",
                                at=at,
                                raw=ev,
                                tokens=merged[mid]["tokens"],
                                tool_name=name,
                                arguments=args,
                                todos=todos,
                            )
                        )
                        continue
                    if str(name).lower() in {"agent", "task"}:
                        merged[mid]["structured_turns"].append(
                            _turn(
                                "subagent_event",
                                f"{name} subagent",
                                str(args.get("prompt") or args.get("description") or ""),
                                at=at,
                                raw=ev,
                                tokens=merged[mid]["tokens"],
                                tool_name=name,
                                arguments=args,
                                subagent_status="started",
                                subagent_name=str(
                                    args.get("subagent_type") or args.get("agent") or name
                                ),
                            )
                        )
                        continue
                    if name in _FILE_TOOL_NAMES:
                        _fpath = (
                            str(
                                args.get("path")
                                or args.get("file_path")
                                or args.get("filename")
                                or ""
                            ).strip()
                            or None
                        )
                        _raw_diff = args.get("patch") or args.get("diff")
                        if not _raw_diff and args.get("old_string") is not None:
                            old = str(args.get("old_string") or "")
                            new = str(args.get("new_string") or "")
                            _raw_diff = f"--- a/{_fpath or 'file'}\n+++ b/{_fpath or 'file'}\n"
                            for _line in old.splitlines():
                                _raw_diff += f"-{_line}\n"
                            for _line in new.splitlines():
                                _raw_diff += f"+{_line}\n"
                        _diff = str(_raw_diff).strip() if _raw_diff else None
                        _fcontent = _diff or str(
                            args.get("content")
                            or args.get("text")
                            or json.dumps(args, ensure_ascii=False)
                        )
                        merged[mid].setdefault("file_edits", []).append(
                            (name, _fpath, _diff, _fcontent)
                        )
                    else:
                        merged[mid]["structured_turns"].append(
                            _turn(
                                "tool_call",
                                f"{name}(...)",
                                json.dumps(args, indent=2, ensure_ascii=False),
                                at=at,
                                raw=ev,
                                tokens=merged[mid]["tokens"],
                                tool_name=name,
                                arguments=args,
                            )
                        )

    final = []
    for mid in order:
        turn = merged[mid]
        assistant_turns: list[dict[str, Any]] = []
        if turn.get("thinking_content"):
            assistant_turns.append(
                _turn(
                    "thinking",
                    turn["thinking_content"][:80],
                    turn["thinking_content"],
                    at=turn["at"],
                    raw=turn["raw"],
                    tokens=turn["tokens"],
                )
            )
        # Emit file_edit turns before the agent response turn
        for fe_name, fe_path, fe_diff, fe_content in turn.get("file_edits", []):
            assistant_turns.append(
                _turn(
                    "file_edit",
                    f"{fe_name}({fe_path or ''})",
                    fe_content,
                    at=turn["at"],
                    raw=turn["raw"],
                    tokens=turn["tokens"],
                    path=fe_path,
                    diff=fe_diff,
                )
            )
        assistant_turns.extend(turn.get("structured_turns", []))
        if turn["kind"] != "unknown":
            assistant_turns.append(
                _turn(
                    turn["kind"],
                    turn["content"][:80],
                    turn["content"],
                    at=turn["at"],
                    raw=turn["raw"],
                    tokens=turn["tokens"],
                )
            )
        final.extend(_apply_tokens_once(assistant_turns, turn["tokens"]))
    return final


# ---------------------------------------------------------------------------
# Copilot parser
# ---------------------------------------------------------------------------


def _parse_copilot(content: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}  # toolCallId -> toolName from execution_start
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        et = ev.get("type")
        at = ev.get("timestamp")
        data = ev.get("data") or {}
        if et == "tool.execution_start":
            tcid = data.get("toolCallId")
            if tcid:
                tool_names[tcid] = data.get("toolName") or "unknown"
        elif et == "user.message":
            msg = str(data.get("content") or "")
            clean_msg = _PASTED_CONTENT_TAG_RE.sub("", msg).strip()
            if clean_msg:
                turns.append(_turn("user_message", clean_msg[:80], clean_msg, at=at, raw=ev))
            pasted_attachment = _extract_pasted_content_attachment(msg)
            if pasted_attachment is not None:
                turns.append(
                    _turn(
                        "pasted_content",
                        "Pasted file",
                        "",
                        at=at,
                        raw=ev,
                        attachments=[pasted_attachment],
                    )
                )
            for attachment in data.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                attachment_type = str(attachment.get("type") or "file")
                path = (
                    str(attachment.get("path") or attachment.get("filePath") or "").strip() or None
                )
                text_content = str(attachment.get("text") or "").strip() or None
                turns.append(
                    _turn(
                        "attachment",
                        attachment.get("displayName") or attachment_type.replace("_", " "),
                        text_content or "",
                        at=at,
                        raw=ev,
                        attachments=[
                            _make_attachment(
                                attachment_type=attachment_type,
                                path=path,
                                display_name=str(attachment.get("displayName") or "").strip()
                                or None,
                                content=text_content,
                                title=str(
                                    attachment.get("displayName")
                                    or attachment_type.replace("_", " ")
                                ),
                                metadata=attachment,
                            )
                        ],
                    )
                )
        elif et == "assistant.message":
            msg = str(data.get("content") or "")
            toks = {"out": data.get("outputTokens", 0)}
            assistant_turns: list[dict[str, Any]] = []
            # reasoning text (skip encrypted opaque content)
            reasoning = str(data.get("reasoningText") or "")
            if reasoning:
                assistant_turns.append(
                    _turn("thinking", reasoning[:80], reasoning, at=at, tokens=toks, raw=ev)
                )
            if msg:
                assistant_turns.append(
                    _turn("agent_message", msg[:80], msg, at=at, tokens=toks, raw=ev)
                )
            for req in data.get("toolRequests") or []:
                name = req.get("name", "unknown")
                args_raw = req.get("arguments") or {}
                args = _coerce_mapping(args_raw)
                todos = _extract_todos(args_raw)
                if todos:
                    assistant_turns.append(
                        _turn(
                            "todo_write",
                            f"{name} · {len(todos)} item{'s' if len(todos) != 1 else ''}",
                            "",
                            at=at,
                            tokens=toks,
                            raw=ev,
                            tool_name=name,
                            arguments=args or args_raw,
                            todos=todos,
                        )
                    )
                    continue
                if str(name).lower() in {"agent", "task"}:
                    assistant_turns.append(
                        _turn(
                            "subagent_event",
                            f"{name} subagent",
                            str(args.get("prompt") or args.get("description") or ""),
                            at=at,
                            tokens=toks,
                            raw=ev,
                            tool_name=name,
                            arguments=args or args_raw,
                            subagent_status="started",
                            subagent_name=str(
                                args.get("subagent_type") or args.get("agent") or name
                            ),
                        )
                    )
                    continue
                lowered = name.lower()
                is_file_tool = (
                    "edit" in lowered
                    or "write" in lowered
                    or "create" in lowered
                    or "patch" in lowered
                    or "replace" in lowered
                )
                if is_file_tool:
                    _fpath = (
                        str(
                            args.get("path") or args.get("file_path") or args.get("filename") or ""
                        ).strip()
                        or None
                    )
                    _raw_diff = args.get("diff") or args.get("patch")
                    if not _raw_diff and isinstance(args_raw, str):
                        raw_patch = args_raw.strip()
                        if raw_patch:
                            _raw_diff = raw_patch
                            patch_paths = _extract_patch_paths(raw_patch)
                            if patch_paths and not _fpath:
                                _fpath = patch_paths[0]
                    if not _raw_diff and args.get("old_string") is not None:
                        old = str(args.get("old_string") or "")
                        new = str(args.get("new_string") or "")
                        _raw_diff = f"--- a/{_fpath or 'file'}\n+++ b/{_fpath or 'file'}\n"
                        for _line in old.splitlines():
                            _raw_diff += f"-{_line}\n"
                        for _line in new.splitlines():
                            _raw_diff += f"+{_line}\n"
                    _diff = str(_raw_diff).strip() if _raw_diff else None
                    c_text = _diff or str(
                        args.get("content")
                        or args.get("text")
                        or json.dumps(args, ensure_ascii=False)
                    )
                    assistant_turns.append(
                        _turn(
                            "file_edit",
                            f"{name}({_fpath or ''})",
                            c_text,
                            at=at,
                            tokens=toks,
                            raw=ev,
                            path=_fpath,
                            diff=_diff,
                            tool_name=name,
                            arguments=args or args_raw,
                        )
                    )
                else:
                    c_text = (
                        json.dumps(args, ensure_ascii=False)
                        if isinstance(args, dict) and args
                        else str(args_raw)
                    )
                    assistant_turns.append(
                        _turn(
                            "tool_call",
                            f"{name}(...)",
                            c_text,
                            at=at,
                            tokens=toks,
                            raw=ev,
                            tool_name=name,
                            arguments=args or args_raw,
                        )
                    )
            turns.extend(_apply_tokens_once(assistant_turns, toks))
        elif et in {"subagent.started", "subagent.completed", "subagent.failed"}:
            status = et.split(".", 1)[1]
            description = str(data.get("agentDescription") or "").strip()
            turns.append(
                _turn(
                    "subagent_event",
                    str(data.get("agentDisplayName") or data.get("agentName") or "Subagent"),
                    description,
                    at=at,
                    raw=ev,
                    subagent_status=status,
                    subagent_id=str(ev.get("agentId") or data.get("toolCallId") or ""),
                    subagent_name=str(
                        data.get("agentDisplayName") or data.get("agentName") or "subagent"
                    ),
                    subagent_description=description or None,
                )
            )
        elif et == "tool.execution_complete":
            tcid = data.get("toolCallId")
            tn = tool_names.get(tcid or "", "") or data.get("toolName") or "unknown"
            metrics = (data.get("toolTelemetry") or {}).get("metrics") or {}
            out_t = int(metrics.get("resultForLlmLength", 0) or 0) // 4
            result_payload = data.get("result")
            result_text = ""
            if isinstance(result_payload, dict):
                result_text = _extract_first_text(
                    result_payload,
                    (
                        "content",
                        "detailedContent",
                        "output",
                        "stdout",
                        "stderr",
                        "result",
                        "summary",
                        "message",
                    ),
                )
            if not result_text:
                result_text = _text_from_value(result_payload)
            if not result_text and out_t:
                result_text = f"{out_t} tokens of output"
            turns.append(
                _turn(
                    "tool_call",
                    f"{tn} output",
                    result_text,
                    at=at,
                    raw=ev,
                    tool_name=tn,
                )
            )
    return turns


# ---------------------------------------------------------------------------
# OpenCode parser
# ---------------------------------------------------------------------------


def _parse_opencode(content: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        _type = ev.get("_type", "")
        data = ev.get("data") or {}
        at = ev.get("timestamp") or data.get("at")
        if _type == "message" and data.get("role") == "user":
            summary = data.get("summary") or {}
            if isinstance(summary, dict) and summary.get("diffs"):
                diffs = summary["diffs"]
                turns.append(
                    _turn(
                        "attachment",
                        f"User attached {len(diffs)} file context(s)",
                        "",
                        at=at,
                        raw=ev,
                        attachments=[
                            _make_attachment(
                                attachment_type="file",
                                path=str(diff.get("file") or "").strip() or None,
                                content=str(diff.get("after") or diff.get("before") or "").strip()
                                or None,
                                title=f"Attached context {i + 1}",
                                metadata={"patch": diff.get("patch")},
                            )
                            for i, diff in enumerate(diffs)
                            if isinstance(diff, dict)
                        ],
                    )
                )
        elif _type == "part":
            pt = data.get("type", "")
            role = ev.get("role", "")
            if pt == "step-finish":
                toks = data.get("tokens") or {}
                cache = toks.get("cache") or {}
                if turns:
                    turns[-1]["tokens"] = {
                        "in": toks.get("input", 0),
                        "out": toks.get("output", 0),
                        "thinking": toks.get("reasoning", 0),
                        "cache_read": cache.get("read", 0),
                    }
            elif pt == "tool":
                tool = data.get("tool", "unknown")
                inp = (data.get("state") or {}).get("input") or {}
                todos = _extract_todos(inp) or _extract_todos(
                    (data.get("state") or {}).get("output")
                )
                if todos:
                    turns.append(
                        _turn(
                            "todo_write",
                            f"{tool} · {len(todos)} item{'s' if len(todos) != 1 else ''}",
                            "",
                            at=at,
                            raw=ev,
                            tool_name=tool,
                            arguments=inp,
                            todos=todos,
                        )
                    )
                    continue
                kind = (
                    "shell_command"
                    if tool == "bash"
                    else "file_edit"
                    if tool in ("edit", "write", "replace")
                    else "tool_call"
                )

                if kind == "file_edit":
                    _fpath = str(inp.get("filePath") or inp.get("path") or "").strip() or None
                    _raw_diff = inp.get("diff") or inp.get("patch")
                    if not _raw_diff and inp.get("old_string") is not None:
                        old = str(inp.get("old_string") or "")
                        new = str(inp.get("new_string") or "")
                        _raw_diff = f"--- a/{_fpath or 'file'}\n+++ b/{_fpath or 'file'}\n"
                        for line in old.splitlines():
                            _raw_diff += f"-{line}\n"
                        for line in new.splitlines():
                            _raw_diff += f"+{line}\n"
                    _diff = str(_raw_diff).strip() if _raw_diff else None
                    content_text = _diff or str(
                        inp.get("content") or json.dumps(inp, indent=2, ensure_ascii=False)
                    )
                    summary = f"{tool}({_fpath or ''})"
                elif kind == "shell_command":
                    _fpath = None
                    _diff = None
                    content_text = str(inp.get("command") or "")
                    summary = content_text[:100]
                else:
                    _fpath = None
                    _diff = None
                    content_text = json.dumps(inp, indent=2, ensure_ascii=False)
                    summary = f"{tool}(...)"

                turns.append(
                    _turn(
                        kind,
                        summary,
                        content_text,
                        at=at,
                        raw=ev,
                        path=_fpath,
                        diff=_diff,
                        tool_name=tool,
                        arguments=inp,
                    )
                )
            elif pt == "reasoning":
                t = str(data.get("text") or "").strip()
                if t:
                    turns.append(_turn("thinking", t[:80], t, at=at, raw=ev))
            elif pt == "text":
                t = str(data.get("text") or "").strip()
                if t:
                    turns.append(
                        _turn(
                            "user_message" if role == "user" else "agent_message",
                            t[:80],
                            t,
                            at=at,
                            raw=ev,
                        )
                    )
    return turns
