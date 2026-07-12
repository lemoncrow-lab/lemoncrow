"""Codex/OpenCode never set a launch-time session env var for the MCP server
(unlike Claude's window-anchored file, or CLAUDE_CODE_SESSION_ID). Without a
fallback, every MCP tool call's savings row lands in the unattributed
quarantine ledger and the session always shows "Saved $0.000" even though
Cost displays correctly. This covers the workspace_state.json bridge fix:
Codex/OpenCode hooks refresh it with the live session_id, and
mcp_server._resolved_host_session() reads it as a fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.core.capabilities import plugin_runtime
from lemoncrow.gateway.adapters import mcp_server


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Deterministic workspace identity shared by the hook writer and the MCP
    # server reader, and a private LEMONCROW_ROOT so the test never touches
    # the real ~/.lemoncrow store.
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    for var in (
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "GITHUB_COPILOT_SESSION_ID",
        "CURSOR_SESSION_ID",
        "CURSOR_TRACE_ID",
        "HERMES_SESSION_ID",
        "ANTIGRAVITY_SESSION_ID",
        "AGY_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    # No real `claude` process ancestor in the test runner -- pin this
    # explicitly rather than relying on /proc absence so the test is stable
    # across platforms/CI.
    monkeypatch.setattr(mcp_server, "_resolve_live_session_id", lambda: "")
    mcp_server._SAVINGS_SIDECAR_PATH_BY_SID.clear()


def test_opencode_hook_bridges_session_id_for_mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set the same way the real subprocess is launched: `lc mcp --host
    # opencode` -> LEMONCROW_AGENT=opencode (see cli/commands/mcp.py mcp_group).
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    payload = {"session_id": "ses_abc123", "prompt": "hi", "model": "gpt-5.6-terra"}
    plugin_runtime._write_opencode_session_state(tmp_path, payload)

    state_path = plugin_runtime._opencode_session_state_path(tmp_path, payload)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["session_id"] == "ses_abc123"
    assert state["model"] == "gpt-5.6-terra"

    sid, host = mcp_server._resolved_host_session()
    assert (sid, host) == ("ses_abc123", "opencode")
    assert mcp_server._get_mcp_model() == "gpt-5.6-terra"


def test_opencode_lifecycle_tracks_tools_and_renders_idle_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    root = tmp_path / ".lemoncrow"
    payload = {"session_id": "ses-status", "cwd": str(tmp_path), "model": "gpt-5.6-terra"}

    plugin_runtime.build_opencode_user_prompt_output(root, {**payload, "prompt": "inspect the parser"})
    assert (
        plugin_runtime.build_opencode_post_tool_use_output(
            root,
            {
                **payload,
                "tool_name": "lc_read",
                "tool_input": {"files": ["a.py"]},
                "tool_response": {"output": "ok"},
            },
        ).get("no_output")
        is True
    )

    status = plugin_runtime.build_opencode_stop_output(root, payload)

    assert "LemonCrow session idle." in status["uiMessage"]
    assert "1 prompt turn · 1 tool call" in status["uiMessage"]
    assert "tools: lc_read×1" in status["uiMessage"]  # noqa: RUF001
    assert plugin_runtime.build_opencode_stop_output(root, payload).get("no_output") is True


def test_codex_hook_bridges_session_id_for_mcp_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_AGENT", "codex")
    payload = {"session_id": "rollout-xyz", "cwd": str(tmp_path)}
    # The codex SessionStart hook (update_notification.py) is the session_id
    # writer and stamps host alongside it; mirror that pairing here.
    plugin_runtime._write_codex_session_state(
        tmp_path,
        payload,
        {"session_id": "rollout-xyz", "host": "codex", "model": "gpt-5.6-terra"},
    )

    sid, host = mcp_server._resolved_host_session()
    assert (sid, host) == ("rollout-xyz", "codex")
    assert mcp_server._get_mcp_model() == "gpt-5.6-terra"


def test_no_bridge_file_resolves_empty(tmp_path: Path) -> None:
    assert mcp_server._resolved_host_session() == ("", "")
    assert mcp_server._workspace_bridge_session_id() == ""


def test_bridge_written_by_other_host_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A codex-stamped slot must not be adopted by an OpenCode MCP server:
    # last-writer-wins across hosts would otherwise attribute savings to a
    # phantom session (codex sid under host "opencode").
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    payload = {"session_id": "rollout-999", "cwd": str(tmp_path)}
    plugin_runtime._write_codex_session_state(tmp_path, payload, {"session_id": "rollout-999", "host": "codex"})

    assert mcp_server._workspace_bridge_session_id() == ""
    assert mcp_server._resolved_host_session() == ("", "")


def test_bridge_without_host_stamp_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Legacy slots (written before the host stamp existed) carry no ownership
    # signal, so the reader must fail closed -> quarantine ledger.
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    payload = {"session_id": "ses_legacy", "cwd": str(tmp_path)}
    plugin_runtime._write_codex_session_state(tmp_path, payload, {"session_id": "ses_legacy"})

    assert mcp_server._workspace_bridge_session_id() == ""


def test_claude_never_uses_workspace_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude has a window-anchored resolver; adopting the workspace-shared slot
    # would cross-contaminate concurrent windows in one repo.
    monkeypatch.setenv("LEMONCROW_AGENT", "claude")
    payload = {"session_id": "claude-uuid-1", "prompt": "hi"}
    state_path = plugin_runtime._opencode_session_state_path(tmp_path, payload)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"session_id": "claude-uuid-1", "host": "claude"}), encoding="utf-8")

    assert mcp_server._workspace_bridge_session_id() == ""
