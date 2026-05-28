"""Unit tests for the commit embedding helper."""

from __future__ import annotations

from atelier.infra.code_intel.git_history.embedder import (
    decode_embedding,
    embed_summary,
    embedding_dim,
)
from atelier.infra.code_intel.git_history.models import CommitSummary


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


def test_embed_returns_1536_bytes() -> None:
    summary = _make_summary()
    blob = embed_summary(summary)
    # 384 float32 values x 4 bytes each = 1536 bytes
    assert len(blob) == 384 * 4


def test_decode_roundtrip() -> None:
    summary = _make_summary()
    blob = embed_summary(summary)
    decoded = decode_embedding(blob)
    assert len(decoded) == 384
    # Values should be reasonable floats
    assert all(isinstance(v, float) for v in decoded)


def test_embed_includes_files_in_text() -> None:
    # Two summaries with same text but different files should produce different vectors
    s1 = _make_summary(files_touched=["src/auth.py"])
    s2 = _make_summary(files_touched=["src/database.py"])
    blob1 = embed_summary(s1)
    blob2 = embed_summary(s2)
    assert blob1 != blob2


def test_embedding_dim_constant() -> None:
    assert embedding_dim() == 384
