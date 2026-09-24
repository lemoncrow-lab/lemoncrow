"""Version and capability handshake.

The design's degradation table says a version mismatch must "refuse with an
explicit message; never degrade silently". That is the whole point of this
module: negotiation either returns a fully-resolved :class:`Negotiation` or
raises :class:`~lemoncrow_server.errors.ServerError` with
``protocol_version_mismatch``. There is no partial mode, no best-effort
downgrade and no implicit feature sniffing.

The protocol version is a date string, bumped whenever a wire shape changes in
a way an older client cannot parse. Capabilities are additive feature names
negotiated per session, so a capability can ship without a protocol bump.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .errors import AgentAction, ErrorCode, ServerError

__all__ = [
    "PROTOCOL_VERSION",
    "REQUIRED_CLIENT_CAPABILITIES",
    "RESULT_REUSE_CAPABILITY",
    "SERVER_CAPABILITIES",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "Negotiation",
    "negotiate",
    "server_identity",
]

#: Wire protocol this build speaks.
PROTOCOL_VERSION: Final[str] = "2026-09-14"

#: Every protocol version this build still accepts from a client.
SUPPORTED_PROTOCOL_VERSIONS: Final[frozenset[str]] = frozenset({PROTOCOL_VERSION})

#: Optional client-held payload reuse. The server validates an opaque token
#: before allowing the client to reuse a response it already has; the server
#: never trusts a client cache as code/index state.
RESULT_REUSE_CAPABILITY: Final[str] = "result_reuse.v1"

#: Optional features the server offers. A client opts in by listing them.
SERVER_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "tools.v1",  # POST /v1/tools/{name}
        "views.v1",  # POST /v1/views/open, manifest-chunks, overlay, close
        "blobs.v1",  # POST /v1/blobs
        "overlay.v1",  # revisioned optimistic overlay writes
        "blob_miss.v1",  # {need:[...]} answer + exactly one retry
        "index_query.v1",  # POST /v1/views/{id}/query: lexical/symbol/relations/semantic
        RESULT_REUSE_CAPABILITY,  # client holds payload; server validates exact reuse
        "stream.ndjson",  # chunked application/x-ndjson tool responses
        "stream.sse",  # chunked text/event-stream tool responses
        "cancel.v1",  # POST /v1/sessions/{id}/cancel
        "local_fs",  # negotiated same-host read optimization
    }
)

#: Capabilities a client MUST declare. ``blob_miss.v1`` is required because a
#: client that cannot answer ``{need:[...]}`` turns every cold read into a hard
#: failure, and the server has no way to tell that apart from a broken client.
REQUIRED_CLIENT_CAPABILITIES: Final[frozenset[str]] = frozenset({"blob_miss.v1"})

#: ``local_fs`` is a same-host optimization only. Granting it needs both a
#: client offer and a server-side allowance; CI runs conformance with it off.
_SAME_HOST_CAPABILITY: Final[str] = "local_fs"

_MAX_CLIENT_CAPABILITIES: Final[int] = 64
_MAX_NAME_LEN: Final[int] = 64


@dataclass(frozen=True, slots=True)
class Negotiation:
    """The settled result of one handshake."""

    protocol_version: str
    capabilities: frozenset[str]
    client_name: str
    client_version: str
    local_fs: bool

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "capabilities": sorted(self.capabilities),
            "local_fs": self.local_fs,
        }


def _clean_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be a non-empty string",
            details={"field": field},
        )
    text = value.strip()
    if len(text) > _MAX_NAME_LEN:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} exceeds {_MAX_NAME_LEN} characters",
            details={"field": field, "limit": _MAX_NAME_LEN},
        )
    return text


def negotiate(
    body: Mapping[str, Any],
    *,
    allow_local_fs: bool = False,
) -> Negotiation:
    """Resolve a client handshake or refuse loudly.

    ``allow_local_fs`` is the server-side half of the same-host optimization.
    A client offering ``local_fs`` against a server that does not allow it gets
    a granted capability set without ``local_fs`` -- an optimization declined is
    not an error -- but every *required* capability it fails to offer is.
    """
    client_protocol = body.get("protocol_version")
    if not isinstance(client_protocol, str) or not client_protocol:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "protocol_version is required",
            details={"field": "protocol_version", "server_protocol": PROTOCOL_VERSION},
        )
    if client_protocol not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ServerError(
            ErrorCode.PROTOCOL_VERSION_MISMATCH,
            (
                f"client protocol {client_protocol!r} is not supported by this server "
                f"(supported: {', '.join(sorted(SUPPORTED_PROTOCOL_VERSIONS))})"
            ),
            details={
                "client_protocol": client_protocol,
                "server_protocol": PROTOCOL_VERSION,
                "supported": sorted(SUPPORTED_PROTOCOL_VERSIONS),
            },
            action=AgentAction.UPGRADE_CLIENT,
        )

    raw_caps = body.get("capabilities", [])
    if not isinstance(raw_caps, (list, tuple)):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "capabilities must be an array of strings",
            details={"field": "capabilities"},
        )
    if len(raw_caps) > _MAX_CLIENT_CAPABILITIES:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"capabilities exceeds {_MAX_CLIENT_CAPABILITIES} entries",
            details={"field": "capabilities", "limit": _MAX_CLIENT_CAPABILITIES},
        )
    offered: set[str] = set()
    for entry in raw_caps:
        offered.add(_clean_name(entry, "capabilities[]"))

    missing = sorted(REQUIRED_CLIENT_CAPABILITIES - offered)
    if missing:
        raise ServerError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            f"client does not offer required capabilities: {', '.join(missing)}",
            details={"missing": missing, "required": sorted(REQUIRED_CLIENT_CAPABILITIES)},
            action=AgentAction.UPGRADE_CLIENT,
        )

    granted = (offered & SERVER_CAPABILITIES) | REQUIRED_CLIENT_CAPABILITIES
    local_fs = _SAME_HOST_CAPABILITY in granted and allow_local_fs
    if not local_fs:
        granted.discard(_SAME_HOST_CAPABILITY)

    return Negotiation(
        protocol_version=client_protocol,
        capabilities=frozenset(granted),
        client_name=_clean_name(body.get("client_name", "unknown"), "client_name"),
        client_version=_clean_name(body.get("client_version", "0"), "client_version"),
        local_fs=local_fs,
    )


def server_identity(
    *,
    allow_local_fs: bool,
    name: str = "lemoncrow-server",
    extra_endpoints: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The unauthenticated discovery document.

    It advertises protocol and capability names only. No tenant, principal,
    repository, view or content fact is reachable from here, so it is safe to
    serve before authentication -- which is what lets a client fail fast on a
    version mismatch instead of failing confusingly on a 401.
    """
    capabilities = set(SERVER_CAPABILITIES)
    if not allow_local_fs:
        capabilities.discard(_SAME_HOST_CAPABILITY)
    endpoints = {
        "handshake": "/v1/handshake",
        "session_open": "/v1/sessions",
        "tools": "/v1/tools",
        "views_open": "/v1/views/open",
        "blobs": "/v1/blobs",
    }
    if extra_endpoints:
        endpoints.update(extra_endpoints)
    return {
        "name": name,
        "protocol_version": PROTOCOL_VERSION,
        "supported_protocol_versions": sorted(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": sorted(capabilities),
        "required_client_capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
        "endpoints": endpoints,
    }
