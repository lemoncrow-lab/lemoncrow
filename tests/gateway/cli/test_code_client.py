from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.app import _argv_for_program


def test_permanent_cli_entrypoints_map_to_canonical_cli() -> None:
    project_root = Path(__file__).parents[3]
    with (project_root / "pyproject.toml").open("rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]

    assert scripts["lemoncrow"] == "lemoncrow.gateway.cli:main"
    assert scripts["lc"] == scripts["lemoncrow"]
    assert scripts["lemoncode"] == scripts["lemoncrow"]


def test_lemoncode_program_name_maps_to_code_group() -> None:
    assert _argv_for_program("lemoncode", ["-p", "hello"]) == ["code", "-p", "hello"]
    assert _argv_for_program("lc", ["code"]) == ["code"]


def test_code_without_subcommand_starts_managed_engine(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run_coding_engine(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        "lemoncrow.gateway.cli.coding_engine.run_coding_engine",
        fake_run_coding_engine,
    )
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(tmp_path / "store"),
            "code",
            "--project-root",
            str(tmp_path),
            "--no-mcp",
            "--optimization-mode",
            "enforce",
            "--local-retrieval",
            "force",
            "--local-retrieval-model",
            "ollama/qwen2.5-coder:7b",
            "-p",
            "hello",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["prompt"] == "hello"
    assert captured["engine"] == "auto"
    assert captured["mcp_enabled"] is False
    assert captured["optimization_mode"] == "enforce"
    assert captured["local_retrieval"] == "force"
    assert captured["local_retrieval_model"] == "ollama/qwen2.5-coder:7b"
    assert captured["store_root"] == tmp_path / "store"


def test_existing_code_subcommands_remain_available() -> None:
    result = CliRunner().invoke(cli, ["code", "index", "--help"])
    assert result.exit_code == 0
    assert "--repo-root" in result.output


def test_provider_without_model_fails_before_starting_runtime(tmp_path: Path) -> None:
    from lemoncrow.gateway.cli.interactive import _run_code_async

    with pytest.raises(click.ClickException, match="--provider requires --model"):
        asyncio.run(
            _run_code_async(
                store_root=tmp_path / "store",
                project_root=tmp_path,
                provider="openai",
                model="",
                budget="balanced",
                cache_policy="auto",
                yolo=False,
                max_cost=None,
                prompt="hello",
                resume=None,
                mcp_enabled=False,
                mcp_schema_mode="auto",
            )
        )
