"""Persistent content-addressed cache for internal-LLM results.

Internal-LLM calls (summaries for background compaction, consolidation, etc.)
are effectively pure functions of ``(text, model, max_tokens, backend)``: the
same input yields an equivalent summary, so recomputing it burns provider tokens
for nothing.

Results are persisted to a dedicated SQLite sidecar (``internal_llm_cache.sqlite``)
under the LemonCrow root, so a summary computed in one process or session is reused
by the next instead of recomputed cold. A small in-memory LRU sits in front to
avoid a disk read on repeat hits within a process. Capacity is bounded by row
count (LRU eviction); tune it with ``LEMONCROW_INTERNAL_LLM_CACHE_MAX_ENTRIES`` and
disable caching entirely with ``LEMONCROW_INTERNAL_LLM_CACHE=0``.

Self-contained (stdlib only) on purpose: this module lives in the infra layer and
must not import from ``core/`` or ``gateway/``, so it resolves the LemonCrow root
from the same ``LEMONCROW_ROOT`` / ``LEMONCROW_STORE_ROOT`` env convention used
elsewhere rather than importing ``core.foundation.paths``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

_DB_FILENAME = "internal_llm_cache.sqlite"

# Generous default: cached summaries are small strings and most machines have
# ample disk/RAM, so a large entry count costs little while greatly improving hit
# rate across sessions. Override per-process with
# LEMONCROW_INTERNAL_LLM_CACHE_MAX_ENTRIES.
_DEFAULT_MAX_ENTRIES = 16384

# Cap on the in-memory hot layer that fronts SQLite (per process).
_MEM_LAYER_CAP = 1024


def _enabled() -> bool:
    return os.environ.get("LEMONCROW_INTERNAL_LLM_CACHE", "1") != "0"


def _configured_max_entries() -> int:
    raw = os.environ.get("LEMONCROW_INTERNAL_LLM_CACHE_MAX_ENTRIES")
    if raw is None:
        return _DEFAULT_MAX_ENTRIES
    try:
        configured = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ENTRIES
    return max(1, configured)


def _lemoncrow_root() -> Path:
    root_env = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root_env) if root_env else Path.home() / ".lemoncrow"


def _default_db_path() -> Path:
    return _lemoncrow_root() / _DB_FILENAME


class _SummaryStore:
    """SQLite-backed summary cache with a small in-memory LRU front layer.

    Thread-safe: a fresh connection per operation (sqlite3 connections are not
    shareable across threads) plus an in-process lock guarding the memory layer.
    Multiprocess-safe via WAL journaling + upsert. Eviction is LRU by row count.

    A get served from the in-memory hot layer does not refresh the row's on-disk
    recency, so SQLite-level eviction approximates LRU from writes and cold reads
    -- acceptable for a cost-saving cache where evicting a still-warm entry only
    costs one recomputation.
    """

    def __init__(self, db_path: str | Path, *, max_entries: int | None = None) -> None:
        resolved = max_entries if max_entries is not None else _configured_max_entries()
        self.db_path = Path(db_path)
        self._max_entries = max(1, resolved)
        self._mem: OrderedDict[str, str] = OrderedDict()
        self._mem_cap = min(self._max_entries, _MEM_LAYER_CAP)
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            hot = self._mem.get(key)
            if hot is not None:
                self._mem.move_to_end(key)
                return hot
        with contextlib.closing(self._connect()) as conn:
            self._init_schema(conn)
            row = conn.execute(
                "SELECT value FROM internal_llm_summary_cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE internal_llm_summary_cache "
                "SET hit_count = hit_count + 1, "
                "last_hit_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE key = ?",
                (key,),
            )
            conn.commit()
            value = str(row["value"])
        self._remember(key, value)
        return value

    def put(self, key: str, value: str) -> None:
        with contextlib.closing(self._connect()) as conn:
            self._init_schema(conn)
            conn.execute(
                "INSERT INTO internal_llm_summary_cache(key, value, hit_count, last_hit_at) "
                "VALUES (?, ?, 0, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, last_hit_at = excluded.last_hit_at",
                (key, value),
            )
            self._evict(conn)
            conn.commit()
        self._remember(key, value)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS internal_llm_summary_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at TEXT NOT NULL
            )
            """)

    def _evict(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) AS n FROM internal_llm_summary_cache").fetchone()
        count = int(row["n"]) if row is not None else 0
        overflow = count - self._max_entries
        if overflow > 0:
            conn.execute(
                "DELETE FROM internal_llm_summary_cache WHERE key IN ("
                "SELECT key FROM internal_llm_summary_cache "
                "ORDER BY last_hit_at ASC, hit_count ASC, key ASC LIMIT ?)",
                (overflow,),
            )

    def _remember(self, key: str, value: str) -> None:
        with self._lock:
            self._mem[key] = value
            self._mem.move_to_end(key)
            while len(self._mem) > self._mem_cap:
                self._mem.popitem(last=False)


_store_lock = threading.Lock()
_SUMMARY_STORE: _SummaryStore | None = None


def _store() -> _SummaryStore:
    global _SUMMARY_STORE
    with _store_lock:
        if _SUMMARY_STORE is None:
            _SUMMARY_STORE = _SummaryStore(_default_db_path())
        return _SUMMARY_STORE


def _reset_store_for_tests() -> None:
    """Drop the cached singleton so the next call rebuilds it (test isolation)."""
    global _SUMMARY_STORE
    with _store_lock:
        _SUMMARY_STORE = None


def _endpoint_fingerprint(backend: str) -> str:
    """Identity of the endpoint a summary was produced against.

    The OpenAI-compatible backend resolves its base URL and API key from env at
    call time, so the same ``(text, model, max_tokens, backend)`` can route to
    different providers (OpenAI direct, OpenRouter, a local vllm). Folding the
    base URL and an API-key *fingerprint* (a hash -- never the raw secret) into
    the cache key prevents one endpoint's summary from being served for another.
    """
    if backend not in ("openai", "openai_compatible"):
        return ""
    base_url = os.environ.get("LEMONCROW_OPENAI_BASE_URL") or ""
    api_key = os.environ.get("LEMONCROW_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    key_fp = hashlib.sha256(api_key.encode("utf-8")).hexdigest() if api_key else ""
    return f"{base_url}\x00{key_fp}"


def summary_key(text: str, *, model: str | None, max_tokens: int, backend: str) -> str:
    payload = f"{backend}\x00{_endpoint_fingerprint(backend)}\x00{model or ''}\x00{max_tokens}\x00{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_summarize(
    text: str,
    *,
    model: str | None,
    max_tokens: int,
    backend: str,
    compute: Callable[[], str],
) -> str:
    """Return a cached summary for identical inputs, else compute and store it.

    Only successful results are cached -- if ``compute`` raises, the exception
    propagates and nothing is stored.
    """
    if not _enabled():
        return compute()
    store = _store()
    key = summary_key(text, model=model, max_tokens=max_tokens, backend=backend)
    cached = store.get(key)
    if cached is not None:
        return cached
    value = compute()
    store.put(key, value)
    return value


__all__ = ["cached_summarize", "summary_key"]
