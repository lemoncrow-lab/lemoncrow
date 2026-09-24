"""Deployment-neutral request limits, cancellation, and global concurrency.

These are engine mechanics shared by the public loopback server and the hosted
composition. Tenant fairness does not live here: the enterprise package extends
:class:`Limits` with a per-organization ceiling and supplies its own concurrency
gate. The shared handlers only depend on the small ``hold(scope_id)`` shape, so
local mode never needs to know what an organization is.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from .errors import ErrorCode, ServerError

__all__ = [
    "CancellationRegistry",
    "CancellationToken",
    "ConcurrencyGate",
    "Limits",
]

_MIB: Final[int] = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Limits:
    """Static ceilings required by the shared server engine."""

    max_request_bytes: int = 16 * _MIB
    max_blob_bytes: int = 8 * _MIB
    max_indexed_content_bytes: int = _MIB
    inline_rebuild_budget: int = 5_000
    max_query_results: int = 200
    max_review_draft_bytes: int = 64 * _MIB
    max_review_drafts: int = 8
    max_blobs_per_request: int = 256
    max_manifest_entries_per_chunk: int = 5_000
    max_overlay_entries: int = 256
    max_need_paths: int = 256
    max_concurrent_requests: int = 64
    dispatch_workers: int = 16
    tool_deadline_s: float = 120.0
    stream_chunk_bytes: int = 32 * 1024
    max_sessions: int = 4_096
    max_open_views: int = 16_384
    max_materialized_views: int = 64
    materialized_bytes_quota: int = 2 * 1024 * _MIB

    def __post_init__(self) -> None:
        for name in (
            "max_request_bytes",
            "max_blob_bytes",
            "max_indexed_content_bytes",
            "max_review_draft_bytes",
            "max_review_drafts",
            "inline_rebuild_budget",
            "max_query_results",
            "max_blobs_per_request",
            "max_manifest_entries_per_chunk",
            "max_overlay_entries",
            "max_need_paths",
            "max_concurrent_requests",
            "dispatch_workers",
            "stream_chunk_bytes",
            "max_sessions",
            "max_open_views",
            "max_materialized_views",
            "materialized_bytes_quota",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.tool_deadline_s <= 0:
            raise ValueError("tool_deadline_s must be > 0")


class ConcurrencyGate:
    """Immediate process-wide in-flight ceiling for the shared/local engine."""

    __slots__ = ("_lock", "_total", "_total_limit")

    def __init__(self, limits: Limits) -> None:
        self._total_limit = limits.max_concurrent_requests
        self._total = 0
        self._lock = asyncio.Lock()

    @property
    def in_flight(self) -> int:
        return self._total

    def snapshot(self) -> Mapping[str, int]:
        """Diagnostic shape compatible with richer deployment gates."""
        return {} if self._total == 0 else {"server": self._total}

    @contextlib.asynccontextmanager
    async def hold(self, scope_id: str = "") -> AsyncIterator[None]:
        """Hold one global slot; ``scope_id`` is accepted for composition parity."""
        del scope_id
        async with self._lock:
            if self._total >= self._total_limit:
                raise ServerError(
                    ErrorCode.CONCURRENCY_LIMIT,
                    "server is at its in-flight request limit",
                    details={"limit": self._total_limit, "scope": "server"},
                )
            self._total += 1
        try:
            yield
        finally:
            async with self._lock:
                self._total -= 1


class CancellationToken:
    """A one-shot cancel flag observable from the request coroutine."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class CancellationRegistry:
    """Session-scoped request cancellation shared by both compositions."""

    __slots__ = ("_tokens",)

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], CancellationToken] = {}

    @contextlib.contextmanager
    def track(self, session_id: str, request_id: str) -> Iterator[CancellationToken]:
        token = CancellationToken()
        key = (session_id, request_id)
        self._tokens[key] = token
        try:
            yield token
        finally:
            self._tokens.pop(key, None)

    def cancel(self, session_id: str, request_id: str) -> bool:
        token = self._tokens.get((session_id, request_id))
        if token is None:
            return False
        token.cancel()
        return True

    def cancel_session(self, session_id: str) -> int:
        cancelled = 0
        for (sid, _rid), token in list(self._tokens.items()):
            if sid == session_id:
                token.cancel()
                cancelled += 1
        return cancelled

    @property
    def tracked(self) -> int:
        return len(self._tokens)
