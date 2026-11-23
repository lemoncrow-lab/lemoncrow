"""What this client declares at the handshake.

The server refuses a client that does not offer ``blob_miss.v1``, and it is
right to: a client that cannot answer ``{"need": [...]}`` turns every cold read
into a hard failure, and from the server's side that is indistinguishable from
a broken client. So the required capability is not a constant this module may
drift from -- it is the set :mod:`lemoncrow_client.blobs` actually implements,
and ``tests/test_contract_conformance.py`` asserts the two agree with the
server's own ``REQUIRED_CLIENT_CAPABILITIES`` when the server is importable.

The protocol version is a date string. The client speaks two: N
(:data:`PROTOCOL_VERSION`) and N-1 (:data:`PREVIOUS_PROTOCOL_VERSION`), the
same window the server serves. :func:`negotiate_protocol` offers N and steps
down to N-1 once when a server one version behind refuses N. Anything else is
refused loudly at ``POST /v1/handshake`` before authentication, naming the
side to upgrade, so a client that must be upgraded learns that instead of
seeing a confusing 401. ``lemoncrow_server_core.protocol`` documents how a
version is cut and N-1 retired.

Review features are gated on the server's Review capability manifest
(:class:`ReviewManifest`). An unsupported feature degrades with "this server
does not support X"; a write the server could only apply partially is refused
before it is sent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from .errors import AgentAction, ClientError, ErrorCode

__all__ = [
    "CAPTURE_OPTIONS",
    "CLIENT_CAPABILITIES",
    "CLIENT_REVIEW_SCHEMAS",
    "HEADER_HOST",
    "HEADER_HOST_SESSION",
    "HEADER_MODEL",
    "HEADER_REQUEST_ID",
    "HEADER_REVISION",
    "HEADER_SESSION",
    "HEADER_VIEW",
    "PREVIOUS_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "REQUIRED_CLIENT_CAPABILITIES",
    "RESULT_REUSE_CAPABILITY",
    "REVIEW_FEATURES",
    "SAME_HOST_CAPABILITY",
    "ReviewManifest",
    "RouteRequirement",
    "negotiate_protocol",
    "parse_review_manifest",
    "protocol_window",
    "review_route_requirement",
]

#: Wire protocol this build speaks (N). Must match the server's
#: ``lemoncrow_server_core.protocol.PROTOCOL_VERSION``.
PROTOCOL_VERSION: Final[str] = "2026-10-05"

#: The protocol before it (N-1), still spoken to a server one version behind.
#: Must match the server's ``PREVIOUS_PROTOCOL_VERSION``.
PREVIOUS_PROTOCOL_VERSION: Final[str] = "2026-09-14"

#: The minimum the server will accept. Offering less is refused at handshake.
REQUIRED_CLIENT_CAPABILITIES: Final[frozenset[str]] = frozenset({"blob_miss.v1"})

#: Everything this client implements. Each name corresponds to code in this
#: package, not to an aspiration:
#:
#: ``blob_miss.v1``    :meth:`lemoncrow_client.blobs.BlobService.fill`
#: ``tools.v1``        :meth:`lemoncrow_client.session.RemoteSession.call_tool`
#: ``views.v1``        :meth:`lemoncrow_client.session.RemoteSession.open_view`
#: ``blobs.v1``        :meth:`lemoncrow_client.blobs.BlobService.upload`
#: ``overlay.v1``      :meth:`lemoncrow_client.blobs.BlobService.push`
#: ``index_query.v1``  :meth:`lemoncrow_client.session.RemoteSession.query_index`
RESULT_REUSE_CAPABILITY: Final[str] = "result_reuse.v1"

CLIENT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        "blob_miss.v1",
        "tools.v1",
        "views.v1",
        "blobs.v1",
        "overlay.v1",
        "index_query.v1",
        RESULT_REUSE_CAPABILITY,
    }
)

#: Offered only when the operator opted in *and* the client can write a proof
#: file into the worktree. It is an optimization the server may decline; the
#: upload path is the one that always works and the one CI runs.
SAME_HOST_CAPABILITY: Final[str] = "local_fs"

HEADER_SESSION: Final[str] = "X-LemonCrow-Session"
HEADER_VIEW: Final[str] = "X-LemonCrow-View"
HEADER_REVISION: Final[str] = "X-LemonCrow-View-Revision"
HEADER_REQUEST_ID: Final[str] = "X-LemonCrow-Request-Id"
HEADER_HOST_SESSION: Final[str] = "X-LemonCrow-Host-Session"
HEADER_HOST: Final[str] = "X-LemonCrow-Host"
HEADER_MODEL: Final[str] = "X-LemonCrow-Model"


# --------------------------------------------------------------------------- #
# The support window                                                          #
# --------------------------------------------------------------------------- #

_ISO_DATE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")


def _protocol_date(value: object) -> date | None:
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def protocol_window() -> tuple[str, str]:
    """``(N, N-1)``, read at call time so a test can stand this build up as another version."""

    return PROTOCOL_VERSION, PREVIOUS_PROTOCOL_VERSION


def window_refusal(served: Iterable[str]) -> ClientError | None:
    """The loud refusal when the server's versions share none with this client's window.

    ``None`` when they overlap, or when the server named no version this client
    can order (the caller then keeps the server's own refusal).
    """

    current, previous = protocol_window()
    versions = sorted(version for version in set(served) if _protocol_date(version) is not None)
    if not versions or {current, previous} & set(versions):
        return None
    newest = _protocol_date(versions[-1])
    current_date = _protocol_date(current)
    server_older = newest is not None and current_date is not None and newest < current_date
    message = f"this client speaks protocol {current} and {previous}; the server speaks {', '.join(versions)}: " + (
        "the server is older; upgrade the server" if server_older else "upgrade the client"
    )
    return ClientError(
        ErrorCode.PROTOCOL_VERSION_MISMATCH,
        message,
        details={
            "client_protocols": [current, previous],
            "server_protocols": versions,
            "direction": "server_older" if server_older else "server_newer",
        },
        action=AgentAction.UPGRADE_SERVER if server_older else AgentAction.UPGRADE_CLIENT,
    )


def _served_versions(details: Mapping[str, Any]) -> list[str]:
    raw = details.get("supported")
    if isinstance(raw, list):
        return [str(version) for version in raw if isinstance(version, str)]
    # A refusal from before the window carried only the server's own version.
    server = details.get("server_protocol")
    return [server] if isinstance(server, str) else []


def negotiate_protocol(post: Callable[[str], Mapping[str, Any]]) -> tuple[Mapping[str, Any], str]:
    """Offer N; on a version refusal, step down to N-1 once if the server serves it.

    ``post`` sends one negotiation body at the given version and returns the
    payload or raises the server's refusal. Returns ``(payload, version)``.
    """

    current, previous = protocol_window()
    offered = current
    try:
        payload = post(offered)
    except ClientError as exc:
        if exc.code is not ErrorCode.PROTOCOL_VERSION_MISMATCH:
            raise
        served = _served_versions(exc.details)
        if previous not in served:
            refusal = window_refusal(served)
            if refusal is not None:
                raise refusal from exc
            raise
        offered = previous
        payload = post(offered)
    negotiated = payload.get("protocol_version")
    if negotiated != offered:
        # A version this client did not offer is a server bug, not a downgrade
        # path: pretending otherwise is how a client parses a wire format it
        # does not know.
        raise ClientError(
            ErrorCode.PROTOCOL_VERSION_MISMATCH,
            f"server negotiated protocol {negotiated!r}; this client offered {offered!r}",
            details={"server": str(negotiated), "client": offered},
            action=AgentAction.UPGRADE_CLIENT,
        )
    return payload, offered


# --------------------------------------------------------------------------- #
# The Review capability manifest                                              #
# --------------------------------------------------------------------------- #

#: Every Review feature a manifest names. Must equal the server's
#: ``REVIEW_FEATURES``.
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

#: Capture request fields that change what a capture does. Must equal the
#: server's ``CAPTURE_OPTIONS``.
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
        "with_workspace",
        "workspace_root",
    }
)

#: Review schemas this client reads back. A server serving none of these
#: versions is refused, naming the side to upgrade. The packet schema is the
#: caller's (the Review engine builds it) and is checked per write.
CLIENT_REVIEW_SCHEMAS: Final[Mapping[str, frozenset[int]]] = {
    "evidence": frozenset({1}),
    "policy": frozenset({1}),
    "record": frozenset({1}),
}

#: The first protocol whose servers publish a manifest.
_MANIFEST_SINCE: Final[str] = "2026-10-05"

_FEATURE_LABELS: Final[Mapping[str, str]] = {
    "local_workspace": "local workspace Reviews",
    "workspace_refresh": "workspace refresh",
    "cross_device_history": "cross-device Review history",
    "collaboration": "Review collaboration",
    "participants": "Review participants",
    "requests": "Review requests",
    "provider_sync": "provider sync",
    "organization_policy": "organization Review policy",
    "sharing": "Review sharing",
    "guest_review": "guest Review",
    "private_drafts": "private drafts",
    "audit_history": "Review audit history",
    "brief": "the Review brief",
    "ask": "Ask",
    "capture": "Review capture",
    "evidence_read": "reading Review evidence",
    "evidence_upload": "attaching Review evidence",
}

_SCHEMA_LABELS: Final[Mapping[str, str]] = {
    "packet": "Review packets",
    "evidence": "Review evidence",
    "policy": "Review readiness policy",
    "record": "Review records",
}

_MODE_LABELS: Final[Mapping[str, str]] = {
    "derived": "server-derived Review capture",
    "packet": "client-built Review packets",
}

# lc-debt: pre-manifest baselines exist only for 2026-09-14 servers; delete them when that version leaves the window.
_LEGACY_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "screenshot",
        "image",
        "video",
        "playwright_trace",
        "document",
        "live_preview",
        "verification",
        "sbom",
        "provenance",
        "vulnerability_scan",
        "waiver",
        "intent",
        "premerge_check",
        "deployment",
    }
)
_LEGACY_SCHEMAS: Final[Mapping[str, frozenset[int]]] = {
    "packet": frozenset({2}),
    "evidence": frozenset({1}),
    "policy": frozenset({1}),
    "record": frozenset({1}),
}
#: What a 2026-09-14 loopback server served; everything else is off unless it
#: published the boolean, and a published boolean always wins.
_LEGACY_LOCAL_ON: Final[frozenset[str]] = frozenset(
    {"local_workspace", "workspace_refresh", "capture", "evidence_read", "evidence_upload"}
)
#: What a 2026-09-14 hosted server served. It published no ``ask`` and served it.
_LEGACY_HOSTED_ON: Final[frozenset[str]] = frozenset(
    {
        "cross_device_history",
        "collaboration",
        "participants",
        "requests",
        "provider_sync",
        "organization_policy",
        "ask",
        "capture",
        "evidence_read",
        "evidence_upload",
    }
)


def _option_set(value: object) -> bool:
    """An option is requested when it differs from its default, never by its key alone.

    Defaults are ``False``, ``""`` (or blank), ``None``, ``0`` and empty
    containers. ``lemoncrow_server_core.protocol._option_set`` must agree.
    """

    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


@dataclass(frozen=True, slots=True)
class RouteRequirement:
    """What one Review API route needs from the server's manifest."""

    feature: str | None
    schema: str | None
    evidence_kind: str = ""


@dataclass(frozen=True, slots=True)
class ReviewManifest:
    """The server's Review capabilities as this client reads them.

    ``declared`` is false only for a server from before manifests, whose
    capabilities are the baseline its deployment served then.
    """

    declared: bool
    protocol_version: str
    features: Mapping[str, bool]
    capture_modes: frozenset[str]
    capture_options: frozenset[str]
    schemas: Mapping[str, frozenset[int]]
    evidence_kinds: frozenset[str]

    def supports(self, feature: str) -> bool:
        return self.features.get(feature) is True

    def require(self, feature: str) -> None:
        """Refuse, with "this server does not support X", a feature the server lacks."""

        label = _FEATURE_LABELS.get(feature, feature)
        details = {"feature": feature, "protocol_version": self.protocol_version}
        if feature not in self.features:
            raise ClientError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                f"this server does not support {label}: the server is older; upgrade the server",
                details=details,
                action=AgentAction.UPGRADE_SERVER,
            )
        if not self.features[feature]:
            raise ClientError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                f"this server does not support {label}",
                details=details,
                action=AgentAction.ABANDON,
            )

    def _unserved(self, schema: str, details: Mapping[str, Any]) -> ClientError:
        return ClientError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            f"this server does not support {_SCHEMA_LABELS.get(schema, schema)}",
            details=details,
            action=AgentAction.ABANDON,
        )

    def require_schema(self, schema: str, version: int) -> None:
        """Refuse a write in a schema version the server does not read."""

        supported = self.schemas.get(schema, frozenset())
        details = {"schema": schema, "version": version, "supported": sorted(supported)}
        if not supported:
            raise self._unserved(schema, details)
        if version in supported:
            return
        older = version < min(supported)
        raise ClientError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            (
                f"this server does not support {_SCHEMA_LABELS.get(schema, schema)} at schema {version} "
                f"(it reads {', '.join(map(str, sorted(supported)))}); nothing was sent: "
                + ("upgrade the client" if older else "the server is older; upgrade the server")
            ),
            details=details,
            action=AgentAction.UPGRADE_CLIENT if older else AgentAction.UPGRADE_SERVER,
        )

    def require_shared_schema(self, schema: str) -> None:
        """Refuse a route whose schema this client and the server share no version of."""

        readable = CLIENT_REVIEW_SCHEMAS.get(schema)
        if readable is None:
            return
        served = self.schemas.get(schema, frozenset())
        details = {"schema": schema, "served": sorted(served), "readable": sorted(readable)}
        if not served:
            raise self._unserved(schema, details)
        if served & readable:
            return
        server_older = max(served) < min(readable)
        raise ClientError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            (
                f"this server serves {_SCHEMA_LABELS.get(schema, schema)} at schema "
                f"{', '.join(map(str, sorted(served)))}; this client reads {', '.join(map(str, sorted(readable)))}: "
                + ("the server is older; upgrade the server" if server_older else "upgrade the client")
            ),
            details=details,
            action=AgentAction.UPGRADE_SERVER if server_older else AgentAction.UPGRADE_CLIENT,
        )

    def require_capture(self, mode: str, body: Mapping[str, Any]) -> None:
        """Refuse a capture the server would reject or apply only in part."""

        self.require("capture")
        if mode not in self.capture_modes:
            raise ClientError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                f"this server does not support {_MODE_LABELS.get(mode, mode)}",
                details={"capture_mode": mode, "supported": sorted(self.capture_modes)},
                action=AgentAction.FIX_REQUEST,
            )
        unsupported = sorted(name for name in CAPTURE_OPTIONS - self.capture_options if _option_set(body.get(name)))
        if unsupported:
            raise ClientError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                (
                    f"this server does not honor Review capture option(s) {', '.join(unsupported)}; "
                    "nothing was sent. Drop them or upgrade the server"
                ),
                details={"unsupported_options": unsupported, "honored": sorted(self.capture_options)},
                action=AgentAction.UPGRADE_SERVER,
            )

    def require_packet(self, packet: Mapping[str, Any]) -> None:
        """Refuse a packet whose schema the server does not read; malformed ones are the server's to reject."""

        version = packet.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool):
            self.require_schema("packet", version)

    def require_route(self, requirement: RouteRequirement) -> None:
        if requirement.feature is not None:
            self.require(requirement.feature)
        if requirement.schema is not None:
            self.require_shared_schema(requirement.schema)
        kind = requirement.evidence_kind
        if kind and self.evidence_kinds and kind not in self.evidence_kinds:
            raise ClientError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                f"this server does not support {kind!r} evidence (it stores: {', '.join(sorted(self.evidence_kinds))})",
                details={"evidence_kind": kind, "supported": sorted(self.evidence_kinds)},
                action=AgentAction.FIX_REQUEST,
            )


