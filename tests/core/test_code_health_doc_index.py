"""WS10 N17 -- design-doc indexing into a separate retrieval corpus.

Verifies a doc chunk is indexed and retrievable, that indexing is off by default
and must be opted into, and that it never touches code retrieval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.code_health.doc_index import (
    DesignDocStore,
    chunk_markdown,
    doc_indexing_enabled,
    index_design_docs,
    recall_design_docs,
)

_DOC = (
    "# Architecture\n\n"
    "The system has three layers with a strict dependency direction.\n\n"
    "## Storage\n\n"
    "Persistence uses a SQLite ledger and a separate vector store for embeddings.\n\n"
    "## Routing\n\n"
    "Requests are routed by complexity tier to the warm stdio path.\n"
)


def test_chunk_markdown_heading_tree() -> None:
    chunks = chunk_markdown("design.md", _DOC)
    crumbs = {c.heading_path for c in chunks}
    assert "Architecture" in crumbs
    assert "Architecture > Storage" in crumbs
    assert "Architecture > Routing" in crumbs
    # Each chunk carries a 1-based source line and non-empty body.
    for chunk in chunks:
        assert chunk.line_start >= 1
        assert chunk.text.strip()


def test_index_and_recall_doc_chunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "design.md").write_text(_DOC, encoding="utf-8")
    atelier_root = tmp_path / "state"

    # Explicit enable=True bypasses the env flag (test opt-in).
    receipt = index_design_docs(repo_root=repo, atelier_root=atelier_root, paths=["design.md"], enable=True)
    assert receipt["enabled"] is True
    assert receipt["indexed_chunks"] >= 3
    assert receipt["docs"] == 1

    # A query about storage recalls the Storage section chunk.
    result = recall_design_docs(atelier_root=atelier_root, query="sqlite vector store embeddings", limit=5)
    assert result["result_count"] >= 1
    top = result["results"][0]
    assert top["doc"].endswith("design.md")
    assert "Storage" in top["heading_path"] or "sqlite" in top["text"].lower()
    assert top["line"] >= 1


def test_indexing_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_DOC_INDEXING", raising=False)
    assert doc_indexing_enabled() is False

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "design.md").write_text(_DOC, encoding="utf-8")
    atelier_root = tmp_path / "state"

    # No enable flag, env unset -> no write, observable via enabled=False.
    receipt = index_design_docs(repo_root=repo, atelier_root=atelier_root, paths=["design.md"])
    assert receipt["enabled"] is False
    assert receipt["indexed_chunks"] == 0
    # The separate store was never created.
    assert DesignDocStore(atelier_root).count() == 0


def test_env_flag_enables_indexing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_DOC_INDEXING", "1")
    assert doc_indexing_enabled() is True
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "design.md").write_text(_DOC, encoding="utf-8")
    atelier_root = tmp_path / "state"
    receipt = index_design_docs(repo_root=repo, atelier_root=atelier_root, paths=["design.md"])
    assert receipt["enabled"] is True
    assert receipt["indexed_chunks"] >= 3


def test_doc_store_is_separate_from_code_index(tmp_path: Path) -> None:
    """Indexing docs must not create or mutate the semantic code index cache."""
    from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability
    from atelier.core.capabilities.semantic_file_memory.indexer import _CACHE_FILENAME

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "mod.py").write_text("def f() -> int:\n    return 1\n", encoding="utf-8")
    (repo / "design.md").write_text(_DOC, encoding="utf-8")
    atelier_root = tmp_path / "state"

    # Seed the code index, capture its searchable state.
    cap = SemanticFileMemoryCapability(atelier_root)
    cap.summarize_file(repo / "src" / "mod.py")
    before = cap.semantic_search("f")
    code_cache = atelier_root / _CACHE_FILENAME
    code_bytes_before = code_cache.read_bytes()

    # Index docs into the separate store.
    index_design_docs(repo_root=repo, atelier_root=atelier_root, paths=["design.md"], enable=True)

    # Code index cache is byte-identical; code search is unchanged.
    assert code_cache.read_bytes() == code_bytes_before
    after = SemanticFileMemoryCapability(atelier_root).semantic_search("f")
    assert [r["path"] for r in after] == [r["path"] for r in before]


def test_recall_empty_when_nothing_indexed(tmp_path: Path) -> None:
    result = recall_design_docs(atelier_root=tmp_path / "empty", query="anything", limit=5)
    assert result["kind"] == "recall_docs"
    assert result["result_count"] == 0
    assert result["results"] == []
