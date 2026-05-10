"""Session parser for Atelier — converts raw JSONL session content into a
readable conversation timeline.

Public API::

    from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns
    turns = parse_session_turns(content, source="claude")

Supported sources: ``"claude"``, ``"codex"``, ``"opencode"``, ``"gemini"``, ``"copilot"``.
"""

from __future__ import annotations

import json
import re
from typing import Any

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
) -> dict[str, Any]:
    # Normalise timestamp
    at_str = None
    if at:
        at_str = str(at)

    return {
        "kind": kind,
        "at": at_str,
        "summary": summary,
        "content": content,
        "tokens": tokens or {},
        "raw": raw,
    }


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
                    xml_match = re.search(r"<(task|prompt|request|question)[^>]*>(.*?)</\1>", t, re.I | re.S)
                    parts.append(xml_match.group(2).strip() if xml_match else t)
        return "\n\n".join(parts)
    return ""


def _is_codex_system_message(text: str) -> bool:
    return any(text.strip().startswith(p) for p in _SYSTEM_PREFIXES_CODEX)


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
            text = _extract_text_from_claude_content(msg.get("content", ""))
            if text:
                if msg_id not in order:
                    order.append(msg_id)
                messages[msg_id] = [_turn("user_message", text[:80], text, at=at, raw=ev)]

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

            blocks = []
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                if bt == "text":
                    t = block.get("text", "").strip()
                    if t:
                        blocks.append(_turn("agent_message", t[:80], t, at=at, tokens=tokens, raw=ev))
                elif bt in ("thinking", "reasoning", "redacted"):
                    t = block.get("thinking") or block.get("text") or ""
                    if t:
                        blocks.append(_turn("thinking", t[:80], t, at=at, tokens=tokens, raw=ev))
                elif bt == "tool_use":
                    name = block.get("name", "unknown")
                    inp = block.get("input") or {}
                    kind = (
                        "file_edit"
                        if name in ("Edit", "Write", "MultiEdit")
                        else "shell_command" if name == "Bash" else "tool_call"
                    )

                    # High-fidelity extraction: use plain string for code/diffs
                    content_text = ""
                    if kind == "file_edit":
                        content_text = str(
                            inp.get("diff")
                            or inp.get("patch")
                            or inp.get("content")
                            or inp.get("text")
                            or json.dumps(inp, indent=2, ensure_ascii=False)
                        )
                    elif kind == "shell_command":
                        content_text = str(inp.get("command") or "")
                    else:
                        content_text = json.dumps(inp, indent=2, ensure_ascii=False)

                    summary = (
                        f"{name}({inp.get('file_path') or inp.get('path', '')})"
                        if kind == "file_edit"
                        else content_text[:100] if kind == "shell_command" else f"{name}(...)"
                    )
                    blocks.append(_turn(kind, summary, content_text, at=at, tokens=tokens, raw=ev))

            if blocks:
                messages[msg_id] = blocks

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
        if ev.get("type") != "event_msg":
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
        elif pt == "exec_command_end":
            cmd = str(
                payload.get("command", "")[-1]
                if isinstance(payload.get("command"), list)
                else payload.get("command", "")
            )
            if cmd:
                last_turn = _turn("shell_command", cmd[:100], cmd, at=at, raw=ev)
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
            msg = "".join(str(b.get("text", "")) for b in ev.get("content", []) if isinstance(b, dict))
            if msg and not _is_codex_system_message(msg):
                turns.append(
                    _turn(
                        "user_message" if role == "user" else "agent_message",
                        msg[:80],
                        msg,
                        at=at,
                        raw=ev,
                    )
                )
        elif et == "function_call":
            name = str(ev.get("name") or "unknown")
            args_raw = ev.get("arguments")
            args = {}
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"raw": args_raw}
            elif isinstance(args_raw, dict):
                args = args_raw

            kind = (
                "file_edit"
                if name in ("apply_patch", "write_file", "edit_file")
                else "shell_command" if name == "exec_command" else "tool_call"
            )

            if kind == "file_edit":
                content_text = str(
                    args.get("patch")
                    or args.get("content")
                    or args.get("diff")
                    or args.get("text")
                    or json.dumps(args, indent=2, ensure_ascii=False)
                )
                summary = f"{name}({args.get('path') or args.get('file_path') or ''})"
            elif kind == "shell_command":
                content_text = str(args.get("command") or args.get("cmd") or args_raw)
                summary = content_text[:100]
            else:
                content_text = (
                    json.dumps(args, indent=2, ensure_ascii=False) if isinstance(args, dict) else str(args_raw)
                )
                summary = f"{name}(...)"

            turns.append(_turn(kind, summary, content_text, at=at, raw=ev))
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
            }

        t_raw = ev.get("tokens") or {}
        merged[mid]["tokens"]["in"] = max(merged[mid]["tokens"]["in"], t_raw.get("input", 0))
        merged[mid]["tokens"]["out"] = max(merged[mid]["tokens"]["out"], t_raw.get("output", 0))
        merged[mid]["tokens"]["thinking"] = max(merged[mid]["tokens"]["thinking"], t_raw.get("thoughts", 0))

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
            txt = c if isinstance(c, str) else "".join(p.get("text", "") for p in (c or []) if isinstance(p, dict))
            if txt:
                merged[mid]["content"] = txt
            tcalls = ev.get("toolCalls") or []
            if tcalls:
                merged[mid]["kind"] = "tool_call"
                c_parts = []
                for tc in tcalls:
                    name = tc.get("name", "unknown")
                    args = tc.get("args") or {}
                    # Try to extract plain content if it's a file tool
                    if name in ("write_file", "edit_file", "apply_patch"):
                        c_parts.append(
                            str(
                                args.get("content")
                                or args.get("patch")
                                or args.get("diff")
                                or json.dumps(args, ensure_ascii=False)
                            )
                        )
                    else:
                        c_parts.append(f"{name}({json.dumps(args, ensure_ascii=False)})")

                sep = "\n\n" if merged[mid]["content"] else ""
                merged[mid]["content"] += sep + "\n".join(c_parts)

    final = []
    for mid in order:
        turn = merged[mid]
        if turn.get("thinking_content"):
            final.append(
                _turn(
                    "thinking",
                    turn["thinking_content"][:80],
                    turn["thinking_content"],
                    at=turn["at"],
                    raw=turn["raw"],
                    tokens=turn["tokens"],
                )
            )
        if turn["kind"] != "unknown":
            final.append(
                _turn(
                    turn["kind"],
                    turn["content"][:80],
                    turn["content"],
                    at=turn["at"],
                    raw=turn["raw"],
                    tokens=turn["tokens"],
                )
            )
    return final


