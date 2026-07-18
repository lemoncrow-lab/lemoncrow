#!/usr/bin/env python3
"""PreToolUse read-after-edit guard.

Blocks the one wasteful case: a whole-file re-read (``full=true`` with no range) of a
file already edited this session. The edit response already returned the changed
region, and a full re-read re-injects the whole file -- which is then re-cached
on every later turn. Targeted range reads and reads of un-edited files pass
through untouched.

Edited files are recorded by loop_discipline_post.py (shared session state).
Fail-open; opt-out via LEMONCROW_READ_AFTER_EDIT_GUARD=0.

Note: this hook deliberately does NOT block grep/rg over source. Steering toward
explore/search lives in the agent instructions + the strength of the indexed
tools, not a hard PreToolUse deny (which mis-fired on legitimate searches).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _root() -> Path:
    raw = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(raw) if raw else Path.home() / ".lemoncrow"


def _workspace_key(path: str) -> str:
    import re
    from hashlib import sha256
    from pathlib import Path as _Path

    resolved = _Path(path).expanduser().resolve()
    home = _Path.home().resolve()
    try:
        parts = resolved.relative_to(home).parts
    except ValueError:
        parts = [p for p in resolved.parts if p and p != "/"]
    sanitized = [re.sub(r"[^a-zA-Z0-9.\-_]", "-", p) for p in parts if p]
    label = re.sub(r"-{2,}", "-", "-".join(sanitized)).strip("-")
    if len(label) > 120:
        label = label[:110].rstrip("-") + "--" + sha256(str(resolved).encode()).hexdigest()[:6]
    return label or sha256(str(resolved).encode()).hexdigest()[:12]


def _agent_key(payload: dict[str, Any]) -> str:
    """Per-agent state key so a read-only sub-agent never inherits another
    agent's edits.

    Every hook payload carries a unique ``agent_id`` for sub-agents (Task /
    workflow fan-out) and omits it for the top-level agent, which falls back to
    its ``session_id``. Sub-agents SHARE the parent's session_id, so session_id
    alone cannot separate them; ``transcript_path`` is useless too -- the host
    reports the top-level session transcript for every agent. ``agent_id`` is
    the only per-agent discriminator, and it rides in both Pre and Post payloads
    so the recorder and this guard resolve the same key.
    """
    raw = payload.get("agent_id") or payload.get("session_id") or "main"
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(raw)) or "main"


def _edited_paths(agent_key: str) -> set[str]:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = _workspace_key(workspace)
    sp = _root() / "workspaces" / h / "loop_discipline" / f"{agent_key}.json"
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(sp.read_text("utf-8"))
        if isinstance(data, dict):
            return {str(p) for p in (data.get("edited_paths") or [])}
    return set()


def _is_read(name: str, ti: dict[str, Any]) -> bool:
    """True only for an actual read tool call.

    Gate on the tool NAME, never on the mere presence of a 'files'/'path' key:
    other tools carry those fields for unrelated reasons -- e.g. a
    StructuredOutput call whose payload enumerates a files=[...] array -- and
    inferring 'read' from shape wrongly denied them (an enumerated path that
    happened to be in the edited set produced a bogus :full-read deny).
    """
    return name in ("read", "Read") or name.endswith("__read")


# ':Lx-Ly' / ':full' / ':head=N' / ':tail=N' / ':summary' / ':outline' suffixes
# accepted by the read tool's files=[] string entries.
_BOUND_SUFFIX = re.compile(r":(L?\d+(?:-L?\d+)?|full|head=\d+|tail=\d+|summary|outline)$", re.IGNORECASE)


def _split_suffix(raw: str) -> tuple[str, str]:
    """Return (path, suffix) with '#fragment' and the read-tool suffix stripped."""
    bare = raw.split("#")[0]
    m = _BOUND_SUFFIX.search(bare)
    if m:
        return bare[: m.start()], m.group(1).lower()
    return bare, ""


def _read_after_edit_line_cap() -> int:
    """Line-count gate for the deny (LEMONCROW_READ_AFTER_EDIT_MAX_LINES, default 400).

    Parity with the Codex adapter: only a LARGE edited file's whole-file
    re-read is real waste. Hard-denying small files starved agents for
    near-zero savings -- benchmark transcripts showed a 3-turn recovery dance
    (denied :full -> ranged read -> edit retry) on files a range read barely
    shrinks.
    """
    raw = os.environ.get("LEMONCROW_READ_AFTER_EDIT_MAX_LINES", "").strip()
    return int(raw) if raw.isdigit() else 400


def _file_line_count(path: str) -> int:
    """On-disk line count, or 0 when unreadable (treated as small: fail-open)."""
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _resolve(path: str) -> str:
    """Workspace-anchored absolute path; '' when resolution is impossible."""
    if not path:
        return ""
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = Path(workspace) / p
        return str(p.resolve())
    except (OSError, RuntimeError, ValueError):
        return ""


def _full_read_targets(ti: dict[str, Any]) -> list[str]:
    """Paths this read call would ingest whole (no range/head/tail/summary bound).

    Covers both input shapes: legacy top-level {path, full} and the files=[]
    schema whose entries are plain strings ('a.py', 'a.py:full', 'a.py:L1-L9')
    or dicts ({path, full?, range?, head?, tail?, summary?, outline?}).
    """
    targets: list[str] = []
    raw_path = str(ti.get("path") or "")
    if raw_path and bool(ti.get("full")) and not (bool(ti.get("range")) or "#" in raw_path):
        targets.append(raw_path.split("#")[0])
    files = ti.get("files")
    if isinstance(files, list):
        for entry in files:
            if isinstance(entry, str):
                path, suffix = _split_suffix(entry)
                # Bare path string = whole-file read; ':full' is explicit.
                if path and suffix in ("", "full"):
                    targets.append(path)
            elif isinstance(entry, dict):
                raw = entry.get("path")
                if not isinstance(raw, str) or not raw:
                    continue
                path, suffix = _split_suffix(raw)
                if not path or (suffix and suffix != "full"):
                    continue
                bounded = any(entry.get(k) for k in ("range", "head", "tail", "summary", "outline"))
                if not bounded:
                    targets.append(path)
    return targets


def _deny(reason: str) -> None:
    """Emit a current-schema PreToolUse 'deny' (Claude Code v2.1.x).

    The legacy top-level {"decision": "block"} form is deprecated for PreToolUse
    and is silently ignored -- denial must go through hookSpecificOutput so the
    tool call is actually blocked and the reason is shown to the agent.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def _dormant() -> bool:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import cap_exhausted

        root = (
            os.environ.get("LEMONCROW_ROOT")
            or os.environ.get("LEMONCROW_STORE_ROOT")
            or str(Path.home() / ".lemoncrow")
        )
        return bool(cap_exhausted(root))
    except Exception:  # noqa: BLE001 — fail-open (active)
        return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, TypeError, OSError):
        return 0
    if _dormant():
        return 0  # dormant: no pre-tool discipline steering
    name = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0

    # Read-after-edit guard.
    if os.environ.get("LEMONCROW_READ_AFTER_EDIT_GUARD", "1") == "0":
        return 0
    if not _is_read(name, ti):
        return 0
    edited = _edited_paths(_agent_key(payload))
    if not edited:
        return 0
    # Entries are resolved absolute paths; a bare basename is the recorder's
    # last-resort fallback when resolution failed. Compare full paths first --
    # basename-only matching false-positives on common names (utils.py).
    basename_entries = {e for e in edited if "/" not in e and "\\" not in e}
    all_basenames = {Path(e).name for e in edited}
    hit = ""
    hit_resolved = ""
    for target in _full_read_targets(ti):
        resolved = _resolve(target)
        base = Path(target).name
        if (
            (resolved and resolved in edited)
            or (base and base in basename_entries)
            or (not resolved and base in all_basenames)
        ):
            hit = base or target
            hit_resolved = resolved
            break
    if not hit:
        return 0
    # Size gate (Codex parity): a small edited file's full re-read is not real
    # waste -- deny only when the re-ingest is actually large. Unknown size
    # (0 lines) fails toward allow.
    if _file_line_count(hit_resolved or hit) < _read_after_edit_line_cap():
        return 0
    reason = (
        f'Edited {hit} already -- read a range (range="L1-L120"), not the whole file; :full re-caches it every turn.'
    )
    _deny(reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
