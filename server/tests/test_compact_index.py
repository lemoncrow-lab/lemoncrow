from __future__ import annotations

import random
import string
from pathlib import Path
from types import SimpleNamespace

import pytest
from lemoncrow_server_core.index.analysis import analyze
from lemoncrow_server_core.index.contracts import AnalysisArtifact, sha256_hex
from lemoncrow_server_core.index.sqlite import SqliteIndex, open_sqlite_index
from lemoncrow_server_core.wiring import build_backend

CAP = 1024**2


@pytest.mark.parametrize("match_any", [False, True])
@pytest.mark.parametrize("grams", [False, True])
@pytest.mark.parametrize("substring", [False, True])
def test_compact_search_matches_legacy_randomized(match_any: bool, grams: bool, substring: bool) -> None:
    rng = random.Random(74129)
    words = ["alpha", "beta", "gamma", "hello_world", "return", "class", "cache", "λambda", "café", "東京"]
    indexes = [SqliteIndex(content_size_cap=CAP, compact=compact) for compact in (False, True)]
    digests = []
    try:
        for number in range(36):
            text = "def item_" + str(number) + "():\n    " + " ".join(rng.choices(words, k=18))
            data = text.encode()
            digest = sha256_hex(data)
            digests.append(digest)
            artifact = analyze(digest, "python-1", data)
            for index in indexes:
                for org in ("org_a", "org_b"):
                    index.backend.analysis.ensure(org, digest, "python-1", data, artifact=artifact)
        queries = [
            *words,
            "hello_w",
            "cache alpha",
            "pha",
            "東京 λambda",
            "",
            "'cache'",
            "missing",
            "class cache",
            "alpha beta gamma",
            ".*alpha.*",
        ]
        for _ in range(20):
            queries.append(" ".join(rng.sample(words, rng.randint(1, 4))))
        for query in queries:
            for ignored in ((), ("class",)):
                values = [
                    dict(
                        index.backend.analysis.lexical_matches(
                            "org_a",
                            digests[::2],
                            query,
                            match_any_term=match_any,
                            include_grams=grams,
                            include_substring_only=substring,
                            ignored_terms=ignored,
                        )
                    )
                    for index in indexes
                ]
                assert values[0] == values[1], (query, ignored, values)
        for index in indexes:
            index.backend.analysis.sweep("org_a", digests[:12])
        for query in queries:
            assert dict(indexes[0].backend.analysis.lexical_matches("org_a", digests, query)) == dict(
                indexes[1].backend.analysis.lexical_matches("org_a", digests, query)
            )
        assert indexes[0].backend.analysis.stored_bytes("org_a") == indexes[1].backend.analysis.stored_bytes("org_a")
    finally:
        for index in indexes:
            index.close()


def test_fts_encoding_preserves_unicode_and_long_tokens_and_parameter_boundaries() -> None:
    indexes = [SqliteIndex(content_size_cap=CAP, compact=compact) for compact in (False, True)]
    needles = (*("term_" + str(i) for i in range(805)), "x" * 40_000 + "a", "x" * 40_000 + "b", "\0", "Ä東京")
    grams = tuple(chr(0x400 + i) + "ab" for i in range(805))
    digest = sha256_hex(b"fixture")
    artifact = AnalysisArtifact(digest, "test", 1, 7, needles, (), trigrams=grams)
    try:
        for index in indexes:
            index.backend.analysis.ensure("org", digest, "test", b"fixture", artifact=artifact)
        for subset in (needles[:400], needles[:401], needles, needles[-4:]):
            expected = indexes[0]._analysis._matched_values("org", (digest,), "analysis_terms", "term", subset)
            assert indexes[1]._analysis._matched_values("org", (digest,), "analysis_terms", "term", subset) == expected
        for subset in (grams[:400], grams[:401], grams):
            for index in indexes:
                assert index._analysis._full_gram_hits("org", (digest,), subset) == {digest: len(subset)}
                assert index._analysis._hits("org", (digest,), "analysis_grams", "gram", subset) == {
                    digest: len(subset)
                }
        assert not indexes[1]._analysis._matched_values("other-org", (digest,), "analysis_terms", "term", needles)
    finally:
        for index in indexes:
            index.close()


def _wired_backend(path: Path):  # type: ignore[no-untyped-def]
    limits = SimpleNamespace(
        max_indexed_content_bytes=CAP,
        inline_rebuild_budget=100,
        max_materialized_views=8,
        materialized_bytes_quota=1024**3,
        tool_deadline_s=30.0,
    )
    return build_backend(SimpleNamespace(index_path=path, workspace_root=None, limits=limits))


def test_server_wiring_uses_compact_for_a_new_shared_store(tmp_path: Path) -> None:
    path = tmp_path / "server.sqlite3"
    backend = _wired_backend(path)
    data = b"def alpha():\n    return 'wired_compact_marker'\n"
    digest = sha256_hex(data)
    try:
        backend.analysis.ensure("org", digest, "python-1", data)
    finally:
        backend.close_backend()
    conn = __import__("sqlite3").connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM search_documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM analysis_grams").fetchone()[0] == 0
    finally:
        conn.close()


