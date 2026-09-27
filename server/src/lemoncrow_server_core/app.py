"""Shared HTTP handlers for the LemonCrow server engine.

The class in this module deliberately does not own authentication, RBAC,
entitlement, quota, hosted Review, or deployment wiring.  Those are composition
concerns supplied by subclasses.  Session/view/index/dispatch behavior lives
here once so local and hosted servers cannot drift.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import multiprocessing
import os
import re
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from typing import Any, Final, TypeVar

from aiohttp import web

from .context import Principal, TenantContext, validate_opaque_id
from .decisions import AuditDecision
from .dispatch import BlobMissing, ToolInvocation, ToolResult
from .errors import AgentAction, ErrorCode, ServerError
from .http.wire import (
    NDJSON_MEDIA_TYPE,
    SSE_MEDIA_TYPE,
    iter_json_chunks,
    json_response,
    ndjson_frame,
    parse_json_object,
    read_capped_body,
    require_array,
    require_int,
    require_object,
    require_str,
    require_str_array,
    sse_frame,
)
from .index.analysis import analyze_foreground
from .index.boundary import addressable
from .index.contracts import (
    AnalysisArtifact,
    IndexBackend,
    ManifestEntry,
    RepoIdentityClaim,
    sha256_hex,
    validate_digest,
    validate_path,
)
from .index.deferred import cancel_deferred_view, enqueue_deferred_view
from .index.manifest import required_digests
from .matrix import Site, require_server_side, site_of
from .protocol import RESULT_REUSE_CAPABILITY, negotiate, server_identity
from .sessions import Session, require_acknowledged_revision
from .workspace import WorkspaceStatus

__all__ = [
    "HEADER_REQUEST_ID",
    "HEADER_REVISION",
    "HEADER_VIEW",
    "REQUEST_ID_KEY",
    "CoreServerRoutes",
]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server")
_T = TypeVar("_T")
_ANALYSIS_PARALLEL_MIN_FILES: Final[int] = 192
#: Large enough to amortize process-pool serialization/scheduling, small enough
#: for two workers to keep sharing a heterogeneous upload batch. On the 250k
#: scale fixture's representative 1,000-file sample, 32-file chunks were ~10%
#: faster than the previous 8-file chunks while preserving the same artifacts.
_ANALYSIS_CHUNK_FILES: Final[int] = 32
_READ_PATH_SUFFIX: Final[re.Pattern[str]] = re.compile(
    r":(?:L?\d+(?:-L?\d*)?|full|minified|summary|outline|head=\d+|tail=\d+)(?:=(?:true|1))?$"
)
_READ_PATH_KEYS: Final[tuple[str, ...]] = ("path", "file_path", "filePath")
#: Renewal times kept for session-bound Views before stale ones are dropped.
_VIEW_RENEWALS_KEPT: Final[int] = 4096


def _requested_lease_s(body: Mapping[str, Any], default_s: float, max_s: float) -> float:
    """The lease a View opens with: the client's request, capped, else the default."""
    requested = body.get("lease_s")
    if requested is None:
        return default_s
    if isinstance(requested, bool) or not isinstance(requested, (int, float)) or not 0 < requested:
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "lease_s must be a positive number of seconds")
    return min(float(requested), max_s)


