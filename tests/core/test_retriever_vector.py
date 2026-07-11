"""Tests for vector-aware retriever scoring (P3).

These tests verify scoring behaviour without connecting to any database
and without requiring pgvector or numpy to be installed.

Covers:
  - Default weights (no vector) sum to 1.0
  - Vector weights sum to 1.0
  - score_block with vector disabled includes no 'vector' key
  - score_block with vector enabled includes 'vector' key
  - retrieve() passes through vector_scores correctly
  - is_vector_enabled() reflects env var
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lemoncrow.core.foundation.models import Playbook
from lemoncrow.core.foundation.retriever import (
    WEIGHTS,
    WEIGHTS_WITH_VECTOR,
    TaskContext,
    retrieve,
    score_block,
)
from lemoncrow.infra.storage.vector import (
    cosine_similarity,
    generate_embedding,
    get_cached_embedding,
    get_embedding_dim,
    is_vector_enabled,
    put_cached_embedding,
    vector_cache_key,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _make_block(
    *,
    domain: str = "test",
    title: str = "Test Block",
    situation: str = "When doing test things",
    triggers: list[str] | None = None,
    failure_signals: list[str] | None = None,
    success_count: int = 0,
    failure_count: int = 0,
) -> Playbook:
    return Playbook(
        id=f"test-{title.lower().replace(' ', '-')}",
        title=title,
        domain=domain,
        situation=situation,
        triggers=triggers or [],
        failure_signals=failure_signals or [],
        procedure=["do the thing"],
        success_count=success_count,
        failure_count=failure_count,
    )


def _make_ctx(
    task: str = "test task",
    domain: str = "test",
    errors: list[str] | None = None,
) -> TaskContext:
    return TaskContext(task=task, domain=domain, errors=errors or [])


# --------------------------------------------------------------------------- #
# Weight sanity                                                               #
# --------------------------------------------------------------------------- #


def test_weights_sum_to_one() -> None:
    """Default WEIGHTS must sum to exactly 1.0."""
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"WEIGHTS sum = {total}"


def test_vector_weights_sum_to_one() -> None:
    """WEIGHTS_WITH_VECTOR must sum to exactly 1.0."""
    total = sum(WEIGHTS_WITH_VECTOR.values())
    assert abs(total - 1.0) < 1e-9, f"WEIGHTS_WITH_VECTOR sum = {total}"


def test_history_weight_redistributed() -> None:
    """history weight must be smaller in WEIGHTS_WITH_VECTOR than WEIGHTS."""
    assert WEIGHTS_WITH_VECTOR["history"] < WEIGHTS["history"]
    assert "vector" in WEIGHTS_WITH_VECTOR
    assert "vector" not in WEIGHTS


# --------------------------------------------------------------------------- #
# score_block — vector disabled path                                         #
# --------------------------------------------------------------------------- #


def test_score_block_no_vector_no_vector_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With vector disabled, breakdown must not contain 'vector' key."""
    monkeypatch.delenv("LEMONCROW_VECTOR_SEARCH_ENABLED", raising=False)
    block = _make_block()
    ctx = _make_ctx()
    scored = score_block(block, ctx, use_vector_weights=False)
    assert "vector" not in scored.breakdown


def test_score_block_no_vector_uses_default_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    """history weight in breakdown must equal WEIGHTS['history'] * history_score."""
    monkeypatch.delenv("LEMONCROW_VECTOR_SEARCH_ENABLED", raising=False)
    # Block with known history: 1 success, 0 failures → history_score = 1.0
    block = _make_block(success_count=1, failure_count=0)
    ctx = _make_ctx()
    scored = score_block(block, ctx, use_vector_weights=False)
    expected_history = WEIGHTS["history"] * 1.0  # 0.10
    assert abs(scored.breakdown["history"] - expected_history) < 1e-9


# --------------------------------------------------------------------------- #
# score_block — vector enabled path                                          #
# --------------------------------------------------------------------------- #


def test_score_block_with_vector_has_vector_key() -> None:
    """With use_vector_weights=True, breakdown must include 'vector' key."""
    block = _make_block()
    ctx = _make_ctx()
    scored = score_block(block, ctx, vector_score=0.8, use_vector_weights=True)
    assert "vector" in scored.breakdown


def test_score_block_vector_score_clamped() -> None:
    """vector_score is clamped to [0, 1] before weighting."""
    block = _make_block()
    ctx = _make_ctx()
    # Provide out-of-range value
    scored_high = score_block(block, ctx, vector_score=2.0, use_vector_weights=True)
    scored_normal = score_block(block, ctx, vector_score=1.0, use_vector_weights=True)
    # Both should produce the same result (clamped to 1.0)
    assert abs(scored_high.breakdown["vector"] - scored_normal.breakdown["vector"]) < 1e-9


def test_score_block_null_vector_score_zero() -> None:
    """When vector_score=None with vector enabled, vector component = 0."""
    block = _make_block()
    ctx = _make_ctx()
    scored = score_block(block, ctx, vector_score=None, use_vector_weights=True)
    assert scored.breakdown["vector"] == 0.0