def test_server_wiring_preserves_a_legacy_normalized_shared_store(tmp_path: Path) -> None:
    path = tmp_path / "legacy-server.sqlite3"
    legacy = open_sqlite_index(path, content_size_cap=CAP, compact=False)
    data = b"def alpha():\n    return 'legacy_wired_marker'\n"
    digest = sha256_hex(data)
    try:
        legacy.backend.analysis.ensure("org", digest, "python-1", data)
    finally:
        legacy.close()

    backend = _wired_backend(path)
    try:
        assert backend.analysis.lexical_matches("org", (digest,), "legacy_wired_marker")
    finally:
        backend.close_backend()
    conn = __import__("sqlite3").connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analysis_grams").fetchone()[0] > 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='search_documents'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_auto_mode_uses_compact_for_new_store_and_reopens_it_compact(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite3"
    data = b"def alpha():\n    return 'markerword'\n"
    digest = sha256_hex(data)
    index = open_sqlite_index(path, content_size_cap=CAP, compact=None)
    try:
        index.backend.analysis.ensure("org", digest, "python-1", data)
        assert index._db.scalar_int("SELECT COUNT(*) FROM search_documents") == 1
        assert index._db.scalar_int("SELECT COUNT(*) FROM analysis_grams") == 0
    finally:
        index.close()

    reopened = open_sqlite_index(path, content_size_cap=CAP, compact=None)
    try:
        assert reopened.backend.analysis.lexical_matches("org", (digest,), "markerword")
        assert reopened._db.scalar_int("SELECT COUNT(*) FROM search_documents") == 1
    finally:
        reopened.close()


def test_auto_mode_preserves_existing_normalized_store_without_implicit_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    first = b"def alpha():\n    return 'legacy_marker'\n"
    digest = sha256_hex(first)
    legacy = open_sqlite_index(path, content_size_cap=CAP, compact=False)
    try:
        legacy.backend.analysis.ensure("org", digest, "python-1", first)
        assert legacy._db.scalar_int("SELECT COUNT(*) FROM analysis_grams") > 0
        assert not legacy._db._has_table("search_documents")
    finally:
        legacy.close()

    reopened = open_sqlite_index(path, content_size_cap=CAP, compact=None)
    try:
        assert not reopened._db._has_table("search_documents")
        assert reopened.backend.analysis.lexical_matches("org", (digest,), "legacy_marker")
    finally:
        reopened.close()


def test_compact_delete_reinsert_and_reopen_have_no_ghost_postings(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    data = b"alpha beta gamma"
    digest = sha256_hex(data)
    index = SqliteIndex(path, content_size_cap=CAP, compact=True)
    for profile in ("z-profile", "a-profile"):
        index.backend.analysis.ensure("org", digest, profile, data)
    assert index._db.scalar_int("SELECT COUNT(*) FROM search_documents") == 1
    assert index.backend.analysis.sweep("org", (digest,)) == 2
    assert index.backend.analysis.stored_bytes("org") == 0
    replacement = b"entirely different"
    other = sha256_hex(replacement)
    index.backend.analysis.ensure("org", other, "lexical-1", replacement)
    index.close()
    index = SqliteIndex(path, content_size_cap=CAP, compact=True)
    try:
        assert not index.backend.analysis.lexical_matches("org", (digest, other), "alpha")
        assert index.backend.analysis.lexical_matches("org", (other,), "different")
        for table in ("search_terms", "search_grams"):
            index._db._conn.execute(f"INSERT INTO {table}({table}) VALUES ('integrity-check')")
    finally:
        index.close()


def test_compressed_postings_reduce_actual_database_bytes(tmp_path: Path) -> None:
    rng = random.Random(87241)
    paths = [tmp_path / "legacy.sqlite3", tmp_path / "compact.sqlite3"]
    indexes = [
        SqliteIndex(path, content_size_cap=CAP, compact=compact)
        for path, compact in zip(paths, (False, True), strict=True)
    ]
    try:
        for number in range(120):
            words = ["".join(rng.choices(string.ascii_lowercase, k=12)) for _ in range(70)]
            data = ("def file_" + str(number) + "():\n    return " + " ".join(words)).encode()
            digest = sha256_hex(data)
            artifact = analyze(digest, "python-1", data)
            for index in indexes:
                index.backend.content.put("org_a", digest, data)
                index.backend.analysis.ensure("org_a", digest, "python-1", data, artifact=artifact)
        for index in indexes:
            index._db._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        sizes = [path.stat().st_size for path in paths]
        assert sizes[1] < sizes[0] * 0.5, sizes
        assert indexes[1]._db.scalar_int("SELECT COUNT(*) FROM analysis_grams") == 0
        assert indexes[1]._db.scalar_int("SELECT COUNT(*) FROM analysis_terms") == 0
    finally:
        for index in indexes:
            index.close()
