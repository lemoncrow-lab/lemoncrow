"""Typed errors the calling agent can branch on.

The thin client is a dumb pipe: whatever the server says is what the agent
sees. A bare HTTP status is not actionable -- "403" does not tell an agent
whether to re-authenticate, ask for access, or give up -- so every refusal
carries three machine-readable fields:

``code``
    A stable member of :class:`ErrorCode`. Never free text.
``retryable``
    Whether repeating the identical request can succeed.
``action``
    The single next step that resolves it, from :class:`AgentAction`.

``message`` exists for humans reading a transcript and is deliberately
generated from the code plus non-sensitive identifiers. Content, source bytes
and repository paths never appear in it; ``details`` is restricted to scalars
the client already knows (protocol versions, revisions, counts, limits).

The one protocol answer that is *not* an error is the blob miss: a tool that
needs content the server does not have answers ``{"need": [...]}`` with HTTP
200 (see :mod:`lemoncrow_server_core.blobmiss`). Only the *second*
unsatisfied attempt becomes :attr:`ErrorCode.BLOB_MISSING`.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "AgentAction",
    "ErrorCode",
    "ServerError",
    "error_payload",
]


class ErrorCode(StrEnum):
    """Every refusal this server can emit."""

    # Handshake / protocol
    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    PAYLOAD_INVALID = "payload_invalid"

    # Identity
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_CONFIGURED = "not_configured"

    # Session / view lifecycle
    SESSION_UNKNOWN = "session_unknown"
    SESSION_EXPIRED = "session_expired"
    VIEW_UNKNOWN = "view_unknown"
    VIEW_REVISION_STALE = "view_revision_stale"
    VIEW_REVISION_UNACKNOWLEDGED = "view_revision_unacknowledged"
    VIEW_REVISION_CONFLICT = "view_revision_conflict"
    REVIEW_UNKNOWN = "review_unknown"
    REVIEW_STATE_CONFLICT = "review_state_conflict"

    # Content sync
    BLOB_MISSING = "blob_missing"
    BLOB_DIGEST_MISMATCH = "blob_digest_mismatch"
    MANIFEST_INCOMPLETE = "manifest_incomplete"

    # Tool surface
    PATH_NOT_ADDRESSABLE = "path_not_addressable"
    TOOL_UNKNOWN = "tool_unknown"
    TOOL_NOT_SERVER_SIDE = "tool_not_server_side"
    TOOL_FAILED = "tool_failed"
    DISPATCHER_UNAVAILABLE = "dispatcher_unavailable"

    # Resource governance
    REQUEST_TOO_LARGE = "request_too_large"
    CONCURRENCY_LIMIT = "concurrency_limit"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    SHUTTING_DOWN = "shutting_down"

    INTERNAL = "internal"


class AgentAction(StrEnum):
    """The resolution step an agent (or the client on its behalf) should take."""

    NONE = "none"
    UPGRADE_CLIENT = "upgrade_client"
    REAUTHENTICATE = "reauthenticate"
    REQUEST_ACCESS = "request_access"
    REOPEN_SESSION = "reopen_session"
    REOPEN_VIEW = "reopen_view"
    REFRESH_VIEW_REVISION = "refresh_view_revision"
    UPLOAD_BLOBS = "upload_blobs"
    USE_SYNC_PROTOCOL = "use_sync_protocol"
    USE_CLIENT_TOOL = "use_client_tool"
    RETRY_LATER = "retry_later"
    SPLIT_REQUEST = "split_request"
    FIX_REQUEST = "fix_request"
    ABANDON = "abandon"


# code -> (http status, retryable by default, default agent action)
_SPEC: Final[Mapping[ErrorCode, tuple[int, bool, AgentAction]]] = {
    ErrorCode.PROTOCOL_VERSION_MISMATCH: (426, False, AgentAction.UPGRADE_CLIENT),
    ErrorCode.CAPABILITY_UNSUPPORTED: (400, False, AgentAction.UPGRADE_CLIENT),
    ErrorCode.PAYLOAD_INVALID: (400, False, AgentAction.FIX_REQUEST),
    ErrorCode.UNAUTHENTICATED: (401, False, AgentAction.REAUTHENTICATE),
    ErrorCode.FORBIDDEN: (403, False, AgentAction.REQUEST_ACCESS),
    ErrorCode.NOT_CONFIGURED: (501, False, AgentAction.ABANDON),
    ErrorCode.SESSION_UNKNOWN: (404, False, AgentAction.REOPEN_SESSION),
    ErrorCode.SESSION_EXPIRED: (401, False, AgentAction.REOPEN_SESSION),
    ErrorCode.VIEW_UNKNOWN: (404, False, AgentAction.REOPEN_VIEW),
    ErrorCode.VIEW_REVISION_STALE: (409, True, AgentAction.REFRESH_VIEW_REVISION),
    ErrorCode.VIEW_REVISION_UNACKNOWLEDGED: (409, False, AgentAction.REFRESH_VIEW_REVISION),
    ErrorCode.VIEW_REVISION_CONFLICT: (409, True, AgentAction.REFRESH_VIEW_REVISION),
    ErrorCode.REVIEW_UNKNOWN: (404, False, AgentAction.ABANDON),
    ErrorCode.REVIEW_STATE_CONFLICT: (409, True, AgentAction.FIX_REQUEST),
    ErrorCode.BLOB_MISSING: (409, False, AgentAction.UPLOAD_BLOBS),
    ErrorCode.BLOB_DIGEST_MISMATCH: (400, False, AgentAction.FIX_REQUEST),
    # Not retryable, and not a reopen either. It means the rows this client
    # delivered for the chunks it announced do not hash to the root it
    # announced with them -- repeating the request cannot change that, and
    # neither can a fresh view, because the same walk produces the same rows.
    # The step that resolves it is re-running the sync protocol: walk the
    # worktree again and announce the root those rows actually produce.
    ErrorCode.MANIFEST_INCOMPLETE: (409, False, AgentAction.USE_SYNC_PROTOCOL),
    ErrorCode.PATH_NOT_ADDRESSABLE: (400, False, AgentAction.FIX_REQUEST),
    ErrorCode.TOOL_UNKNOWN: (404, False, AgentAction.ABANDON),
    ErrorCode.TOOL_NOT_SERVER_SIDE: (400, False, AgentAction.USE_CLIENT_TOOL),
    ErrorCode.TOOL_FAILED: (200, False, AgentAction.NONE),
    ErrorCode.DISPATCHER_UNAVAILABLE: (503, True, AgentAction.RETRY_LATER),
    ErrorCode.REQUEST_TOO_LARGE: (413, False, AgentAction.SPLIT_REQUEST),
    ErrorCode.CONCURRENCY_LIMIT: (429, True, AgentAction.RETRY_LATER),
    ErrorCode.DEADLINE_EXCEEDED: (504, True, AgentAction.RETRY_LATER),
    ErrorCode.CANCELLED: (499, False, AgentAction.NONE),
    ErrorCode.SHUTTING_DOWN: (503, True, AgentAction.RETRY_LATER),
    ErrorCode.INTERNAL: (500, False, AgentAction.RETRY_LATER),
}

_ALLOWED_DETAIL_TYPES: Final[tuple[type, ...]] = (str, int, float, bool)


def _clean_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep scalars and flat scalar lists; drop anything that could carry content.

    Nested objects are where a well-meaning caller would eventually attach a
    tool result or a file body. Refusing them structurally is cheaper than
    auditing every call site.
    """
    if not details:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in details.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, bool) or isinstance(value, (str, int, float)):
            cleaned[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(item, _ALLOWED_DETAIL_TYPES) for item in value):
            cleaned[key] = list(value)
    return cleaned


