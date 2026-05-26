#!/usr/bin/env python3
"""UserPromptSubmit hook — capture user prompts into the RunLedger.

Fires each time the user submits a message.  Records the prompt text as an
``agent_message`` event (kind chosen for visibility in the timeline) so the
full conversation context is preserved in the ledger.

Prompt text is truncated to 8 KB to cap ledger file size while keeping full
context for normal prompts.

Fail-open: any error exits silently (code 0) — never blocks the agent.

Payload received on stdin:
  {
    "session_id": "abc123",
    "transcript_path": "...",
    "cwd": "...",
    "permission_mode": "default",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "Write a function to calculate factorial"
  }
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_MAX_PROMPT_BYTES = 8192  # 8 KB


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _session_state_path() -> Path:
    import hashlib

    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()[:12]
    root = Path(os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or Path.home() / ".atelier")
    return root / "workspaces" / h / "session_state.json"


def _read_session_state() -> dict:  # type: ignore[type-arg]
    p = _session_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))  # type: ignore[no-any-return]
    except Exception:
        return {}


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    if root:
        return Path(root)
    state = _read_session_state()
    if state.get("atelier_root"):
        return Path(state["atelier_root"])
    return Path.home() / ".atelier"


def _active_session_id() -> str | None:
    state = _read_session_state()
    return state.get("session_id") or state.get("active_session_id")


# ---------------------------------------------------------------------------
# RunLedger event writer
# ---------------------------------------------------------------------------


def _append_prompt_event(session_id: str, prompt: str) -> None:
    runs_dir = _atelier_root() / "runs"
    run_file = runs_dir / f"{session_id}.json"
    if not run_file.exists():
        return

    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        return

    events: list[dict[str, Any]] = data.setdefault("events", [])
    truncated = len(prompt) > _MAX_PROMPT_BYTES
    stored_prompt = prompt[:_MAX_PROMPT_BYTES]
    short = stored_prompt[:100].replace("\n", " ")

    events.append(
        {
            "kind": "agent_message",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": f"user: {short}{'…' if len(stored_prompt) > 100 else ''}",
            "payload": {
                "role": "user",
                "prompt": stored_prompt,
                "truncated": truncated,
                "event": "UserPromptSubmit",
            },
        }
    )
    data["events"] = events

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=run_file.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(run_file)
    except Exception:
        if tmp_path:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    prompt: str = payload.get("prompt", "") or ""
    if not prompt.strip():
        return 0

    try:
        session_id = _active_session_id()
        if not session_id:
            return 0
        _append_prompt_event(session_id, prompt)
    except Exception:
        pass  # fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
