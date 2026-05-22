"""Tests for the run (bash exec) MCP tool."""

from __future__ import annotations

import json
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
    assert decision.rewrite_payload == {
        "file_path": "src",
        "content_regex": "hello",
        "file_glob_patterns": ["**/*"],
        "ignore_case": True,
        "output_mode": "file_paths_with_content",
    }


def test_run_blocks_destructive_rm(tmp_path: Path) -> None:
    result = run_command("rm -rf /tmp/never-run", cwd=str(tmp_path))
    assert result.exit_code == -1
    assert result.policy_action == "block"
    assert "blocked" in result.stderr


def test_run_blocks_shell_interpreter(tmp_path: Path) -> None:
    result = run_command("bash -c 'echo no'", cwd=str(tmp_path))
    assert result.exit_code == -1
    assert result.policy_action == "block"
    assert result.policy_category == "shell-interpreter"


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
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["exit_code"] == 0
    assert "mcp_ok" in payload["stdout"]


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
            "params": {"name": "shell", "arguments": {"command": "cat sample.txt"}},
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["exit_code"] == 0
    assert payload["rewritten"] is True
    assert payload["rewrite_target"] == "read"
    assert "rewritten" in payload["stdout"]


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
            "params": {"name": "shell", "arguments": {"command": "rg needle src"}},
        }
    )
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["exit_code"] == 0
    assert payload["rewritten"] is True
    assert payload["rewrite_target"] == "grep"
    assert "needle" in payload["stdout"]
