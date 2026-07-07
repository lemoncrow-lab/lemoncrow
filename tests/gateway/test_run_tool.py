"""Tests for the run (bash exec) MCP tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.tool_supervision.bash_exec import classify_command, run_command


def test_run_simple_command(tmp_path: Path) -> None:
    result = run_command("echo hello", cwd=str(tmp_path))
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.truncated is False


def test_run_exit_code(tmp_path: Path) -> None:
    result = run_command("exit 42", cwd=str(tmp_path))
    assert result.exit_code == 42


def test_run_stderr_captured(tmp_path: Path) -> None:
    result = run_command("echo err >&2", cwd=str(tmp_path))
    assert "err" in result.stderr


def test_run_ansi_stripped(tmp_path: Path) -> None:
    result = run_command("printf '\\033[31mred\\033[0m'", cwd=str(tmp_path))
    assert "\x1b" not in result.stdout
    assert "red" in result.stdout


def test_run_truncation(tmp_path: Path) -> None:
    result = run_command("seq 1 500", cwd=str(tmp_path), max_lines=50)
    assert result.truncated is True
    assert result.lines_omitted > 0
    assert "lines omitted" in result.stdout
    # head and tail present
    assert "1\n" in result.stdout
    assert "500" in result.stdout


def test_run_timeout(tmp_path: Path) -> None:
    result = run_command("sleep 10", cwd=str(tmp_path), timeout=1)
    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()


def test_run_duration_recorded(tmp_path: Path) -> None:
    result = run_command("true", cwd=str(tmp_path))
    assert result.duration_ms >= 0


def test_classify_rewrite_cat() -> None:
    decision = classify_command("cat README.md")
    assert decision.action == "rewrite"
    assert decision.rewrite_target == "read"


def test_classify_rewrite_rg() -> None:
    decision = classify_command("rg -i hello src")
    assert decision.action == "rewrite"
    assert decision.rewrite_target == "grep"
    payload = decision.rewrite_payload
    assert payload["file_path"] == "src"
    assert payload["content_regex"] == "hello"
    assert payload["ignore_case"] is True
    assert payload["output_mode"] in ("content", "file_paths_with_content")


def test_run_blocks_destructive_rm(tmp_path: Path) -> None:
    # A target outside the OS temp dir: rm -rf confined to the temp dir is a
    # deliberate exception (agents cleaning up their own scratch files) --
    # see _rm_confined_to_safe_roots. This path stays hard-blocked.
    result = run_command("rm -rf /nonexistent-root-path-never-run", cwd=str(tmp_path))
    assert result.exit_code == -1
    assert result.policy_action == "block"
    assert "blocked" in result.stderr


def test_run_blocks_shell_interpreter(tmp_path: Path) -> None:
    result = run_command("bash -c 'echo no'", cwd=str(tmp_path))
    assert result.exit_code == -1
    assert result.policy_action == "block"
    assert result.policy_category == "shell-interpreter"


def test_classify_allows_shell_noexec_syntax_check() -> None:
    for cmd in ("bash -n script.sh", "sh -n script.sh", "bash -o noexec script.sh", "bash -nx script.sh"):
        decision = classify_command(cmd)
        assert decision.action != "block", cmd
        assert decision.category != "shell-interpreter", cmd


def test_classify_still_blocks_executing_shell(tmp_path: Path) -> None:
    script = tmp_path / "real.sh"
    script.write_text("echo ok\n")
    for cmd in (
        "bash -c 'echo hi'",
        "bash -lc 'echo hi'",
        "sh -s",
        "sh nonexistent-script.sh",  # missing file: stays blocked
        f"bash -c 'echo hi' {script}",  # -c wins even with a real file argument
    ):
        decision = classify_command(cmd)
        assert decision.action == "block", cmd
        assert decision.category == "shell-interpreter", cmd


def test_classify_allows_existing_script_file(tmp_path: Path) -> None:
    script = tmp_path / "install.sh"
    script.write_text("echo ok\n")
    for cmd in (f"bash {script}", f"sh {script} --flag arg", f"bash -x {script}", f"bash -- {script}"):
        decision = classify_command(cmd)
        assert decision.action != "block", cmd


def test_run_via_mcp_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    from atelier.gateway.adapters.mcp_server import _handle

    resp = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run", "arguments": {"command": "echo mcp_ok"}},
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    assert "mcp_ok" in text
    assert "exit_code=" not in text


def test_run_via_mcp_rewrites_cat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    from atelier.gateway.adapters.mcp_server import _handle

    f = tmp_path / "sample.txt"
    f.write_text("rewritten\n", encoding="utf-8")
    resp = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "cat sample.txt"}},
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    assert "rewritten" in text
    assert "exit_code=" not in text


def test_run_via_mcp_rewrites_rg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    from atelier.gateway.adapters.mcp_server import _handle

    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "a.py").write_text("def needle():\n    return 1\n", encoding="utf-8")
    resp = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "rg needle src"}},
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert isinstance(text, str)
    assert "needle" in text
    assert "exit_code=" not in text
