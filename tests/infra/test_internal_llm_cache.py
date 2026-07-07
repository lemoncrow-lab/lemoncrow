"""Tests for the persistent internal-LLM summary cache."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from atelier.infra.internal_llm import cache as llm_cache


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Point the singleton store at a throwaway root and reset it so each test
    # gets a fresh on-disk DB; clear env knobs that could leak between tests.
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    monkeypatch.delenv("ATELIER_STORE_ROOT", raising=False)
    monkeypatch.delenv("ATELIER_INTERNAL_LLM_CACHE", raising=False)
    monkeypatch.delenv("ATELIER_INTERNAL_LLM_CACHE_MAX_ENTRIES", raising=False)
    llm_cache._reset_store_for_tests()
    yield
    llm_cache._reset_store_for_tests()


def test_cached_summarize_memoizes_identical_inputs() -> None:
    calls = {"n": 0}

    def _compute() -> str:
        calls["n"] += 1
        return f"summary-{calls['n']}"

    first = llm_cache.cached_summarize("text", model="m", max_tokens=64, backend="openai", compute=_compute)
    second = llm_cache.cached_summarize("text", model="m", max_tokens=64, backend="openai", compute=_compute)
    assert first == second == "summary-1"
    assert calls["n"] == 1


def test_cached_summarize_distinct_keys_recompute() -> None:
    calls = {"n": 0}

    def _compute() -> str:
        calls["n"] += 1
        return f"summary-{calls['n']}"

    llm_cache.cached_summarize("text-a", model="m", max_tokens=64, backend="openai", compute=_compute)
    llm_cache.cached_summarize("text-b", model="m", max_tokens=64, backend="openai", compute=_compute)
    llm_cache.cached_summarize(
        "text-a", model="m", max_tokens=128, backend="openai", compute=_compute
    )  # diff max_tokens
    assert calls["n"] == 3


def test_summary_key_distinguishes_openai_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same (text, model, max_tokens, backend) routed at two different OpenAI-
    # compatible endpoints must not collide on the same cache key, or one
    # provider's summary gets served for another (cross-endpoint poisoning).
    monkeypatch.setenv("ATELIER_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("ATELIER_OPENAI_API_KEY", "key-one")
    first = llm_cache.summary_key("text", model="m", max_tokens=64, backend="openai")

    monkeypatch.setenv("ATELIER_OPENAI_BASE_URL", "http://localhost:8000/v1")
    second = llm_cache.summary_key("text", model="m", max_tokens=64, backend="openai")
    assert first != second  # different base_url

    monkeypatch.setenv("ATELIER_OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("ATELIER_OPENAI_API_KEY", "key-two")
    third = llm_cache.summary_key("text", model="m", max_tokens=64, backend="openai")
    assert first != third  # same base_url, different api key


def test_cached_summarize_disabled_recomputes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_INTERNAL_LLM_CACHE", "0")
    calls = {"n": 0}

    def _compute() -> str:
        calls["n"] += 1
        return "summary"

    llm_cache.cached_summarize("text", model="m", max_tokens=64, backend="openai", compute=_compute)
    llm_cache.cached_summarize("text", model="m", max_tokens=64, backend="openai", compute=_compute)
    assert calls["n"] == 2


def test_cached_summarize_does_not_cache_exceptions() -> None:
    calls = {"n": 0}

    def _boom() -> str:
        calls["n"] += 1
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        llm_cache.cached_summarize("text", model="m", max_tokens=64, backend="openai", compute=_boom)
    with pytest.raises(RuntimeError):
        llm_cache.cached_summarize("text", model="m", max_tokens=64, backend="openai", compute=_boom)
    assert calls["n"] == 2  # nothing cached on failure


def test_cache_persists_across_store_instances(tmp_path: Path) -> None:
    # Two independent store instances on the same DB file model two processes:
    # a summary written by one is served from disk by the other (cold memory).
    db = tmp_path / "persist.sqlite"
    writer = llm_cache._SummaryStore(db)
    writer.put("k", "the-summary")
    reader = llm_cache._SummaryStore(db)
    assert reader.get("k") == "the-summary"


def test_cached_summarize_survives_singleton_reset() -> None:
    # End-to-end persistence through the public API: compute once, drop the
    # in-process singleton (as a new process would), and confirm the next call
    # is served from disk without recomputing.
    calls = {"n": 0}

    def _compute() -> str:
        calls["n"] += 1
        return "persisted"

    first = llm_cache.cached_summarize("t", model="m", max_tokens=64, backend="openai", compute=_compute)
    llm_cache._reset_store_for_tests()  # simulate a new process: cold in-memory layer
    second = llm_cache.cached_summarize("t", model="m", max_tokens=64, backend="openai", compute=_compute)
    assert first == second == "persisted"
    assert calls["n"] == 1  # served from the on-disk DB, not recomputed


def test_store_evicts_least_recently_used_at_row_limit(tmp_path: Path) -> None:
    db = tmp_path / "evict.sqlite"
    store = llm_cache._SummaryStore(db, max_entries=3)
    store.put("a", "1")
    store.put("b", "2")
    store.put("c", "3")
    # Bump 'a' at the SQLite level via a cold-memory reader: the hot layer on
    # `store` would serve 'a' from memory and never refresh its on-disk recency.
    assert llm_cache._SummaryStore(db).get("a") == "1"
    store.put("d", "4")  # over the 3-row limit -> evict the least-recently-used
    fresh = llm_cache._SummaryStore(db)  # cold layer, reads the DB directly
    assert fresh.get("a") == "1"  # protected: most-recently used
    assert fresh.get("b") is None  # evicted: least-recently used
    assert fresh.get("d") == "4"  # just written


def test_configured_max_entries_default() -> None:
    assert llm_cache._configured_max_entries() == llm_cache._DEFAULT_MAX_ENTRIES
    assert llm_cache._DEFAULT_MAX_ENTRIES >= 8192  # generous, not the old tiny 256


def test_configured_max_entries_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_INTERNAL_LLM_CACHE_MAX_ENTRIES", "5000")
    assert llm_cache._configured_max_entries() == 5000


def test_configured_max_entries_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_INTERNAL_LLM_CACHE_MAX_ENTRIES", "not-an-int")
    assert llm_cache._configured_max_entries() == llm_cache._DEFAULT_MAX_ENTRIES


def test_default_db_path_respects_atelier_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path))
    assert llm_cache._default_db_path() == tmp_path / "internal_llm_cache.sqlite"
