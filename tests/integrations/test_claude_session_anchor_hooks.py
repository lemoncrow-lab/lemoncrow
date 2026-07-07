"""Window-anchor upkeep by the Claude hooks.

The MCP server attributes every savings row (and the statusline sidecar) to the
session id found in this window's identity file. Claude Code fires
SessionStart(clear) with the PRE-clear session id, so the anchor goes stale on
/clear unless the UserPromptSubmit hook re-registers the live id — the exact
failure that showed ↓ $0.000 in the statusline while savings accrued under a
dead session.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from atelier.core.foundation import session_window as sw
from integrations.claude.plugin.hooks import session_start, user_prompt

SESSION_START = cast(Any, session_start)
USER_PROMPT = cast(Any, user_prompt)

_WINDOW = (43210, 987654)  # (pid, btime) — fixed fake window identity


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Point both hooks and session_window at an isolated root/workspace."""
    root = tmp_path / "atelier"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(sw, "host_window_id", lambda: _WINDOW)
    return root, sw.workspace_hash(str(workspace))


def test_user_prompt_reanchors_window_to_live_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, ws_hash = _setup(tmp_path, monkeypatch)
    # Stale anchor left behind by SessionStart before a /clear.
    sw.register_window_session(root, ws_hash, session_id="pre-clear-sid", source="startup")
    assert sw.resolve_window_session_id(root, ws_hash) == "pre-clear-sid"

    monkeypatch.setattr(user_prompt, "_persist_last_user_prompt", lambda prompt: None)
    monkeypatch.setattr(user_prompt, "_read_session_state", lambda: {})
    monkeypatch.setattr(user_prompt, "_write_session_state", lambda state: None)
    monkeypatch.setattr(
        USER_PROMPT.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "post-clear-sid",
                    "prompt": "continue with the statusline fix",
                    "transcript_path": "",
                }
            )
        ),
    )

    assert user_prompt.main() == 0
    # The prompt payload carries the live id — the anchor must now point at it.
    assert sw.resolve_window_session_id(root, ws_hash) == "post-clear-sid"


def test_session_start_clear_does_not_anchor_preclear_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, ws_hash = _setup(tmp_path, monkeypatch)
    sw.register_window_session(root, ws_hash, session_id="live-sid", source="prompt")

    def _run(payload: dict[str, Any]) -> None:
        monkeypatch.setattr(SESSION_START.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert session_start.main() == 0

    # SessionStart(clear) carries the PRE-clear id — it must not repoint the anchor.
    _run({"session_id": "pre-clear-sid", "source": "clear", "cwd": str(tmp_path / "ws")})
    assert sw.resolve_window_session_id(root, ws_hash) == "live-sid"

    # Every other source carries the live id and must keep anchoring.
    _run({"session_id": "resumed-sid", "source": "resume", "cwd": str(tmp_path / "ws")})
    assert sw.resolve_window_session_id(root, ws_hash) == "resumed-sid"
