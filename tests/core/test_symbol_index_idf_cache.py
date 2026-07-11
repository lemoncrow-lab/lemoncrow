"""Regression: BM25 IDF cache must invalidate on content changes, not just
entry-count changes (HIGH #36)."""

from __future__ import annotations

from typing import Any

from lemoncrow.core.capabilities.semantic_file_memory.search import SymbolIndex


class _FakeIndex:
    """Duck-typed stand-in for FileIndex exposing only all_entries()."""

    def __init__(self, entries: dict[str, dict[str, Any]]) -> None:
        self._entries = entries

    def all_entries(self) -> dict[str, dict[str, Any]]:
        return dict(self._entries)


def test_idf_cache_invalidates_when_content_changes_without_count_change() -> None:
    index = _FakeIndex(
        {
            "a.py": {"symbols": ["alpha"], "content_hash": "h1"},
        }
    )
    sym = SymbolIndex(index)

    # Prime the cache and confirm the original term is searchable.
    assert sym.bm25_search("alpha")
    assert not sym.bm25_search("beta")

    # Edit the file: same entry count, new content + new hash, new term.
    index._entries["a.py"] = {"symbols": ["beta"], "content_hash": "h2"}

    # The new term must now be searchable (stale IDF would return nothing).
    assert sym.bm25_search("beta")
