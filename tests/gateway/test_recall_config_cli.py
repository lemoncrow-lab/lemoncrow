from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lemoncrow.core.capabilities.plugin_runtime import plugin_settings_path
from lemoncrow.gateway.cli import cli


def test_recall_config_writes_settings(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    result = CliRunner().invoke(
        cli,
        [
            "--root",
            str(root),
            "session",
            "recall",
            "config",
            "--auto-index",
            "--embedder",
            "ollama",
            "--embed-model",
            "nomic-embed-text",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(plugin_settings_path(root).read_text("utf-8"))
    assert data["recallAutoIndex"] is True
    assert data["recallEmbedder"] == "ollama"
    assert data["recallEmbedModel"] == "nomic-embed-text"


def test_recall_config_no_auto_index(tmp_path: Path) -> None:
    # 'openai'/'ollama' are the only real recall embedder choices (see
    # _make_recall_embedder): Claude has no embeddings API, and anything else
    # falls back to the null/FTS-only embedder, so the CLI restricts to these.
    root = tmp_path / ".lemoncrow"
    result = CliRunner().invoke(
        cli, ["--root", str(root), "session", "recall", "config", "--no-auto-index", "--embedder", "openai"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(plugin_settings_path(root).read_text("utf-8"))
    assert data["recallAutoIndex"] is False
    assert data["recallEmbedder"] == "openai"
