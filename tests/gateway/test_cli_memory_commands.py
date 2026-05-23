from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from atelier.gateway.adapters.cli import cli


def test_cli_memory_upsert_and_get_round_trip(tmp_path: Path) -> None:
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
                    "op": "block_upsert",
                    "agent_id": "atelier:code",
                    "label": "scratch",
                    "value": "hello",
                }
            ),
            "--json",
        ],
    )
    assert upsert.exit_code == 0, upsert.output
    payload = json.loads(upsert.output)
    assert payload["version"] >= 1

    get = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "tools",
            "call",
            "memory",
            "--dev",
            "--args",
            json.dumps({"op": "block_get", "agent_id": "atelier:code", "label": "scratch"}),
            "--json",
        ],
    )
    assert get.exit_code == 0, get.output
    block = json.loads(get.output)
    assert block["id"] == payload["id"]
    assert block["value"] == "hello"


def test_cli_memory_upsert_reads_value_from_file(tmp_path: Path) -> None:
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
                    "op": "block_upsert",
                    "agent_id": "atelier:code",
                    "label": "from-file",
                    "value": value_file.read_text(encoding="utf-8"),
                }
            ),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    get = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "tools",
            "call",
            "memory",
            "--dev",
            "--args",
            json.dumps({"op": "block_get", "agent_id": "atelier:code", "label": "from-file"}),
            "--json",
        ],
    )
    assert json.loads(get.output)["value"] == "file-backed memory"


def test_cli_memory_upsert_redacts_secret_payload(tmp_path: Path) -> None:
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
                    "op": "block_upsert",
                    "agent_id": "t",
                    "label": "leak",
                    "value": "AKIAIOSFODNN7EXAMPLE secretvalue",
                }
            ),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"]