def test_score_block_history_weight_reduced_with_vector() -> None:
    """history weight must be halved when vector is enabled."""
    block = _make_block(success_count=1)
    ctx = _make_ctx()
    scored_no_vec = score_block(block, ctx, use_vector_weights=False)
    scored_vec = score_block(block, ctx, vector_score=0.0, use_vector_weights=True)
    # history component in vec mode should be 0.05 * score, not 0.10 * score
    assert scored_vec.breakdown["history"] < scored_no_vec.breakdown["history"]


# --------------------------------------------------------------------------- #
# retrieve() with vector_scores parameter                                    #
# --------------------------------------------------------------------------- #


def test_retrieve_accepts_vector_scores(tmp_path: Path) -> None:
    """retrieve() must accept and use vector_scores without error."""
    from lemoncrow.infra.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(root=tmp_path)
    store.init()
    block = _make_block(domain="shop", triggers=["checkout", "cart"])
    store.upsert_block(block)

    ctx = TaskContext(task="checkout flow", domain="shop")
    vector_scores = {block.id: 0.9}
    results = retrieve(
        store,
        ctx,
        vector_scores=vector_scores,
        use_vector_weights=True,
    )
    # Must return without error; vector component included in scores
    ids = {r.block.id for r in results}
    assert block.id in ids
    for r in results:
        if r.block.id == block.id:
            assert "vector" in r.breakdown


def test_retrieve_vector_scores_empty_dict(tmp_path: Path) -> None:
    """retrieve() with empty vector_scores must still work (vector = 0.0)."""
    from lemoncrow.infra.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(root=tmp_path)
    store.init()
    block = _make_block(domain="api", triggers=["api", "endpoint"])
    store.upsert_block(block)

    ctx = TaskContext(task="api endpoint", domain="api")
    results = retrieve(store, ctx, vector_scores={}, use_vector_weights=True)
    for r in results:
        assert r.breakdown.get("vector", 0.0) == 0.0


# --------------------------------------------------------------------------- #
# is_vector_enabled()                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("TRUE", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_is_vector_enabled_env(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    """is_vector_enabled() must reflect LEMONCROW_VECTOR_SEARCH_ENABLED."""
    monkeypatch.setenv("LEMONCROW_VECTOR_SEARCH_ENABLED", value)
    assert is_vector_enabled() == expected


def test_is_vector_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_vector_enabled() returns False when env var is not set."""
    monkeypatch.delenv("LEMONCROW_VECTOR_SEARCH_ENABLED", raising=False)
    assert is_vector_enabled() is False


# --------------------------------------------------------------------------- #
# generate_embedding and cosine_similarity                                  #
# --------------------------------------------------------------------------- #


def test_generate_embedding_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local embeddings must return the same vector for the same input."""
    monkeypatch.setenv("LEMONCROW_EMBEDDING_PROVIDER", "local")
    v1 = generate_embedding("hello world", dim=64)
    v2 = generate_embedding("hello world", dim=64)
    assert v1 == v2


def test_generate_embedding_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local embedding dimension must match requested dim."""
    monkeypatch.setenv("LEMONCROW_EMBEDDING_PROVIDER", "local")
    v = generate_embedding("test", dim=64)
    assert len(v) == 64


def test_playbook_embedding_cache_round_trip(tmp_path: Path) -> None:
    """Playbook embedding cache should round-trip vectors by cache key and embedder."""
    cache_key = vector_cache_key("rb-cache", "rendered content")
    assert get_cached_embedding(tmp_path, cache_key=cache_key, embedder_name="local:test") is None

    put_cached_embedding(
        tmp_path,
        cache_key=cache_key,
        embedder_name="local:test",
        vector=[0.25, 0.75],
    )

    assert get_cached_embedding(tmp_path, cache_key=cache_key, embedder_name="local:test") == [
        0.25,
        0.75,
    ]


def test_generate_embedding_different_inputs_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different inputs must produce different local embeddings."""
    monkeypatch.setenv("LEMONCROW_EMBEDDING_PROVIDER", "local")
    v1 = generate_embedding("hello", dim=64)
    v2 = generate_embedding("world", dim=64)
    assert v1 != v2


def test_cosine_similarity_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """cosine_similarity of a vector with itself must be ~1.0."""
    monkeypatch.setenv("LEMONCROW_EMBEDDING_PROVIDER", "local")
    v = generate_embedding("test text", dim=64)
    sim = cosine_similarity(v, v)
    assert abs(sim - 1.0) < 1e-6


def test_cosine_similarity_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """cosine_similarity must be in [-1, 1]."""
    monkeypatch.setenv("LEMONCROW_EMBEDDING_PROVIDER", "local")
    v1 = generate_embedding("alpha", dim=64)
    v2 = generate_embedding("beta", dim=64)
    sim = cosine_similarity(v1, v2)
    assert -1.0 <= sim <= 1.0


def test_cosine_similarity_mismatch_raises() -> None:
    """cosine_similarity must raise ValueError for dimension mismatch."""
    with pytest.raises(ValueError, match="dimension"):
        cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])


def test_get_embedding_dim_default() -> None:
    """get_embedding_dim returns 1536 when env var is not set."""
    orig = os.environ.pop("LEMONCROW_EMBEDDING_DIM", None)
    try:
        assert get_embedding_dim() == 1536
    finally:
        if orig is not None:
            os.environ["LEMONCROW_EMBEDDING_DIM"] = orig
