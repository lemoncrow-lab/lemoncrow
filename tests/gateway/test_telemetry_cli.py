from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from atelier.gateway.cli import cli


def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)


def test_telemetry_status_writes_local_event_and_show_outputs_send_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATELIER_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("ATELIER_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("ATELIER_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.setenv("ATELIER_TELEMETRY", "0")

    root = tmp_path / "a"
    _invoke(root, "init")
    status = _invoke(root, "telemetry", "status", "--json")
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload["remote_enabled"] is False
    assert payload["local_db_path"].endswith("telemetry.db")

    shown = _invoke(root, "telemetry", "show")
    assert shown.exit_code == 0, shown.output
    events = json.loads(shown.output)
    assert events[0]["event"] == "cli_command_invoked"
    assert set(events[0]["props"]) == {"command_name", "session_id", "anon_id"}


def test_telemetry_toggles_and_reset_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_TELEMETRY_DB", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("ATELIER_TELEMETRY_CONFIG", str(tmp_path / "telemetry.toml"))
    monkeypatch.setenv("ATELIER_TELEMETRY_ID_PATH", str(tmp_path / "telemetry_id"))
    monkeypatch.delenv("ATELIER_TELEMETRY", raising=False)

    root = tmp_path / "a"
    _invoke(root, "init")
    off = _invoke(root, "telemetry", "off")
    assert off.exit_code == 0, off.output
    assert "off" in off.output
    lexical = _invoke(root, "telemetry", "lexical", "off")
    assert lexical.exit_code == 0, lexical.output
    reset = _invoke(root, "telemetry", "reset-id")
    assert reset.exit_code == 0, reset.output
    assert len(reset.output.strip()) == 36
