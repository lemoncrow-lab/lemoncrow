#!/usr/bin/env python3
"""PostToolUse hook: record edited file paths; nudge one-lookup-per-turn chains.

Runs on every PostToolUse. For the LemonCrow edit tool (name-agnostic across
hosts) it records the resolved paths of files edited this session (basename
only when resolution fails) so the PreToolUse read-after-edit guard
(pre_tool_discipline.py) can spot a redundant full re-read.

For a lookup (read, code_search, or a read-only grep/sed/cat/ls/git-diff shell
command) it checks the agent's own transcript: when this is the third turn in a
row that made exactly one lookup, it adds one line of context asking for the
next independent lookups in one message. Each turn re-reads the whole
conversation, so a chain of single lookups costs a full context read per
lookup (Nov 2026 Claude sessions: ~17k such follow-up turns, mostly in code
and review sub-agents). At most one nudge per turn. Fail-open: any error exits
0 and prints nothing.

The cycle-cap that previously lived here was removed: a soft nudge was ignored
and a hard block backfired in benchmarks (task3 78 turns vs 55). Cost is
enforced in the tools, not by policing the model's iteration.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


def _agent_key(payload: dict[str, Any]) -> str:
    """Per-agent state key so a read-only sub-agent never inherits another
    agent's edits, and its edits never falsely block the parent.

    Sub-agents (Task / workflow fan-out) carry a unique ``agent_id`` in every
    hook payload; the top-level agent omits it and falls back to ``session_id``
    (sub-agents share the parent's session_id, so that alone cannot separate
    them). Read identically by the PreToolUse guard so record and check align.
    """
    raw = payload.get("agent_id") or payload.get("session_id") or "main"
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(raw)) or "main"


def _state_dir() -> Path:
    # Key by workspace so concurrent/sequential tasks never share state.
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    return Path(workspace).expanduser().resolve() / ".lemoncrow" / "workspace" / "loop_discipline"


def _state_path(agent_key: str) -> Path:
    return _state_dir() / f"{agent_key}.json"


def _prune(ttl_seconds: float = 86_400.0) -> None:
    """Delete per-agent state files untouched within ttl. Sub-agent files are
    ephemeral -- one per sub-agent -- so without this they accumulate forever.
    The current agent's file was just written (mtime=now) so is never pruned.
    """
    with contextlib.suppress(OSError):
        now = time.time()
        for f in _state_dir().glob("*.json"):
            with contextlib.suppress(OSError):
                if now - f.stat().st_mtime > ttl_seconds:
                    f.unlink()


def _load(agent_key: str) -> dict[str, Any]:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(_state_path(agent_key).read_text("utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def _save(agent_key: str, state: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        p = _state_path(agent_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")


def _record_key(target: str) -> str:
    """Resolved absolute path for an edit target; basename as last resort.

    Strips '#fragment' and ':Lx-Ly' range suffixes (same pattern as
    rich_edit._parse_target), then resolves workspace-relative so the
    read-after-edit guard compares full paths -- utils.py in two different
    packages must not collide on basename.
    """
    bare = re.sub(r":L?\d+(?:-L?\d+)?$", "", target.split("#")[0], flags=re.I)
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    try:
        p = Path(bare).expanduser()
        if not p.is_absolute():
            p = Path(workspace) / p
        return str(p.resolve())
    except (OSError, RuntimeError, ValueError):
        return Path(bare).name


def _is_edit(name: str, ti: dict[str, Any]) -> bool:
    return isinstance(ti.get("edits"), list) or name.endswith("__edit") or name == "edit"


def _edit_targets(ti: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("file_path", "path", "filename"):
        val = ti.get(key)
        if isinstance(val, str) and val:
            out.append(val)
    edits = ti.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                fp = entry.get("file_path") or entry.get("path")
                if isinstance(fp, str) and fp:
                    out.append(fp)
    return out


_LC_TOOL = re.compile(r"^mcp__(?:lc|lemoncrow|plugin_lemoncrow_lc|plugin_lemoncrow_lemoncrow)__(\w+)$")
_HOST_LOOKUPS = frozenset({"Read", "Grep", "Glob"})
_SHELL_LOOKUPS = frozenset({"grep", "rg", "ag", "sed", "cat", "head", "tail", "nl", "ls", "find", "fd", "wc", "tree"})
_GIT_LOOKUPS = frozenset({"diff", "show", "log", "blame", "grep"})
_CD_PREFIX = re.compile(r"^(?:cd\s+\S+\s*&&\s*)+")
# A redirect into a file makes the command a write (`cat > f <<EOF`); 2>, >& and
# >/dev/null do not. A quoted '>' in a pattern also lands here: no nudge (fail-safe).
_WRITE_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(?!&|/dev/null)\S")
_STREAK = 3
_TAIL_BYTES = 1 << 20
_NUDGE = (
    "LemonCrow: {n} lookups in a row, one per turn; every turn re-reads the whole conversation. "
    "Put the next independent reads and searches in ONE message: `read` takes several files and "
    "ranges, `bash` takes `command: [...]`, and independent calls can go side by side."
)


def _is_lookup_command(ti: dict[str, Any]) -> bool:
    """A single read-only shell lookup; a command array is already a batch."""
    if ti.get("bg") or ti.get("action") or ti.get("id"):
        return False
    cmd = ti.get("command")
    if isinstance(cmd, list):
        if len(cmd) != 1:
            return False
        cmd = cmd[0]
    if not isinstance(cmd, str):
        return False
    text = _CD_PREFIX.sub("", cmd.strip())
    words = text.split()
    if not words or _WRITE_REDIRECT.search(text):
        return False
    verb = os.path.basename(words[0])
    if verb == "git":
        rest = words[1:]
        while len(rest) >= 2 and rest[0] == "-C":
            rest = rest[2:]
        while rest and rest[0] == "--no-pager":
            rest = rest[1:]
        return bool(rest) and rest[0] in _GIT_LOOKUPS
    if verb == "sed" and any(w == "-i" or w.startswith(("-i", "--in-place")) for w in words[1:]):
        return False
    return verb in _SHELL_LOOKUPS


def _is_lookup(name: str, ti: Any) -> bool:
    if not isinstance(ti, dict):
        return False
    m = _LC_TOOL.match(name)
    short = m.group(1) if m else name
    if m and short in ("read", "code_search"):
        return True
    if not m and short in _HOST_LOOKUPS:
        return True
    return short in ("bash", "Bash") and _is_lookup_command(ti)


def _transcript_for(payload: dict[str, Any]) -> Path | None:
    """This agent's own transcript.

    A sub-agent's hook payload carries the parent's transcript_path plus its
    agent_id; its turns live in <session>/subagents/[<subdir>/]agent-<id>.jsonl.
    """
    raw = payload.get("transcript_path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    agent = payload.get("agent_id")
    if not agent:
        return path
    agent = str(agent)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", agent):
        return None
    name = f"agent-{agent}.jsonl"
    if path.name == name:
        return path
    base = path.with_suffix("") / "subagents"
    if (base / name).is_file():
        return base / name
    return next(base.glob(f"**/{name}"), None)


def _assistant_turns(path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """(message id, tool_use blocks) per assistant message in the transcript tail.

    Claude writes one entry per content block, all sharing the message id.
    """
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        start = max(0, fh.tell() - _TAIL_BYTES)
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start:
        lines = lines[1:]
    turns: list[tuple[str, list[dict[str, Any]]]] = []
    index: dict[str, int] = {}
    for line in lines:
        if b'"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        mid = str(msg.get("id") or entry.get("uuid") or len(turns))
        content = msg.get("content")
        blocks = (
            [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            if isinstance(content, list)
            else []
        )
        if mid in index:
            known = turns[index[mid]][1]
            seen = {b.get("id") for b in known}
            known.extend(b for b in blocks if b.get("id") not in seen)
        else:
            index[mid] = len(turns)
            turns.append((mid, blocks))
    return turns


def _single_lookup(blocks: list[dict[str, Any]]) -> bool:
    return len(blocks) == 1 and _is_lookup(str(blocks[0].get("name") or ""), blocks[0].get("input"))


def _batch_nudge(payload: dict[str, Any], name: str, ti: dict[str, Any]) -> str | None:
    """The nudge for the third (sixth, ...) single-lookup turn in a row, once per turn."""
    if not _is_lookup(name, ti):
        return None
    path = _transcript_for(payload)
    if path is None or not path.is_file():
        return None
    turns = _assistant_turns(path)
    call_id = payload.get("tool_use_id")
    current = next((i for i, (_, blocks) in enumerate(turns) if any(b.get("id") == call_id for b in blocks)), None)
    if current is None:
        # The running turn is usually written only after its tools finish:
        # count it as one more single lookup, keyed to the last completed turn.
        anchor = turns[-1][0] if turns else ""
        streak, prior = 1, turns
    else:
        if not _single_lookup(turns[current][1]):
            return None
        anchor = turns[current][0]
        streak, prior = 1, turns[:current]
    for _, blocks in reversed(prior):
        if not _single_lookup(blocks):
            break
        streak += 1
    if streak < _STREAK or streak % _STREAK:
        return None
    agent_key = _agent_key(payload)
    state = _load(agent_key)
    if state.get("batch_nudge_anchor") == anchor:
        return None
    state["batch_nudge_anchor"] = anchor
    _save(agent_key, state)
    _prune()
    return _NUDGE.format(n=streak)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    name = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0
    if not _is_edit(name, ti):
        try:
            nudge = _batch_nudge(payload, name, ti)
        except Exception:
            return 0
        if nudge:
            out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": nudge}}
            sys.stdout.write(json.dumps(out) + "\n")
        return 0
    try:
        agent_key = _agent_key(payload)
        state = _load(agent_key)
        edited = set(state.get("edited_paths") or [])
        for target in _edit_targets(ti):
            edited.add(_record_key(target))
        state["edited_paths"] = sorted(edited)[-80:]
        _save(agent_key, state)
        _prune()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
