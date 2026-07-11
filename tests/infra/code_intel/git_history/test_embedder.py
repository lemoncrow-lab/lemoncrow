"""Unit tests for the commit embedding helper."""

from __future__ import annotations

import pytest

from lemoncrow.infra.code_intel.git_history import embedder as history_embedder_module
from lemoncrow.infra.code_intel.git_history.embedder import (
    decode_embedding,
    embed_summary,
    embedding_dim,
)
from lemoncrow.infra.code_intel.git_history.models import CommitSummary
from lemoncrow.infra.embeddings.factory import get_code_embedder, make_code_embedder
from lemoncrow.infra.embeddings.ollama_embedder import OllamaEmbedder


def _make_summary(**kwargs) -> CommitSummary:  # type: ignore[no-untyped-def]
    defaults = {
        "sha": "abc123def456",
        "author_date": 1700000000,
        "files_touched": ["src/auth.py"],
        "summary": "Fixed authentication session leak in logout flow.",
        "summary_model": "claude-haiku-4-5",
        "prompt_version": "v1",
    }
    return CommitSummary(**{**defaults, **kwargs})


def _use_fake_code_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic test embedder (the removed 'local' pin's role): turns the
    semantic/ANN path on so the vector store + retrieval is exercised. Test-only."""
    import lemoncrow.infra.embeddings.factory as _factory

    class _Fake:
        dim = 384
        name = "test:hashing"

        def embed(self, texts: list[str]) -> list[list[float]]:
            from lemoncrow.infra.storage.vector import generate_embedding

            return [generate_embedding(t, dim=self.dim) for t in texts]

    fake = _Fake()

    def _fake_make(pin: str | None = None, model: str | None = None) -> object:
        return fake

    _fake_make.cache_clear = lambda: None  # type: ignore[attr-defined]
    # get_code_embedder() looks up factory.make_code_embedder at call time, so
    # patching it here reaches every import site, including direct callers.
    monkeypatch.setattr(_factory, "make_code_embedder", _fake_make)


@pytest.fixture(autouse=True)
def _pin_fake_code_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_fake_code_embedder(monkeypatch)
    monkeypatch.delenv("LEMONCROW_CODE_EMBED_MODEL", raising=False)
    history_embedder_module._embedder = None
    make_code_embedder.cache_clear()


def test_embed_returns_bytes_for_current_code_embedder_dim() -> None:
    summary = _make_summary()
    blob = embed_summary(summary)
    assert len(blob) == embedding_dim() * 4


def test_decode_roundtrip() -> None:
    summary = _make_summary()
    blob = embed_summary(summary)
    decoded = decode_embedding(blob)
    assert len(decoded) == embedding_dim()
    # Values should be reasonable floats
    assert all(isinstance(v, float) for v in decoded)


def test_embed_includes_files_in_text() -> None:
    # Two summaries with same text but different files should produce different vectors
    s1 = _make_summary(files_touched=["src/auth.py"])
    s2 = _make_summary(files_touched=["src/database.py"])
    blob1 = embed_summary(s1)
    blob2 = embed_summary(s2)
    assert blob1 != blob2


def test_embedding_dim_matches_code_embedder() -> None:
    assert embedding_dim() == get_code_embedder().dim


def test_ollama_embedder_uses_env_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LEMONCROW_OLLAMA_EMBED_TIMEOUT_SECONDS", "42")
    embedder = OllamaEmbedder()
    assert embedder._timeout_seconds == 42.0
