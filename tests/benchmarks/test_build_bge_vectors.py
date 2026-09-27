from __future__ import annotations

import sqlite3
from pathlib import Path

from experiments.retrieval_symbol_vote.build_bge_vectors import (
    _vec_provenance,
    _vector_store_paths,
    _vectors_db_for,
)


def test_vectors_db_for_uses_isolated_index_directory(tmp_path: Path) -> None:
    main = tmp_path / "repo-a" / "code_context.sqlite"
    assert _vectors_db_for(main) == main.parent / "vectors.sqlite"


def test_vec_provenance_reads_secondary_vectors_database(tmp_path: Path) -> None:
    main = tmp_path / "code_context.sqlite"
    main.touch()
    vectors = _vectors_db_for(main)
    conn = sqlite3.connect(vectors)
    conn.execute("""
        CREATE TABLE symbol_vectors (
            repo_id TEXT, symbol_id TEXT, content_hash TEXT, embedder_name TEXT,
            embedding_dim INTEGER, index_version INTEGER, vector_blob BLOB
        )
        """)
    conn.executemany(
        "INSERT INTO symbol_vectors VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("r", "a", "h", "bge:model", 1536, 1, b"x"),
            ("r", "b", "h", "bge:model", 1536, 1, b"x"),
        ],
    )
    conn.commit()
    conn.close()

    assert _vec_provenance(main) == ("bge:model", 1536, 2)


def test_vector_store_paths_supports_sidecar_and_legacy_main(tmp_path: Path) -> None:
    main = tmp_path / "code_context.sqlite"
    assert _vector_store_paths(main) == (tmp_path / "vectors.sqlite", main)


def test_vec_provenance_falls_back_to_legacy_main_database(tmp_path: Path) -> None:
    main = tmp_path / "code_context.sqlite"
    conn = sqlite3.connect(main)
    conn.execute("""
        CREATE TABLE symbol_vectors (
            repo_id TEXT, symbol_id TEXT, content_hash TEXT, embedder_name TEXT,
            embedding_dim INTEGER, index_version INTEGER, vector_blob BLOB
        )
        """)
    conn.executemany(
        "INSERT INTO symbol_vectors VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("r", "a", "h", "bge:legacy", 1536, 1, b"x"),
            ("r", "b", "h", "bge:legacy", 1536, 1, b"x"),
            ("r", "c", "h", "bge:legacy", 1536, 1, b"x"),
        ],
    )
    conn.commit()
    conn.close()

    assert _vec_provenance(main) == ("bge:legacy", 1536, 3)