class ServerError(Exception):
    """A refusal with a stable code, an HTTP status and an agent-actionable hint."""

    __slots__ = ("action", "code", "details", "message", "retryable", "status")

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
        action: AgentAction | None = None,
        status: int | None = None,
    ) -> None:
        default_status, default_retryable, default_action = _SPEC[code]
        self.code: ErrorCode = code
        self.message: str = message
        self.details: dict[str, Any] = _clean_details(details)
        self.retryable: bool = default_retryable if retryable is None else retryable
        self.action: AgentAction = default_action if action is None else action
        self.status: int = default_status if status is None else status
        super().__init__(f"{code.value}: {message}")

    def to_wire(self) -> dict[str, Any]:
        return error_payload(
            self.code,
            self.message,
            details=self.details,
            retryable=self.retryable,
            action=self.action,
        )


def error_payload(
    code: ErrorCode,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
    retryable: bool | None = None,
    action: AgentAction | None = None,
) -> dict[str, Any]:
    """Build the on-the-wire error envelope."""
    _, default_retryable, default_action = _SPEC[code]
    return {
        "error": {
            "code": code.value,
            "message": message,
            "retryable": default_retryable if retryable is None else retryable,
            "action": (default_action if action is None else action).value,
            "details": _clean_details(details),
        }
    }


def http_status_for(code: ErrorCode) -> int:
    return _SPEC[code][0]
