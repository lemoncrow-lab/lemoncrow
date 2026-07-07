from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from atelier.core.foundation.paths import session_dir

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "integrations" / "claude" / "plugin" / "scripts" / "statusline.sh"
_SOURCE_PYTHON = str(ROOT / ".venv" / "bin" / "python")


def _run_statusline(root: Path, payload: dict[str, object], *, env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    env.update(
        {
            "ATELIER_ROOT": str(root),
            "ATELIER_STORE_ROOT": str(root),
            "ATELIER_NO_COLOR": "1",
            "ATELIER_PYTHON": _SOURCE_PYTHON,
        }
    )
    env.update(env_extra or {})
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def _payload(session_id: str, *, input_tokens: int = 1200) -> dict[str, object]:
    # Nonzero tokens by default: the statusline now hides the savings figure
    # until the session registers real input/cache usage, so tests that exercise
    # the savings display must look like a session that has actually done work.
    return {
        "session_id": session_id,
        "model": {"display_name": "Sonnet"},
        "context_window": {
            "used_percentage": 0,
            "current_usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
        "cost": {"total_cost_usd": 0.0, "total_duration_ms": 0},
    }


def test_statusline_does_not_borrow_from_stale_bridge_in_new_main_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # New sid
    new_sid = "a1b2c3d4-e5f6-47a8-b9c0-d1e2f3a4b5c6"

    # Old session has savings
    old_sid = "3d829763-d5f4-47ce-b45f-8006fe864df2"
    # Canonical nested layout: sessions/YYYY/MM/DD/<host>/<session_id>/ -- the
    # flat sessions/<id>/ layout was retired (see paths.session_dir).
    sidecar = session_dir(tmp_path, "claude", old_sid)
    sidecar.mkdir(parents=True)
    (sidecar / "savings.jsonl").write_text(
        json.dumps({"tool": "search", "tokens": 603, "calls": 0}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")

    # Run statusline for the NEW session (no parent relationship possible)
    output = _run_statusline(tmp_path, _payload(new_sid), env_extra={"CLAUDE_WORKSPACE_ROOT": str(workspace)})

    # It should report 0 savings, not the stale session's $0.002.
    assert "$0.002" not in output
    assert "$0.000(I:" in output


def test_statusline_borrows_for_subagents_via_transcript(tmp_path: Path) -> None:
    # Need to setup transcript for subagent linking
    home = tmp_path / "home"
    config_dir = home / ".claude"
    projects_dir = config_dir / "projects" / "workspace"
    projects_dir.mkdir(parents=True)

    parent_sid = "3d829763-d5f4-47ce-b45f-8006fe864df2"
    subagent_sid = "agent-a5c5037039b7b4621"

    # Transcript for subagent linking to parent
    transcript = {"sessionId": parent_sid}
    (projects_dir / f"{subagent_sid}.jsonl").write_text(json.dumps(transcript) + "\n", encoding="utf-8")

    # Parent session has savings
    sidecar = session_dir(tmp_path, "claude", parent_sid)
    sidecar.mkdir(parents=True)
    (sidecar / "savings.jsonl").write_text(
        json.dumps({"tool": "search", "tokens": 603, "calls": 0}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")

    # Run statusline for the SUBAGENT session
    env = {"CLAUDE_CONFIG_DIR": str(config_dir)}
    output = _run_statusline(tmp_path, _payload(subagent_sid), env_extra=env)

    # It SHOULD borrow the 603 savings from the parent transcript
    assert "$0.002" in output


def test_statusline_hides_savings_until_real_token_usage(tmp_path: Path) -> None:
    """Zero input AND zero cache tokens must not display savings, even when a
    sidecar carries a value: those are stale or cross-attributed from a sibling
    session until this session registers real usage. Once tokens are nonzero the
    figure shows normally. CLAUDE_CONFIG_DIR is isolated to a temp dir so no real
    transcript on the host supplies token counts.
    """
    sid = "3d829763-d5f4-47ce-b45f-8006fe864df2"
    sidecar = session_dir(tmp_path, "claude", sid)
    sidecar.mkdir(parents=True)
    (sidecar / "savings.jsonl").write_text(
        json.dumps({"tool": "search", "tokens": 603, "calls": 0}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "auth.json").write_text(json.dumps({"authenticated": True}), encoding="utf-8")
    config_dir = tmp_path / "home" / ".claude"
    config_dir.mkdir(parents=True)
    env = {"CLAUDE_CONFIG_DIR": str(config_dir)}

    zero = _run_statusline(tmp_path, _payload(sid, input_tokens=0), env_extra=env)
    assert "$0.000(I:0" in zero

    active = _run_statusline(tmp_path, _payload(sid, input_tokens=1200), env_extra=env)
    assert "$0.000(I:1k" in active
    assert "$0.002(R:603)" in active
