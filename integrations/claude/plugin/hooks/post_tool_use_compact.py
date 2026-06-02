#!/usr/bin/env python3
"""Optional Claude Code PostToolUse hook for compacting large tool outputs."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_THRESHOLD_TOKENS = 500
DEFAULT_BUDGET_TOKENS = 400


def _session_state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict[str, Any]:
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        result = json.loads(p.read_text("utf-8"))
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("atelier_root"):
        return Path(state["atelier_root"])
    return Path.home() / ".atelier"


def _compact_config() -> tuple[int, int]:
    config_path = _atelier_root() / "config.toml"
    if not config_path.exists():
        return DEFAULT_THRESHOLD_TOKENS, DEFAULT_BUDGET_TOKENS

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return DEFAULT_THRESHOLD_TOKENS, DEFAULT_BUDGET_TOKENS

    compact_config = data.get("compact", {}) if isinstance(data, dict) else {}
    threshold = int(compact_config.get("threshold_tokens", DEFAULT_THRESHOLD_TOKENS))
    budget = int(compact_config.get("budget_tokens", DEFAULT_BUDGET_TOKENS))
    return max(1, threshold), max(1, budget)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_stringify(item) for item in value if item is not None)
    if isinstance(value, dict):
        for key in ("text", "content", "output", "stdout", "stderr"):
            if key in value:
                rendered = _stringify(value[key])
                if rendered:
                    return rendered
        try:
            return json.dumps(value, indent=2, sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)


def _extract_output(payload: dict[str, Any]) -> str:
    for key in (
        "tool_output",
        "tool_response",
        "response",
        "result",
        "output",
        "stdout",
        "stderr",
    ):
        rendered = _stringify(payload.get(key))
        if rendered:
            return rendered
    return ""


def _content_type(payload: dict[str, Any]) -> str:
    tool_name = str(payload.get("tool_name", "") or "").lower()
    if tool_name in {"read", "edit", "write", "multiedit"}:
        return "file"
    if tool_name in {"grep", "glob"}:
        return "grep"
    if tool_name == "bash":
        return "bash"
    return "tool_output"


def _token_estimate(text: str) -> int:
    return max(1, len(text.split())) if text else 0


def _response_payload(result: Any) -> dict[str, Any]:
    compacted = result.compacted
    recovery = result.recovery_hint
    return {
        "decision": "approve",
        "toolOutput": compacted,
        "replacement_output": compacted,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"Tool output compacted to save context. Recovery: {recovery}",
        },
        "atelierCompactToolOutput": result.model_dump(mode="json"),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0

    try:
        output = _extract_output(payload)
        if not output:
            return 0

        threshold, budget = _compact_config()
        if _token_estimate(output) <= threshold:
            return 0

        from atelier.core.capabilities.tool_supervision.compact_output import compact

        command = payload.get("command") or payload.get("tool_input", {}).get("command")
        hint = "Re-run the original tool call to fetch the full output."
        if command:
            hint = f"Re-run command for full output: {command}"

        result = compact(
            content=output,
            content_type=_content_type(payload),
            budget_tokens=budget,
            recovery_hint=hint,
        )
        if result.method == "passthrough":
            return 0

        print(json.dumps(_response_payload(result)))
    except (ImportError, OSError, ValueError, TypeError):
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
