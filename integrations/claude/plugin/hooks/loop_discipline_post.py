#!/usr/bin/env python3
"""PostToolUse hook: record edited file basenames.

Runs on every PostToolUse and filters internally to the Atelier edit tool
(name-agnostic across hosts). It records the basenames of files edited this
session so the PreToolUse read-after-edit guard (pre_tool_discipline.py) can
spot a redundant full re-read. Fail-open: any error exits 0 and prints nothing.

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
from pathlib import Path
from typing import Any


def _root() -> Path:
    raw = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    return Path(raw) if raw else Path.home() / ".atelier"


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


def _state_path() -> Path:
    # Key by workspace so concurrent/sequential tasks never share state.
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = _workspace_key(workspace)
    return _root() / "workspaces" / h / "loop_discipline.json"


def _load() -> dict[str, Any]:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(_state_path().read_text("utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def _save(state: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")


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


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:  # noqa: BLE001 - lifecycle hooks must be fail-open
        return 0
    name = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict) or not _is_edit(name, ti):
        return 0
    try:
        state = _load()
        edited = set(state.get("edited_paths") or [])
        for target in _edit_targets(ti):
            # Strip '#fragment' and ':Lx-Ly' range suffixes (same pattern as
            # rich_edit._parse_target) so ranged edits record the bare basename.
            bare = re.sub(r":L?\d+(?:-L?\d+)?$", "", target.split("#")[0], flags=re.I)
            edited.add(Path(bare).name)
        state["edited_paths"] = sorted(edited)[-80:]
        _save(state)
    except Exception:  # noqa: BLE001 - lifecycle hooks must be fail-open
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
