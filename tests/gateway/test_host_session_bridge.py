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
        "LEMONCODE_SESSION_ID",
        "LEMONCODE_CLI",
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


def test_opencode_prompt_claims_exact_session_review_inbox_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    root = tmp_path / ".lemoncrow"
    payload = {"session_id": "ses-review", "cwd": str(tmp_path), "prompt": "continue"}
    enqueue_session_message(
        root,
        host="opencode",
        session_id="ses-review",
        message="Apply review annotation ann-opencode and verify it.",
    )
    enqueue_session_message(
        root,
        host="opencode",
        session_id="other-session",
        message="do not leak this",
    )

    first = plugin_runtime.build_opencode_user_prompt_output(root, payload)
    assert "ann-opencode" in first["additionalContext"]
    assert "do not leak this" not in first["additionalContext"]

    second = plugin_runtime.build_opencode_user_prompt_output(root, payload)
    assert "additionalContext" not in second


def test_lemoncode_prompt_claims_only_lemoncode_review_inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    monkeypatch.setenv("LEMONCROW_AGENT", "lemoncode")
    root = tmp_path / ".lemoncrow"
    payload = {"session_id": "ses-review", "cwd": str(tmp_path), "prompt": "continue"}
    enqueue_session_message(
        root,
        host="lemoncode",
        session_id="ses-review",
        message="Apply LemonCode review annotation ann-lemon and verify it.",
    )
    enqueue_session_message(
        root,
        host="opencode",
        session_id="ses-review",
        message="opencode feedback must stay isolated",
    )

    first = plugin_runtime.build_opencode_user_prompt_output(root, payload, host="lemoncode")
    assert "ann-lemon" in first["additionalContext"]
    assert "opencode feedback" not in first["additionalContext"]

    state_path = plugin_runtime._opencode_session_state_path(root, payload)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["host"] == "lemoncode"

    from lemoncrow.core.foundation.paths import session_dir

    run_file = session_dir(root, "lemoncode", "ses-review") / "run.json"
    run = json.loads(run_file.read_text(encoding="utf-8"))
    assert run["agent"] == "lemoncode"


def test_opencode_session_bridge_marks_active_then_idle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    root = tmp_path / ".lemoncrow"
    payload = {"session_id": "ses-activity", "cwd": str(tmp_path), "model": "gpt-5.6-terra"}

    plugin_runtime.build_opencode_user_prompt_output(root, {**payload, "prompt": "inspect"})
    state_path = plugin_runtime._opencode_session_state_path(root, payload)
    active = json.loads(state_path.read_text(encoding="utf-8"))
    assert active["activity"] == "active"
    assert active["activity_at"]

    plugin_runtime.build_opencode_stop_output(root, payload)
    idle = json.loads(state_path.read_text(encoding="utf-8"))
    assert idle["activity"] == "idle"
    assert idle["activity_at"]


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

    assert "lc session idle." in status["uiMessage"]
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


def test_request_scoped_model_wins_over_workspace_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_AGENT", "codex")
    payload = {"session_id": "bridge-sid", "cwd": str(tmp_path)}
    plugin_runtime._write_codex_session_state(
        tmp_path,
        payload,
        {"session_id": "bridge-sid", "host": "codex", "model": "stale-model"},
    )

    prior = mcp_server._set_request_session("caller-sid", "codex", "gpt-5.6-sol")
    try:
        assert mcp_server._get_mcp_model() == "gpt-5.6-sol"
    finally:
        mcp_server._clear_request_session(prior)


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


