"""Tests for the benchmark CLI subcommand workflow."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from atelier.gateway.cli import cli


def test_benchmark_run_action_writes_runtime_report(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path / ".atelier"),
            "benchmark",
            "run",
            "--prompt",
            "Fix Shopify publish",
            "--rounds",
            "2",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tasks"]
    report_path = tmp_path / ".atelier" / "benchmarks" / "runtime" / "latest.json"
    assert report_path.exists()


def test_benchmark_compare_and_export_actions(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"
    run_one = runner.invoke(
        cli,
        ["--root", str(root), "benchmark", "run", "--prompt", "Fix Shopify publish", "--json"],
    )
    assert run_one.exit_code == 0, run_one.output

    latest = root / "benchmarks" / "runtime" / "latest.json"
    other = root / "benchmarks" / "runtime" / "other.json"
    other.write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")

    compare = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "compare",
            "--input",
            str(latest),
            "--input",
            str(other),
        ],
    )
    assert compare.exit_code == 0, compare.output
    assert len(json.loads(compare.output)["reports"]) == 2

    export_path = root / "benchmarks" / "runtime" / "report.csv"
    export = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "export",
            "--input",
            str(latest),
            "--output",
            str(export_path),
            "--format",
            "csv",
        ],
    )
    assert export.exit_code == 0, export.output
    assert export_path.exists()


def test_benchmark_savings_action_runs_paired_commands(tmp_path: Path) -> None:
    script = tmp_path / "bench_agent.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "mode = os.environ['ATELIER_BENCH_MODE']",
                "if mode == 'baseline':",
                "    print(json.dumps({'input_tokens': 100, 'output_tokens': 50, 'cost_usd': 0.002, 'success': True}))",
                "else:",
                "    print(json.dumps({'input_tokens': 60, 'output_tokens': 30, 'cost_usd': 0.001, 'success': True}))",
            ]
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    root = tmp_path / ".atelier"
    result = runner.invoke(
        cli,
        [
            "--root",
            str(root),
            "benchmark",
            "savings",
            "--prompt",
            "Fix a failing benchmark",
            "--baseline-command",
            f"{sys.executable} {script}",
            "--atelier-command",
            f"{sys.executable} {script}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tokens_saved"] == 60
    assert payload["reduction_pct"] == 40.0
    assert payload["cost_saved_usd"] == 0.001
    assert (root / "benchmarks" / "savings" / "latest.json").exists()


def test_benchmark_core_command_runs(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path / ".atelier"),
            "benchmark",
            "core",
            "--prompt",
            "Validate publish workflow",
            "--rounds",
            "2",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["suite"] == "core"
    assert payload["report"]["tasks"]


def test_benchmark_packs_command_runs(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--root",
            str(tmp_path / ".atelier"),
            "benchmark",
            "packs",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["suite"] == "domains"
    assert payload["domains_total"] >= payload["domains_benchmarked"]


def test_benchmark_legacy_top_level_commands_are_removed(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"

    assert runner.invoke(cli, ["--root", str(root), "benchmark-core", "--json"]).exit_code != 0
    assert runner.invoke(cli, ["--root", str(root), "benchmark", "--prompt", "Fix PDP", "--json"]).exit_code != 0


def test_help_command_shows_root_and_nested_command_help(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / ".atelier"

    root_help = runner.invoke(cli, ["--root", str(root), "help"])
    assert root_help.exit_code == 0, root_help.output
    assert "Commands:" in root_help.output
    assert "benchmark" in root_help.output

    command_help = runner.invoke(cli, ["--root", str(root), "help", "benchmark", "run"])
    assert command_help.exit_code == 0, command_help.output
    assert "Usage: cli benchmark run" in command_help.output
    assert "--prompt" in command_help.output
