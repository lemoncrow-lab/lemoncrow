from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.plugin_runtime import (
    apply_recall_settings,
    plugin_settings_path,
    set_recall_settings,
)


def test_apply_only_changes_provided() -> None:
    base = {"liveReviewer": True, "recallEmbedder": "local"}
    out = apply_recall_settings(base, auto_index=True)
    assert out["recallAutoIndex"] is True
    assert out["liveReviewer"] is True  # preserved
    assert out["recallEmbedder"] == "local"  # unchanged (None arg)


def test_apply_sets_all_fields() -> None:
    out = apply_recall_settings({}, auto_index=False, embedder="ollama", embed_model="nomic")
    assert out == {"recallAutoIndex": False, "recallEmbedder": "ollama", "recallEmbedModel": "nomic"}


def test_set_recall_settings_merges_existing_file(tmp_path: Path) -> None:
    path = plugin_settings_path(tmp_path)
    path.write_text(json.dumps({"liveReviewer": True}), encoding="utf-8")
    updated = set_recall_settings(tmp_path, auto_index=True, embedder="openai")
    assert updated["recallAutoIndex"] is True
    assert updated["recallEmbedder"] == "openai"
    on_disk = json.loads(path.read_text("utf-8"))
    assert on_disk["liveReviewer"] is True  # other settings preserved
    assert on_disk["recallAutoIndex"] is True


def test_set_recall_settings_creates_file_when_absent(tmp_path: Path) -> None:
    updated = set_recall_settings(tmp_path, auto_index=False, embedder="ollama")
    assert plugin_settings_path(tmp_path).exists()
    assert updated["recallEmbedder"] == "ollama"
