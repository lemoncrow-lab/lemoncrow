"""CLI tests for ``lc eval mini``.

Verifies:
- ``lc eval mini --help`` exits 0
- ``lc eval mini --dry-run --json`` works with no API keys
- The report is written to the default path and to a custom ``--output`` path
- JSON output is valid and validates against MiniEvalReport
- A dry-run never claims success (status is dry_run, never pass)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from pathlib import Path as _Path

import pytest
from click.testing import CliRunner

_REPO_ROOT = _Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.mini.schema import MiniEvalReport  # noqa: E402
from lemoncrow.gateway.cli import cli  # noqa: E402

_API_KEY_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AZURE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
)


@pytest.fixture()
def offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An LemonCrow root with all known API keys removed (offline)."""
    for var in _API_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / ".lemoncrow"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    return root


def test_eval_mini_help_exits_zero() -> None:
    result = CliRunner().invoke(cli, ["benchmark", "mini", "--help"])
    assert result.exit_code == 0, result.output
    assert "mini eval suite" in result.output.lower()
    assert "--dry-run" in result.output


def test_eval_mini_dry_run_json_offline(offline_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["--root", str(offline_env), "benchmark", "mini", "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "dry_run"
    assert data["suite"] == "mini"
    assert data["total_tasks"] >= 1
    assert all(c["status"] == "skipped" for c in data["cases"])


def test_eval_mini_dry_run_json_validates_schema(offline_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["--root", str(offline_env), "benchmark", "mini", "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.output
    report = MiniEvalReport.model_validate_json(result.output)
    assert report.status == "dry_run"
    assert report.total_cost_usd == 0.0
    assert report.routing_regression_rate == 0.0


def test_eval_mini_writes_default_report_path(offline_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["--root", str(offline_env), "benchmark", "mini", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    json_path = offline_env / "evals" / "mini-report.json"
    md_path = offline_env / "evals" / "mini-report.md"
    assert json_path.exists()
    assert md_path.exists()
    assert json_path.stat().st_size > 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["status"] == "dry_run"


def test_eval_mini_custom_output_path(offline_env: Path, tmp_path: Path) -> None:
    out = tmp_path / "custom" / "report.json"
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(offline_env),
            "benchmark",
            "mini",
            "--dry-run",
            "--json",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert out.with_suffix(".md").exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "dry_run"


def test_eval_mini_dry_run_never_claims_success(offline_env: Path) -> None:
    """A dry-run must never report status=pass nor non-zero accepted tasks."""
    result = CliRunner().invoke(
        cli,
        ["--root", str(offline_env), "benchmark", "mini", "--dry-run", "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] != "pass"
    assert data["accepted_tasks"] == 0
    assert data["accepted_patch_rate"] == 0.0


def test_eval_mini_limit_option(offline_env: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["--root", str(offline_env), "benchmark", "mini", "--dry-run", "--json", "--limit", "2"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total_tasks"] == 2