def _strings(value: object) -> frozenset[str]:
    return frozenset(item for item in value if isinstance(item, str)) if isinstance(value, list) else frozenset()


def _versions(value: object) -> frozenset[int]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, int) and not isinstance(item, bool))


def _published_features(review: Mapping[str, Any]) -> dict[str, bool]:
    return {name: value for name, value in review.items() if isinstance(name, str) and isinstance(value, bool)}


def _legacy_manifest(protocol_version: str, identity: Mapping[str, Any], review: Mapping[str, Any]) -> ReviewManifest:
    deployment = identity.get("deployment")
    local = isinstance(deployment, Mapping) and deployment.get("mode") == "local"
    products = identity.get("products")
    product = products.get("review") if isinstance(products, Mapping) else None
    if isinstance(product, Mapping) and product.get("available") is False:
        return ReviewManifest(
            declared=False,
            protocol_version=protocol_version,
            features=dict.fromkeys(REVIEW_FEATURES, False),
            capture_modes=frozenset(),
            capture_options=frozenset(),
            schemas={},
            evidence_kinds=frozenset(),
        )
    on = _LEGACY_LOCAL_ON if local else _LEGACY_HOSTED_ON
    features = {name: name in on for name in REVIEW_FEATURES}
    features.update({name: value for name, value in _published_features(review).items() if name in features})
    return ReviewManifest(
        declared=False,
        protocol_version=protocol_version,
        features=features,
        capture_modes=frozenset({"packet"}) if local else frozenset({"derived", "packet"}),
        capture_options=CAPTURE_OPTIONS if local else frozenset({"title", "restore_archived"}),
        schemas=dict(_LEGACY_SCHEMAS),
        evidence_kinds=_LEGACY_EVIDENCE_KINDS,
    )


