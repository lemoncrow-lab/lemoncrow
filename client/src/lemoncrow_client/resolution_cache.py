"""Bounded, revision-scoped soft caching for the thin client.

This is deliberately *not* a local code index. The client never derives
symbols, embeddings, graph edges or rankings. It remembers only complete
server responses that the server has explicitly marked reusable for the exact
session/view/revision/tool/arguments tuple. Every cache hit still goes to the
server with the opaque validator first, so authentication, authorization,
audit and accounting remain server-side; the server can then answer "reuse
what you already have" without materializing the workspace or executing the
index query again.

The budget is an eviction ceiling, not a reservation. By default it is 10% of
currently available memory, bounded by the process/container headroom and a
4 GiB automatic ceiling. A 16 GiB machine with 16 GiB genuinely available
therefore gets about 1.6 GiB of potential cache, but a fresh process consumes
almost none of it until useful responses arrive.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CACHEABLE_TOOLS",
    "CacheKey",
    "CacheStats",
    "CachedResult",
    "ResolutionCache",
    "available_memory_bytes",
    "cache_budget_bytes",
    "make_cache_key",
]

_KIB: Final[int] = 1024
_MIB: Final[int] = 1024 * _KIB
_GIB: Final[int] = 1024 * _MIB
_AUTO_FRACTION: Final[float] = 0.10
_MIN_AUTO_BUDGET: Final[int] = 8 * _MIB
_MAX_AUTO_BUDGET: Final[int] = 4 * _GIB
_FALLBACK_BUDGET: Final[int] = 256 * _MIB
_MAX_EXPLICIT_BUDGET: Final[int] = 16 * _GIB
_MAX_ENTRIES: Final[int] = 16_384
_MAX_SINGLE_ENTRY: Final[int] = 64 * _MIB

#: Only deterministic, revision-bound code-resolution surfaces participate.
#: Dynamic/user-state tools (memory, web_fetch, verify, context, etc.) remain
#: uncached even if their current implementation happens to be deterministic.
CACHEABLE_TOOLS: Final[frozenset[str]] = frozenset({"code_search", "read", "relations", "search"})


@dataclass(frozen=True, slots=True)
class CacheKey:
    session_id: str
    view_id: str
    view_revision: int
    tool: str
    arguments_digest: str


@dataclass(frozen=True, slots=True)
class CachedResult:
    payload: Mapping[str, Any]
    validator: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CacheStats:
    budget_bytes: int
    bytes_used: int
    entries: int
    hits: int
    misses: int
    evictions: int


class ResolutionCache:
    """Memory-only LRU of server-validated tool responses."""

    __slots__ = ("_budget", "_bytes", "_entries", "_evictions", "_hits", "_items", "_misses")

    def __init__(self, budget_bytes: int, *, max_entries: int = _MAX_ENTRIES) -> None:
        self._budget = max(0, int(budget_bytes))
        self._entries = max(1, int(max_entries))
        self._items: OrderedDict[CacheKey, CachedResult] = OrderedDict()
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @property
    def enabled(self) -> bool:
        return self._budget > 0

    @property
    def stats(self) -> CacheStats:
        return CacheStats(
            budget_bytes=self._budget,
            bytes_used=self._bytes,
            entries=len(self._items),
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
        )

    def get(self, key: CacheKey | None) -> CachedResult | None:
        if key is None or not self.enabled:
            return None
        entry = self._items.get(key)
        if entry is None:
            self._misses += 1
            return None
        self._items.move_to_end(key)
        self._hits += 1
        return entry

    def put(self, key: CacheKey | None, payload: Mapping[str, Any], validator: str) -> None:
        if key is None or not self.enabled or not validator:
            return
        snapshot: dict[str, Any] = {str(name): value for name, value in payload.items()}
        size = _wire_size(snapshot)
        if size <= 0:
            return
        single_limit = min(_MAX_SINGLE_ENTRY, max(1, self._budget // 4))
        if size > single_limit:
            return
        previous = self._items.pop(key, None)
        if previous is not None:
            self._bytes -= previous.size_bytes
        self._items[key] = CachedResult(payload=snapshot, validator=validator, size_bytes=size)
        self._bytes += size
        self._trim()

    def prune_scope(self, session_id: str, view_id: str, view_revision: int) -> None:
        """Drop entries that cannot possibly match the client's current view."""
        stale = [
            key
            for key in self._items
            if key.session_id != session_id or key.view_id != view_id or key.view_revision != view_revision
        ]
        for key in stale:
            self._drop(key, eviction=False)

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    def _trim(self) -> None:
        while self._items and (self._bytes > self._budget or len(self._items) > self._entries):
            key = next(iter(self._items))
            self._drop(key, eviction=True)

    def _drop(self, key: CacheKey, *, eviction: bool) -> None:
        entry = self._items.pop(key, None)
        if entry is None:
            return
        self._bytes = max(0, self._bytes - entry.size_bytes)
        if eviction:
            self._evictions += 1


