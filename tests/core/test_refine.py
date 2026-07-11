"""Tests for grow-and-refine block merging."""

from __future__ import annotations

from lemoncrow.core.foundation.models import Playbook
from lemoncrow.core.foundation.refine import grow_or_create, merge_blocks


def _block(
    block_id: str,
    *,
    domain: str = "coding",
    procedure: list[str] | None = None,
    dead_ends: list[str] | None = None,
) -> Playbook:
    return Playbook(
        id=block_id,
        title=block_id,
        domain=domain,
        situation="sit",
        procedure=procedure or ["run pytest", "reorder imports"],
        dead_ends=dead_ends or [],
    )


class _FakeEmbedder:
    """Deterministic embedder: vector keyed by substring presence."""

    dim = 3
    name = "fake"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0, 0.0, 0.0]
            for key, value in self._mapping.items():
                if key in text:
                    vec = value
                    break
            out.append(vec)
        return out


def test_token_path_merges_similar_block() -> None:
    existing = _block(
        "b1",
        procedure=["run pytest", "reorder imports", "check mypy"],
        dead_ends=["old dead end"],
    )
    incoming = _block(
        "b2",
        procedure=["run pytest", "reorder imports", "check mypy", "clear cache"],
        dead_ends=["new dead end"],
    )
    result = grow_or_create(incoming, [existing])
    assert result.merged is True
    assert result.target_id == "b1"
    assert result.block.id == "b1"  # target identity preserved
    assert "clear cache" in result.block.procedure  # incoming knowledge folded in
    assert "new dead end" in result.block.dead_ends
    assert "old dead end" in result.block.dead_ends


def test_token_path_keeps_dissimilar_block_new() -> None:
    existing = _block("b1", procedure=["deploy to prod", "rotate credentials"])
    incoming = _block("b2", procedure=["write a haiku", "paint a fence"])
    result = grow_or_create(incoming, [existing])
    assert result.merged is False
    assert result.target_id is None
    assert result.block.id == "b2"


def test_different_domain_never_merges() -> None:
    existing = _block("b1", domain="infra", procedure=["run pytest", "reorder imports"])
    incoming = _block("b2", domain="coding", procedure=["run pytest", "reorder imports"])
    result = grow_or_create(incoming, [existing])
    assert result.merged is False


def test_embedder_path_drives_match() -> None:
    existing = _block("b1", procedure=["alpha unique steps here"])
    incoming = _block("b2", procedure=["beta different words entirely"])
    # Token Jaccard would be low, but embeddings say they are near-identical.
    embedder = _FakeEmbedder({"alpha": [1.0, 0.0, 0.0], "beta": [0.99, 0.01, 0.0]})
    result = grow_or_create(incoming, [existing], embedder=embedder)
    assert result.merged is True
    assert result.target_id == "b1"


def test_embedder_failure_degrades_to_tokens() -> None:
    class _BrokenEmbedder:
        dim = 3
        name = "broken"

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("ollama down")

    existing = _block("b1", procedure=["run pytest", "reorder imports", "check mypy"])
    incoming = _block("b2", procedure=["run pytest", "reorder imports", "check mypy"])
    result = grow_or_create(incoming, [existing], embedder=_BrokenEmbedder())
    assert result.merged is True  # fell back to token similarity


def test_merge_blocks_bumps_updated_at() -> None:
    target = _block("b1")
    incoming = _block("b2", procedure=["new step"])
    merged = merge_blocks(target, incoming)
    assert merged.updated_at >= target.updated_at
    assert "new step" in merged.procedure
