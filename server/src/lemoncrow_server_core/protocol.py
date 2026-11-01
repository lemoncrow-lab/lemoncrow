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

Support window
--------------
A server serves exactly two protocol versions: the one it speaks (N,
:data:`PROTOCOL_VERSION`) and the one before it (N-1,
:data:`PREVIOUS_PROTOCOL_VERSION`). Inside that window nothing a client relied
on disappears; outside it the refusal names the side to upgrade -- "upgrade the
client" for an older offer, "the server is older; upgrade the server" for a
newer one. The client keeps the same two dates and steps down once from N to
N-1 when a server one version behind refuses N.

Cutting a version retires N-1 in the same change, on both sides:

1. ``PREVIOUS_PROTOCOL_VERSION`` takes the old ``PROTOCOL_VERSION`` and
   ``PROTOCOL_VERSION`` takes the new date, here and in
   ``lemoncrow_client.protocol``. ``client/tests/test_contract_conformance.py``
   fails until the two windows are equal.
2. The old N-1 is now outside the window: its clients are refused with
   "upgrade the client" and its servers by new clients with "the server is
   older; upgrade the server".
3. Every client-side shim for the retired version goes with it -- today the
   pre-manifest Review baselines in ``lemoncrow_client.protocol``, which only a
   2026-09-14 server needs.

Review capability manifest
--------------------------
``product_capabilities.review`` in the discovery document is the Review
manifest: every name in :data:`REVIEW_FEATURES` as a boolean, the capture modes
and capture options this server honors, the evidence kinds it stores and the
packet, evidence, policy and record schema versions it understands. The same
document is embedded in the handshake (``server``) and served on the Review API
at ``GET /v1/reviews/manifest``. Clients gate Review features on it; a write
the server could only apply partially fails closed with a typed error on both
sides (:func:`require_packet_schema`, :func:`require_capture_options`,
:func:`require_capture_mode`).
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from .errors import AgentAction, ErrorCode, ServerError

__all__ = [
    "BRANCH_REVIEW_PATH",
    "CAPTURE_MODES",
    "CAPTURE_OPTIONS",
    "PREVIOUS_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "PROTOCOL_WINDOW",
    "REQUIRED_CLIENT_CAPABILITIES",
    "RESULT_REUSE_CAPABILITY",
    "REVIEW_FEATURES",
    "REVIEW_MANIFEST_SCHEMA",
    "SERVER_CAPABILITIES",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "Negotiation",
    "ProtocolWindow",
    "branch_review_query",
    "branch_review_wire",
    "negotiate",
    "require_capture_mode",
    "require_capture_options",
    "require_packet_schema",
    "review_capabilities",
    "server_identity",
]

#: Wire protocol this build speaks (N). 2026-10-05 added the Review capability
#: manifest and the two-version window.
PROTOCOL_VERSION: Final[str] = "2026-10-05"

#: The protocol before it (N-1), still served in full.
PREVIOUS_PROTOCOL_VERSION: Final[str] = "2026-09-14"

_ISO_DATE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")


def _protocol_date(value: object) -> date | None:
    """A protocol version as a calendar date, or ``None`` when it is not one."""

    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ProtocolWindow:
    """The two protocol versions a build serves: ``current`` (N) and ``previous`` (N-1)."""

    current: str
    previous: str

    def __post_init__(self) -> None:
        current = _protocol_date(self.current)
        previous = _protocol_date(self.previous)
        if current is None or previous is None:
            raise ValueError(f"protocol versions are YYYY-MM-DD dates, not {self.current!r}/{self.previous!r}")
        if not previous < current:
            raise ValueError(f"N-1 ({self.previous}) must be strictly older than N ({self.current})")

    @property
    def versions(self) -> frozenset[str]:
        return frozenset({self.current, self.previous})

    def refusal(self, offered: str) -> ServerError:
        """The loud refusal for a version outside this window."""

        supported = sorted(self.versions)
        offered_date = _protocol_date(offered)
        current = _protocol_date(self.current)
        if offered_date is not None and current is not None and offered_date > current:
            direction = "client_newer"
            action = AgentAction.UPGRADE_SERVER
            message = (
                f"client protocol {offered!r} is newer than this server's {self.current!r} "
                f"(supported: {', '.join(supported)}): the server is older; upgrade the server"
            )
        else:
            direction = "client_older" if offered_date is not None else "unrecognized"
            action = AgentAction.UPGRADE_CLIENT
            message = (
                f"client protocol {offered!r} is not supported by this server "
                f"(supported: {', '.join(supported)}): upgrade the client"
            )
        return ServerError(
            ErrorCode.PROTOCOL_VERSION_MISMATCH,
            message,
            details={
                "client_protocol": offered,
                "server_protocol": self.current,
                "supported": supported,
                "direction": direction,
            },
            action=action,
        )


