"""Bounded in-memory drafts for Review captures that span several requests.

A capture that fits one request never touches this. When the client's packet
does not fit, its first request opens a draft carrying the packet head and the
first files, later requests append files in order, and the last one commits by
returning the assembled body to the ordinary capture path -- so a Review is
validated and stored exactly once, from the complete packet.

A draft is a cache of an upload in flight, never a Review: it is bound to the
principal, organization and repository that opened it, expires on its own, and
all open drafts share one aggregate byte budget in addition to the count cap.
Losing one (restart, expiry) costs the client a fresh capture, not data.

lc-debt: process-local, so a multi-replica deployment must route one capture's
requests to one replica; upgrade path is a durable draft table.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from .errors import AgentAction, ErrorCode, ServerError

__all__ = ["ReviewDrafts"]

_TTL_S: Final[float] = 900.0


@dataclass(slots=True)
class _Draft:
    org_id: str
    subject: str
    repo_id: str
    body: dict[str, Any]
    next_seq: int
    size: int
    expires: float


class ReviewDrafts:
    __slots__ = ("_clock", "_drafts", "_max_bytes", "_max_drafts", "_ttl_s")

    def __init__(
        self,
        *,
        max_drafts: int,
        max_bytes: int,
        ttl_s: float = _TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_drafts = max_drafts
        self._max_bytes = max_bytes
        self._ttl_s = ttl_s
        self._clock = clock
        self._drafts: dict[str, _Draft] = {}

    def accept(
        self,
        *,
        org_id: str,
        subject: str,
        repo_id: str,
        draft_id: str | None,
        body: dict[str, Any],
        size: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return ``(complete body, None)`` to capture now, or ``(None, ack)``."""
        now = self._clock()
        for stale in [key for key, draft in self._drafts.items() if draft.expires <= now]:
            del self._drafts[stale]
        if draft_id is None:
            if not body.pop("draft", False):
                return body, None
            return None, self._open(org_id, subject, repo_id, body, size, now)
        draft = self._drafts.get(draft_id)
        if draft is None or (draft.org_id, draft.subject, draft.repo_id) != (org_id, subject, repo_id):
            # Another caller's draft reads exactly like an expired one.
            raise ServerError(
                ErrorCode.REVIEW_UNKNOWN,
                "the review draft is unknown or expired",
                details={"field": "draft_id"},
                action=AgentAction.ABANDON,
            )
        seq, files, blobs = body.get("seq"), body.get("files"), body.get("new_blobs", {})
        if seq != draft.next_seq or isinstance(seq, bool):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "the review draft chunk is out of order",
                details={"field": "seq", "expected": draft.next_seq},
                action=AgentAction.FIX_REQUEST,
            )
        if (
            not isinstance(files, list)
            or not all(isinstance(item, dict) for item in files)
            or not isinstance(blobs, dict)
            or not isinstance(body.get("final"), bool)
        ):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "a review draft chunk needs files (list), new_blobs (object) and final (bool)",
                details={"field": "files"},
                action=AgentAction.FIX_REQUEST,
            )
        self._charge(draft_id, draft, size)
        draft.body["packet"]["files"].extend(files)
        draft.body["new_blobs"].update(blobs)
        if body["final"]:
            del self._drafts[draft_id]
            return draft.body, None
        draft.next_seq += 1
        draft.expires = now + self._ttl_s
        return None, {"draft_id": draft_id, "next_seq": draft.next_seq}

    def _open(
        self, org_id: str, subject: str, repo_id: str, body: dict[str, Any], size: int, now: float
    ) -> dict[str, Any]:
        packet = body.get("packet")
        if not isinstance(packet, dict) or not isinstance(packet.get("files"), list):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "a review draft needs a packet with a files list",
                details={"field": "packet"},
                action=AgentAction.FIX_REQUEST,
            )
        if not isinstance(body.get("new_blobs"), dict):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "new_blobs must be an object",
                details={"field": "new_blobs"},
                action=AgentAction.FIX_REQUEST,
            )
        if len(self._drafts) >= self._max_drafts:
            raise ServerError(
                ErrorCode.CONCURRENCY_LIMIT,
                "too many review uploads are in progress",
                action=AgentAction.RETRY_LATER,
            )
        draft_id = f"rdraft_{secrets.token_hex(16)}"
        draft = _Draft(org_id, subject, repo_id, body, 1, 0, now + self._ttl_s)
        self._charge(draft_id, draft, size)
        self._drafts[draft_id] = draft
        return {"draft_id": draft_id, "next_seq": 1}

    def _charge(self, draft_id: str, draft: _Draft, size: int) -> None:
        incoming = max(size, 0)
        new_size = draft.size + incoming
        retained_elsewhere = sum(row.size for key, row in self._drafts.items() if key != draft_id)
        retained_total = retained_elsewhere + new_size
        if retained_total > self._max_bytes:
            self._drafts.pop(draft_id, None)
            raise ServerError(
                ErrorCode.REQUEST_TOO_LARGE,
                "the review upload exceeds the server draft-memory limit",
                details={"limit": self._max_bytes, "declared": retained_total},
                action=AgentAction.SPLIT_REQUEST,
            )
        draft.size = new_size
