from __future__ import annotations

import json
from pathlib import Path

import pytest

import atelier.infra.embeddings.factory as fac
from atelier.core.capabilities import session_recall


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict = {"make_embedder": [], "make_code_embedder": []}

    def fake_make_embedder(pin: str | None = None) -> str:
        calls["make_embedder"].append(pin)
        return f"emb:{pin}"

    def fake_make_code_embedder(pin: str | None = None, model: str | None = None) -> str:
        calls["make_code_embedder"].append((pin, model))
        return f"code:{pin}:{model}"

    monkeypatch.setattr(fac, "make_embedder", fake_make_embedder)
    monkeypatch.setattr(fac, "make_code_embedder", fake_make_code_embedder)
    for var in ("ATELIER_RECALL_EMBEDDER", "ATELIER_RECALL_EMBED_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return calls


def test_env_ollama_uses_code_embedder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    monkeypatch.setenv("ATELIER_RECALL_EMBEDDER", "ollama")
    monkeypatch.setenv("ATELIER_RECALL_EMBED_MODEL", "nomic-embed-text")
    result = session_recall._make_recall_embedder(tmp_path)
    assert result == "code:ollama:nomic-embed-text"
    assert captured["make_code_embedder"] == [("ollama", "nomic-embed-text")]


def test_env_codex_maps_to_openai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    monkeypatch.setenv("ATELIER_RECALL_EMBEDDER", "codex")
    session_recall._make_recall_embedder(tmp_path)
    assert captured["make_embedder"] == ["openai"]


def test_claude_falls_back_to_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    # Claude has no embeddings API; must NOT be passed as a pin (would raise).
    monkeypatch.setenv("ATELIER_RECALL_EMBEDDER", "claude")
    session_recall._make_recall_embedder(tmp_path)
    assert captured["make_embedder"] == [None]
    assert captured["make_code_embedder"] == []


def test_settings_used_when_no_env(tmp_path: Path, captured: dict) -> None:
    (tmp_path / "plugin_settings.json").write_text(
        json.dumps({"recallEmbedder": "ollama", "recallEmbedModel": "mxbai"}), encoding="utf-8"
    )
    session_recall._make_recall_embedder(tmp_path)
    assert captured["make_code_embedder"] == [("ollama", "mxbai")]


def test_env_overrides_settings_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    (tmp_path / "plugin_settings.json").write_text(json.dumps({"recallEmbedder": "local"}), encoding="utf-8")
    monkeypatch.setenv("ATELIER_RECALL_EMBEDDER", "openai")
    session_recall._make_recall_embedder(tmp_path)
    assert captured["make_embedder"] == ["openai"]


def test_no_env_no_settings_defaults(tmp_path: Path, captured: dict) -> None:
    session_recall._make_recall_embedder(tmp_path)
    assert captured["make_embedder"] == [None]
