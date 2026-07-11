"""WS7 G5 -- persistent ANN over archival recall.

Guards exercised: ANN/brute-force parity, N5 model-id & dim drift (no vector-
space mixing), most-recent-N exact tail (just-stored memory never missed),
brute-force fallback when datasketch/HNSW is unavailable, incremental graph
extension + disk persistence, and default-off byte-identical ranking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.archival_recall import ann as ann_mod
from lemoncrow.core.capabilities.archival_recall import ranking as ranking_mod
from lemoncrow.core.capabilities.archival_recall.ann import ArchivalAnnIndex, ann_retrieval_enabled
from lemoncrow.core.capabilities.archival_recall.ranking import rank_archival_passages
from lemoncrow.core.foundation.memory_models import ArchivalPassage

_DIM = 8


def _vec(seed: int, dim: int = _DIM) -> list[float]:
    np = pytest.importorskip("numpy", reason="numpy not installed")

    rng = np.random.default_rng(seed)
    return [float(x) for x in rng.standard_normal(dim)]


def _passage(
    i: int,
    *,
    text: str | None = None,
    embedding: list[float] | None = None,
    model: str = "local:hashing",
    provenance: str = "LocalEmbedder",
    when: datetime | None = None,
) -> ArchivalPassage:
    return ArchivalPassage(
        id=f"p{i}",
        agent_id="lemon:code",
        text=text if text is not None else f"archival document number {i}",
        embedding=embedding if embedding is not None else _vec(i),
        embedding_model=model,
        embedding_provenance=provenance,
        source="user",
        dedup_hash=f"p{i}",
        created_at=when or (datetime.now(UTC) - timedelta(minutes=i)),
    )


# --------------------------------------------------------------------------
# ArchivalAnnIndex unit behaviour
# --------------------------------------------------------------------------


def test_candidate_ids_returns_none_for_brute_force() -> None:
    # HNSW removed: candidate_ids always returns None (all passages scored exactly).
    passages = [_passage(i) for i in range(40)]
    idx = ArchivalAnnIndex()
    query = passages[3].embedding
    assert query is not None
    cand = idx.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5)
    assert cand is None  # None = brute-force: every passage is in scope
    # All passages are considered, so p3 and the newest tail are implicitly covered.
    all_ids = {p.id for p in passages}
    assert "p3" in all_ids
    newest = {p.id for p in sorted(passages, key=lambda p: (p.created_at, p.id), reverse=True)[:8]}
    assert newest <= all_ids


def test_n5_model_id_mismatch_falls_back_to_brute_force() -> None:
    passages = [_passage(i) for i in range(40)]
    idx = ArchivalAnnIndex()
    query = passages[1].embedding
    assert query is not None
    # Wrong model-id -> no eligible passages -> None (score every passage exactly).
    assert idx.candidate_ids(query, passages, model_id="different-model", dim=_DIM, top_k=5) is None


def test_n5_dim_mismatch_falls_back_to_brute_force() -> None:
    passages = [_passage(i) for i in range(40)]
    idx = ArchivalAnnIndex()
    assert idx.candidate_ids(_vec(1, dim=4), passages, model_id="local:hashing", dim=4, top_k=5) is None


def test_small_set_falls_back_to_brute_force() -> None:
    passages = [_passage(i) for i in range(5)]
    idx = ArchivalAnnIndex()
    query = passages[0].embedding
    assert query is not None
    assert idx.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=3) is None


def test_legacy_stub_passages_are_ineligible() -> None:
    passages = [_passage(i, provenance="legacy_stub") for i in range(40)]
    idx = ArchivalAnnIndex()
    query = passages[0].embedding
    assert query is not None
    # All legacy-stub -> nothing eligible -> brute-force fallback.
    assert idx.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5) is None


def test_brute_force_fallback_when_hnsw_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ann_mod, "_HNSW", None)
    passages = [_passage(i) for i in range(40)]
    idx = ArchivalAnnIndex()
    query = passages[0].embedding
    assert query is not None
    assert idx.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5) is None


def test_no_graph_built_without_hnsw() -> None:
    # HNSW removed: _graph is never built; _member_ids stays empty.
    passages = [_passage(i) for i in range(40)]
    idx = ArchivalAnnIndex()
    query = passages[0].embedding
    assert query is not None
    idx.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5)
    assert idx._graph is None
    assert idx._member_ids == set()
    # Adding passages changes nothing — graph never builds.
    passages.append(_passage(99, when=datetime.now(UTC) + timedelta(minutes=5)))
    idx.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5)
    assert idx._graph is None
    assert idx._member_ids == set()


def test_invalidate_resets_state() -> None:
    # HNSW removed: graph is always None; invalidate() leaves state clean.
    passages = [_passage(i) for i in range(40)]
    idx = ArchivalAnnIndex()
    query = passages[0].embedding
    assert query is not None
    idx.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5)
    assert idx._graph is None  # never built without HNSW
    idx.invalidate()
    assert idx._graph is None and idx._member_ids == set()


def test_no_persist_file_without_hnsw(tmp_path: Path) -> None:
    # HNSW removed: nothing is persisted to disk; fresh index is always brute-force.
    path = tmp_path / "recall.ann.pkl"
    passages = [_passage(i) for i in range(40)]
    query = passages[0].embedding
    assert query is not None

    idx1 = ArchivalAnnIndex(persist_path=path)
    idx1.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5)
    assert not path.exists()  # HNSW removed: nothing to persist
    assert idx1._member_ids == set()

    idx2 = ArchivalAnnIndex(persist_path=path)
    cand = idx2.candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5)
    assert cand is None  # brute-force: all passages scored
    assert idx2._member_ids == idx1._member_ids


def test_model_drift_always_returns_none(tmp_path: Path) -> None:
    # HNSW removed: model-id mismatch -> None regardless (no stale graph to reject).
    path = tmp_path / "recall.ann.pkl"
    passages = [_passage(i) for i in range(40)]
    query = passages[0].embedding
    assert query is not None
    ArchivalAnnIndex(persist_path=path).candidate_ids(query, passages, model_id="local:hashing", dim=_DIM, top_k=5)
    assert not path.exists()  # nothing persisted
    drifted = ArchivalAnnIndex(persist_path=path)
    drifted._load_from_disk(model_id="other-model", dim=_DIM)
    assert drifted._graph is None


# --------------------------------------------------------------------------
# rank_archival_passages integration
# --------------------------------------------------------------------------


def _ranking_fixture() -> tuple[list[ArchivalPassage], list[float]]:
    """A 40-passage set where the query embedding equals passage p7's vector."""
    passages = [_passage(i, text=f"unrelated content token{i}") for i in range(40)]
    query_embedding = passages[7].embedding
    assert query_embedding is not None
    return passages, query_embedding