def test_opencode_edit_records_opencode_provenance_and_signals_review_prewarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import live_runtime

    monkeypatch.setenv("LEMONCROW_AGENT", "opencode")
    root = tmp_path / ".lemoncrow"
    payload = {"session_id": "ses-edit", "cwd": str(tmp_path), "model": "gpt-5.6-terra"}
    plugin_runtime.build_opencode_user_prompt_output(root, {**payload, "prompt": "edit it"})
    prepared: list[tuple[Path, Path, str, str]] = []
    monkeypatch.setattr(
        live_runtime,
        "request_live_review_prepare",
        lambda store_root, repo_root, *, host, session_id: prepared.append(
            (Path(store_root), Path(repo_root), host, session_id)
        ),
    )

    plugin_runtime.build_opencode_post_tool_use_output(
        root,
        {
            **payload,
            "tool_name": "apply_patch",
            "tool_input": {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
            "tool_response": {"output": "ok"},
        },
    )

    assert prepared == [(root, tmp_path, "opencode", "ses-edit")]
    run_file = plugin_runtime._codex_run_file(root, "ses-edit")
    events = json.loads(run_file.read_text(encoding="utf-8"))["events"]
    edit = next(event for event in events if event["kind"] == "file_edit")
    assert edit["payload"]["host"] == "opencode"


def test_lemoncode_shared_bridge_keeps_exact_host_for_inbox_prompt_and_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.foundation.paths import session_dir
    from lemoncrow.pro.capabilities.review import live_runtime
    from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

    monkeypatch.setenv("LEMONCROW_AGENT", "lemoncode")
    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = {"session_id": "lemon-sid", "cwd": str(workspace), "model": "gpt-test"}
    enqueue_session_message(root, host="lemoncode", session_id="lemon-sid", message="Address annotation ann-lemon.")
    prepared: list[tuple[Path, Path, str, str]] = []
    monkeypatch.setattr(
        live_runtime,
        "request_live_review_prepare",
        lambda store_root, repo_root, *, host, session_id: prepared.append(
            (Path(store_root), Path(repo_root), host, session_id)
        )
        or object(),
    )

    output = plugin_runtime.build_opencode_user_prompt_output(
        root, {**payload, "prompt": "Continue the fix"}, host="lemoncode"
    )
    assert "ann-lemon" in output["additionalContext"]
    state = json.loads(
        plugin_runtime._opencode_session_state_path(root, payload, host="lemoncode").read_text(encoding="utf-8")
    )
    assert state["host"] == "lemoncode"
    assert prepared == [(root, workspace, "lemoncode", "lemon-sid")]

    plugin_runtime.build_opencode_post_tool_use_output(
        root,
        {
            **payload,
            "tool_name": "apply_patch",
            "tool_input": {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
            "tool_response": {"output": "ok"},
        },
        host="lemoncode",
    )
    run = json.loads((session_dir(root, "lemoncode", "lemon-sid") / "run.json").read_text(encoding="utf-8"))
    edit = next(event for event in run["events"] if event["kind"] == "file_edit")
    assert edit["payload"]["host"] == "lemoncode"
    assert prepared[-1] == (root, workspace, "lemoncode", "lemon-sid")


def test_codex_first_prompt_seeds_live_review_prepare_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import live_runtime

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[Path, Path, str, str]] = []
    monkeypatch.setattr(
        live_runtime,
        "request_live_review_prepare",
        lambda store_root, repo_root, *, host, session_id: calls.append(
            (Path(store_root), Path(repo_root), host, session_id)
        )
        or object(),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "codex-seed",
        "cwd": str(workspace),
        "prompt": "inspect",
    }

    plugin_runtime.build_codex_user_prompt_output(root, payload)
    plugin_runtime.build_codex_user_prompt_output(root, {**payload, "prompt": "continue"})

    assert calls == [(root, workspace, "codex", "codex-seed")]


def test_codex_prompt_rearms_live_review_prepare_after_lease_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review import live_runtime

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[str] = []
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_REARM_MS", "5000")
    monkeypatch.setattr(
        live_runtime,
        "request_live_review_prepare",
        lambda _store_root, _repo_root, *, host, session_id: calls.append(f"{host}:{session_id}") or object(),
    )
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "codex-rearm",
        "cwd": str(workspace),
        "prompt": "inspect",
        "now_ms": 10_000,
    }

    plugin_runtime.build_codex_user_prompt_output(root, payload)
    plugin_runtime.build_codex_user_prompt_output(root, {**payload, "prompt": "soon", "now_ms": 14_999})
    plugin_runtime.build_codex_user_prompt_output(root, {**payload, "prompt": "later", "now_ms": 15_000})

    assert calls == ["codex:codex-rearm", "codex:codex-rearm"]
    stats = json.loads(plugin_runtime.session_stats_path(root, "codex-rearm").read_text(encoding="utf-8"))
    assert stats["live_review_seeded_at_ms"] == 15_000


