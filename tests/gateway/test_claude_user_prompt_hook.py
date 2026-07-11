from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "integrations" / "claude" / "plugin" / "hooks" / "user_prompt.py"


def test_user_prompt_hook_persists_last_user_prompt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lemoncrow_root = tmp_path / ".lemoncrow"
    env = os.environ.copy()
    env.update(
        {
            "LEMONCROW_ROOT": str(lemoncrow_root),
            "LEMONCROW_STORE_ROOT": str(lemoncrow_root),
            "CLAUDE_WORKSPACE_ROOT": str(workspace),
        }
    )

    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "fix the auth flow",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    from lemoncrow.core.foundation.paths import workspace_key

    session_state = lemoncrow_root / "workspaces" / workspace_key(workspace) / "session_state.json"
    data = json.loads(session_state.read_text(encoding="utf-8"))
    assert data["last_user_prompt"] == "fix the auth flow"


def test_user_prompt_hook_bumps_session_turns_for_real_prompts(tmp_path: Path) -> None:
    """mcp_server.py's convergence-spiral detector resets its gather streak on
    a new user message by reading sessions/<id>/stats.json's `turns` counter --
    this hook must call update_session_stats (the same call PostToolUse /
    SubagentStop already use) once per real user-submitted prompt."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lemoncrow_root = tmp_path / ".lemoncrow"
    session_id = "sess-turns"
    env = os.environ.copy()
    env.update(
        {
            "LEMONCROW_ROOT": str(lemoncrow_root),
            "LEMONCROW_STORE_ROOT": str(lemoncrow_root),
            "CLAUDE_WORKSPACE_ROOT": str(workspace),
        }
    )
    from lemoncrow.core.foundation.paths import session_dir

    stats_path = session_dir(lemoncrow_root, "claude", session_id) / "stats.json"

    for i, prompt in enumerate(["first message", "second message"], start=1):
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "prompt": prompt,
            "transcript_path": str(tmp_path / "transcript.jsonl"),
        }
        subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            env=env,
        )
        data = json.loads(stats_path.read_text(encoding="utf-8"))
        assert data["turns"] == i


def test_user_prompt_hook_ignores_noop_continuation_for_turns(tmp_path: Path) -> None:
    """The harness-injected 'Continue from where you left off.' retry is not a
    real user directive and must not bump the turns counter -- otherwise a
    stuck, silent model could wipe its own gather-streak warning via the
    harness's own retry instead of actually editing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lemoncrow_root = tmp_path / ".lemoncrow"
    session_id = "sess-turns-noop"
    env = os.environ.copy()
    env.update(
        {
            "LEMONCROW_ROOT": str(lemoncrow_root),
            "LEMONCROW_STORE_ROOT": str(lemoncrow_root),
            "CLAUDE_WORKSPACE_ROOT": str(workspace),
        }
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "prompt": "Continue from where you left off.",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    from lemoncrow.core.foundation.paths import session_dir

    stats_path = session_dir(lemoncrow_root, "claude", session_id) / "stats.json"
    assert not stats_path.exists()
