from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_defaults_bootstrap_cli_writes_manifest_and_reports_statuses(monkeypatch, tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".lemoncrow"
    target = tmp_path / "workspace"

    monkeypatch.chdir(REPO_ROOT)

    first = runner.invoke(
        cli,
        ["--root", str(root), "defaults", "bootstrap", "--target-root", str(target), "--json"],
    )
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert any(entry["status"] == "created" for entry in first_payload["entries"])
    manifest_path = target / "defaults" / "manifest.json"
    assert manifest_path.is_file()

    second = runner.invoke(
        cli,
        ["--root", str(root), "defaults", "bootstrap", "--target-root", str(target), "--json"],
    )
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["target_root"] == str(target.resolve())
    assert {entry["status"] for entry in second_payload["entries"]} == {"skipped"}
