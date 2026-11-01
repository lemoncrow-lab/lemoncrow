"""Search-first PreToolUse routing: main/subagent, exception, telemetry."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "integrations/claude/plugin/hooks/search_first.py"
_SPEC = importlib.util.spec_from_file_location("lc_search_first_hook", _HOOK)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def _event(command: str | list[str], *, agent: bool = False, tool: str = "mcp__lc__bash") -> dict:
    result = {"tool_name": tool, "tool_input": {"command": command}, "session_id": "test-session"}
    if agent:
        result["agent_id"] = "test-agent"
    return result


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn 'symbol' backend/src",
        "rg -n 'symbol' frontend",
        "cd backend && grep -rn 'thing' src | head -40",
        "ls src; grep -n 'handler' src/foo.py",
        ["git status --short", "grep -R 'handler' src | head -10"],
        "cat src/foo.py | grep -n 'handler'",
    ],
)
def test_source_discovery_must_use_indexed_tools(command):
    assert _MOD.classify(_event(command, agent=True)) == ("deny", "source_search_via_bash")


@pytest.mark.parametrize(
    "command",
    [
        "git diff | grep 'test'",
        "git status --short | grep ' M '",
        "pytest -q | grep 'failed'",
        "cd backend && git diff | grep 'function'",
        "git show HEAD | grep -n 'thing'",
    ],
)
def test_generated_output_filters_preserve_shell_semantics(command):
    assert _MOD.classify(_event(command)) == ("allow_shell", "generated_output")


def test_override_for_exact_semantics():
    assert _MOD.classify(_event("rg -n 'x' src | cut -f1 # lc:allow-shell-search")) == (
        "allow_shell",
        "explicit_exception",
    )


def test_no_capture_or_deny_of_unrelated_commands():
    assert _MOD.classify(_event("uv run pytest -q")) is None
    assert _MOD.classify(_event("git grep -n symbol")) == ("deny", "source_search_via_bash")
    assert _MOD.classify(_event("grep test", tool="SomethingElse")) is None
    assert _MOD.classify(_event("rg -n 'x' src", tool="mcp__lc__code_search")) == ("indexed", "code_search")
    assert _MOD.classify(_event("rg x src", tool="Bash")) == ("deny", "source_search_via_bash")


def test_deny_and_telemetry_for_subagent(tmp_path, monkeypatch):
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setattr(_MOD, "_dormant", lambda: False)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(_event("rg -n secret src", agent=True))))
    out = __import__("io").StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert _MOD.main() == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
    logged = list((tmp_path / "telemetry/search-routing").glob("*.jsonl"))
    assert len(logged) == 1
    payload = json.loads(logged[0].read_text())
    assert payload == {"decision": "deny", "reason": "source_search_via_bash", "role": "subagent"}
    assert "secret" not in logged[0].read_text()


def test_disabled_guard_does_not_write_or_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_SEARCH_FIRST", "0")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(_event("rg x src"))))
    out = __import__("io").StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert _MOD.main() == 0
    assert out.getvalue() == ""
    assert not (tmp_path / "telemetry/search-routing").exists()


def test_hook_wired_into_existing_pre_tool_process():
    hooks = json.loads((_HOOK.parent / "hooks.json").read_text())["hooks"]["PreToolUse"]
    assert any("pre_tool_discipline.py" in x.get("command", "") for h in hooks for x in h["hooks"])
    assert "from search_first import _record, classify" in (_HOOK.parent / "pre_tool_discipline.py").read_text()
    assert not any("search_first.py" in x.get("command", "") for h in hooks for x in h["hooks"])


@pytest.mark.parametrize(
    "command",
    [
        "rg -n 'needle' .",
        "grep -rn 'needle' src",
        "cd nested && rg -n needle . | head -20",
        "grep -F 'value' arbitrary.txt",
    ],
)
def test_non_git_workspace_allows_bash_search(tmp_path, command):
    standalone = tmp_path / "standalone"
    (standalone / "nested").mkdir(parents=True)
    event = _event(command, agent=True)
    event["tool_input"]["cwd"] = str(standalone)
    assert _MOD.classify(event) == ("allow_shell", "non_git_directory")


def test_cd_from_git_to_non_git_directory_allows_search(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    outside = tmp_path / "standalone"
    outside.mkdir()
    event = _event(f"cd {outside} && grep -rn needle .", agent=True)
    event["tool_input"]["cwd"] = str(checkout)
    assert _MOD.classify(event) == ("allow_shell", "non_git_directory")


def test_cd_from_non_git_to_git_worktree_is_guarded(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    event = _event(f"cd {checkout} && rg -n needle .")
    event["cwd"] = str(standalone)
    assert _MOD.classify(event) == ("deny", "source_search_via_bash")


def test_nested_linked_git_worktree_is_guarded(tmp_path):
    checkout = tmp_path / "worktree"
    nested = checkout / "src" / "feature"
    nested.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: /tmp/worktree-example")
    event = _event("rg -n needle .")
    event["tool_input"]["cwd"] = str(nested)
    assert _MOD.classify(event) == ("deny", "source_search_via_bash")


def test_non_git_path_with_unknown_shell_variable_is_fail_open(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    event = _event('cd "$UNKNOWN_DIRECTORY" && rg needle .')
    event["tool_input"]["cwd"] = str(checkout)
    assert _MOD.classify(event) == ("allow_shell", "unknown_worktree")


def test_mixed_batch_with_git_search_is_guarded(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    event = _event(["rg needle .", f"cd {checkout} && grep needle ."], agent=True)
    event["tool_input"]["cwd"] = str(tmp_path)
    assert _MOD.classify(event) == ("deny", "source_search_via_bash")


def test_installed_hook_process_allows_non_git_workspace(tmp_path):
    event = _event("rg -n needle .", agent=True)
    event["tool_input"]["cwd"] = str(tmp_path)
    hook = _HOOK.parent / "pre_tool_discipline.py"
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=True,
        env={**__import__("os").environ, "LEMONCROW_ROOT": str(tmp_path)},
    )
    assert result.stdout == ""
    logged = list((tmp_path / "telemetry/search-routing").glob("*.jsonl"))
    assert len(logged) == 1
    assert json.loads(logged[0].read_text())["reason"] == "non_git_directory"


def test_script_runs_as_hook_process(tmp_path):
    event = _event("cd repo && rg -n foo src | head", agent=True)
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=True,
        env={**__import__("os").environ, "LEMONCROW_ROOT": str(tmp_path)},
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
