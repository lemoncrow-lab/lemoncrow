"""In-process unit tests for Codex lifecycle-hook parity (Phase 1).

These exercise the ``build_codex_*`` runtime functions directly (no subprocess),
so they run in the default fast suite. The subprocess-level smoke tests live in
``test_codex_plugin_hooks.py`` (marked slow).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import lemoncrow.bench.mode  # noqa: F401 - registers sys.modules entry; see bench_mode below
from lemoncrow.core.capabilities import plugin_runtime
from lemoncrow.gateway.cli.commands import sessions as session_commands
from lemoncrow.gateway.hosts.session_parsers import _session_parser

# lemoncrow.bench's own __init__.py does `from lemoncrow.bench.mode import ..., mode`, which
# rebinds the `mode` attribute on the lemoncrow.bench package to that function -- shadowing
# the submodule. Reach the real module via sys.modules instead of attribute access.
bench_mode = sys.modules["lemoncrow.bench.mode"]

ROOT = Path(__file__).resolve().parents[2]


def _seed_run_file(root: Path, session_id: str) -> Path:
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_file = runs / f"{session_id}.json"
    run_file.write_text(
        json.dumps({"session_id": session_id, "events": [], "files_touched": []}),
        encoding="utf-8",
    )
    return run_file


def _write_session_state(root: Path, payload: dict, state: dict) -> None:
    path = plugin_runtime._codex_session_state_path(root, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _events(root: Path, session_id: str) -> list[dict]:
    data = json.loads((root / "runs" / f"{session_id}.json").read_text(encoding="utf-8"))
    return data["events"]


# --------------------------------------------------------------------------
# tool normalization
# --------------------------------------------------------------------------
def test_normalize_codex_tool_maps_native_and_mcp_tools() -> None:
    assert plugin_runtime._normalize_codex_tool("apply_patch") == "edit"
    assert plugin_runtime._normalize_codex_tool("mcp__lemon__edit") == "edit"
    assert plugin_runtime._normalize_codex_tool("mcp__plugin_lemoncrow_lemon__Edit") == "edit"
    assert plugin_runtime._normalize_codex_tool("lemon.edit") == "edit"
    assert plugin_runtime._normalize_codex_tool("lemon.bash") == "bash"
    assert plugin_runtime._normalize_codex_tool("lemon::bash") == "bash"
    assert plugin_runtime._normalize_codex_tool("mcp__anything_lemoncrow_anything__read") == "read"
    assert plugin_runtime._normalize_codex_tool("shell") == "bash"
    assert plugin_runtime._normalize_codex_tool("local_shell") == "bash"
    assert plugin_runtime._normalize_codex_tool("read") == "read"
    assert plugin_runtime._normalize_codex_tool("web_search") == "other"


def test_codex_native_tool_replacement_maps_apply_patch_to_lemon_edit() -> None:
    # apply_patch is Codex's native patch tool -- must nudge to mcp__lemon__edit
    # exactly like edit/write/multiedit, not fall through unmapped (regression for
    # the gap where apply_patch calls went unnudged and landed as native patches).
    for tool_name in ("apply_patch", "patch", "replace", "edit", "write", "multiedit"):
        replacement = plugin_runtime._codex_native_tool_replacement({"tool_name": tool_name})
        assert replacement is not None
        assert replacement[0] == "mcp__lemon__edit"


def test_session_tool_normalizers_use_generic_lemoncrow_namespace() -> None:
    for name in (
        "mcp__lemon__read",
        "mcp__plugin_lemoncrow_lemon__read",
        "lemon.read",
        "lemon::read",
        "lemon_read",
        "mcp__anything_lemoncrow_anything__read",
    ):
        assert session_commands._is_lemoncrow_tool_name(name)
        assert session_commands._base_tool_name(name) == "read"
        assert _session_parser._is_lemoncrow_mcp_tool(name)
        assert _session_parser._normalize_tool_basename(name) == "read"


# --------------------------------------------------------------------------
# PreToolUse read-after-edit guard
# --------------------------------------------------------------------------
def test_pre_tool_use_denies_full_reread_after_edit(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    plugin_runtime.build_codex_post_tool_use_ledger_output(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "apply_patch",
            "tool_input": {"file_path": "src/a.py", "old_string": "x = 1", "new_string": "x = 2"},
            "cwd": str(tmp_path),
        },
    )

    out = plugin_runtime.build_codex_pre_tool_use_output(
        root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "mcp__lemon__read",
            "tool_input": {"files": ["src/a.py:full"]},
            "cwd": str(tmp_path),
        },
    )

    hook = out.get("hookSpecificOutput") or {}
    assert hook.get("permissionDecision") == "deny"
    assert "Edited" in hook.get("permissionDecisionReason", "")


def test_pre_tool_use_allows_ranges_and_unedited_files(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    plugin_runtime.build_codex_post_tool_use_ledger_output(
        root,
        {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "apply_patch",
            "tool_input": {"file_path": "src/a.py", "old_string": "x = 1", "new_string": "x = 2"},
            "cwd": str(tmp_path),
        },
    )

    ranged = plugin_runtime.build_codex_pre_tool_use_output(
        root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "mcp__lemon__read",
            "tool_input": {"files": ["src/a.py:L1-L20"]},
            "cwd": str(tmp_path),
        },
    )
    other = plugin_runtime.build_codex_pre_tool_use_output(
        root,
        {
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "mcp__lemon__read",
            "tool_input": {"files": ["src/b.py:full"]},
            "cwd": str(tmp_path),
        },
    )

    assert ranged.get("no_output") is True
    assert other.get("no_output") is True


# --------------------------------------------------------------------------
# PostToolUse run-ledger capture + failure rescue
# --------------------------------------------------------------------------
def test_post_tool_use_records_file_edit(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": "apply_patch",
        "tool_input": {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
        "cwd": str(tmp_path),
    }
    out = plugin_runtime.build_codex_post_tool_use_ledger_output(root, payload)
    assert out.get("no_output") is True
    events = _events(root, session_id)
    file_edits = [e for e in events if e["kind"] == "file_edit"]
    assert len(file_edits) == 1
    assert file_edits[0]["payload"]["path"] == "a.py"
    assert "x = 2" in file_edits[0]["payload"]["diff"]
    data = json.loads((root / "runs" / f"{session_id}.json").read_text(encoding="utf-8"))
    assert "a.py" in data["files_touched"]


def test_post_tool_use_ignores_read_tools(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": "read",
        "tool_input": {"path": "a.py"},
        "cwd": str(tmp_path),
    }
    assert plugin_runtime.build_codex_post_tool_use_ledger_output(root, payload).get("no_output") is True
    assert _events(root, session_id) == []


def test_post_tool_use_records_command_and_rescues_on_repeat_failure(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": "shell",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"stderr": "AssertionError: boom", "exit_code": 1},
        "cwd": str(tmp_path),
    }
    first = plugin_runtime.build_codex_post_tool_use_ledger_output(root, payload)
    assert first.get("no_output") is True
    second = plugin_runtime.build_codex_post_tool_use_ledger_output(root, payload)
    assert "rescue" in second.get("systemMessage", "").lower()
    commands = [e for e in _events(root, session_id) if e["kind"] == "command_result"]
    assert len(commands) == 2
    assert commands[0]["payload"]["ok"] is False
    assert commands[0]["payload"]["command"] == "pytest -q"


def test_post_tool_use_successful_command_is_silent(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": "shell",
        "tool_input": {"command": "echo hi"},
        "tool_response": {"stdout": "hi", "exit_code": 0},
        "cwd": str(tmp_path),
    }
    out = plugin_runtime.build_codex_post_tool_use_ledger_output(root, payload)
    assert out.get("no_output") is True
    commands = [e for e in _events(root, session_id) if e["kind"] == "command_result"]
    assert commands[0]["payload"]["ok"] is True


# --------------------------------------------------------------------------
# Compaction lifecycle
# --------------------------------------------------------------------------
def test_post_compact_bumps_epoch_and_notes(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "PostCompact",
        "session_id": session_id,
        "cwd": str(tmp_path),
        "trigger": "auto",
    }
    assert plugin_runtime.build_codex_post_compact_output(root, payload).get("no_output") is True
    state = json.loads(plugin_runtime._codex_session_state_path(root, payload).read_text(encoding="utf-8"))
    assert state["compaction_epoch"] == 1
    notes = [e for e in _events(root, session_id) if e["kind"] == "note"]
    assert any("completed" in e["summary"] for e in notes)


def test_pre_compact_snapshots_occupancy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.context_state.host_context_state",
        lambda host, session_id: (123_000, "gpt-5.5"),
    )
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "PreCompact",
        "session_id": session_id,
        "cwd": str(tmp_path),
        "trigger": "manual",
    }
    assert plugin_runtime.build_codex_pre_compact_output(root, payload).get("no_output") is True
    state = json.loads(plugin_runtime._codex_session_state_path(root, payload).read_text(encoding="utf-8"))
    assert state["precompact_occupancy"] == 123_000
    assert state["precompact_pending"] is True
    notes = [e for e in _events(root, session_id) if e["kind"] == "note"]
    assert any("starting" in e["summary"] for e in notes)


# --------------------------------------------------------------------------
# UserPromptSubmit + Stop enrichment
# --------------------------------------------------------------------------
def test_user_prompt_records_agent_message_and_last_prompt(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "prompt": "Refactor the parser",
        "cwd": str(tmp_path),
    }
    plugin_runtime._codex_enrich_user_prompt(root, payload)
    state = json.loads(plugin_runtime._codex_session_state_path(root, payload).read_text(encoding="utf-8"))
    assert state["last_user_prompt"] == "Refactor the parser"
    messages = [e for e in _events(root, session_id) if e["kind"] == "agent_message"]
    assert len(messages) == 1
    assert messages[0]["payload"]["role"] == "user"
    assert messages[0]["payload"]["prompt"] == "Refactor the parser"


def test_user_prompt_banks_pending_compaction_credit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After /compact, the next user prompt writes the compaction boundary marker.

    PreCompact stored the pre-compaction occupancy; once a turn runs on the
    compacted window the prompt path reads the smaller occupancy and appends a
    ``kind:"compaction"`` marker row (NOT a savings credit — every reader
    zeroes compaction rows; the marker segments carry/cliff attribution), then
    clears the precompact_* keys. Parity with the Claude UserPromptSubmit hook.
    """
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "prompt": "Continue",
        "cwd": str(tmp_path),
    }
    # PreCompact snapshot: 180k tokens were in the window before compaction.
    _write_session_state(
        root,
        payload,
        {
            "precompact_pending": True,
            "precompact_occupancy": 180_000,
            "precompact_model": "gpt-5",
            "precompact_attempts": 0,
        },
    )
    # Post-compaction the window now holds 60k tokens.
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.context_state.host_context_state",
        lambda host, session_id: (60_000, "gpt-5"),
    )

    plugin_runtime._codex_enrich_user_prompt(root, payload)

    from lemoncrow.core.foundation.paths import session_dir

    sidecar = session_dir(root, "codex", session_id) / "savings.jsonl"
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    comp = [r for r in rows if r.get("kind") == "compaction"]
    assert len(comp) == 1
    assert comp[0]["model"] == "gpt-5"
    # Boundary marker only — every savings reader zeroes compaction rows, so
    # the row carries no dead tokens/usd pricing (it exists to segment
    # carry/cliff attribution at the compaction point).
    assert "tokens" not in comp[0]
    assert "usd" not in comp[0]
    assert "calls" not in comp[0]

    # precompact_* keys are cleared so the credit is one-shot per compaction.
    state = json.loads(plugin_runtime._codex_session_state_path(root, payload).read_text(encoding="utf-8"))
    assert "precompact_pending" not in state
    assert "precompact_occupancy" not in state
    assert "precompact_model" not in state
    assert "precompact_attempts" not in state

    # A second prompt does not double-credit (no pending compaction left).
    plugin_runtime._codex_enrich_user_prompt(root, payload)
    rows2 = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len([r for r in rows2 if r.get("kind") == "compaction"]) == 1


