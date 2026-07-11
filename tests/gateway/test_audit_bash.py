"""Per-command bash spend ledger + `lemon audit bash` CLI (rtk-discover equivalent)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.cli.commands.audit import audit_bash_cmd


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    return tmp_path


def test_bash_command_key_normalizes() -> None:
    assert mcp_server._bash_command_key("git status -s") == "git status"
    assert mcp_server._bash_command_key("cd /x && uv run pytest tests/a.py -q") == "uv run pytest"
    assert mcp_server._bash_command_key("FOO=1 python script.py") == "python"
    assert mcp_server._bash_command_key("ls -la | head") == "ls"
    assert mcp_server._bash_command_key("make lint") == "make lint"
    assert mcp_server._bash_command_key("/usr/bin/docker ps -a") == "docker ps"
    assert mcp_server._bash_command_key("") == ""


def test_record_bash_command_stats_accumulates(tmp_path: Path) -> None:
    mcp_server._record_bash_command_stats("git status", shipped_chars=120, omitted_chars=400)
    mcp_server._record_bash_command_stats("git status -s", shipped_chars=80, omitted_chars=100)
    state = json.loads((tmp_path / "smart_state.json").read_text())
    row = state["bash_commands"]["git status"]
    assert row == {"calls": 2, "shipped_chars": 200, "omitted_chars": 500}


def test_record_bash_command_stats_bounded(tmp_path: Path) -> None:
    for i in range(mcp_server._BASH_STATS_MAX_KEYS + 5):
        mcp_server._record_bash_command_stats(f"tool{i}", shipped_chars=i, omitted_chars=0)
    state = json.loads((tmp_path / "smart_state.json").read_text())
    assert len(state["bash_commands"]) <= mcp_server._BASH_STATS_MAX_KEYS


def _write_state(tmp_path: Path) -> None:
    (tmp_path / "smart_state.json").write_text(
        json.dumps(
            {
                "bash_commands": {
                    "pytest": {"calls": 4, "shipped_chars": 40000, "omitted_chars": 20000},
                    "git status": {"calls": 10, "shipped_chars": 2000, "omitted_chars": 8000},
                }
            }
        )
    )


def test_audit_bash_cli_json(tmp_path: Path) -> None:
    _write_state(tmp_path)
    result = CliRunner().invoke(audit_bash_cmd, ["--json"], obj={"root": tmp_path})
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["commands"][0]["command"] == "pytest"
    assert payload["commands"][0]["shipped_tokens"] == 10000
    assert payload["commands"][0]["saved_tokens"] == 5000
    assert payload["commands"][1]["command"] == "git status"


def test_audit_bash_cli_text(tmp_path: Path) -> None:
    _write_state(tmp_path)
    result = CliRunner().invoke(audit_bash_cmd, ["--no-color"], obj={"root": tmp_path})
    assert result.exit_code == 0, result.output
    assert "pytest" in result.output
    assert "git status" in result.output


def test_audit_bash_cli_empty(tmp_path: Path) -> None:
    result = CliRunner().invoke(audit_bash_cmd, ["--no-color"], obj={"root": tmp_path})
    assert result.exit_code == 0, result.output
    assert "No bash command stats" in result.output
