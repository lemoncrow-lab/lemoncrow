"""The typed vocabulary the client branches on.

The server's refusals carry ``code``, ``retryable`` and ``action`` from a
closed vocabulary so an agent can act on them rather than parse prose. This
module is the client's half of that contract: the same two enumerations, plus
the few codes the client itself originates when it never reached the server at
all.

Two rules keep it honest:

* **Nothing is invented.** :class:`ErrorCode` and :class:`AgentAction` mirror
  ``lemoncrow_server.errors`` exactly, and a code the client does not
  recognise is surfaced verbatim as :attr:`ErrorCode.UNKNOWN` rather than
  remapped into something familiar.
* **A refusal is never silently downgraded.** A version mismatch, an
  unreachable server and a stale revision are three different problems with
  three different fixes, and the agent is told which one it has.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

__all__ = [
    "OFFLINE_CODES",
    "AgentAction",
    "ClientError",
    "ErrorCode",
    "error_block",
]


class ErrorCode(StrEnum):
    """Every refusal the client can see or raise.

    The first block mirrors the server verbatim. The last block is
    client-originated: these are the failures where no server answer exists,
    so there is nothing to mirror.
    """

    # -- mirrored from the server ------------------------------------- #
    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    PAYLOAD_INVALID = "payload_invalid"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_CONFIGURED = "not_configured"
    SESSION_UNKNOWN = "session_unknown"
    SESSION_EXPIRED = "session_expired"
    VIEW_UNKNOWN = "view_unknown"
    VIEW_REVISION_STALE = "view_revision_stale"
    VIEW_REVISION_UNACKNOWLEDGED = "view_revision_unacknowledged"
    VIEW_REVISION_CONFLICT = "view_revision_conflict"
    REVIEW_UNKNOWN = "review_unknown"
    REVIEW_STATE_CONFLICT = "review_state_conflict"
    BLOB_MISSING = "blob_missing"
    BLOB_DIGEST_MISMATCH = "blob_digest_mismatch"
    MANIFEST_INCOMPLETE = "manifest_incomplete"
    #: A path argument that cannot name content inside the open view -- host
    #: absolute, ``~``-rooted, drive-qualified, or climbing out with ``..``.
    #: Distinct from ``blob_missing`` on purpose: no upload can satisfy it, so
    #: the agent must fix the argument rather than sync and retry.
    PATH_NOT_ADDRESSABLE = "path_not_addressable"
    TOOL_UNKNOWN = "tool_unknown"
    TOOL_NOT_SERVER_SIDE = "tool_not_server_side"
    TOOL_FAILED = "tool_failed"
    DISPATCHER_UNAVAILABLE = "dispatcher_unavailable"
    REQUEST_TOO_LARGE = "request_too_large"
    CONCURRENCY_LIMIT = "concurrency_limit"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    SHUTTING_DOWN = "shutting_down"
    INTERNAL = "internal"

    # -- client-originated --------------------------------------------- #
    #: No answer came back at all: DNS, connect, TLS, timeout or a socket that
    #: died mid-response. Distinct from every server refusal, because the fix
    #: is connectivity rather than anything about the request.
    SERVER_UNREACHABLE = "server_unreachable"
    #: The session never opened, so there is no server-side surface to call.
    #: The client-side tools are unaffected and the message says so.
    SERVER_SESSION_UNAVAILABLE = "server_session_unavailable"
    #: A local tool needs a program that is not installed. The client never
    #: downloads one, so this is a terminal answer with the name in it.
    LOCAL_TOOL_UNAVAILABLE = "local_tool_unavailable"
    #: A local tool ran and failed.
    LOCAL_TOOL_FAILED = "local_tool_failed"
    #: A code the server sent that this client build does not know. Surfaced
    #: rather than remapped, so an unfamiliar refusal is visible as unfamiliar.
    UNKNOWN = "unknown"


class AgentAction(StrEnum):
    """The resolution step an agent should take. Mirrors the server."""

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


#: The codes that mean "the server is not answering". A tool routed to the
#: server turns one of these into a typed tool error; the client-side tools do
#: not see them at all.
OFFLINE_CODES: frozenset[ErrorCode] = frozenset({ErrorCode.SERVER_UNREACHABLE, ErrorCode.SERVER_SESSION_UNAVAILABLE})


def _coerce_code(value: object) -> ErrorCode:
    if isinstance(value, ErrorCode):
        return value
    try:
        return ErrorCode(str(value))
    except ValueError:
        return ErrorCode.UNKNOWN


def _coerce_action(value: object) -> AgentAction:
    if isinstance(value, AgentAction):
        return value
    try:
        return AgentAction(str(value))
    except ValueError:
        return AgentAction.NONE


class ClientError(Exception):
    """A typed refusal, whether the server produced it or the client did."""

    __slots__ = ("action", "code", "details", "message", "retryable", "server_code")

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        action: AgentAction = AgentAction.NONE,
        server_code: str = "",
    ) -> None:
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.retryable = retryable
        self.action = action
        #: The literal code string the server sent, kept even when it did not
        #: map onto a known :class:`ErrorCode`.
        self.server_code = server_code or code.value
        super().__init__(f"{self.server_code}: {message}")

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any], *, status: int = 0) -> ClientError:
        """Rebuild a refusal from the server's error envelope."""
        raw = payload.get("error")
        body: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        raw_code = body.get("code", "")
        details = body.get("details")
        return cls(
            _coerce_code(raw_code),
            str(body.get("message") or f"server refused with HTTP {status}"),
            details=details if isinstance(details, Mapping) else None,
            retryable=bool(body.get("retryable", False)),
            action=_coerce_action(body.get("action")),
            server_code=str(raw_code or ""),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "code": self.server_code,
            "message": self.message,
            "retryable": self.retryable,
            "action": self.action.value,
            "details": dict(self.details),
        }


def error_block(error: ClientError) -> dict[str, Any]:
    """One MCP content block carrying a refusal the agent can branch on.

    The text form is what a model reads; the JSON line under it is what an
    agent framework parses. Both say the same three things -- code, whether a
    retry can help, and the single next step -- because a refusal the agent
    cannot act on is indistinguishable from a crash.
    """
    return {
        "type": "text",
        "text": (
            f"[lemoncrow:{error.server_code}] {error.message}\n"
            f"retryable={str(error.retryable).lower()} action={error.action.value}\n"
            f"{json.dumps(error.to_wire(), sort_keys=True)}"
        ),
    }