#: The support window. Read at call time, so a test can stand this build up as
#: the server one version behind by replacing it.
PROTOCOL_WINDOW: ProtocolWindow = ProtocolWindow(current=PROTOCOL_VERSION, previous=PREVIOUS_PROTOCOL_VERSION)

#: Every protocol version this build still accepts from a client.
SUPPORTED_PROTOCOL_VERSIONS: Final[frozenset[str]] = PROTOCOL_WINDOW.versions

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
    window = PROTOCOL_WINDOW
    client_protocol = body.get("protocol_version")
    if not isinstance(client_protocol, str) or not client_protocol:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "protocol_version is required",
            details={"field": "protocol_version", "server_protocol": window.current},
        )
    if client_protocol not in window.versions:
        raise window.refusal(client_protocol)

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
    window = PROTOCOL_WINDOW
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
        "protocol_version": window.current,
        "supported_protocol_versions": sorted(window.versions),
        "capabilities": sorted(capabilities),
        "required_client_capabilities": sorted(REQUIRED_CLIENT_CAPABILITIES),
        "endpoints": endpoints,
    }


# --------------------------------------------------------------------------- #
# Review capability manifest                                                  #
# --------------------------------------------------------------------------- #

#: Shape version of the manifest document itself.
REVIEW_MANIFEST_SCHEMA: Final[int] = 1

#: Every Review feature a manifest names, each as a boolean. The first fourteen
#: are what a 2026-09-14 server already published; ``capture``,
#: ``evidence_read`` and ``evidence_upload`` were implied by Review being
#: available and are explicit from 2026-10-05.
REVIEW_FEATURES: Final[tuple[str, ...]] = (
    "local_workspace",
    "workspace_refresh",
    "cross_device_history",
    "collaboration",
    "participants",
    "requests",
    "provider_sync",
    "organization_policy",
    "sharing",
    "guest_review",
    "private_drafts",
    "audit_history",
    "brief",
    "ask",
    "capture",
    "evidence_read",
    "evidence_upload",
)

#: How a capture names its change: a client-built packet, or base/head Views
#: the server derives the packet from.
CAPTURE_MODES: Final[frozenset[str]] = frozenset({"derived", "packet"})

#: Capture request fields that change what a capture does. A server that does
#: not honor one must refuse it rather than capture without it.
CAPTURE_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "title",
        "restore_archived",
        "force_new_review",
        "continue_review_id",
        "reuse_closed_review",
        "review_scope",
        "workspace_branch",
        "capture_intent",
    }
)

_REVIEW_PACKAGE: Final[str] = "lemoncrow.pro.capabilities.review"

#: schema name -> (engine module, constant). Imported lazily: the core server
#: runs without the Review engine, and the record module is not part of every
#: engine build. A schema whose module is absent is published as ``[]``.
_SCHEMA_SOURCES: Final[dict[str, tuple[str, str]]] = {
    "packet": (f"{_REVIEW_PACKAGE}.models", "SCHEMA_VERSION"),
    "evidence": (f"{_REVIEW_PACKAGE}.session_models", "SESSION_SCHEMA_VERSION"),
    "policy": (f"{_REVIEW_PACKAGE}.readiness", "READINESS_POLICY_SCHEMA_VERSION"),
    "record": (f"{_REVIEW_PACKAGE}.review_record", "RECORD_SCHEMA_VERSION"),
}


def _engine_value(module: str, name: str) -> Any:
    try:
        if importlib.util.find_spec(module) is None:
            return None
    except ModuleNotFoundError:
        return None
    return getattr(importlib.import_module(module), name, None)


def _schema_versions(schema: str) -> list[int]:
    module, name = _SCHEMA_SOURCES[schema]
    value = _engine_value(module, name)
    return [value] if isinstance(value, int) and not isinstance(value, bool) else []


