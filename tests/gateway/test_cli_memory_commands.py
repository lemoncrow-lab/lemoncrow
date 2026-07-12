from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.cli import cli


def _reset_remote_mode() -> None:
    mcp_server._remote_client = None


def test_cli_memory_upsert_and_get_round_trip(tmp_path: Path) -> None:
    _reset_remote_mode()
    os.environ.pop("LEMONCROW_SERVICE_URL", None)
    root = tmp_path / ".lemoncrow"
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
                    "agent_id": "lemoncrow:code",
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
                    "agent_id": "lemoncrow:code",
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


def test_cli_memory_remember_and_vote_round_trip(tmp_path: Path) -> None:
    _reset_remote_mode()
    os.environ.pop("LEMONCROW_SERVICE_URL", None)
    root = tmp_path / ".lemoncrow"
    runner = CliRunner()

    remembered = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "memory",
            "remember",
            "Prefer host-neutral memory operations.",
            "--subject",
            "workflow preference",
            "--scope",
            "user",
            "--agent-id",
            "lemoncrow:code",
            "--citations",
            'User input: "host-neutral"',
            "--reason",
            "Keeps CLI, MCP, SDK, and API aligned.",
            "--json",
        ],
    )
    assert remembered.exit_code == 0, remembered.output
    payload = json.loads(remembered.output)
    assert payload["fact"] == "Prefer host-neutral memory operations."
    assert payload["scope"] == "user"

    voted = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "memory",
            "vote",
            "Prefer host-neutral memory operations.",
            "upvote",
            "--reason",
            "Verified by CLI round trip.",
            "--scope",
            "user",
            "--agent-id",
            "lemoncrow:code",
            "--json",
        ],
    )
    assert voted.exit_code == 0, voted.output
    vote_payload = json.loads(voted.output)
    assert vote_payload["direction"] == "upvote"
    assert vote_payload["fact"] == "Prefer host-neutral memory operations."


def test_cli_memory_upsert_reads_value_from_file(tmp_path: Path) -> None:
    _reset_remote_mode()
    os.environ.pop("LEMONCROW_SERVICE_URL", None)
    root = tmp_path / ".lemoncrow"
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
                    "agent_id": "lemoncrow:code",
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
    os.environ.pop("LEMONCROW_SERVICE_URL", None)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path / ".lemoncrow"),
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
