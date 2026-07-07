"""Integration tests for commit chunk search merge in CodeContextEngine."""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest

from atelier.infra.code_intel.git_history.models import CommitSummary

# Dummy embedding blob (4 float32s) so tests never depend on a live embedder.
_DUMMY_EMBEDDING: bytes = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)


def _make_summary(sha: str, summary: str, files: list[str] | None = None) -> CommitSummary:
    return CommitSummary(
        sha=sha,
        author_date=1700000000,
        files_touched=files or ["src/auth.py"],
        summary=summary,
        summary_model="test-model",
        prompt_version="v1",
    )


@pytest.fixture()
def engine_with_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Create a minimal git repo with CodeContextEngine seeded with commit chunks."""
    # Mock embed_summary so tests never need a live embedding service.
    monkeypatch.setattr(
        "atelier.infra.code_intel.git_history.embedder.embed_summary",
        lambda _s: _DUMMY_EMBEDDING,
    )

    # Mock the query-side embedder so _search_commit_chunks gets a query
    # vector with the same dimension as the stored dummy embedding.
    monkeypatch.setattr(
        "atelier.core.capabilities.code_context.embedding.SemanticSearchRanker._embed_query",
        lambda _self, _q: [0.1, 0.2, 0.3, 0.4],
    )

    # Minimal git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "a.py").write_text("x = 1")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    db_dir = tmp_path / ".atelier"
    db_dir.mkdir(parents=True, exist_ok=True)

    from atelier.core.capabilities.code_context.engine import CodeContextEngine

    engine = CodeContextEngine(repo_root=tmp_path, db_path=db_dir / "code.db")

    # Seed two commit chunks directly
    summaries = [
        _make_summary("aaa111bbb222ccc333aaa111", "Fixed auth session token leak on logout"),
        _make_summary("ddd444eee555fff666ddd444", "Improved database query caching performance"),
    ]
    batch = [
        (
            s.sha,
            s.author_date,
            json.dumps(s.files_touched),
            None,
            s.summary,
            s.summary_model,
            _DUMMY_EMBEDDING,
            1,
        )
        for s in summaries
    ]
    import sqlite3

    # Use direct sqlite3 to avoid connection manager conflicts
    conn = sqlite3.connect(str(db_dir / "code.db"))
    conn.row_factory = sqlite3.Row
    with conn:
        # Ensure schema exists
        conn.execute("""CREATE TABLE IF NOT EXISTS commit_chunks (
                commit_sha TEXT PRIMARY KEY, author_date INTEGER NOT NULL,
                files_touched TEXT NOT NULL, symbols_touched TEXT,
                summary TEXT NOT NULL, summary_model TEXT NOT NULL,
                embedding BLOB, index_version INTEGER NOT NULL)""")
        conn.executemany(
            """INSERT OR REPLACE INTO commit_chunks
               (commit_sha, author_date, files_touched, symbols_touched,
                summary, summary_model, embedding, index_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )
    conn.close()

    return engine


def test_search_returns_commit_hits(engine_with_commits: Any) -> None:
    results = engine_with_commits.search_symbols("auth session leak", provenance_filter="commit")
    assert results, "Expected commit hits with mocked embedder"
    provenances = [r.provenance for r in results]
    assert "commit" in provenances, f"Expected commit hit, got provenances: {provenances}"


def test_commit_result_has_commit_sha(engine_with_commits: Any) -> None:
    results = engine_with_commits.search_symbols("auth session leak", provenance_filter="commit")
    assert results, "Expected commit hits with mocked embedder"
    for r in results:
        assert r.commit_sha is not None
        assert len(r.commit_sha) > 0


def test_provenance_filter_commit_only(engine_with_commits: Any) -> None:
    results = engine_with_commits.search_symbols("authentication session", provenance_filter="commit")
    assert results, "Expected commit hits with mocked embedder"
    assert all(
        r.provenance == "commit" for r in results
    ), f"All results should have provenance=commit, got: {[r.provenance for r in results]}"


def test_commit_score_has_penalty(engine_with_commits: Any) -> None:
    results = engine_with_commits.search_symbols("auth session", provenance_filter="commit")
    assert results, "Expected commit hits with mocked embedder"
    # Scores must be < 1.0 because penalty is applied (default 0.1)
    for r in results:
        assert r.score is not None
        assert r.score < 1.0, f"Score {r.score} should be < 1.0 (penalty not applied?)"


def test_commit_sha_survives_tool_search(engine_with_commits: Any) -> None:
    result = engine_with_commits.tool_search("auth session leak", provenance_filter="commit")
    # tool_search returns 'items' key (not 'matches')
    items = result.get("items", [])
    assert items, "Expected items in tool_search result with mocked embedder"
    commit_items = [m for m in items if m.get("provenance") == "commit"]
    assert commit_items, "Expected at least one commit item in tool_search result"
    for item in commit_items:
        assert "commit_sha" in item, f"commit_sha missing from item: {item}"
