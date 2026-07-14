from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.code_context.embedding import SemanticSearchRanker
from lemoncrow.pro.capabilities.code_context.engine import (
    _LINEAGE_INDEX_VERSION,
    CodeContextEngine,
)
from lemoncrow.pro.capabilities.code_context.models import SymbolRecord


class _TaskAwareDummyEmbedder:
    dim = 2
    name = "dummy:code"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("plain embed() should not be used for code-path embeddings")

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(("query", texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(("document", texts))
        return [[0.0, 1.0] for _ in texts]


def _symbol() -> SymbolRecord:
    return SymbolRecord(
        symbol_id="sym-1",
        repo_id="repo",
        file_path="src/auth.py",
        language="python",
        symbol_name="issue_access_token",
        qualified_name="src.auth.issue_access_token",
        kind="function",
        signature="def issue_access_token(user_id: str) -> str:",
        start_byte=0,
        end_byte=10,
        start_line=1,
        end_line=2,
        content_hash="abc123",
    )


def test_maybe_warm_ann_cache_populates_cache_in_background(tmp_path: Path) -> None:
    """``_maybe_warm_ann_cache`` should pre-load the matrix so a real query never pays
    the cold-load cost -- the fix for the 36s p100 latency on large repos (see
    ``_search_symbols_semantic_ann``'s docstring for the full cold-start story).
    """
    import time

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def issue_access_token(user_id):\n    return user_id\n")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite", autosync_enabled=False)
    embedder = _TaskAwareDummyEmbedder()
    engine._semantic_ranker = SemanticSearchRanker(tmp_path, store_root=tmp_path, embedder=embedder)

    assert engine._ann_vectors_cache is None

    with engine._connect() as conn:
        engine._init_schema(conn)
        engine._ann_symbol_index.upsert_vectors(
            conn,
            embedder_name=embedder.name,
            embedding_dim=embedder.dim,
            index_version=engine._current_index_version(),
            vectors={"sym-1": ("abc123", [0.0, 1.0])},
        )

    engine._maybe_warm_ann_cache()
    assert engine._ann_warm_started is True

    for _ in range(50):
        if engine._ann_vectors_cache is not None:
            break
        time.sleep(0.05)

    assert engine._ann_vectors_cache is not None
    cache_key, ids, matrix = engine._ann_vectors_cache
    assert cache_key == (embedder.name, embedder.dim, engine._current_index_version())
    assert ids == ["sym-1"]
    assert matrix.shape == (1, 2)

    # A second call must not spawn another thread or touch the already-warm cache.
    engine._maybe_warm_ann_cache()
    assert engine._ann_vectors_cache == (cache_key, ids, matrix)


def test_maybe_warm_ann_cache_noop_without_embedder(tmp_path: Path) -> None:
    """No configured embedder (null embedder) -> nothing to warm, no thread spawned."""
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite", autosync_enabled=False)
    assert engine._semantic_ranker.available is False

    engine._maybe_warm_ann_cache()

    assert engine._ann_warm_started is False
    assert engine._ann_vectors_cache is None


def test_semantic_search_ranker_uses_task_aware_code_embedder(tmp_path: Path) -> None:
    embedder = _TaskAwareDummyEmbedder()
    ranker = SemanticSearchRanker(tmp_path, store_root=tmp_path, embedder=embedder)

    query_vector = ranker._embed_query("token lookup")
    symbol_vector = ranker._embed_symbol(_symbol(), "issue access token")

    assert query_vector == [1.0, 0.0]
    assert symbol_vector == [0.0, 1.0]
    assert embedder.calls == [
        ("query", ["token lookup"]),
        ("document", ["issue access token"]),
    ]


def test_lineage_ready_preserves_old_chunks_until_full_rebuild_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LINEAGE_ENABLED", "1")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text("def total() -> int:\n    return 1\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    with engine._connect() as conn:
        engine._init_schema(conn)
        conn.execute(
            """INSERT INTO commit_chunks
               (commit_sha, author_date, files_touched, symbols_touched,
                summary, summary_model, embedding, index_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("deadbeef", 1, '["src/orders.py"]', None, "old summary", "stub", b"\x00\x00\x80?", 1),
        )
        conn.executemany(
            "INSERT INTO engine_state(key, value) VALUES (?, ?)",
            [
                ("commit_lineage_head", "old-head"),
                ("commit_lineage_watermark", "deadbeef"),
                ("commit_lineage_embedder_name", "local"),
                ("commit_lineage_embedder_dim", "384"),
            ],
        )
        conn.commit()

    monkeypatch.setattr(engine, "_safe_current_head_sha", lambda: "new-head")
    monkeypatch.setattr(
        "lemoncrow.infra.code_intel.git_history.embedder.embedder_name",
        lambda: "ollama:nomic-embed-text",
    )
    monkeypatch.setattr(
        "lemoncrow.infra.code_intel.git_history.embedder.embedding_dim",
        lambda: 768,
    )

    started: list[bool] = []

    class _FakeThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.code_context.engine.threading.Thread",
        _FakeThread,
    )

    engine._ensure_lineage_ready()

    with engine._connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM commit_chunks").fetchone()
        assert row is not None
        assert int(row["n"]) == 1

    assert engine._lineage_rebuild_full is True
    assert engine._lineage_thread is not None
    assert started == [True]
    assert _LINEAGE_INDEX_VERSION >= 2