def parse_review_manifest(protocol_version: str, identity: Mapping[str, Any]) -> ReviewManifest:
    """Read the Review manifest out of a discovery document (or the handshake's ``server``).

    A declared manifest is read as published. A server on a protocol from
    before manifests gets the baseline its deployment served then, with any
    boolean it did publish taking precedence. A server on a manifest protocol
    that publishes none serves no Review.
    """

    capabilities = identity.get("product_capabilities")
    raw = capabilities.get("review") if isinstance(capabilities, Mapping) else None
    review: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    version = review.get("manifest")
    if isinstance(version, int) and not isinstance(version, bool):
        schemas = review.get("schemas")
        return ReviewManifest(
            declared=True,
            protocol_version=protocol_version,
            features=_published_features(review),
            capture_modes=_strings(review.get("capture_modes")),
            capture_options=_strings(review.get("capture_options")),
            schemas=(
                {str(name): _versions(values) for name, values in schemas.items()}
                if isinstance(schemas, Mapping)
                else {}
            ),
            evidence_kinds=_strings(review.get("evidence_kinds")),
        )
    negotiated = _protocol_date(protocol_version)
    since = _protocol_date(_MANIFEST_SINCE)
    if negotiated is not None and since is not None and negotiated < since:
        return _legacy_manifest(protocol_version, identity, review)
    return ReviewManifest(
        declared=True,
        protocol_version=protocol_version,
        features=dict.fromkeys(REVIEW_FEATURES, False),
        capture_modes=frozenset(),
        capture_options=frozenset(),
        schemas={},
        evidence_kinds=frozenset(),
    )


def review_route_requirement(method: str, path: str) -> RouteRequirement | None:
    """What a Reader API call needs from the manifest; ``None`` for routes every Review server serves."""

    parts = urlsplit(path)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 3 or segments[0] != "api":
        return None
    reads = method.upper() in {"GET", "HEAD"}
    if segments[1] == "evidence":
        return RouteRequirement("evidence_read" if reads else "evidence_upload", "evidence")
    if segments[1] != "reviews" or len(segments) < 4:
        return None
    leaf = segments[3]
    if leaf == "ask":
        return RouteRequirement("ask", None)
    if leaf == "evidence":
        if reads:
            return RouteRequirement("evidence_read", "evidence")
        kinds = parse_qs(parts.query).get("kind") or [""]
        return RouteRequirement("evidence_upload", "evidence", evidence_kind=kinds[0])
    if leaf in {"record", "records"}:
        return RouteRequirement(None, "record")
    if leaf == "readiness":
        return RouteRequirement(None, "policy")
    return None