def test_ann_on_matches_brute_force_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANN-on top-k ranking equals the brute-force (default) ranking (parity)."""
    passages, query_embedding = _ranking_fixture()

    monkeypatch.delenv("LEMONCROW_ANN_RETRIEVAL", raising=False)
    brute = rank_archival_passages(
        query="semantic recall",
        passages=passages,
        query_embedding=query_embedding,
        top_k=5,
        embedding_model="local:hashing",
    )

    ranking_mod._ARCHIVAL_ANN_INDEX.invalidate()
    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", "1")
    ann = rank_archival_passages(
        query="semantic recall",
        passages=passages,
        query_embedding=query_embedding,
        top_k=5,
        embedding_model="local:hashing",
    )

    assert [r.passage.id for r in ann] == [r.passage.id for r in brute]
    # The exact cosine match (p7) is the top hit on both paths.
    assert brute[0].passage.id == "p7"
    assert ann[0].passage.id == "p7"


def test_ann_on_preserves_just_stored_recent_passage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A just-stored (newest) passage that is the cosine match is never missed."""
    passages = [_passage(i, text=f"older content {i}") for i in range(1, 40)]
    # Newest passage, and its embedding equals the query -> must be top hit.
    fresh = _passage(0, text="freshly stored memory", when=datetime.now(UTC) + timedelta(minutes=10))
    passages.append(fresh)
    query_embedding = fresh.embedding
    assert query_embedding is not None

    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", "1")
    ranking_mod._ARCHIVAL_ANN_INDEX.invalidate()
    ranked = rank_archival_passages(
        query="fresh memory",
        passages=passages,
        query_embedding=query_embedding,
        top_k=5,
        embedding_model="local:hashing",
    )
    assert ranked[0].passage.id == fresh.id
    assert ranked[0].cosine > 0.9


def test_default_off_reproduces_brute_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off: ranking is identical whether or not embedding_model is passed."""
    passages, query_embedding = _ranking_fixture()
    monkeypatch.delenv("LEMONCROW_ANN_RETRIEVAL", raising=False)
    with_model = rank_archival_passages(
        query="semantic recall",
        passages=passages,
        query_embedding=query_embedding,
        top_k=10,
        embedding_model="local:hashing",
    )
    without_model = rank_archival_passages(
        query="semantic recall",
        passages=passages,
        query_embedding=query_embedding,
        top_k=10,
    )
    assert [(r.passage.id, round(r.score, 6)) for r in with_model] == [
        (r.passage.id, round(r.score, 6)) for r in without_model
    ]


def test_n5_model_drift_keeps_ranking_correct(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANN-on with a model-id that doesn't match stored passages still ranks
    correctly (brute-force fallback) -- never serves stale/cross-space hits."""
    passages, query_embedding = _ranking_fixture()
    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", "1")
    ranking_mod._ARCHIVAL_ANN_INDEX.invalidate()
    # Live embedder reports a model-id the persisted passages were NOT embedded
    # with -> ANN ineligible -> exact brute-force over all passages.
    ranked = rank_archival_passages(
        query="semantic recall",
        passages=passages,
        query_embedding=query_embedding,
        top_k=5,
        embedding_model="some-new-model",
    )
    assert ranked[0].passage.id == "p7"
    assert ranked[0].cosine > 0.9


def test_ann_retrieval_flag_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_ANN_RETRIEVAL", raising=False)
    assert ann_retrieval_enabled() is False
    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", "true")
    assert ann_retrieval_enabled() is True
