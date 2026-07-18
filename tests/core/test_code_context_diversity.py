"""Filetype-diversity pass: doc overflow is demoted, never dropped."""

from __future__ import annotations

from lemoncrow.pro.capabilities.code_context.diversity import (
    demote_doc_overflow,
    doc_cap,
    is_doc_path,
    query_wants_docs,
)


def test_is_doc_path_matches_doc_suffixes_only() -> None:
    assert is_doc_path("docs/guide.md")
    assert is_doc_path("README.RST")
    assert is_doc_path("notes.txt")
    assert not is_doc_path("src/server.py")
    assert not is_doc_path("Makefile")
    assert not is_doc_path("a/b.mdx.py")


def test_query_wants_docs_detects_doc_intent() -> None:
    assert query_wants_docs("update the README")
    assert query_wants_docs("where is the documentation for retries")
    assert query_wants_docs("fix typo in install.md")
    assert not query_wants_docs("image png jpg vision support in read tool")


def test_doc_cap_is_quarter_window_floor_one() -> None:
    assert doc_cap(8) == 2
    assert doc_cap(24) == 6
    assert doc_cap(3) == 1


def test_demote_doc_overflow_demotes_excess_docs_below_window() -> None:
    ranked = ["a.md", "b.md", "c.md", "d.md", "e.py", "f.py", "g.md", "h.rs"]
    result = demote_doc_overflow(ranked, window=8, cap=2)
    # First two docs keep their slots; later docs demoted below the head while
    # code files move up. Nothing is dropped.
    assert result == ["a.md", "b.md", "e.py", "f.py", "h.rs", "c.md", "d.md", "g.md"]
    assert sorted(result) == sorted(ranked)


def test_demote_doc_overflow_noop_when_within_cap() -> None:
    ranked = ["a.py", "b.md", "c.py", "d.md"]
    assert demote_doc_overflow(ranked, window=8, cap=2) == ranked


def test_demote_doc_overflow_homogeneous_docs_keep_order() -> None:
    ranked = ["a.md", "b.md", "c.md"]
    assert demote_doc_overflow(ranked, window=2, cap=1) == ranked


def test_demote_doc_overflow_window_bounds_head_only() -> None:
    ranked = ["a.md", "b.md", "c.py", "d.md", "e.md"]
    result = demote_doc_overflow(ranked, window=2, cap=1)
    # Head of 2: a.md (doc slot), c.py. Everything else keeps original order.
    assert result == ["a.md", "c.py", "b.md", "d.md", "e.md"]


def test_demote_doc_overflow_custom_path_of() -> None:
    ranked = [{"path": "a.md"}, {"path": "b.md"}, {"path": "c.py"}]
    result = demote_doc_overflow(ranked, window=2, cap=1, path_of=lambda e: str(e["path"]))
    assert [e["path"] for e in result] == ["a.md", "c.py", "b.md"]
