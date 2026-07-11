from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.core.foundation.models import Trace, ValidationResult
from lemoncrow.core.foundation.store import ContextStore
from lemoncrow.gateway.cli import cli


def test_report_cli_outputs_json_and_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".lemoncrow"
    # Run outside a git repo so `init` skips the ~30s code-index bootstrap and
    # project-setup writes; this test only needs an initialized store.
    monkeypatch.chdir(tmp_path)
    from tests.helpers import grant_oauth_pro

    grant_oauth_pro(monkeypatch)
    runner = CliRunner()
    init = runner.invoke(cli, ["--root", str(root), "init"])
    assert init.exit_code == 0, init.output
    store = ContextStore(root)
    store.record_trace(
        Trace(
            id="trace-report",
            agent="codex",
            domain="coding",
            task="change code",
            status="success",
            validation_results=[ValidationResult(name="rubric_code_change", passed=True)],
            created_at=datetime.now(UTC),
        ),
        write_json=False,
    )

    json_result = runner.invoke(cli, ["--root", str(root), "report", "--since", "7d", "--format", "json"])
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["rubric_pass_rate"]["total"] == 1

    markdown_result = runner.invoke(cli, ["--root", str(root), "report", "--since", "7d"])
    assert markdown_result.exit_code == 0, markdown_result.output
    assert "LemonCrow Weekly Governance Report" in markdown_result.output