def test_user_prompt_skips_compaction_credit_when_delta_not_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credit while the post-compaction window hasn't shrunk yet; give up after 3."""
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "prompt": "Continue",
        "cwd": str(tmp_path),
    }
    _write_session_state(
        root,
        payload,
        {
            "precompact_pending": True,
            "precompact_occupancy": 180_000,
            "precompact_model": "gpt-5",
            "precompact_attempts": 0,
        },
    )
    # Occupancy has NOT dropped (delta <= 0) -> no credit, attempts increments.
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.context_state.host_context_state",
        lambda host, session_id: (185_000, "gpt-5"),
    )

    for _ in range(3):
        plugin_runtime._codex_enrich_user_prompt(root, payload)

    from lemoncrow.core.foundation.paths import session_dir

    savings = session_dir(root, "codex", session_id) / "savings.jsonl"
    assert not savings.exists()
    # After 3 unresolved attempts the pending state is cleared (stop trying).
    state = json.loads(plugin_runtime._codex_session_state_path(root, payload).read_text(encoding="utf-8"))
    assert "precompact_pending" not in state


# --------------------------------------------------------------------------
# PermissionRequest auto-deny (Codex-exclusive)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf $HOME",
        "sudo rm -rf /",
        "rm -rf /usr",
        "rm -rf /etc/",
        "rm --recursive --force /",
        "git push --force origin main",
        "git push -f",
        "git push -f origin main",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
    ],
)
def test_permission_request_denies_destructive_commands(tmp_path: Path, command: str) -> None:
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "shell",
        "tool_input": {"command": command},
    }
    out = plugin_runtime.build_codex_permission_request_output(tmp_path / ".lemoncrow", payload)
    assert (out.get("hookSpecificOutput") or {}).get("behavior") == "deny", command


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build/",
        "rm -rf ./node_modules",
        "rm -rf /usr/local/foo",
        "git push --force-with-lease",
        "git push origin main",
        "ls -la",
        "pytest -q",
    ],
)
def test_permission_request_allows_safe_commands(tmp_path: Path, command: str) -> None:
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "shell",
        "tool_input": {"command": command},
    }
    assert plugin_runtime.build_codex_permission_request_output(tmp_path / ".lemoncrow", payload).get("no_output") is True