def _exact_read_paths(arguments: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Exact repository paths named by a ``read`` call, else ``None``.

    ``None`` deliberately means "use the complete workspace". Only request
    shapes whose filesystem scope is explicit are eligible for lazy path
    materialization; symbol-only reads and malformed/ambiguous shapes retain
    the existing whole-tree behavior.
    """

    def normalized(raw: object) -> str | None:
        if not isinstance(raw, str) or not raw:
            return None
        path = _READ_PATH_SUFFIX.sub("", raw)
        try:
            return validate_path(path)
        except ServerError:
            return None

    files = arguments.get("files")
    paths: list[str] = []
    if files is not None:
        if not isinstance(files, list) or not files:
            return None
        for item in files:
            if isinstance(item, str):
                path = normalized(item)
            elif isinstance(item, Mapping):
                raw = next((item.get(key) for key in _READ_PATH_KEYS if item.get(key)), None)
                path = normalized(raw)
            else:
                return None
            if path is None:
                return None
            paths.append(path)
    else:
        raw = next((arguments.get(key) for key in _READ_PATH_KEYS if arguments.get(key)), None)
        path = normalized(raw)
        if path is None:
            return None
        paths.append(path)
    return tuple(dict.fromkeys(paths))


def build_analysis_executor(dispatch_workers: int) -> ProcessPoolExecutor | None:
    """A conservative process pool for CPU-bound Layer-1 derivation.

    Layer-1 analysis is dominated by Python semantic/trigram work, so threads do
    not improve throughput. Spawn avoids forking live SQLite connections and
    server threads. Two workers nearly saturate the measured 2-CPU deployment
    while keeping child-process memory bounded; larger hosts can be revisited
    from evidence rather than multiplying server memory by default.
    """
    process_cpu_count = getattr(os, "process_cpu_count", os.cpu_count)
    workers = min(2, max(1, int(dispatch_workers)), max(1, process_cpu_count() or 1))
    if workers < 2:
        return None
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


def _analyze_chunk(items: Sequence[tuple[str, str, bytes]]) -> tuple[AnalysisArtifact, ...]:
    # Upload acknowledgement needs complete lexical/symbol evidence, but ANN
    # chunk vectors are already a deferred semantic-layer concern. Keeping them
    # off this foreground path materially shortens first sync for large repos.
    return tuple(analyze_foreground(digest, profile, data) for digest, profile, data in items)


HEADER_VIEW: Final[str] = "X-LemonCrow-View"
HEADER_REVISION: Final[str] = "X-LemonCrow-View-Revision"
HEADER_REQUEST_ID: Final[str] = "X-LemonCrow-Request-Id"
HEADER_HOST_SESSION: Final[str] = "X-LemonCrow-Host-Session"
HEADER_HOST: Final[str] = "X-LemonCrow-Host"
HEADER_MODEL: Final[str] = "X-LemonCrow-Model"
REQUEST_ID_KEY: Final[web.RequestKey[str]] = web.RequestKey("lemoncrow_request_id", str)


def _negotiated_stream_media(accept: str) -> str | None:
    lowered = accept.lower()
    if NDJSON_MEDIA_TYPE in lowered:
        return NDJSON_MEDIA_TYPE
    if SSE_MEDIA_TYPE in lowered:
        return SSE_MEDIA_TYPE
    return None


def _limits_wire(config: Any) -> dict[str, Any]:
    limits = config.limits
    return {
        "max_request_bytes": limits.max_request_bytes,
        "max_blob_bytes": limits.max_blob_bytes,
        "max_blobs_per_request": limits.max_blobs_per_request,
        "max_manifest_entries_per_chunk": limits.max_manifest_entries_per_chunk,
        "max_overlay_entries": limits.max_overlay_entries,
        "tool_deadline_s": limits.tool_deadline_s,
        "view_lease_s": config.view_lease_s,
        "session_ttl_s": config.session_ttl_s,
    }


def _manifest_entries(body: Mapping[str, Any], *, limit: int) -> tuple[ManifestEntry, ...]:
    raw_entries = require_array(body, "entries", max_len=limit)
    entries: list[ManifestEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "entries[] must be objects",
                details={"field": "entries"},
            )
        entries.append(ManifestEntry.from_json(raw))
    return tuple(entries)


def _decode_blob(raw: Mapping[str, Any], *, limit: int) -> bytes:
    encoding = str(raw.get("encoding") or "base64").strip().lower()
    payload = raw.get("data")
    if not isinstance(payload, str):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "blobs[].data must be a base64 string",
            details={"field": "data"},
        )
    try:
        compressed = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "blobs[].data is not valid base64",
            details={"field": "data"},
        ) from exc
    if encoding == "base64":
        data = compressed
    elif encoding == "gzip-base64":
        decompressor = zlib.decompressobj(wbits=31)
        try:
            data = decompressor.decompress(compressed, limit + 1)
        except zlib.error as exc:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "blobs[].data is not valid gzip",
                details={"field": "data"},
            ) from exc
        if decompressor.unconsumed_tail or not decompressor.eof:
            raise ServerError(
                ErrorCode.REQUEST_TOO_LARGE,
                "decompressed blob exceeds the server limit",
                details={"limit": limit},
                action=AgentAction.SPLIT_REQUEST,
            )
    else:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "blobs[].encoding must be 'base64' or 'gzip-base64'",
            details={"field": "encoding", "encoding": encoding},
        )
    if len(data) > limit:
        raise ServerError(
            ErrorCode.REQUEST_TOO_LARGE,
            "blob exceeds the server limit",
            details={"limit": limit, "size": len(data)},
            action=AgentAction.SPLIT_REQUEST,
        )
    return data


class CoreServerRoutes:
    """Shared session/tool/view/blob handlers; composition is supplied outside."""

    def core_route_defs(self) -> tuple[web.RouteDef, ...]:
        """Routes owned by the shared engine, independent of deployment mode."""
        return (
            web.get("/healthz", self.health),
            web.get("/readyz", self.ready),
            web.get("/.well-known/lemoncrow-server.json", self.discovery),
            web.post("/v1/handshake", self.handshake),
            web.post("/v1/sessions", self.session_open),
            web.get("/v1/sessions/{session_id}", self.session_status),
            web.post("/v1/sessions/{session_id}/cancel", self.session_cancel),
            web.delete("/v1/sessions/{session_id}", self.session_close),
            web.get("/v1/tools", self.tool_list),
            web.post("/v1/tools/{name}", self.tool_call),
            web.post("/v1/views/open", self.view_open),
            web.post("/v1/views/{view_id}/manifest-chunks", self.view_manifest_chunks),
            web.post("/v1/views/{view_id}/missing", self.view_missing),
            web.post("/v1/views/{view_id}/overlay", self.view_overlay),
            web.post("/v1/views/{view_id}/query", self.view_query),
            web.post("/v1/views/{view_id}/close", self.view_close),
            web.post("/v1/blobs", self.blobs_upload),
        )

    def _storage_budget(
        self,
        request: web.Request,
        principal: Principal,
        view_id: str | None,
        *,
        content_bytes: int,
        indexed_bytes: int,
    ) -> Any:
        del request, principal, view_id, content_bytes, indexed_bytes
        return nullcontext()

    async def _body_with_size(self, request: web.Request) -> tuple[dict[str, Any], int]:
        raw = await read_capped_body(request, self.config.limits.max_request_bytes)
        return parse_json_object(raw), len(raw)

    async def _body(self, request: web.Request) -> dict[str, Any]:
        body, _ = await self._body_with_size(request)
        return body

    def _claimed_revision(self, request: web.Request, body: Mapping[str, Any]) -> int | None:
        raw = request.headers.get(HEADER_REVISION)
        if raw is not None and raw.strip():
            try:
                return int(raw)
            except ValueError as exc:
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    f"{HEADER_REVISION} must be an integer",
                    details={"header": HEADER_REVISION},
                ) from exc
        if "view_revision" in body:
            return require_int(body, "view_revision")
        return None

    def _resolve_view(self, request: web.Request, session: Session) -> str | None:
        header = request.headers.get(HEADER_VIEW, "").strip()
        if not header:
            return session.view_id
        view_id = validate_opaque_id(header, "view_id")
        if session.view_id is not None and view_id != session.view_id:
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "the named view is not the one bound to this session",
                details={"view_id": view_id},
                action=AgentAction.REOPEN_VIEW,
            )
        return view_id

    def _tool_runtime_identity(self, request: web.Request, session: Session) -> tuple[str, str, str]:
        """Runtime accounting identity for public-tool execution.

        Shared/hosted mode intentionally ignores caller-supplied host metadata.
        The loopback composition overrides this for same-machine attribution.
        """
        del request
        return session.session_id, session.client_name, ""

    def _tenant(
        self,
        request: web.Request,
        principal: Principal,
        session: Session,
        *,
        view_id: str | None = None,
        view_revision: int | None = None,
    ) -> TenantContext:
        return TenantContext(
            org_id=principal.org_id,
            principal=principal,
            session_id=session.session_id,
            request_id=request.get(REQUEST_ID_KEY, ""),
            view_id=view_id,
            view_revision=view_revision,
        )

    async def health(self, request: web.Request) -> web.StreamResponse:
        return json_response(
            {
                "status": "draining" if self._draining else "ok",
                "in_flight": self._in_flight,
                "dispatcher_available": self.dispatcher.available,
            }
        )

    def _check_readiness_sync(self) -> None:
        self.backend.check_ready()
        for check in getattr(self, "_extra_readiness_checks", ()):
            check()

    async def ready(self, request: web.Request) -> web.StreamResponse:
        """Traffic readiness, deliberately distinct from process liveness.

        The public answer is intentionally content-free. Detailed readiness is
        an operator concern; exposing which dependency failed on an anonymous
        endpoint turns health probing into infrastructure discovery.
        """

        if self._draining or not self.dispatcher.available:
            return json_response({"status": "not_ready"}, status=503)
        try:
            await self._in_worker(self._check_readiness_sync)
        except Exception as exc:
            _LOG.warning("readiness check failed: %s", type(exc).__name__)
            return json_response({"status": "not_ready"}, status=503)
        return json_response({"status": "ready"})

    def _server_identity_name(self) -> str:
        return "lemoncrow-server"

    def _server_identity_extra_endpoints(self) -> Mapping[str, str]:
        return {}

    def _server_identity(self) -> dict[str, Any]:
        return server_identity(
            allow_local_fs=self.config.allow_local_fs,
            name=self._server_identity_name(),
            extra_endpoints=self._server_identity_extra_endpoints(),
        )

    async def _record_view_owner(self, *, org_id: str, view_id: str, subject: str) -> None:
        await self._authority_call(self.view_owners.record, org_id=org_id, view_id=view_id, subject=subject)

    async def _forget_view_owner(self, org_id: str, view_id: str) -> None:
        self.view_owners.forget(org_id, view_id)

    async def _session_store_call(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run a session-store operation without penalizing local mode.

        Local SessionRegistry is in-memory and executes inline. Hosted Redis
        session authority is blocking I/O and therefore uses the same bounded
        worker pool as other hosted authorities.
        """
        if not bool(getattr(self.sessions, "io_bound", False)):
            return fn(*args, **kwargs)
        return await self._in_worker(partial(fn, *args, **kwargs))

    async def _resolve_session(
        self, request: web.Request, principal: Principal, session_id: str | None = None
    ) -> Session:
        """Resolve through the subclass's subject-aware `_session` boundary."""
        if not bool(getattr(self.sessions, "io_bound", False)):
            session = self._session(request, principal, session_id)
        else:
            session = await self._in_worker(partial(self._session, request, principal, session_id))
        await self._keep_view_leased(session)
        return session

    def _view_lease_renewals(self) -> dict[str, float]:
        renewals: dict[str, float] | None = getattr(self, "_view_lease_renewed_at", None)
        if renewals is None:
            renewals = {}
            self._view_lease_renewed_at = renewals
        return renewals

    def _note_view_leased(self, view_id: str) -> None:
        renewals = self._view_lease_renewals()
        now = time.monotonic()
        renewals[view_id] = now
        if len(renewals) > _VIEW_RENEWALS_KEPT:
            # Forgetting a View only costs it one early renewal.
            horizon = now - self.config.view_lease_s
            for stale in [key for key, at in renewals.items() if at < horizon]:
                del renewals[stale]

    async def _keep_view_leased(self, session: Session) -> None:
        """Extend the lease of the View an active session is bound to.

        Renews once the lease is half spent, so an active View costs one write
        per half lease. A lapsed or closed View is not revived; the request
        that uses it reports that, and the client reopens it.
        """
        view_id = session.view_id
        if not view_id or not self._view_lease_due(view_id):
            return
        try:
            await self._authority_call(self.backend.views.renew, session.org_id, view_id, self.config.view_lease_s)
        except ServerError:
            self._view_lease_renewals().pop(view_id, None)

    def _view_lease_due(self, view_id: str) -> bool:
        """Whether *view_id*'s lease is half spent; claims the renewal when it is."""
        renewed_at = self._view_lease_renewals().get(view_id)
        if renewed_at is not None and time.monotonic() - renewed_at < self.config.view_lease_s / 2:
            return False
        self._note_view_leased(view_id)
        return True

    async def _close_session_store(self, access: Any, session_id: str) -> bool:
        return await self._session_store_call(self.sessions.close, access, session_id)

    async def _bind_session_view(self, access: Any, session_id: str, view_id: str, view_revision: int) -> Session:
        return await self._session_store_call(self.sessions.bind_view, access, session_id, view_id, view_revision)

    async def _acknowledge_session(self, access: Any, session_id: str, view_revision: int) -> Session:
        return await self._session_store_call(self.sessions.acknowledge, access, session_id, view_revision)

    async def discovery(self, request: web.Request) -> web.StreamResponse:
        return json_response(self._server_identity())

    async def handshake(self, request: web.Request) -> web.StreamResponse:
        """Negotiate before authenticating, so a stale client learns *why*.

        Version mismatch and "your credential is wrong" are different problems
        with different fixes. Answering the version question first means a
        client that must upgrade never sees a confusing 401 instead.
        """
        body = await self._body(request)
        negotiation = negotiate(body, allow_local_fs=self.config.allow_local_fs)
        payload = negotiation.to_wire()
        payload["server"] = self._server_identity()
        return json_response(payload)

    async def session_open(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        body = await self._body(request)
        negotiation = negotiate(body, allow_local_fs=self.config.allow_local_fs)
        session = await self._session_store_call(self.sessions.open, principal, negotiation)
        tenant = self._tenant(request, principal, session)
        self._emit("session.open", AuditDecision.ALLOW, tenant)
        payload = session.to_wire(caller=principal.subject)
        payload["tools"] = self._tool_surface(principal)
        payload["limits"] = _limits_wire(self.config)
        return json_response(payload)

    async def session_status(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal, request.match_info["session_id"])
        payload = session.to_wire(caller=principal.subject)
        payload["in_flight"] = self._in_flight
        if session.view_id is not None:
            payload["layers"] = await self._authority_call(
                self.degradation.layer_report, org_id=principal.org_id, view_id=session.view_id
            )
        return json_response(payload)

    async def session_cancel(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal, request.match_info["session_id"])
        body = await self._body(request)
        target = body.get("request_id")
        tenant = self._tenant(request, principal, session)
        if target is None:
            count = await self.cancels.cancel_session_async(session.session_id)
            self._emit("session.cancel", AuditDecision.ALLOW, tenant, counts={"cancelled": count})
            return json_response({"cancelled": count})
        cancelled = await self.cancels.cancel_async(session.session_id, validate_opaque_id(target, "request_id"))
        self._emit("session.cancel", AuditDecision.ALLOW, tenant, counts={"cancelled": int(cancelled)})
        return json_response({"cancelled": 1 if cancelled else 0})

    async def session_close(self, request: web.Request) -> web.StreamResponse:
        """Close a session -- after establishing that it is the caller's to close.

        The order here is the fix. This handler used to cancel every in-flight
        request on the named session and *then* ask whether it could be closed,
        with "can" meaning only "same organization": a colleague could cancel
        another engineer's running work whatever the close returned, and delete
        their session as well. Resolution now happens first, under the caller's
        own subject, and nothing is cancelled or deleted until it succeeds.
        """
        principal = self._principal(request)
        session_id = validate_opaque_id(request.match_info["session_id"], "session_id")
        try:
            session = await self._resolve_session(request, principal, session_id)
        except ServerError as exc:
            if exc.code is not ErrorCode.SESSION_UNKNOWN:
                raise
            # Idempotent, and deliberately identical to closing a session id
            # that never existed: closing is the one verb whose "it was not
            # there" answer is a success, and that answer must not depend on
            # whether somebody else's session happens to be behind the id.
            return json_response({"closed": False, "cancelled": 0})
        cancelled = await self.cancels.cancel_session_async(session.session_id)
        closed = await self._close_session_store(
            self._access(principal, cross_subject=session.subject != principal.subject),
            session.session_id,
        )
        self.blob_miss.forget_session(session.session_id)
        tenant = self._tenant(request, principal, session)
        self._emit("session.close", AuditDecision.ALLOW, tenant, counts={"cancelled": cancelled})
        return json_response({"closed": closed, "cancelled": cancelled})

    def _tool_surface(self, principal: Principal) -> list[dict[str, Any]]:
        allowed = self.authorizer.allowed_tools(principal)
        registered = self.dispatcher.tools
        surface: list[dict[str, Any]] = []
        for name in sorted(allowed):
            site = site_of(name)
            surface.append(
                {
                    "name": name,
                    "site": site.value if site is not None else "unknown",
                    "dispatchable": name in registered,
                }
            )
        return surface

    async def tool_list(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        return json_response(
            {
                "tools": self._tool_surface(principal),
                "dispatcher_available": self.dispatcher.available,
            }
        )

    async def tool_call(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        tool = request.match_info["name"]
        body = await self._body(request)

        tenant_for_audit = self._tenant(request, principal, session, view_id=session.view_id)
        try:
            site = require_server_side(tool)
            self.authorizer.authorize_tool(principal, tool)
        except ServerError as exc:
            self._emit("tool.refuse", AuditDecision.REFUSE, tenant_for_audit, tool=tool, reason_code=exc.code.value)
            raise

        view_id = self._resolve_view(request, session)
        committed = 0
        if view_id is not None:
            # Before the store is consulted at all: a tool call naming a view
            # the caller does not own must not even prove that the view exists,
            # let alone materialize its tree and read out of it.
            await self._owned_view(request, principal, session, view_id)
            committed = (await self._authority_call(self.backend.views.get, principal.org_id, view_id)).view_revision
        try:
            bound_revision = require_acknowledged_revision(
                session=session,
                committed_revision=committed,
                claimed_revision=self._claimed_revision(request, body),
            )
        except ServerError as exc:
            self._emit("tool.refuse", AuditDecision.REFUSE, tenant_for_audit, tool=tool, reason_code=exc.code.value)
            raise

        tenant = self._tenant(request, principal, session, view_id=view_id, view_revision=bound_revision)
        arguments = require_object(body, "arguments")
        reuse_validator = (
            self.result_reuse.issue(
                org_id=principal.org_id,
                subject=principal.subject,
                session_id=session.session_id,
                view_id=view_id,
                view_revision=bound_revision,
                tool=tool,
                arguments=arguments,
            )
            if RESULT_REUSE_CAPABILITY in session.capabilities
            else None
        )
        runtime_session_id, runtime_host, runtime_model = self._tool_runtime_identity(request, session)
        invocation = ToolInvocation(
            tool=tool,
            arguments=arguments,
            tenant=tenant,
            client_name=session.client_name,
            host_session_id=runtime_session_id,
            host_name=runtime_host,
            model=runtime_model,
        )

        async with self.gate.hold(principal.org_id):
            async with self.cancels.track_async(session.session_id, tenant.request_id) as token:
                if self.result_reuse.matches(body.get("reuse_validator"), reuse_validator):
                    self._emit(
                        "tool.invoke",
                        AuditDecision.ALLOW,
                        tenant,
                        tool=tool,
                        counts={
                            "content_blocks": 0,
                            "degraded": 0,
                            "admin": int(site is Site.SERVER_ADMIN),
                            "client_cache_reuse": 1,
                        },
                    )
                    return json_response(
                        {
                            "tool": tool,
                            "reuse": True,
                            "reuse_validator": reuse_validator,
                            "view_revision": bound_revision,
                        }
                    )
                if view_id is not None:
                    # A blob miss a proven same-host worktree can satisfy is not
                    # a miss. Filling here keeps `local_fs` uniform across the
                    # sync routes and the tool path; without a grant it is a
                    # no-op and the `{need: [...]}` answer stands.
                    await self._in_worker(self._fill_from_local_fs, principal, view_id)
                try:
                    miss = await self._authority_call(self.blob_miss.resolve, invocation)
                except ServerError as exc:
                    self._emit(
                        "tool.refuse",
                        AuditDecision.REFUSE,
                        tenant,
                        tool=tool,
                        reason_code=exc.code.value,
                        counts={"missing_paths": int(exc.details.get("missing_path_count", 0))},
                    )
                    raise
                if miss is not None:
                    self._emit(
                        "tool.blob_miss",
                        AuditDecision.ALLOW,
                        tenant,
                        tool=tool,
                        counts={"need_paths": len(miss.need)},
                    )
                    return json_response(miss.to_wire())

                try:
                    invocation, workspace = await self._bind_workspace(invocation)
                except ServerError as exc:
                    self._emit("tool.refuse", AuditDecision.REFUSE, tenant, tool=tool, reason_code=exc.code.value)
                    raise

                try:
                    result = await self._dispatch(invocation, token)
                except BlobMissing as exc:
                    self._emit(
                        "tool.blob_miss",
                        AuditDecision.ALLOW,
                        tenant,
                        tool=tool,
                        counts={"need_paths": len(exc.paths)},
                    )
                    return json_response(
                        {
                            "need": list(exc.paths[: self.config.limits.max_need_paths]),
                            "view_id": view_id or "",
                            "view_revision": bound_revision,
                            "retryable_once": True,
                        }
                    )
                except ServerError as exc:
                    self._emit(
                        "tool.refuse",
                        AuditDecision.ERROR if exc.code is ErrorCode.INTERNAL else AuditDecision.REFUSE,
                        tenant,
                        tool=tool,
                        reason_code=exc.code.value,
                    )
                    raise

                signal = await self._authority_call(
                    self.degradation.evaluate,
                    tool=tool,
                    org_id=principal.org_id,
                    view_id=view_id,
                    workspace=workspace,
                )
                if signal is not None:
                    result = result.degrade(signal.wire_reason)

                counts = {
                    "content_blocks": len(result.content),
                    "degraded": int(result.degraded),
                    "admin": int(site is Site.SERVER_ADMIN),
                }
                if workspace is not None:
                    counts.update(workspace.counts())
                self._emit("tool.invoke", AuditDecision.ALLOW, tenant, tool=tool, counts=counts)

                payload = result.to_wire(view_revision=bound_revision)
                if reuse_validator is not None and signal is None and not result.is_error and not result.degraded:
                    payload["reuse_validator"] = reuse_validator
                if signal is not None:
                    payload["degradation"] = signal.to_wire()
                media = _negotiated_stream_media(request.headers.get("accept", ""))
                if media is None:
                    return json_response(payload)
                return await self._stream(request, payload, media=media, token=token)

    async def _bind_workspace(self, invocation: ToolInvocation) -> tuple[ToolInvocation, WorkspaceStatus | None]:
        """Attach the tree this call answers from, and say how complete it is.

        This is the join between the index and the tool surface, and the whole
        point of it is what it refuses to do: it never leaves ``workspace_root``
        unset when a materializer is configured. An unset root sends the public
        dispatcher back to the server's own process directory -- which in Mode A
        is an unrelated checkout, and in every mode is not the revision the
        caller is bound to.

        Order matters and is the reason this runs here rather than beside the
        invocation. Materialization happens *after* the same-host fill and
        *after* the blob-miss guard, so a call that is about to be answered
        ``{need: [...]}`` never pays for a tree, and the tree it does build sees
        whatever the fill just satisfied.

        Run in the dispatch executor: it writes files, and a five-thousand-file
        view would otherwise block the event loop for every other tenant.
        """
        if self.workspace_resolver is not None:
            root = self.workspace_resolver(invocation.tenant)
            return replace(invocation, workspace_root=root), None

        # Native index tools can explicitly prove that their authoritative
        # answer does not require a filesystem tree. Keep this capability on
        # the dispatcher rather than a server-side tool-name allowlist: wrappers
        # that still reach the public registry retain full materialization by
        # default, and a future native tool has to opt in at its implementation
        # boundary before it can bypass the workspace isolation layer.
        requires_workspace = getattr(self.dispatcher, "requires_workspace", None)
        if callable(requires_workspace) and not requires_workspace(invocation):
            view_id = invocation.tenant.view_id
            if view_id is None:
                return invocation, None
            revision = int(invocation.tenant.view_revision or 0)
            status = CoreServerRoutes._content_status(self, invocation.tenant.org_id, view_id, revision)
            if status is None and hasattr(self, "backend"):
                # Sync normally populated this already. A cold/restarted route
                # can still recover the same fact without creating tens of
                # thousands of filesystem entries. Lightweight binder test
                # doubles intentionally omit the backend and keep the old
                # no-status behavior.
                await asyncio.get_running_loop().run_in_executor(
                    self._executor,
                    CoreServerRoutes._still_missing,
                    self,
                    invocation.tenant.principal,
                    view_id,
                )
                status = CoreServerRoutes._content_status(self, invocation.tenant.org_id, view_id, revision)
            return invocation, status

        materializer = self.workspace
        if materializer is None:
            return invocation, None
        view_id = invocation.tenant.view_id
        if view_id is None:
            # Nothing is bound, so there is nothing to answer from. An empty
            # directory is the honest answer; the process directory would let
            # a tool describe the server's own checkout to a tenant.
            return replace(invocation, workspace_root=str(materializer.empty_root)), None
        org_id = invocation.tenant.org_id
        revision = int(invocation.tenant.view_revision or 0)
        exact_paths = _exact_read_paths(invocation.arguments) if invocation.tool == "read" else None
        if exact_paths is not None and hasattr(self, "backend"):
            for path in exact_paths:
                entry = await CoreServerRoutes._authority_call(self, self.backend.views.entry, org_id, view_id, path)
                if entry is None:
                    # A read path may name a directory (or another non-file
                    # selector). Only exact manifest entries are safe for
                    # path-scoped materialization.
                    exact_paths = None
                    break
        if exact_paths is None:
            materialize = partial(materializer.materialize, org_id, view_id, revision)
        else:
            materialize = partial(materializer.materialize_paths, org_id, view_id, revision, exact_paths)
        materialized = await asyncio.get_running_loop().run_in_executor(self._executor, materialize)
        return replace(invocation, workspace_root=str(materialized.root)), materialized.status

    async def _dispatch(self, invocation: ToolInvocation, token: Any) -> ToolResult:
        """Run the synchronous dispatcher off the event loop, with a deadline.

        Cancellation stops the *response*; it cannot interrupt a call already
        running inside the worker thread. The abandoned thread is bounded by
        ``dispatch_workers`` and by the tool deadline, and the client is told
        the truth (``cancelled``) rather than being left waiting.
        """
        loop = asyncio.get_running_loop()
        work: asyncio.Future[ToolResult] = loop.run_in_executor(self._executor, self.dispatcher.dispatch, invocation)
        waiter: asyncio.Future[None] = asyncio.ensure_future(token.wait())
        racing: set[asyncio.Future[Any]] = {work, waiter}
        try:
            done, _pending = await asyncio.wait(
                racing,
                timeout=self.config.limits.tool_deadline_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not waiter.done():
                waiter.cancel()
        if work in done:
            return work.result()
        if waiter in done:
            raise ServerError(
                ErrorCode.CANCELLED,
                "request was cancelled by the client",
                details={"tool": invocation.tool},
            )
        raise ServerError(
            ErrorCode.DEADLINE_EXCEEDED,
            "tool call exceeded the server deadline",
            details={"tool": invocation.tool, "deadline_s": self.config.limits.tool_deadline_s},
        )

    async def _stream(
        self,
        request: web.Request,
        payload: Mapping[str, Any],
        *,
        media: str,
        token: Any,
    ) -> web.StreamResponse:
        """Write the result as bounded frames, never materializing it encoded."""
        frame = ndjson_frame if media == NDJSON_MEDIA_TYPE else sse_frame
        response = web.StreamResponse(status=200, headers={"Content-Type": media, "Cache-Control": "no-store"})
        response.headers[HEADER_REQUEST_ID] = request.get(REQUEST_ID_KEY, "")
        await response.prepare(request)
        await response.write(
            frame(
                {
                    "type": "begin",
                    "tool": payload.get("tool", ""),
                    "view_revision": payload.get("view_revision"),
                    "degraded": bool(payload.get("degraded", False)),
                }
            )
        )
        seq = 0
        written = 0
        try:
            for piece in iter_json_chunks(payload, self.config.limits.stream_chunk_bytes):
                if token.cancelled:
                    await response.write(frame({"type": "error", "code": ErrorCode.CANCELLED.value, "seq": seq}))
                    await response.write_eof()
                    return response
                await response.write(frame({"type": "result_chunk", "seq": seq, "text": piece}))
                seq += 1
                written += len(piece)
            await response.write(frame({"type": "end", "chunks": seq, "chars": written}))
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # The client hung up mid-stream. Nothing to report to it; the
            # absence of an `end` frame is the signal on its side.
            _LOG.debug("client disconnected during stream after %d chunk(s)", seq)
            return response
        await response.write_eof()
        return response

    async def view_open(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        self.authorizer.authorize_sync(principal)
        body = await self._body(request)

        claim = RepoIdentityClaim.from_json(require_object(body, "repo_identity"))
        repo_id = await self._authority_call(self.backend.repos.canonical_repo_id, principal.org_id, claim)
        chunk_ids = require_str_array(body, "manifest_chunks", max_len=4096)
        # Which demonstrated possession this view may read content through, and
        # the one moment it can be decided. The repository identity is the
        # client's own claim -- a root-commit fingerprint is no harder to learn
        # than a digest -- so claiming a colleague's repository must not inherit
        # their content. It does not: unless a binding names this repository as
        # a sharing unit, the view is scoped to what its opener has themselves
        # demonstrated, and a blob a colleague uploaded reads exactly like one
        # nobody has ever held. Fixed here rather than per request because the
        # derived layers and the materialized tree are cached per view, and a
        # reach that widened for one caller would still be cached for the next.
        shared = self.authorizer.scoped.shares_repository(principal, principal.org_id, repo_id)
        result = await self._authority_call(
            self.backend.views.open,
            org_id=principal.org_id,
            repo_id=repo_id,
            base_revision=require_str(body, "base_revision", max_len=128),
            manifest_root=validate_digest(body.get("manifest_root"), "manifest_root"),
            chunk_ids=chunk_ids,
            lease_s=_requested_lease_s(body, self.config.view_lease_s, self.config.view_lease_max_s),
            content_subject="" if shared else principal.subject,
            owner_subject=principal.subject,
        )
        self._note_view_leased(result.state.view_id)
        grant = self.local_fs.negotiate(
            require_object(body, "local_fs") or None,
            session_granted="local_fs" in session.capabilities,
            # The nonce this session was issued, and the only value a proof
            # file may carry. It is what makes the offer a demonstration of
            # write access rather than of having guessed a file's contents.
            issued_token=session.local_fs_proof_token,
        )
        if grant is not None:
            grant_key = (principal.org_id, result.state.view_id)
            self._local_fs_grants[grant_key] = grant
            if claim.fingerprint:
                self._local_fs_repo_fingerprints[grant_key] = claim.fingerprint
            while len(self._local_fs_grants) > self.config.limits.max_sessions:
                evicted_key, _grant = self._local_fs_grants.popitem(last=False)
                self._local_fs_repo_fingerprints.pop(evicted_key, None)
                self._local_fs_complete_revisions.pop(evicted_key, None)
        # The workspace belongs to whoever opened it, from this line onwards.
        # The view store mints a fresh identifier on every open -- a warm open
        # reuses the manifest, never the view -- so this is always a first
        # claim and never contested.
        await self._record_view_owner(
            org_id=principal.org_id,
            view_id=result.state.view_id,
            subject=principal.subject,
        )
        # And this is where the view sits in the org -> team -> repo tree. It
        # is the one moment the data plane knows the repository a view belongs
        # to, and without it the scope directory stays empty: repository- and
        # team-scoped bindings would reach nothing, so the only sharing grant
        # expressible would be a per-view one.
        self.authorizer.scoped.note_view(
            org_id=principal.org_id,
            view_id=result.state.view_id,
            repo_id=repo_id,
        )
        await self._bind_session_view(
            self._access(principal),
            session.session_id,
            result.state.view_id,
            result.state.view_revision,
        )
        tenant = self._tenant(
            request,
            principal,
            session,
            view_id=result.state.view_id,
            view_revision=result.state.view_revision,
        )
        self._emit(
            "view.open",
            AuditDecision.ALLOW,
            tenant,
            counts={
                "manifest_chunks": len(chunk_ids),
                "declared_digests": len(result.declared_digests),
                "warm": int(result.warm),
            },
        )
        payload = result.to_wire()
        if result.state.manifest_complete and self.backend.warm_generation is not None:
            # Hosted nodes can hydrate an immutable generation bundle before
            # the first query. A miss/stale bundle is only an optimization miss;
            # normal deferred derivation remains the source of correctness.
            await self._authority_call(
                self.backend.warm_generation,
                principal.org_id,
                result.state.view_id,
            )
        # Layer-2 says what the view declares; the boundary below it says which
        # of those it may actually read. The join happens here so no store ever
        # answers a question about content outside the asking view's scope --
        # another tenant's, or a colleague's in this one.
        missing = await self._in_worker(self._still_missing, principal, result.state.view_id)
        payload["missing_digests"] = missing
        if result.state.manifest_complete and not missing:
            enqueue_deferred_view(self.backend, principal.org_id, result.state.view_id)
        payload["layers"] = await self._authority_call(
            self.degradation.layer_report, org_id=principal.org_id, view_id=result.state.view_id
        )
        payload["local_fs"] = grant.to_wire() if grant is not None else {"granted": False}
        payload["content_size_cap"] = self.config.limits.max_indexed_content_bytes
        return json_response(payload)

    def _required_entries(self, org_id: str, view_id: str) -> tuple[ManifestEntry, ...]:
        """Membership rows whose content the server actually wants uploaded."""
        membership = self.backend.views.membership(org_id, view_id)
        cap = self.config.limits.max_indexed_content_bytes
        return tuple(membership[path] for path in sorted(membership) if membership[path].size <= cap)

    def _fill_from_local_fs(self, principal: Principal, view_id: str) -> int:
        """Read whatever the proven worktree can still satisfy. Zero when off.

        Called at exactly the points an upload would otherwise be demanded, so
        the capability changes where bytes come from and nothing else -- which
        is why what it reads is recorded as possession exactly as an upload is.
        The client proved same-host access to this worktree before the grant was
        issued and every byte read is verified against the digest the manifest
        declared, so this is a demonstration in precisely the sense the boundary
        means: the bytes were in the server's hand and it hashed them.
        """
        org_id = principal.org_id
        key = (org_id, view_id)
        grant = self._local_fs_grants.get(key)
        if grant is None:
            return 0
        state = self.backend.views.get(org_id, view_id)
        complete_at = self._local_fs_complete_revisions.get(key)
        if state.manifest_complete and complete_at == state.view_revision:
            self._local_fs_complete_revisions.move_to_end(key)
            return 0

        entries = self._required_entries(org_id, view_id)
        outstanding = self._outstanding(org_id, view_id, entries)
        if not outstanding:
            if state.manifest_complete:
                self._local_fs_complete_revisions[key] = state.view_revision
                self._local_fs_complete_revisions.move_to_end(key)
            return 0

        backend = self.backend.for_repository(org_id, state.repo_id)
        filled = self.local_fs.fill(
            grant,
            org_id=org_id,
            entries=entries,
            content=backend.content,
            analysis=backend.analysis,
            needed=outstanding,
            analyze_batch=self._derive_analysis_artifacts_sync,
        )
        self.backend.provenance.record(
            org_id,
            state.repo_id,
            principal.subject,
            filled.filled,
        )
        if state.manifest_complete and not (set(outstanding) - set(filled.filled)):
            self._local_fs_complete_revisions[key] = state.view_revision
            self._local_fs_complete_revisions.move_to_end(key)
            while len(self._local_fs_complete_revisions) > self.config.limits.max_sessions:
                self._local_fs_complete_revisions.popitem(last=False)
        return len(filled.filled)

    def _outstanding(self, org_id: str, view_id: str, entries: Sequence[ManifestEntry]) -> list[str]:
        """Declared digests this view cannot yet address, in manifest order.

        The one answer behind ``missing_digests`` everywhere it appears, and the
        reason it is one function: an over-cap file is never listed, because it
        is manifested and not uploaded by policy, and a digest the organization
        holds under a scope this view cannot reach is listed exactly like one
        nobody has ever uploaded. Asking cannot tell those two apart, which is
        what stops the resume protocol from becoming a content-existence oracle
        pointed at the tenant's own staff.
        """
        wanted = required_digests(entries, size_cap=self.config.limits.max_indexed_content_bytes)
        reachable = addressable(self.backend.views, self.backend.provenance, org_id, view_id, wanted)
        return [digest for digest in wanted if digest not in reachable]

    async def _in_worker(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Run blocking view work off the event loop.

        A same-host fill reads and hashes the developer checkout. On the loop
        that stalls every request, health checks included, and clients see
        ``timed out`` instead of an answer.
        """
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def _authority_call(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Call an authoritative store without penalizing local storage.

        SQLite/memory are deliberately inline; adding an executor hop to every
        local view operation would regress the zero-config path. A hosted hybrid
        backend marks its authority as I/O-bound, at which point lifecycle calls
        run on the same bounded executor already used for blocking view work.
        """
        if not self.backend.authority_io_bound:
            return fn(*args, **kwargs)
        return await self._in_worker(partial(fn, *args, **kwargs))

    async def _content_call(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Call Layer-1 content without imposing a thread hop on local stores."""
        if not self.backend.content_io_bound:
            return fn(*args, **kwargs)
        return await self._in_worker(partial(fn, *args, **kwargs))

    def _derive_analysis_artifacts_sync(
        self, pending: Sequence[tuple[str, str, bytes]]
    ) -> tuple[AnalysisArtifact, ...]:
        """CPU-bound Layer-1 derivation for callers already off the event loop."""
        if not pending:
            return ()
        executor = getattr(self, "_analysis_executor", None)
        if executor is None or len(pending) < _ANALYSIS_PARALLEL_MIN_FILES:
            return _analyze_chunk(pending)
        chunks = tuple(
            tuple(pending[start : start + _ANALYSIS_CHUNK_FILES])
            for start in range(0, len(pending), _ANALYSIS_CHUNK_FILES)
        )
        try:
            futures = tuple(executor.submit(_analyze_chunk, chunk) for chunk in chunks)
            parts = tuple(future.result() for future in futures)
        except Exception as exc:
            # The process pool is an optimization only. A broken/unavailable
            # worker must cost throughput, never correctness or availability.
            _LOG.warning("parallel Layer-1 analysis unavailable; falling back inline: %s", exc)
            return _analyze_chunk(pending)
        return tuple(artifact for part in parts for artifact in part)

    async def _derive_analysis_artifacts(
        self, pending: Sequence[tuple[str, str, bytes]]
    ) -> tuple[AnalysisArtifact, ...]:
        """Derive one validated upload batch, parallel only when it pays."""
        if not pending:
            return ()
        executor = getattr(self, "_analysis_executor", None)
        if executor is None or len(pending) < _ANALYSIS_PARALLEL_MIN_FILES:
            return _analyze_chunk(pending)
        chunks = tuple(
            tuple(pending[start : start + _ANALYSIS_CHUNK_FILES])
            for start in range(0, len(pending), _ANALYSIS_CHUNK_FILES)
        )
        loop = asyncio.get_running_loop()
        try:
            parts = await asyncio.gather(*(loop.run_in_executor(executor, _analyze_chunk, chunk) for chunk in chunks))
        except Exception as exc:
            # Parallel derivation is purely an optimization. If a worker cannot
            # start/import or the pool becomes broken, preserve correctness and
            # availability by deriving the already-validated bounded batch here.
            _LOG.warning("parallel Layer-1 analysis unavailable; falling back inline: %s", exc)
            return _analyze_chunk(pending)
        return tuple(artifact for part in parts for artifact in part)

    def _search_prewarm_work(self, principal: Principal, view_id: str) -> None:
        workspace = self.workspace
        prewarm = getattr(self.dispatcher, "prewarm_workspace", None)
        # A native index dispatcher may expose a filesystem accelerator while
        # explicitly declaring that it is optional. In that composition, eager
        # prewarm would reconstruct the entire repository during sync and erase
        # the cold-start benefit of workspace-independent search.
        if getattr(self.dispatcher, "eager_workspace_prewarm", True) is False:
            return
        if workspace is None or not callable(prewarm):
            return
        try:
            current = self.backend.views.get(principal.org_id, view_id)
            if not current.manifest_complete:
                return
            materialized = workspace.materialize(principal.org_id, view_id, current.view_revision)
            prewarm(materialized.root, current.view_revision)
        except Exception as exc:
            _LOG.debug("search accelerator prewarm skipped for view %s: %s", view_id, exc)

    async def _prewarm_search(self, principal: Principal, view_id: str) -> None:
        """Prepare the initial shard before sync completion returns.

        This shifts the one-time directory-index build into bootstrap, where the
        old benchmark/provider also paid its warmup, instead of charging the
        first user-visible ``code_search``. Accelerator failure is ignored.
        """
        await self._in_worker(self._search_prewarm_work, principal, view_id)

    def _schedule_search_refresh(self, principal: Principal, view_id: str) -> None:
        """Refresh an overlayed View without delaying edit/sync responses."""
        prewarm = getattr(self.dispatcher, "prewarm_workspace", None)
        if self.workspace is None or not callable(prewarm):
            return
        self._executor.submit(self._search_prewarm_work, principal, view_id)

    def _still_missing(self, principal: Principal, view_id: str) -> list[str]:
        """Which digests this view needs and cannot yet address.

        Computed from the view's own membership after any same-host fill, so the
        answer is identical whether the bytes arrived by upload or off disk.
        The same pass records an exact content-completeness status for native
        workspace-free tools. That preserves the old degradation contract
        without reconstructing a filesystem tree merely to count missing paths.
        """
        self._fill_from_local_fs(principal, view_id)
        org_id = principal.org_id
        state = self.backend.views.get(org_id, view_id)
        bound_backend = self.backend.for_view(org_id, view_id)
        reachability = bound_backend.view_content_reachability
        if reachability is not None:
            summary = reachability(org_id, view_id, self.config.limits.max_indexed_content_bytes)
            missing = list(summary.missing_digests)
            required_paths = summary.required_paths
            missing_paths = summary.missing_paths
        else:
            entries = self._required_entries(org_id, view_id)
            missing = self._outstanding(org_id, view_id, entries)
            missing_set = set(missing)
            required_paths = len(entries)
            missing_paths = sum(1 for entry in entries if entry.content_digest in missing_set)
        over_cap_paths = max(0, state.entry_count - required_paths)
        absent_paths = over_cap_paths + missing_paths
        status = WorkspaceStatus(
            view_revision=state.view_revision,
            member_paths=state.entry_count,
            materialized_paths=max(0, state.entry_count - absent_paths),
            absent_paths=absent_paths,
            over_cap_paths=over_cap_paths,
        )
        cache = getattr(self, "_content_status_by_view", None)
        if cache is None:
            cache = {}
            self._content_status_by_view = cache
        cache[(org_id, view_id)] = status
        return missing

    def _content_status(self, org_id: str, view_id: str, view_revision: int) -> WorkspaceStatus | None:
        cache = getattr(self, "_content_status_by_view", None)
        if not isinstance(cache, dict):
            return None
        status = cache.get((org_id, view_id))
        if isinstance(status, WorkspaceStatus) and status.view_revision == view_revision:
            return status
        return None

    async def view_manifest_chunks(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        self.authorizer.authorize_sync(principal)
        view_id = validate_opaque_id(request.match_info["view_id"], "view_id")
        await self._owned_view(request, principal, session, view_id)
        body = await self._body(request)
        entries = _manifest_entries(body, limit=self.config.limits.max_manifest_entries_per_chunk)
        raw_completion_check = body.get("completion_check")
        if raw_completion_check is not None and not isinstance(raw_completion_check, bool):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "completion_check must be a boolean",
                details={"field": "completion_check"},
                action=AgentAction.FIX_REQUEST,
            )
        completion_check = raw_completion_check is not False
        state = await self._authority_call(
            self.backend.views.put_manifest_chunk,
            org_id=principal.org_id,
            view_id=view_id,
            chunk_id=require_str(body, "chunk_id", max_len=128),
            entries=entries,
        )
        tenant = self._tenant(request, principal, session, view_id=view_id, view_revision=state.view_revision)
        self._emit("view.manifest_chunk", AuditDecision.ALLOW, tenant, counts={"entries": len(entries)})
        payload = state.to_wire()
        if completion_check:
            missing_digests = await self._in_worker(self._still_missing, principal, view_id)
            payload["missing_digests"] = missing_digests
            if not missing_digests:
                self._schedule_search_refresh(principal, view_id)
        else:
            # New clients make one explicit completion query after the entire
            # manifest stream. Returning an empty placeholder keeps the response
            # shape stable without doing an O(View) scan per intermediate chunk.
            payload["missing_digests"] = []
        return json_response(payload)

    async def view_missing(self, request: web.Request) -> web.StreamResponse:
        """Resumption primitive: which declared digests are still absent.

        An interrupted upload resumes by asking again rather than by tracking
        byte offsets: uploads are idempotent per digest, so "what do you still
        need" is both the resume point and the integrity check.
        """
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        view_id = validate_opaque_id(request.match_info["view_id"], "view_id")
        # Resumption is a step of the upload protocol, and its answer is a
        # statement about which content this organization holds. It therefore
        # takes the same gate as the upload itself. Until this line existed the
        # route consulted no authorizer at all: a credential was the whole
        # check, so any principal that could authenticate could enumerate a
        # tenant's outstanding digests.
        self.authorizer.authorize_sync(principal, view_id=view_id)
        # "Which digests does this view still need" is a question about one
        # engineer's workspace, and the set of paths behind those digests is
        # their working copy. It is answered only to whoever the view belongs
        # to.
        await self._owned_view(request, principal, session, view_id)
        await self._authority_call(self.backend.views.get, principal.org_id, view_id)
        body = await self._body(request)
        asked = require_str_array(body, "digests", max_len=self.config.limits.max_blobs_per_request)
        for digest in asked:
            validate_digest(digest)
        # The answer is restricted to digests *this view's own manifest*
        # declares. Org scoping already stops one tenant learning about
        # another's content; this stops a caller learning anything about a
        # digest it never supplied a manifest row for, even inside its own
        # organization -- a digest it did not declare always reads as missing.
        #
        # And a digest it *did* declare reads as missing unless this view has
        # demonstrated possession of it, so declaring one is no better a probe
        # than not declaring it: both answers are "upload it", whether or not a
        # colleague already did.
        outstanding = set(await self._in_worker(self._still_missing, principal, view_id))
        declared = set(await self._authority_call(self.backend.views.declared_digests, principal.org_id, view_id))
        answer = [digest for digest in asked if digest in declared and digest in outstanding]
        undeclared = [digest for digest in asked if digest not in declared]
        if not outstanding:
            current = self.backend.views.get(principal.org_id, view_id)
            if current.manifest_complete:
                enqueue_deferred_view(self.backend, principal.org_id, view_id)
            if current.view_revision == 0:
                await self._prewarm_search(principal, view_id)
            else:
                self._schedule_search_refresh(principal, view_id)
        return json_response(
            {
                "missing_digests": answer + undeclared,
                "outstanding_digests": sorted(outstanding),
            }
        )

    async def view_overlay(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        self.authorizer.authorize_sync(principal)
        view_id = validate_opaque_id(request.match_info["view_id"], "view_id")
        await self._owned_view(request, principal, session, view_id)
        body = await self._body(request)
        entries = _manifest_entries(body, limit=self.config.limits.max_overlay_entries)
        removed = require_str_array(body, "removed_paths", max_len=self.config.limits.max_overlay_entries)
        for path in removed:
            validate_path(path)
        state = await self._authority_call(
            self.backend.views.apply_overlay,
            org_id=principal.org_id,
            view_id=view_id,
            expected_view_revision=require_int(body, "expected_view_revision"),
            client_seq=require_int(body, "client_seq", minimum=1),
            entries=entries,
            removed_paths=removed,
        )
        self.backend.links.note_revision(principal.org_id, view_id, state.view_revision)
        self.backend.semantic.note_revision(principal.org_id, view_id, state.view_revision)
        await self._acknowledge_session(self._access(principal), session.session_id, state.view_revision)
        tenant = self._tenant(request, principal, session, view_id=view_id, view_revision=state.view_revision)
        self._emit(
            "view.overlay",
            AuditDecision.ALLOW,
            tenant,
            counts={"entries": len(entries), "removed": len(removed)},
        )
        payload = state.to_wire()
        missing = await self._in_worker(self._still_missing, principal, view_id)
        payload["missing_digests"] = missing
        if state.manifest_complete and not missing:
            enqueue_deferred_view(self.backend, principal.org_id, view_id)
        return json_response(payload)

    async def view_query(self, request: web.Request) -> web.StreamResponse:
        """Query the three layers, bound to an acknowledged revision.

        Bound exactly like a tool call: a query that names a revision the
        server has not committed, or one it has already moved past, is refused
        rather than answered from a view the caller has not observed. "Edit
        then search" is the same contract here as it is on ``/v1/tools``.
        """
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        view_id = validate_opaque_id(request.match_info["view_id"], "view_id")
        # Searching a tenant's index is reading that tenant's content, and this
        # route used to say so nowhere: a credential was the whole check. The
        # same query through ``/v1/tools/code_search`` has always consumed
        # ``content:search``; arriving on this path did not make it free.
        self.authorizer.authorize_permission(principal, "content:search", view_id=view_id)
        # Searching a view is reading it. Whose view it is decides that, not
        # which organization the searcher belongs to.
        await self._owned_view(request, principal, session, view_id)
        body = await self._body(request)
        committed = (await self._authority_call(self.backend.views.get, principal.org_id, view_id)).view_revision
        bound = require_acknowledged_revision(
            session=session,
            committed_revision=committed,
            claimed_revision=self._claimed_revision(request, body),
        )
        await self._in_worker(self._fill_from_local_fs, principal, view_id)

        async with self.gate.hold(principal.org_id):
            answer = await asyncio.get_running_loop().run_in_executor(
                self._executor,
                lambda: self.queries.run(
                    org_id=principal.org_id,
                    view_id=view_id,
                    kind=require_str(body, "kind", max_len=32),
                    query=str(body.get("query", "") or "")[:1024],
                    path=str(body.get("path", "") or "")[:1024],
                    symbol=str(body.get("symbol", "") or "")[:256],
                    limit=int(body.get("limit", 50) or 50) if isinstance(body.get("limit", 50), int) else 50,
                ),
            )

        tenant = self._tenant(request, principal, session, view_id=view_id, view_revision=bound)
        self._emit(
            "index.query",
            AuditDecision.ALLOW,
            tenant,
            counts={
                "hits": len(answer.hits),
                "searched_paths": answer.searched_paths,
                "degraded": int(answer.degraded),
            },
        )
        payload = answer.to_wire()
        payload["view_id"] = view_id
        payload["layers"] = await self._authority_call(
            self.degradation.layer_report, org_id=principal.org_id, view_id=view_id
        )
        return json_response(payload)

    async def view_close(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        view_id = validate_opaque_id(request.match_info["view_id"], "view_id")
        # Everything below this line destroys tenant state: the view, its link
        # and semantic rows, any proven worktree grant, and the materialized
        # tree. It is the most destructive write the data plane has, so it
        # takes the permission every other write to tenant view state takes --
        # not the lifecycle permission a read-only reviewer also holds, and
        # certainly not nothing, which is what it took before.
        self.authorizer.authorize_sync(principal, view_id=view_id)
        # ...and it destroys *someone's* workspace. Holding the permission says
        # a principal may destroy a workspace; it does not say whose.
        #
        #
        # This route used to answer 200 for *any* view id, on the argument that
        # a 404 would make it an enumeration oracle. The argument does not hold
        # and the answer cost more than it bought: an oracle would need "not
        # yours but real" to differ from "never existed", and under the rule
        # above those are one answer -- the only case that differs is a live
        # view the caller owns, which is knowledge the caller already has. What
        # the 200 did buy was a lie ("closed": true, for a workspace still
        # standing) and, downstream, a successful-looking close that the quota
        # middleware meters: a colleague could zero another engineer's
        # workspace accounting with one refused request. It refuses like every
        # other view route now.
        await self._owned_view(request, principal, session, view_id)
        cancel_deferred_view(self.backend, view_id)
        await self._authority_call(self.backend.views.close, principal.org_id, view_id)
        self.backend.links.forget(principal.org_id, view_id)
        self.backend.semantic.forget(principal.org_id, view_id)
        await self._forget_view_owner(principal.org_id, view_id)
        self.authorizer.scoped.forget_view(principal.org_id, view_id)
        self._local_fs_grants.pop((principal.org_id, view_id), None)
        self._local_fs_repo_fingerprints.pop((principal.org_id, view_id), None)
        self._local_fs_complete_revisions.pop((principal.org_id, view_id), None)
        content_status = getattr(self, "_content_status_by_view", None)
        if isinstance(content_status, dict):
            content_status.pop((principal.org_id, view_id), None)
        if self.workspace is not None:
            forget_accelerator = getattr(self.dispatcher, "forget_workspace", None)
            if callable(forget_accelerator):
                await self._in_worker(forget_accelerator, self.workspace.tree_path(principal.org_id, view_id))
            # The materialized copy of a tenant's source goes with the view it
            # was made for. Lease eviction is the backstop for a client that
            # never says goodbye; this is the normal path. Removing a large
            # tree is blocking filesystem work, so keep it off aiohttp's event
            # loop while preserving close's remove-before-response contract.
            await self._in_worker(self.workspace.forget, principal.org_id, view_id)
        tenant = self._tenant(request, principal, session, view_id=view_id)
        self._emit("view.close", AuditDecision.ALLOW, tenant)
        return json_response({"closed": True, "view_id": view_id})

    def _commit_prepared_blobs(
        self,
        org_id: str,
        prepared: Sequence[tuple[str, Mapping[str, Any], str]],
        artifacts: Mapping[tuple[str, str], AnalysisArtifact],
        budget: Any,
        backend: IndexBackend | None = None,
    ) -> list[str]:
        """Commit one validated upload batch, on a worker for remote content.

        The quota context intentionally lives inside this function: its census
        reads the ContentStore before and after the write, so a remote object
        catalogue must not enter/exit that context on aiohttp's event loop.
        """
        backend = self.backend if backend is None else backend
        stored: list[str] = []
        batch = getattr(backend.analysis, "batch", None)
        with budget:
            # The request is already bounded by max_blobs_per_request and
            # max_request_bytes. Keep the validated upload batch in one durable
            # analysis transaction instead of committing once per file; the
            # latter turns a large cold sync into extreme SQLite/WAL write
            # amplification. Stores without a bulk transaction keep their
            # existing behavior through nullcontext().
            with batch() if callable(batch) else nullcontext():
                for digest, raw, profile in prepared:
                    data = _decode_blob(raw, limit=self.config.limits.max_blob_bytes)
                    ref = backend.content.put(org_id, digest, data)
                    backend.analysis.ensure(
                        org_id,
                        digest,
                        profile,
                        data,
                        artifact=artifacts[(digest, profile)],
                    )
                    stored.append(ref.digest)
        return stored

    async def blobs_upload(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        session = await self._resolve_session(request, principal)
        self.authorizer.authorize_sync(principal)
        # An upload stores org-scoped content and names no view in its body,
        # but it may still *carry* one in a header -- and everything around it
        # reads that header: the audit record is scoped by it, and the quota
        # middleware attributes the uploaded bytes to it. A caller naming a
        # colleague's view here was charging their workspace, so the header is
        # resolved and owned like anywhere else rather than being taken on
        # trust because this particular handler has no use for it.
        view_id = self._resolve_view(request, session)
        if view_id is not None:
            await self._owned_view(request, principal, session, view_id)
        body = await self._body(request)
        raw_blobs = require_array(body, "blobs", max_len=self.config.limits.max_blobs_per_request)
        raw_completion_check = body.get("completion_check")
        if raw_completion_check is not None and not isinstance(raw_completion_check, bool):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "completion_check must be a boolean",
                details={"field": "completion_check"},
                action=AgentAction.FIX_REQUEST,
            )
        # Omitted means a legacy client: retain the old post-upload completion
        # scan. New clients mark intermediate batches false and only the final
        # batch true, reducing large sync from O(batches * view) to one O(view)
        # completion check without changing old-client semantics.
        completion_check = raw_completion_check is not False
        backend = (
            self.backend.for_view(principal.org_id, view_id)
            if view_id is not None
            else self.backend.for_unbound(principal.org_id)
        )
        prepared: list[tuple[str, Mapping[str, Any], str]] = []
        contents: dict[str, int] = {}
        artifacts: dict[tuple[str, str], AnalysisArtifact] = {}
        pending_analysis: list[tuple[str, str, bytes]] = []
        pending_keys: set[tuple[str, str]] = set()
        indexed_bytes = 0
        total = 0
        # Validate the whole batch before writing any part of it.
        for raw in raw_blobs:
            if not isinstance(raw, dict):
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "blobs[] entries must be objects",
                    details={"field": "blobs"},
                )
            digest = validate_digest(raw.get("content_digest"))
            data = _decode_blob(raw, limit=self.config.limits.max_blob_bytes)
            if sha256_hex(data) != digest:
                raise ServerError(
                    ErrorCode.BLOB_DIGEST_MISMATCH,
                    "uploaded content does not match the declared digest",
                    action=AgentAction.FIX_REQUEST,
                )
            total += len(data)
            profile = str(raw.get("parser_profile") or "lexical-1")
            # Retain the bounded wire payload rather than all decompressed
            # blobs. Decode each again at commit, one blob at a time.
            prepared.append((digest, raw, profile))
            contents[digest] = backend.content.storage_size(principal.org_id, data)
            key = (digest, profile)
            if key in artifacts or key in pending_keys:
                continue
            artifact = backend.analysis.get(principal.org_id, digest, profile)
            if artifact is None:
                pending_keys.add(key)
                pending_analysis.append((digest, profile, data))
            else:
                artifacts[key] = artifact
        for artifact in await self._derive_analysis_artifacts(pending_analysis):
            key = (artifact.digest, artifact.parser_profile)
            artifacts[key] = artifact
            indexed_bytes += backend.analysis.storage_size(principal.org_id, artifact)
        missing = await self._content_call(backend.content.missing, principal.org_id, tuple(contents))
        content_bytes = sum(contents[digest] for digest in missing)
        budget = self._storage_budget(
            request,
            principal,
            view_id,
            content_bytes=content_bytes,
            indexed_bytes=indexed_bytes,
        )
        stored = await self._content_call(
            self._commit_prepared_blobs,
            principal.org_id,
            prepared,
            artifacts,
            budget,
            backend,
        )

        # This is one of exactly two places where possession is *demonstrated*:
        # the bytes arrived, the store hashed them and refused the ones that did
        # not match. The row is attributed to the engineer who sent them and to
        # the repository their open view belongs to, which is what later makes
        # the digest addressable there and nowhere else. An upload with no view
        # bound records nothing: it is still stored and still deduplicated, but
        # there is no repository to attribute it to, and an unattributable row
        # would read exactly like the repository-wide scope.
        recorded = 0
        if view_id is not None and stored:

            def record_possession() -> int:
                repo_id = self.backend.views.get(principal.org_id, view_id).repo_id
                return self.backend.provenance.record(principal.org_id, repo_id, principal.subject, stored)

            recorded = await self._authority_call(record_possession)

        tenant = self._tenant(request, principal, session, view_id=view_id)
        self._emit(
            "blob.upload",
            AuditDecision.ALLOW,
            tenant,
            counts={"blobs": len(stored), "bytes": total, "provenance_rows": recorded},
        )
        if view_id is not None and completion_check:
            try:
                outstanding = await self._in_worker(self._still_missing, principal, view_id)
            except Exception:
                outstanding = ["unknown"]
            if not outstanding:
                current = self.backend.views.get(principal.org_id, view_id)
                if current.manifest_complete:
                    enqueue_deferred_view(self.backend, principal.org_id, view_id)
                if current.view_revision == 0:
                    await self._prewarm_search(principal, view_id)
                else:
                    self._schedule_search_refresh(principal, view_id)
        return json_response({"stored": stored, "bytes": total})