def make_cache_key(
    *,
    session_id: str,
    view_id: str,
    view_revision: int,
    tool: str,
    arguments: Mapping[str, Any],
) -> CacheKey | None:
    if tool not in CACHEABLE_TOOLS or not session_id or not view_id:
        return None
    try:
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
    return CacheKey(
        session_id=session_id,
        view_id=view_id,
        view_revision=view_revision,
        tool=tool,
        arguments_digest=digest,
    )


def cache_budget_bytes(override: str | None = None, *, available_bytes: int | None = None) -> int:
    """Return the response-cache ceiling.

    ``override`` is ``LEMONCROW_RESOLUTION_CACHE_BYTES``. It accepts bytes or a
    K/M/G suffix; ``0`` disables the cache. Invalid values fail soft to the
    automatic budget, just like the timeout configuration does.
    """
    if override is not None and override.strip():
        explicit = _parse_size(override)
        if explicit is not None:
            return min(explicit, _MAX_EXPLICIT_BUDGET)
    available = available_bytes if available_bytes is not None else available_memory_bytes()
    if available is None or available <= 0:
        return _FALLBACK_BUDGET
    target = int(available * _AUTO_FRACTION)
    # On a memory-starved process, never let the floor itself become pressure.
    pressure_cap = max(_MIN_AUTO_BUDGET, available // 4)
    return min(max(target, _MIN_AUTO_BUDGET), _MAX_AUTO_BUDGET, pressure_cap)


def available_memory_bytes() -> int | None:
    """Best-effort available RAM, respecting a tighter Linux cgroup limit."""
    candidates = [value for value in (_proc_available(), _sysconf_available(), _cgroup_available()) if value]
    return min(candidates) if candidates else None


def _proc_available() -> int | None:
    try:
        rows = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for row in rows:
        if not row.startswith("MemAvailable:"):
            continue
        fields = row.split()
        if len(fields) >= 2:
            try:
                value = int(fields[1]) * _KIB
            except ValueError:
                return None
            return value if value > 0 else None
    return None


def _sysconf_available() -> int | None:
    try:
        pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    value = pages * page_size
    return value if value > 0 else None


def _cgroup_available() -> int | None:
    candidates: list[int] = []
    for limit_path, current_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    ):
        limit = _read_memory_number(limit_path)
        current = _read_memory_number(current_path)
        if limit is not None and current is not None and limit > current:
            candidates.append(limit - current)
    return min(candidates) if candidates else None


def _read_memory_number(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # cgroup v1 often reports an enormous sentinel when memory is unlimited.
    if value <= 0 or value >= (1 << 60):
        return None
    return value


def _parse_size(raw: str) -> int | None:
    text = raw.strip().lower()
    units = {
        "gib": _GIB,
        "gb": _GIB,
        "g": _GIB,
        "mib": _MIB,
        "mb": _MIB,
        "m": _MIB,
        "kib": _KIB,
        "kb": _KIB,
        "k": _KIB,
        "b": 1,
        "": 1,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if suffix and not text.endswith(suffix):
            continue
        number = text[: -len(suffix)] if suffix else text
        try:
            value = float(number)
        except ValueError:
            continue
        if value < 0:
            return None
        return int(value * units[suffix])
    return None


def _wire_size(payload: Mapping[str, Any]) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return 0