def test_permission_request_ignores_non_bash_tools(tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "apply_patch",
        "tool_input": {"file_path": "a.py"},
    }
    assert plugin_runtime.build_codex_permission_request_output(tmp_path / ".lemoncrow", payload).get("no_output") is True


# --------------------------------------------------------------------------
# codex exec --json telemetry collector (Codex-exclusive)
# --------------------------------------------------------------------------
def test_ingest_codex_exec_events_records_command_and_file(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    session_id = "run1"
    _seed_run_file(root, session_id)
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest -q", "exit_code": 1, "output": "boom"},
            }
        ),
        json.dumps({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py"}]}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
        "not json",
    ]
    count = plugin_runtime.ingest_codex_exec_events(root, session_id, lines)
    assert count == 2
    events = _events(root, session_id)
    kinds = {e["kind"] for e in events}
    assert kinds == {"command_result", "file_edit"}
    cmd = next(e for e in events if e["kind"] == "command_result")
    assert cmd["payload"]["ok"] is False
    assert cmd["payload"]["command"] == "pytest -q"
    data = json.loads((root / "runs" / f"{session_id}.json").read_text(encoding="utf-8"))
    assert "a.py" in data["files_touched"]


def test_ingest_codex_exec_events_noop_without_session(tmp_path: Path) -> None:
    assert plugin_runtime.ingest_codex_exec_events(tmp_path / ".lemoncrow", "", ["{}"]) == 0


# --------------------------------------------------------------------------
# Per-role Codex agents
# --------------------------------------------------------------------------
def test_write_codex_agents_defaults_to_code_only(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.workspace_host_overrides import write_codex_agents

    target = tmp_path / "agents"
    written = write_codex_agents(target, repo_root=ROOT)
    # Default install ships only the code role; other roles are opt-in via role_ids.
    assert len(written) == 1
    names = {p.name for p in written}
    assert names == {"lemoncrow.code.toml"}
    text = (target / "lemoncrow.code.toml").read_text(encoding="utf-8")
    assert 'name = "lemoncrow.code"' in text
    assert "developer_instructions" in text


def test_write_codex_agents_generates_all_surfaced_roles_when_requested(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.default_definitions import SURFACED_ROLE_IDS
    from lemoncrow.core.capabilities.workspace_host_overrides import write_codex_agents

    target = tmp_path / "agents"
    written = write_codex_agents(target, repo_root=ROOT, role_ids=SURFACED_ROLE_IDS)
    # 8 host-facing roles (incl. general) + the autonomous auto/bare roles = 10.
    assert len(written) == 10
    names = {p.name for p in written}
    assert {"lemoncrow.code.toml", "lemoncrow.explore.toml", "lemoncrow.solve.toml"} <= names
    text = (target / "lemoncrow.code.toml").read_text(encoding="utf-8")
    assert 'name = "lemoncrow.code"' in text
    assert "developer_instructions" in text


def test_render_codex_agent_toml_escapes_hostile_body() -> None:
    import tomllib

    from lemoncrow.core.capabilities.workspace_host_overrides import _render_codex_agent_toml

    # Body with everything that breaks naive TOML rendering: a regex backslash,
    # a Windows path, a bare quote, and a literal triple-quote run.
    body = 'use regex \\d+ and path C:\\temp; quote " and triple """ end'
    description = 'a "quoted" desc with \\ backslash'
    rendered = _render_codex_agent_toml("code", description, body, "gpt-5.5")
    parsed = tomllib.loads(rendered)  # must not raise
    assert parsed["name"] == "lemoncrow.code"
    assert parsed["model"] == "gpt-5.5"
    assert parsed["description"] == 'a "quoted" desc with \\ backslash'
    instr = parsed["developer_instructions"]
    assert "\\d+" in instr  # literal backslash-d survived (not a TOML escape)
    assert "C:\\temp" in instr
    assert '"""' in instr  # literal triple-quote round-tripped


def test_write_codex_agents_prunes_stale_roles(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.workspace_host_overrides import write_codex_agents

    target = tmp_path / "agents"
    target.mkdir()
    (target / "lemoncrow.removed.toml").write_text('name = "lemoncrow.removed"\n', encoding="utf-8")
    write_codex_agents(target, repo_root=ROOT)
    assert not (target / "lemoncrow.removed.toml").exists()


# --------------------------------------------------------------------------
# Manifest wiring
# --------------------------------------------------------------------------
def test_codex_hooks_manifest_includes_new_lifecycle_events() -> None:
    data = json.loads((ROOT / "integrations" / "codex" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "Stop",
    ):
        assert event in data["hooks"], f"missing hooks.json event: {event}"
    assert "PermissionRequest" in data["hooks"]
    rendered = json.dumps(data)
    assert "compact.py" in rendered
    assert "permission_request.py" in rendered
    assert "${PLUGIN_ROOT}/hooks/" in rendered
    assert "__LEMONCROW_PYTHON__" in rendered
    assert "__LEMONCROW_REPO_SRC__" in rendered
