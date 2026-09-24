"""Opaque validators for client-held, revision-bound tool results.

The server stores no response cache here. A successful deterministic tool call
gets an HMAC over its tenant/session/view/revision/tool/arguments identity. A
client that already holds the corresponding payload may present that validator
on the next identical request. Authentication, authorization and revision
binding happen before validation; a valid token then lets the server skip
workspace materialization and dispatch while still auditing the invocation.

The HMAC key is process-local and random. A restart or load-balancer hop simply
turns a would-be hit into a normal tool execution, which is exactly the safe
failure mode and avoids another distributed cache/state dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from typing import Any, Final

__all__ = ["CACHEABLE_TOOLS", "ResultReuseValidator"]

CACHEABLE_TOOLS: Final[frozenset[str]] = frozenset({"code_search", "read", "relations", "search"})
_PREFIX: Final[str] = "rr1_"


class ResultReuseValidator:
    __slots__ = ("_key",)

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key if key is not None else secrets.token_bytes(32)

    def issue(
        self,
        *,
        org_id: str,
        subject: str,
        session_id: str,
        view_id: str | None,
        view_revision: int,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> str | None:
        if tool not in CACHEABLE_TOOLS or not view_id:
            return None
        try:
            canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError):
            return None
        message = "\x00".join((org_id, subject, session_id, view_id, str(view_revision), tool, canonical)).encode(
            "utf-8", "surrogatepass"
        )
        return _PREFIX + hmac.new(self._key, message, hashlib.sha256).hexdigest()

    @staticmethod
    def matches(presented: object, expected: str | None) -> bool:
        return isinstance(presented, str) and expected is not None and hmac.compare_digest(presented, expected)
