"""Regression tests for FileIndex cache eviction (recency, not alphabetical)."""

from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import indexer
from atelier.core.capabilities.semantic_file_memory.indexer import FileIndex


def test_save_evicts_oldest_by_recency_not_alphabet(tmp_path: Path, monkeypatch) -> None:
    """Over-capacity caches must drop the least-recently-written entries.

    Alphabetical eviction would drop ``aaa`` (early alphabet) even though it was
    written most recently; recency-based eviction must keep it and drop ``zzz``.
    """
    monkeypatch.setattr(indexer, "_MAX_CACHE_ENTRIES", 1)
    idx = FileIndex(tmp_path)
    state = {
        "v": 2,
        "files": {
            "zzz.py": {"content_hash": "h1", "cached_at": 1.0},  # oldest
            "aaa.py": {"content_hash": "h2", "cached_at": 2.0},  # newest
        },
    }

    idx._save(state)

    remaining = idx.all_entries()
    assert set(remaining) == {"aaa.py"}


def test_reput_protects_early_alphabet_file_from_eviction(tmp_path: Path, monkeypatch) -> None:
    """Re-analyzing (re-put) an early-alphabet file bumps its recency."""
    monkeypatch.setattr(indexer, "_MAX_CACHE_ENTRIES", 1)
    idx = FileIndex(tmp_path)

    early = tmp_path / "aaa.py"
    late = tmp_path / "zzz.py"
    early.write_text("a = 1\n", encoding="utf-8")
    late.write_text("z = 1\n", encoding="utf-8")

    # Force a stable, increasing clock so ordering is deterministic.
    clock = iter([10.0, 20.0])
    monkeypatch.setattr(indexer.time, "time", lambda: next(clock))

    idx.put(late, {"payload": "z"})
    idx.put(early, {"payload": "a"})  # newer -> early survives, late evicted

    remaining = set(idx.all_entries())
    assert remaining == {str(early)}


def test_legacy_entries_without_cached_at_are_evicted_first(tmp_path: Path, monkeypatch) -> None:
    """Entries from older caches (no ``cached_at``) sort as oldest."""
    monkeypatch.setattr(indexer, "_MAX_CACHE_ENTRIES", 1)
    idx = FileIndex(tmp_path)
    state = {
        "v": 2,
        "files": {
            "legacy.py": {"content_hash": "h1"},  # no cached_at
            "fresh.py": {"content_hash": "h2", "cached_at": 5.0},
        },
    }

    idx._save(state)

    assert set(idx.all_entries()) == {"fresh.py"}