# ---------------------------------------------------------------------------
# Copilot parser
# ---------------------------------------------------------------------------


def _parse_copilot(content: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        et = ev.get("type")
        at = ev.get("timestamp")
        data = ev.get("data") or {}
        if et == "user.message":
            msg = str(data.get("content") or "")
            if msg:
                turns.append(_turn("user_message", msg[:80], msg, at=at, raw=ev))
        elif et == "assistant.message":
            msg = str(data.get("content") or "")
            toks = {"out": data.get("outputTokens", 0)}
            if msg:
                turns.append(_turn("agent_message", msg[:80], msg, at=at, tokens=toks, raw=ev))
            for req in data.get("toolRequests") or []:
                name = req.get("name", "unknown")
                args = req.get("arguments") or {}
                # Plain text extraction for Copilot
                if name in ("edit", "write", "create"):
                    c_text = str(
                        args.get("content")
                        or args.get("diff")
                        or args.get("patch")
                        or json.dumps(args, ensure_ascii=False)
                    )
                else:
                    c_text = json.dumps(args, ensure_ascii=False)
                turns.append(_turn("tool_call", f"{name}(...)", c_text, at=at, tokens=toks, raw=ev))
        elif et == "tool.execution_complete":
            tn = data.get("toolName") or "unknown"
            metrics = (data.get("toolTelemetry") or {}).get("metrics") or {}
            out_t = int(metrics.get("resultForLlmLength", 0) or 0) // 4
            turns.append(_turn("tool_call", f"{tn} output", f"{out_t} tokens of output", at=at, raw=ev))
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
                # Return the absolute raw content of the files joined by newlines.
                # No headers, no JSON. Just the source text.
                plain_text = "\n".join(str(d.get("after") or d.get("before") or "") for d in diffs)
                turns.append(
                    _turn(
                        "user_message",
                        f"User attached {len(diffs)} file context(s)",
                        plain_text,
                        at=at,
                        raw=ev,
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
                kind = (
                    "shell_command"
                    if tool == "bash"
                    else "file_edit" if tool in ("edit", "write", "replace") else "tool_call"
                )

                if kind == "file_edit":
                    content_text = str(
                        inp.get("content")
                        or inp.get("diff")
                        or inp.get("patch")
                        or json.dumps(inp, indent=2, ensure_ascii=False)
                    )
                    summary = f"{tool}({inp.get('filePath') or inp.get('path') or ''})"
                elif kind == "shell_command":
                    content_text = str(inp.get("command") or "")
                    summary = content_text[:100]
                else:
                    content_text = json.dumps(inp, indent=2, ensure_ascii=False)
                    summary = f"{tool}(...)"

                turns.append(_turn(kind, summary, content_text, at=at, raw=ev))
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