def test_opencode_first_prompt_seeds_live_review_prepare_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import live_runtime

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[Path, Path, str, str]] = []
    monkeypatch.setattr(
        live_runtime,
        "request_live_review_prepare",
        lambda store_root, repo_root, *, host, session_id: calls.append(
            (Path(store_root), Path(repo_root), host, session_id)
        )
        or object(),
    )
    payload = {"session_id": "opencode-seed", "cwd": str(workspace), "prompt": "inspect"}

    plugin_runtime.build_opencode_user_prompt_output(root, payload)
    plugin_runtime.build_opencode_user_prompt_output(root, {**payload, "prompt": "continue"})

    assert calls == [(root, workspace, "opencode", "opencode-seed")]


def test_visible_session_turns_are_idempotent_and_host_scoped(tmp_path: Path) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    turns = [
        ("user", "u1", "Please inspect checkout.py"),
        ("assistant", "a1", "I found the validator path and will update it."),
    ]

    assert (
        plugin_runtime.append_visible_session_turns(
            root, host="codex", session_id="visible-sid", turns=turns, workspace=str(workspace)
        )
        == 2
    )
    assert (
        plugin_runtime.append_visible_session_turns(
            root, host="codex", session_id="visible-sid", turns=turns, workspace=str(workspace)
        )
        == 0
    )

    run = json.loads((session_dir(root, "codex", "visible-sid") / "run.json").read_text(encoding="utf-8"))
    assert run["agent"] == "codex"
    assert run["workspace_path"] == str(workspace)
    visible = [event for event in run["events"] if event.get("kind") == "agent_message"]
    assert [event["payload"]["role"] for event in visible] == ["user", "assistant"]
    assert len({event["payload"]["visible_turn_id"] for event in visible}) == 2


def test_opencode_prompt_and_visible_reply_flow_into_same_run_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = {"session_id": "oc-visible", "cwd": str(workspace), "model": "gpt-test"}
    monkeypatch.setattr(
        plugin_runtime,
        "_opencode_visible_reply_blocks",
        lambda session_id, **_kwargs: [("assistant-1", "I changed checkout.py and the focused tests pass.")],
    )

    plugin_runtime.build_opencode_user_prompt_output(root, {**payload, "prompt": "Fix checkout.py"})
    plugin_runtime.build_opencode_user_prompt_output(root, {**payload, "prompt": "Also verify the API contract"})
    plugin_runtime.build_opencode_stop_output(root, payload)
    plugin_runtime.build_opencode_stop_output(root, payload)

    run = json.loads((session_dir(root, "opencode", "oc-visible") / "run.json").read_text(encoding="utf-8"))
    visible = [event for event in run["events"] if event.get("kind") == "agent_message"]
    assert sum(event["payload"].get("role") == "user" for event in visible) == 2
    assistant = [event for event in visible if event["payload"].get("role") == "assistant"]
    assert len(assistant) == 1
    assert "focused tests pass" in assistant[0]["payload"]["message"]


def test_codex_stop_journals_visible_reply_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.foundation.paths import session_dir

    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        plugin_runtime,
        "_codex_visible_reply_blocks",
        lambda payload, session_id: [("reply-1", "Reviewed the changed API and ran its tests.")],
    )
    payload = {"hook_event_name": "Stop", "session_id": "codex-visible", "cwd": str(workspace)}

    plugin_runtime.build_codex_stop_output(root, payload)
    plugin_runtime.build_codex_stop_output(root, payload)

    run = json.loads((session_dir(root, "codex", "codex-visible") / "run.json").read_text(encoding="utf-8"))
    assistant = [
        event
        for event in run["events"]
        if event.get("kind") == "agent_message" and (event.get("payload") or {}).get("role") == "assistant"
    ]
    assert len(assistant) == 1
    assert "ran its tests" in assistant[0]["payload"]["message"]