def review_capabilities(
    features: Mapping[str, bool],
    *,
    capture_modes: Iterable[str],
    capture_options: Iterable[str],
) -> dict[str, Any]:
    """The Review manifest a composition publishes under ``product_capabilities.review``.

    ``features`` must name every entry of :data:`REVIEW_FEATURES` and nothing
    else, so a feature cannot be added to one composition and forgotten in the
    other.
    """

    missing = sorted(set(REVIEW_FEATURES) - set(features))
    unknown = sorted(set(features) - set(REVIEW_FEATURES))
    if missing or unknown:
        raise ValueError(f"Review manifest features: missing {missing}, unknown {unknown}")
    modes = sorted(set(capture_modes))
    options = sorted(set(capture_options))
    if not set(modes) <= CAPTURE_MODES:
        raise ValueError(f"unknown capture modes {sorted(set(modes) - CAPTURE_MODES)}")
    if not set(options) <= CAPTURE_OPTIONS:
        raise ValueError(f"unknown capture options {sorted(set(options) - CAPTURE_OPTIONS)}")
    kinds = _engine_value(f"{_REVIEW_PACKAGE}.session_models", "REVIEW_EVIDENCE_KINDS")
    return {
        "manifest": REVIEW_MANIFEST_SCHEMA,
        **{name: bool(features[name]) for name in REVIEW_FEATURES},
        "schemas": {schema: _schema_versions(schema) for schema in _SCHEMA_SOURCES},
        "evidence_kinds": sorted(str(kind) for kind in kinds) if isinstance(kinds, (tuple, list, frozenset)) else [],
        "capture_modes": modes,
        "capture_options": options,
    }


def require_packet_schema(packet: Mapping[str, Any]) -> None:
    """Refuse, typed, a Review packet in a schema this server does not read.

    Malformed values are left to the packet decoder, which already rejects
    them; this guard only names version skew and the side to upgrade.
    """

    version = packet.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return
    supported = _schema_versions("packet")
    if not supported or version in supported:
        return
    older = version < min(supported)
    raise ServerError(
        ErrorCode.CAPABILITY_UNSUPPORTED,
        (
            f"this server does not support Review packet schema {version} "
            f"(it reads {', '.join(map(str, supported))}); nothing was captured: "
            + ("upgrade the client" if older else "the server is older; upgrade the server")
        ),
        details={"schema": "packet", "version": version, "supported": supported},
        action=AgentAction.UPGRADE_CLIENT if older else AgentAction.UPGRADE_SERVER,
    )


def _option_set(value: object) -> bool:
    """An option is requested when it differs from its default, never by its key alone.

    Defaults are ``False``, ``""`` (or blank), ``None``, ``0`` and empty
    containers. ``lemoncrow_client.protocol._option_set`` must agree.
    """

    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def require_capture_options(body: Mapping[str, Any], *, honored: frozenset[str]) -> None:
    """Refuse a capture that asks for an option this server would silently drop."""

    unsupported = sorted(name for name in CAPTURE_OPTIONS - honored if _option_set(body.get(name)))
    if unsupported:
        raise ServerError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            (
                f"this server does not honor Review capture option(s) {', '.join(unsupported)}; "
                "nothing was captured. Drop them or upgrade the server"
            ),
            details={"unsupported_options": unsupported, "honored": sorted(honored)},
            action=AgentAction.UPGRADE_SERVER,
        )


#: The subject-keyed read a working-tree or staged capture makes before it
#: freezes its range: ``?range_mode=&branch=`` plus the repository, answered
#: ``{"review": null | {id, repo_id, range_mode, status, review_base_sha,
#: review_base_policy}}``. Served by every server that honors the
#: ``workspace_branch`` capture option, beside the Reader API.
BRANCH_REVIEW_PATH: Final[str] = "/api/branch-review"


def branch_review_query(query: Mapping[str, str]) -> tuple[str, str]:
    """``(range_mode, branch)`` of a branch Review lookup, or a refusal naming the bad field."""

    range_mode = query.get("range_mode", "")
    if range_mode not in {"working_tree", "staged"}:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "range_mode must be working_tree or staged",
            details={"field": "range_mode"},
            action=AgentAction.FIX_REQUEST,
        )
    branch = query.get("branch", "")
    if len(branch) > 256 or any(char in branch for char in "\n\r\x00") or not branch.strip():
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "branch must be a non-empty single-line string of at most 256 characters",
            details={"field": "branch"},
            action=AgentAction.FIX_REQUEST,
        )
    return range_mode, branch.strip()


def branch_review_wire(session: Any, repo_id: str) -> dict[str, Any]:
    """The ``review`` object of a branch Review lookup answer."""

    return {
        "id": session.id,
        "repo_id": repo_id,
        "range_mode": session.range_mode,
        "status": session.status,
        "review_base_sha": session.review_base_sha,
        "review_base_policy": session.review_base_policy,
    }


def require_capture_mode(body: Mapping[str, Any], *, modes: frozenset[str]) -> None:
    """Refuse a capture mode this server does not take."""

    mode = "derived" if body.get("derive_packet") is True else "packet"
    if mode in modes:
        return
    label = "server-derived Review capture" if mode == "derived" else "client-built Review packets"
    raise ServerError(
        ErrorCode.CAPABILITY_UNSUPPORTED,
        f"this server does not support {label}; nothing was captured",
        details={"capture_mode": mode, "supported": sorted(modes)},
        action=AgentAction.FIX_REQUEST,
    )
