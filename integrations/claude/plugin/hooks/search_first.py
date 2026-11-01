#!/usr/bin/env python3
"""Claude PreToolUse guard for code search routed through Bash."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_SEARCH = re.compile(r"(?<![\w./-])(?:rg|ripgrep|grep|egrep|fgrep|zgrep)(?=\s|$|[;|&)(])", re.I)
_GENERATED = re.compile(
    r"(?:^|[;&|]\s*)(?:git\s+(?:diff|log|status|show|branch|ls-files)"
    r"|(?:uv\s+run\s+)?(?:pytest|npm|npx|make|docker|journalctl|ps|curl)\b)", re.I)
_SOURCE_SEGMENT = re.compile(
    r"(?:^|[;&]\s*)(?:cd\s+[^;&|]+\s*&&\s*)?(?:rg|grep|egrep|fgrep|ripgrep)\b", re.I)
_SHELL_TOOL = re.compile(r"(?:^Bash$|^bash$|^mcp__(?:lc|lemoncrow|plugin_lemoncrow_lc)__bash$)")
_INDEX_TOOL = re.compile(r"^mcp__(?:lc|lemoncrow|plugin_lemoncrow_lc)__code_search$")
_OVERRIDE = "# lc:allow-shell-search"


_CD = re.compile(
    r"""(?:^|[;&|(\n])\s*cd\s+(?P<directory>'[^']*'|"[^"]*"|[^\s;&|()]+)\s*(?:&&|;|\n)""",
    re.I,
)


def _in_git_worktree(directory: Path) -> bool:
    """Recognize Git repositories and linked worktrees without spawning git."""
    try:
        resolved = directory.expanduser().resolve()
        return any((parent / ".git").exists() for parent in (resolved, *resolved.parents))
    except (OSError, RuntimeError, ValueError):
        return False


def _working_directory(payload: dict[str, Any], ti: dict[str, Any]) -> Path:
    raw = ti.get("cwd") or payload.get("cwd") or os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
    return Path(str(raw)).expanduser().resolve()


def _searches_git_worktree(command: str, base: Path) -> bool | None:
    """Resolve literal cd prefixes before each grep; None means unknown cwd."""
    for match in _SEARCH.finditer(command):
        current = base
        for cd in _CD.finditer(command[: match.start()]):
            raw = cd.group("directory")
            target = raw[1:-1] if raw.startswith(("'", '"')) and raw[-1] == raw[0] else raw
            if not target or "$" in target or chr(96) in target or target == "-":
                return None
            requested = Path(target).expanduser()
            current = (requested if requested.is_absolute() else current / requested).resolve()
        if _in_git_worktree(current):
            return True
    return False


def _commands(tool_input: dict[str, Any]) -> list[str]:
    raw = tool_input.get("command") or tool_input.get("commands") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [value for value in raw if isinstance(value, str)]
    return []


def classify(payload: dict[str, Any]) -> tuple[str, str] | None:
    name = str(payload.get("tool_name") or "")
    if _INDEX_TOOL.fullmatch(name):
        return "indexed", "code_search"
    if not _SHELL_TOOL.fullmatch(name):
        return None
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return None
    commands = _commands(ti)
    if not commands or not any(_SEARCH.search(c) for c in commands):
        return None
    if ti.get("bg") or ti.get("background") or ti.get("interactive") or ti.get("id"):
        return "allow_shell", "shell_execution_mode"
    if any(_OVERRIDE in c for c in commands):
        return "allow_shell", "explicit_exception"
    relevant = [c for c in commands if _SEARCH.search(c)]
    # Do not block filtering generated output, e.g. git diff piped to grep.
    if all(_GENERATED.search(c) and not _SOURCE_SEGMENT.search(c) for c in relevant):
        return "allow_shell", "generated_output"
    # Restrict enforcement to Git worktrees; standalone directories are not indexed.
    base = _working_directory(payload, ti)
    locations = [_searches_git_worktree(c, base) for c in relevant]
    if None in locations:
        return "allow_shell", "unknown_worktree"
    if not any(locations):
        return "allow_shell", "non_git_directory"
    return "deny", "source_search_via_bash"


def _record(payload: dict[str, Any], decision: str, reason: str) -> None:
    """Anonymous daily route events (no command/repo/session identifiers), 14-day retention."""
    try:
        root = Path(os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow")
        directory = root / "telemetry" / "search-routing"
        day = datetime.now(UTC).date()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{day.isoformat()}.jsonl"
        if not target.exists():
            cutoff = (day - timedelta(days=14)).isoformat()
            for old in directory.glob("????-??-??.jsonl"):
                if old.stem < cutoff:
                    old.unlink(missing_ok=True)
        event = {"decision": decision, "reason": reason, "role": "subagent" if payload.get("agent_id") else "main"}
        data = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        pass


def _dormant() -> bool:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import cap_exhausted
        root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or str(Path.home() / ".lemoncrow")
        return bool(cap_exhausted(root))
    except Exception:
        return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        classified = classify(payload)
        if classified is None:
            return 0
        decision, reason = classified
        if os.environ.get("LEMONCROW_SEARCH_FIRST", "1") == "0" or _dormant():
            return 0
        _record(payload, decision, reason)
        if decision == "deny":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Source search via Bash is disabled. Use mcp__lc__code_search "
                    "to discover code, then mcp__lc__read for exact ranges. "
                    "For required shell-only search add '# lc:allow-shell-search'."
                ),
            }}))
    except (OSError, ValueError, TypeError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
