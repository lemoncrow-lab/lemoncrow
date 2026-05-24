from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.cli import cli


def _reset_remote_mode() -> None:
    mcp_server._remote_client = None


def test_cli_memory_upsert_and_get_round_trip(tmp_path: Path) -> None:
    _reset_remote_mode()
    os.environ.pop("ATELIER_SERVICE_URL", None)
    root = tmp_path / ".atelier"
    runner = CliRunner()

    upsert = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "tools",
            "call",
            "memory",
            "--dev",
            "--args",
            json.dumps(
                {
                    "op": "store_fact",
                    "agent_id": "atelier:code",
                    "subject": "scratch",
                    "fact": "hello",
                    "citations": 'User input: "hello"',
                    "reason": "Round-trip fixture",
                    "scope": "user",
                }
            ),
            "--json",
        ],
    )
    assert upsert.exit_code == 0, upsert.output
    payload = json.loads(upsert.output)
    assert payload["id"]
    assert payload["fact"] == "hello"

    upsert_again = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "tools",
            "call",
            "memory",
            "--dev",
            "--args",
            json.dumps(
                {
                    "op": "store_fact",
                    "agent_id": "atelier:code",
                    "subject": "scratch",
                    "fact": "hello",
                    "citations": 'User input: "hello"',
                    "reason": "Round-trip fixture",
                    "scope": "user",
                }
            ),
            "--json",
        ],
    )
    assert upsert_again.exit_code == 0, upsert_again.output
    repeated = json.loads(upsert_again.output)
    assert repeated["id"] == payload["id"]


def test_cli_memory_upsert_reads_value_from_file(tmp_path: Path) -> None:
    _reset_remote_mode()
    os.environ.pop("ATELIER_SERVICE_URL", None)
    root = tmp_path / ".atelier"
    value_file = tmp_path / "value.md"
    value_file.write_text("file-backed memory", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "tools",
            "call",
            "memory",
            "--dev",
            "--args",
            json.dumps(
                {
                    "op": "store_fact",
                    "agent_id": "atelier:code",
                    "subject": "from-file",
                    "fact": value_file.read_text(encoding="utf-8"),
                    "citations": 'User input: "file-backed memory"',
                    "reason": "File-backed fixture",
                    "scope": "user",
                }
            ),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["fact"] == "file-backed memory"


def test_cli_memory_upsert_redacts_secret_payload(tmp_path: Path) -> None:
    _reset_remote_mode()
    os.environ.pop("ATELIER_SERVICE_URL", None)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path / ".atelier"),
            "tools",
            "call",
            "memory",
            "--dev",
            "--args",
            json.dumps(
                {
                    "op": "store_fact",
                    "agent_id": "t",
                    "subject": "leak",
                    "fact": "AKIAIOSFODNN7EXAMPLE secretvalue",
                    "citations": 'User input: "secret"',
                    "reason": "Redaction fixture",
                    "scope": "user",
                }
            ),
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert "fact rejected: likely secret leakage" in result.output
