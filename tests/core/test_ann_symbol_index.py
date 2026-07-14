"""WS7 G4 -- persistent ANN over code symbols.

Guards exercised here: ANN/brute-force parity, N5 model-id & dim drift, N16
index_version staleness, brute-force fallback when datasketch/HNSW is
unavailable, and default-off byte-identical behaviour vs the existing semantic
path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lemoncrow.infra.storage.vector import cosine_similarity
from lemoncrow.pro.capabilities.code_context import CodeContextEngine
from lemoncrow.pro.capabilities.code_context import ann_symbol_index as ann_mod
from lemoncrow.pro.capabilities.code_context.ann_symbol_index import (
    SymbolAnnIndex,
    ann_retrieval_enabled,
    ensure_symbol_vector_schema,
)


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


def _seeded_vectors(n: int, dim: int, *, seed: int) -> dict[str, tuple[str, list[float]]]:
    np = pytest.importorskip("numpy", reason="numpy not installed")

    rng = np.random.default_rng(seed)
    return {f"s{i}": (f"h{i}", [float(x) for x in rng.standard_normal(dim)]) for i in range(n)}


def _brute_force_top_k(query: list[float], vectors: dict[str, tuple[str, list[float]]], k: int) -> list[str]:
    scored = sorted(
        ((cosine_similarity(query, vec), sid) for sid, (_, vec) in vectors.items()),
        key=lambda item: (-item[0], item[1]),
    )
    return [sid for score, sid in scored if score > 0][:k]


# --------------------------------------------------------------------------
# Unit-level: SymbolAnnIndex
# --------------------------------------------------------------------------


def test_ann_topk_matches_brute_force_above_threshold() -> None:
    """ANN top-k equals exact brute-force top-k on a deterministic small set."""
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(48, 16, seed=11)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=16, index_version=1, vectors=vectors)
    stored = idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=16)
    assert len(stored) == 48

    query = vectors["s7"][1]
    ann = idx.query(query, stored, limit=5, index_version=1, embedder_name="m1", embedding_dim=16)
    brute = _brute_force_top_k(query, vectors, 5)
    assert ann == brute


def test_ann_small_set_uses_brute_force_and_is_exact() -> None:
    """Below the ANN threshold, exact cosine is used -- and must be exact."""
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(6, 16, seed=3)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=16, index_version=1, vectors=vectors)
    stored = idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=16)

    query = vectors["s2"][1]
    result = idx.query(query, stored, limit=4, index_version=1, embedder_name="m1", embedding_dim=16)
    assert result == _brute_force_top_k(query, vectors, 4)
    # s2 is its own nearest neighbour.
    assert result[0] == "s2"


def test_n5_model_id_change_never_mixes_vector_spaces() -> None:
    """A model-id change makes old vectors ineligible (N5)."""
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(30, 16, seed=5)
    idx.upsert_vectors(conn, embedder_name="model-a", embedding_dim=16, index_version=1, vectors=vectors)

    # New model-id: zero eligible vectors, so neighbours are never served from a
    # stale model. The old rows remain only under model-a.
    assert idx.load_current_vectors(conn, embedder_name="model-b", embedding_dim=16) == []
    assert len(idx.load_current_vectors(conn, embedder_name="model-a", embedding_dim=16)) == 30
    assert idx.existing_stamped_ids(conn, embedder_name="model-b", embedding_dim=16) == set()


def test_n5_dim_change_never_mixes_vector_spaces() -> None:
    """A dim change makes old vectors ineligible (N5); cosine never compares dims."""
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(20, 16, seed=9)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=16, index_version=1, vectors=vectors)

    assert idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=32) == []
    assert len(idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=16)) == 20
    # A vector whose stored dim disagrees with the requested dim is dropped on load.
    idx.upsert_vectors(
        conn,
        embedder_name="m1",
        embedding_dim=8,
        index_version=1,
        vectors={"s0": ("h0", [0.1] * 16)},  # dim 16 payload under an 8-dim stamp is skipped on write
    )
    assert idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=8) == []


def test_n5_re_embed_overwrites_old_stamp() -> None:
    """Re-storing a symbol under a new stamp supersedes the old row (no mixing)."""
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    idx.upsert_vectors(
        conn, embedder_name="old", embedding_dim=4, index_version=1, vectors={"s0": ("h", [1.0, 0.0, 0.0, 0.0])}
    )
    idx.upsert_vectors(
        conn, embedder_name="new", embedding_dim=4, index_version=2, vectors={"s0": ("h", [0.0, 1.0, 0.0, 0.0])}
    )
    # Old stamp gone; only the new stamp remains for s0.
    assert idx.load_current_vectors(conn, embedder_name="old", embedding_dim=4) == []
    new_rows = idx.load_current_vectors(conn, embedder_name="new", embedding_dim=4)
    assert len(new_rows) == 1 and new_rows[0].vector == [0.0, 1.0, 0.0, 0.0]


def test_existing_stamped_ids_is_content_not_version_gated() -> None:
    """Freshness keys on symbol_id under the live model/dim, NOT index_version.

    symbol_id encodes the file content hash, so a present id is content-fresh by
    construction; index_version is provenance only. Gating on it would make every
    post-bump reindex re-embed the whole repo. This mirrors load_current_vectors,
    which also keys eligibility on (embedder_name, embedding_dim) alone.
    """
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(10, 8, seed=1)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=8, index_version=1, vectors=vectors)

    # A later reindex bumps index_version but does not re-stamp unchanged rows;
    # they stay fresh (same symbol_id), so the caller skips re-embedding them.
    assert len(idx.existing_stamped_ids(conn, embedder_name="m1", embedding_dim=8)) == 10
    # A different model or dim shares no vector space -> nothing fresh.
    assert idx.existing_stamped_ids(conn, embedder_name="m2", embedding_dim=8) == set()
    assert idx.existing_stamped_ids(conn, embedder_name="m1", embedding_dim=16) == set()


def test_brute_force_fallback_when_hnsw_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """With datasketch.HNSW unavailable, query falls back to exact brute-force."""
    monkeypatch.setattr(ann_mod, "_HNSW", None)
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(50, 16, seed=2)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=16, index_version=1, vectors=vectors)
    stored = idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=16)

    query = vectors["s9"][1]
    result = idx.query(query, stored, limit=5, index_version=1, embedder_name="m1", embedding_dim=16)
    # No graph was built; results still exact.
    assert idx._graph is None
    assert result == _brute_force_top_k(query, vectors, 5)


def test_graph_key_always_none_without_hnsw() -> None:
    """HNSW removed: _graph_key is always None; query() uses exact brute-force."""
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(40, 16, seed=4)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=16, index_version=1, vectors=vectors)
    stored = idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=16)
    query = vectors["s1"][1]

    idx.query(query, stored, limit=5, index_version=1, embedder_name="m1", embedding_dim=16)
    key_v1 = idx._graph_key
    idx.query(query, stored, limit=5, index_version=2, embedder_name="m1", embedding_dim=16)
    key_v2 = idx._graph_key
    assert key_v1 is None  # no HNSW graph built
    assert key_v2 is None  # no HNSW graph built
    assert key_v1 == key_v2  # both None — exact cosine used for both versions


def test_invalidate_resets_to_clean_state() -> None:
    # HNSW removed: _graph is always None; invalidate() is a no-op but must leave clean state.
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(40, 16, seed=6)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=16, index_version=1, vectors=vectors)
    stored = idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=16)
    idx.query(vectors["s0"][1], stored, limit=3, index_version=1, embedder_name="m1", embedding_dim=16)
    assert idx._graph is None  # never built without HNSW
    idx.invalidate()
    assert idx._graph is None and idx._graph_key is None


def test_schema_creation_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_symbol_vector_schema(conn)
    ensure_symbol_vector_schema(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(symbol_vectors)").fetchall()}
    # Blob-only schema: the packed float32 payload is the sole vector column
    # (JSON text storage was ~4x the disk and ~100x slower to load).
    assert {"embedder_name", "embedding_dim", "index_version", "vector_blob"} <= cols
    assert "vector_json" not in cols


def test_legacy_json_store_migrates_to_blob_only() -> None:
    """An old JSON-backed store is converted in place: blob backfilled from the
    JSON, the JSON column dropped, and reads return the same vectors."""
    import json
    import struct

    conn = sqlite3.connect(":memory:")
    # Build the legacy schema by hand (vector_json NOT NULL, no blob column).
    conn.execute("""
        CREATE TABLE symbol_vectors (
            repo_id TEXT NOT NULL, symbol_id TEXT NOT NULL, content_hash TEXT NOT NULL,
            embedder_name TEXT NOT NULL, embedding_dim INTEGER NOT NULL,
            index_version INTEGER NOT NULL, vector_json TEXT NOT NULL,
            PRIMARY KEY (repo_id, symbol_id))
        """)
    vecs = {"s0": [0.1, 0.2, 0.3, 0.4], "s1": [0.5, 0.6, 0.7, 0.8]}
    for sid, v in vecs.items():
        conn.execute(
            "INSERT INTO symbol_vectors VALUES (?,?,?,?,?,?,?)",
            ("repo", sid, "h", "m1", 4, 1, json.dumps(v)),
        )
    conn.commit()

    ensure_symbol_vector_schema(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(symbol_vectors)")}
    assert "vector_json" not in cols  # dropped to reclaim disk
    assert "vector_blob" in cols
    # Blobs are the exact packed float32 of the original JSON.
    for sid, v in vecs.items():
        blob = conn.execute("SELECT vector_blob FROM symbol_vectors WHERE symbol_id=?", (sid,)).fetchone()[0]
        assert struct.unpack("4f", blob) == pytest.approx(tuple(v))

    idx = SymbolAnnIndex("repo")
    stored = idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=4)
    assert {sv.symbol_id for sv in stored} == {"s0", "s1"}
    assert dict(zip((sv.symbol_id for sv in stored), (sv.vector for sv in stored), strict=False))[
        "s0"
    ] == pytest.approx(vecs["s0"])


def test_load_current_matrix_matches_load_current_vectors() -> None:
    """The fast frombuffer matrix loader agrees with the per-row list loader."""
    np = pytest.importorskip("numpy", reason="numpy not installed")
    conn = sqlite3.connect(":memory:")
    idx = SymbolAnnIndex("repo")
    vectors = _seeded_vectors(12, 16, seed=7)
    idx.upsert_vectors(conn, embedder_name="m1", embedding_dim=16, index_version=1, vectors=vectors)

    ids, matrix = idx.load_current_matrix(conn, embedder_name="m1", embedding_dim=16)
    stored = idx.load_current_vectors(conn, embedder_name="m1", embedding_dim=16)
    by_id = {sv.symbol_id: sv.vector for sv in stored}
    assert set(ids) == set(by_id)
    assert matrix.shape == (12, 16)
    for row_i, sid in enumerate(ids):
        assert np.asarray(by_id[sid], dtype=np.float32) == pytest.approx(matrix[row_i])


# --------------------------------------------------------------------------
# Flag gating
# --------------------------------------------------------------------------


def test_ann_retrieval_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_ANN_RETRIEVAL", raising=False)
    assert ann_retrieval_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_ann_retrieval_enabled_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", value)
    assert ann_retrieval_enabled() is True


# --------------------------------------------------------------------------
# Engine integration
# --------------------------------------------------------------------------


def _write_semantic_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        "def issue_access_token(user_id: str) -> str:\n"
        '    """Create a login session token for an authenticated user."""\n'
        "    session_token = f'session:{user_id}'\n"
        "    return session_token\n"
        "\n"
        "def revoke_access_token(token: str) -> None:\n"
        '    """Invalidate a session token after logout."""\n'
        "    return None\n",
        encoding="utf-8",
    )
    (root / "src" / "audit.py").write_text(
        "def create_login_history_for_authenticated_user(user_id: str) -> dict[str, str]:\n"
        '    """Record login history entries for audit review."""\n'
        "    return {'user_id': user_id}\n",
        encoding="utf-8",
    )


def test_engine_semantic_store_built_at_index_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured embedder turns semantic search on via the index-time vector
    store -- there is no separate flag. The store is populated during index_repo
    (no on-the-fly embedding on the query path), so semantic search resolves the
    expected top hit and the persistent table exists after indexing."""
    _use_fake_code_embedder(monkeypatch)
    monkeypatch.delenv("LEMONCROW_ANN_RETRIEVAL", raising=False)
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    hits = engine.search_symbols("create login token for authenticated user", limit=5, mode="semantic")
    assert hits
    assert hits[0].symbol_name == "issue_access_token"
    # Index-time embedding: a configured embedder builds the persistent vector
    # store as part of the index, not lazily on the query path.
    with engine._connect() as conn:
        present = conn.execute(
            "SELECT name FROM vectors.sqlite_master WHERE type='table' AND name='symbol_vectors'"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM symbol_vectors").fetchone()[0]
    assert present is not None
    assert count > 0


def test_engine_ann_on_matches_brute_force_top_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ANN-on returns the same top semantic hit as the default brute-force path."""
    _use_fake_code_embedder(monkeypatch)
    _write_semantic_fixture_repo(tmp_path)

    monkeypatch.delenv("LEMONCROW_ANN_RETRIEVAL", raising=False)
    engine_off = CodeContextEngine(tmp_path, db_path=tmp_path / "off.sqlite")
    engine_off.index_repo()
    off_hits = engine_off.search_symbols("create login token for authenticated user", limit=5, mode="semantic")

    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", "1")
    engine_on = CodeContextEngine(tmp_path, db_path=tmp_path / "on.sqlite")
    engine_on.index_repo()
    on_hits = engine_on.search_symbols("create login token for authenticated user", limit=5, mode="semantic")

    assert off_hits and on_hits
    assert on_hits[0].symbol_name == off_hits[0].symbol_name == "issue_access_token"
    # The opt-in vector table now exists and is provenance-stamped.
    with engine_on._connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT embedder_name, embedding_dim, index_version FROM symbol_vectors"
        ).fetchall()
    assert rows
    assert all(int(row[1]) > 0 for row in rows)


def test_engine_index_version_bump_invalidates_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_fake_code_embedder(monkeypatch)
    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", "1")
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    engine.search_symbols("create login token for authenticated user", limit=5, mode="semantic")

    with engine._connect() as conn:
        engine._init_schema(conn)
        engine._bump_index_version(conn)
        conn.commit()
    # invalidate() ran inside the bump; the cached graph is dropped.
    assert engine._ann_symbol_index._graph is None
    # A subsequent query still returns the correct top hit (rebuild + re-embed).
    hits = engine.search_symbols("create login token for authenticated user", limit=5, mode="semantic")
    assert hits and hits[0].symbol_name == "issue_access_token"


def test_engine_ann_fallback_when_hnsw_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With HNSW unavailable, the ANN-on engine path still returns correct hits."""
    _use_fake_code_embedder(monkeypatch)
    monkeypatch.setenv("LEMONCROW_ANN_RETRIEVAL", "1")
    monkeypatch.setattr(ann_mod, "_HNSW", None)
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    hits = engine.search_symbols("create login token for authenticated user", limit=5, mode="semantic")
    assert hits and hits[0].symbol_name == "issue_access_token"


def test_engine_incremental_reindex_prunes_stale_vectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Editing a file drops its old vectors instead of orphaning them.

    symbol_id encodes the file content hash, so an edit yields fresh ids; the
    re-index must delete the superseded vectors (and re-embed only the changed
    file) so the store stays 1:1 with live symbols -- no orphan accumulation,
    no stale rows polluting ranking, and re-embedding stays incremental.
    """
    _use_fake_code_embedder(monkeypatch)
    monkeypatch.delenv("LEMONCROW_ANN_RETRIEVAL", raising=False)
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite", autosync_enabled=False)
    engine.index_repo()

    def counts() -> tuple[int, int, int]:
        with engine._connect() as conn:
            n_sym = conn.execute("SELECT COUNT(*) FROM symbols WHERE repo_id = ?", (engine.repo_id,)).fetchone()[0]
            n_vec = conn.execute("SELECT COUNT(*) FROM symbol_vectors WHERE repo_id = ?", (engine.repo_id,)).fetchone()[
                0
            ]
            orphans = conn.execute(
                "SELECT COUNT(*) FROM symbol_vectors v WHERE v.repo_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM symbols s WHERE s.repo_id = v.repo_id AND s.symbol_id = v.symbol_id)",
                (engine.repo_id,),
            ).fetchone()[0]
        return int(n_sym), int(n_vec), int(orphans)

    n_sym0, n_vec0, orphans0 = counts()
    assert n_vec0 > 0 and orphans0 == 0 and n_vec0 == n_sym0

    target = next(tmp_path.rglob("*.py"))
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef _added_helper() -> int:\n    return 42\n",
        encoding="utf-8",
    )
    engine.index_repo(force=False)

    n_sym1, n_vec1, orphans1 = counts()
    assert orphans1 == 0  # stale vectors of the edited file were pruned, not orphaned
    assert n_vec1 == n_sym1  # store stays 1:1 with live symbols
    assert n_sym1 == n_sym0 + 1  # the newly added symbol was indexed + embedded
