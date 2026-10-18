"""Concrete single-user loopback composition for the public LemonCrow server."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from aiohttp import web

from lemoncrow.pro.capabilities.review.gitdiff import RevRange
from lemoncrow.pro.capabilities.review.models import oversized_diff_reason, review_packet_from_dict
from lemoncrow.pro.capabilities.review.review_plan import review_brief_enabled

from .app import (
    HEADER_HOST,
    HEADER_HOST_SESSION,
    HEADER_MODEL,
    HEADER_REQUEST_ID,
    REQUEST_ID_KEY,
    CoreServerRoutes,
    build_analysis_executor,
)
from .blobmiss import BlobMissGuard
from .config import LocalServerConfig
from .context import Principal, TenantContext, validate_opaque_id
from .decisions import AuditDecision
from .degradation import DegradationPolicy
from .dispatch import ToolDispatcher, UnavailableDispatcher
from .errors import AgentAction, ErrorCode, ServerError, error_payload
from .http.wire import json_response, require_int, require_object, require_str
from .index.analysis import profile_for_path
from .index.contracts import IndexBackend, ManifestEntry, sha256_hex
from .index.deferred import cancel_deferred_view, enqueue_deferred_view
from .index.lifecycle import Maintenance
from .index.localfs import LocalFsGrant, LocalFsSource
from .index.manifest import chunk_manifest, manifest_root
from .index.query import IndexQuery
from .limits import CancellationRegistry, ConcurrencyGate
from .local_agent_guard import is_approval_decision, is_judgment_request, peer_agent_host
from .local_browser_auth import (
    LOCAL_BROWSER_CLAIM_PATH,
    LOCAL_BROWSER_PAIR_PATH,
    LOCAL_BROWSER_PAIR_TTL_S,
    LOCAL_BROWSER_SESSION_HEADER,
    LOCAL_BROWSER_SESSION_TTL_S,
    LocalBrowserSessions,
    browser_authorized_path,
    local_browser_refusal,
    local_host_refusal,
)
from .local_review import LocalReviewSurface
from .local_review_service import (  # noqa: F401 -- the title/location helpers are re-exported for callers
    LocalReviewService,
    _automatic_review_title,
    _public_snapshot_number,
    _review_location_metadata,
)
from .local_reviews import LocalReviewReader, LocalReviewRepository
from .protocol import CAPTURE_OPTIONS, require_capture_mode, require_packet_schema, review_capabilities
from .result_reuse import ResultReuseValidator
from .review_derivation import derive_review_packet_from_views, gitlinks_from_wire, review_range_from_wire
from .review_drafts import ReviewDrafts
from .review_jobs import ReviewCaptureJobs
from .service_process import ServiceProcess, service_command
from .service_protocol import (
    HEADER_SERVICE_TOKEN,
    INTERNAL_PREFIX,
    PRINCIPAL_KEY,
    SERVICE_DASHBOARD,
    SERVICE_REVIEW,
    jsonable,
    token_matches,
)
from .service_proxy import LocalServices
from .sessions import Session, SessionAccess, SessionRegistry
from .workspace import ViewMaterializer

__all__ = ["LocalServerApp", "build_local_app"]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.local")
_PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {"/healthz", "/readyz", "/.well-known/lemoncrow-server.json", "/v1/handshake", LOCAL_BROWSER_CLAIM_PATH}
)
_SAFE_BROWSER_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
#: The loopback server captures client-built packets and derives packets from
#: the client's own synced base/head Views (``derive_packet``, as ``lcr`` sends).
LOCAL_CAPTURE_MODES: Final[frozenset[str]] = frozenset({"derived", "packet"})
_DIGEST: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
# What a service may ask of the index this process owns: read-only lookups.
_CODE_INTELLIGENCE_METHODS: Final[frozenset[str]] = frozenset(
    {
        "probe",
        "readiness",
        "find_symbol",
        "neighborhood",
        "file_relationships",
        "references",
        "semantic_search",
        "ranking_metadata",
        "repository_file_links",
        "path_digest",
    }
)


@dataclass(frozen=True, slots=True)
class _LocalViewOwner:
    org_id: str
    view_id: str
    subject: str


class _LocalViewOwners:
    def __init__(self, max_views: int, on_evict: Callable[[_LocalViewOwner], None]) -> None:
        self._max = max_views
        self._on_evict = on_evict
        self._owners: OrderedDict[tuple[str, str], _LocalViewOwner] = OrderedDict()

    def record(self, *, org_id: str, view_id: str, subject: str) -> None:
        key = (org_id, view_id)
        self._owners[key] = _LocalViewOwner(org_id, view_id, subject)
        self._owners.move_to_end(key)
        while len(self._owners) > self._max:
            _key, owner = self._owners.popitem(last=False)
            self._on_evict(owner)

    def owner(self, org_id: str, view_id: str) -> _LocalViewOwner | None:
        key = (org_id, view_id)
        owner = self._owners.get(key)
        if owner is not None:
            self._owners.move_to_end(key)
        return owner

    def forget(self, org_id: str, view_id: str) -> None:
        self._owners.pop((org_id, view_id), None)


class _LocalAuthorizer:
    """Single-principal authorization: tenancy is impossible on loopback local mode."""

    def __init__(self, dispatcher: ToolDispatcher) -> None:
        self._dispatcher = dispatcher

    def allowed_tools(self, principal: Principal) -> frozenset[str]:
        del principal
        return self._dispatcher.tools

    def authorize_tool(self, principal: Principal, tool: str) -> None:
        del principal
        if tool not in self._dispatcher.tools:
            raise ServerError(ErrorCode.TOOL_UNKNOWN, f"{tool} is not registered", details={"tool": tool})

    def authorize_sync(self, principal: Principal, *, view_id: str | None = None) -> None:
        del principal, view_id

    def authorize_permission(self, principal: Principal, permission: str, *, view_id: str | None = None) -> None:
        del principal, permission, view_id


class _LocalAuthenticator:
    def __init__(self, token: str) -> None:
        if len(token) < 16:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "local server token must be at least 16 characters")
        self._digest = hashlib.sha256(token.encode("utf-8")).digest()
        self.principal = Principal(
            subject="user_local",
            org_id="org_local",
            token_id="tok_local",
            auth_method="static_token",
        )

    def authenticate(self, authorization: str | None) -> Principal:
        prefix = "bearer "
        if not authorization or not authorization.lower().startswith(prefix):
            raise ServerError(
                ErrorCode.UNAUTHENTICATED,
                "a bearer credential is required",
                action=AgentAction.REAUTHENTICATE,
            )
        presented = authorization[len(prefix) :].strip()
        digest = hashlib.sha256(presented.encode("utf-8")).digest()
        if not hmac.compare_digest(self._digest, digest):
            raise ServerError(
                ErrorCode.UNAUTHENTICATED,
                "credential is not recognized",
                action=AgentAction.REAUTHENTICATE,
            )
        return self.principal


class LocalServerApp(CoreServerRoutes):
    def _server_identity(self) -> dict[str, Any]:
        payload = super()._server_identity()
        payload["deployment"] = {"mode": "local"}
        dashboard = {
            "installed": self.config.dashboard_enabled,
            "configured": self.config.dashboard_enabled,
            "available": self.config.dashboard_enabled,
        }
        payload["products"] = {
            "core": {"installed": True, "configured": True, "available": True},
            "review": {
                "installed": self.config.review_enabled,
                "configured": self.config.review_enabled,
                "available": self.config.review_enabled,
            },
            "dashboard": dashboard,
            # The web app still gates its Dashboard pages on the Runtime product.
            "runtime": dict(dashboard),
        }
        payload["product_capabilities"] = {}
        if self.config.dashboard_enabled:
            payload["product_capabilities"]["dashboard"] = {
                "code_map": True,
                "code_ask": True,
                "local_editor": True,
                "review_capture": self.config.review_enabled,
                # /api/project-knowledge is a local Review service route.
                "project_knowledge": self.config.review_enabled,
            }
        if self.config.review_enabled:
            payload["product_capabilities"]["review"] = review_capabilities(
                {
                    "local_workspace": True,
                    "workspace_refresh": True,
                    "cross_device_history": False,
                    "collaboration": False,
                    "participants": False,
                    "requests": False,
                    "provider_sync": False,
                    "organization_policy": False,
                    "sharing": False,
                    "guest_review": False,
                    "private_drafts": False,
                    "audit_history": False,
                    "brief": review_brief_enabled(),
                    "ask": self.review_reader.ask_enabled,
                    "capture": True,
                    "evidence_read": True,
                    "evidence_upload": True,
                },
                capture_modes=LOCAL_CAPTURE_MODES,
                capture_options=CAPTURE_OPTIONS,
            )
        return payload

    """Runnable public composition: one loopback user, one shared engine."""

    async def ready(self, request: web.Request) -> web.StreamResponse:
        # No tool dispatcher to wait on: readiness is the engine's storage.
        if self._draining:
            return json_response({"status": "not_ready"}, status=503)
        try:
            await self._in_worker(self._check_readiness_sync)
        except Exception as exc:
            _LOG.warning("readiness check failed: %s", type(exc).__name__)
            return json_response({"status": "not_ready"}, status=503)
        return json_response({"status": "ready"})

    def _tool_runtime_identity(self, request: web.Request, session: Session) -> tuple[str, str, str]:
        sid = request.headers.get(HEADER_HOST_SESSION, "").strip()
        host = request.headers.get(HEADER_HOST, "").strip().lower()
        model = request.headers.get(HEADER_MODEL, "").strip()
        safe_sid = bool(sid) and len(sid) <= 256 and all(ch.isalnum() or ch in "._:-" for ch in sid)
        safe_host = bool(host) and len(host) <= 64 and all(ch.isalnum() or ch in "_-" for ch in host)
        safe_model = len(model) <= 256 and "\r" not in model and "\n" not in model
        if safe_sid and safe_host and safe_model:
            return sid, host, model
        return super()._tool_runtime_identity(request, session)

    def __init__(
        self,
        *,
        config: LocalServerConfig,
        backend: IndexBackend,
        token: str | None,
        dispatcher: ToolDispatcher | None = None,
        workspace: ViewMaterializer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.backend = backend
        self.dispatcher = (
            dispatcher if dispatcher is not None else UnavailableDispatcher("Runtime product is not configured")
        )
        self.local_principal = Principal(
            subject="user_local",
            org_id="org_local",
            token_id="tok_local",
            auth_method="local" if config.local_no_auth else "static_token",
        )
        self.authenticator = None if config.local_no_auth else _LocalAuthenticator(token or "")
        self.browser_sessions = None if config.local_no_auth else LocalBrowserSessions(token or "", clock=clock)
        self.browser_principal = Principal(
            subject="user_local",
            org_id="org_local",
            token_id="browser_local",
            auth_method="local_browser_session",
        )
        self.authorizer = _LocalAuthorizer(self.dispatcher)
        self.clock = clock
        self.workspace_resolver = None
        self.workspace = workspace
        self.sessions = SessionRegistry(
            ttl_s=config.session_ttl_s,
            max_sessions=config.limits.max_sessions,
            clock=clock,
        )
        self.view_owners = _LocalViewOwners(config.limits.max_open_views, self._reclaim_view)
        self.gate = ConcurrencyGate(config.limits)
        self.cancels = CancellationRegistry()
        self.blob_miss = BlobMissGuard(
            backend,
            max_need_paths=config.limits.max_need_paths,
            content_size_cap=config.limits.max_indexed_content_bytes,
        )
        self.degradation = DegradationPolicy(backend)
        self.result_reuse = ResultReuseValidator()
        self.queries = IndexQuery(
            backend,
            content_size_cap=config.limits.max_indexed_content_bytes,
            max_limit=config.limits.max_query_results,
        )
        self.maintenance = Maintenance(
            backend,
            retention_s=config.blob_retention_s,
            clock=clock,
            # A Review is reopened long after capture; the content its stored
            # source trees name must outlive the Views that uploaded it, or its
            # surfaces, previews and restored code intelligence cannot be rebuilt.
            reference_providers=(self._review_references,),
        )
        self.local_fs = LocalFsSource(
            allow=config.allow_local_fs,
            size_cap=config.limits.max_indexed_content_bytes,
            clock=clock,
        )
        self._local_fs_grants: OrderedDict[tuple[str, str], LocalFsGrant] = OrderedDict()
        self._local_fs_repo_fingerprints: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._local_fs_complete_revisions: OrderedDict[tuple[str, str], int] = OrderedDict()
        self.review_repository = LocalReviewRepository(
            config.resolved_review_root,
            source_content_reader=lambda digest: backend.content.get("org_local", digest),
        )
        self.review_reader = LocalReviewReader(
            self.review_repository,
            listen_port=config.listen_port,
            code_intelligence_factory=self._review_code_intelligence,
        )
        self._review_restore_lock = threading.Lock()
        # Review and Dashboard are hosted here only when services are not
        # isolated; otherwise the same routes are forwarded to their workers.
        self.review_service = LocalReviewService(
            self.review_repository,
            self.review_reader,
            principal=self._principal,
            body=self._body,
            run_blocking=self._in_worker,
        )
        # Runtime Dashboard stays outside the Review-only import/package boundary.
        if config.dashboard_enabled:
            from .dashboard_service import DashboardService

            # Looked up per request, so a replaced `_legacy_dashboard` takes effect.
            self.dashboard_service = DashboardService(lambda: self._legacy_dashboard())
        else:
            self.dashboard_service = None
        self.services: LocalServices | None = _isolated_services(config) if config.isolate_services else None
        self.review_surface = LocalReviewSurface(
            config.resolved_review_root,
            port=config.listen_port,
            frontend_dir=config.frontend_dir,
            landing="/home" if config.dashboard_enabled else "/reviews",
        )
        self.review_drafts = ReviewDrafts(
            max_drafts=config.limits.max_review_drafts,
            max_bytes=config.limits.max_review_draft_bytes,
        )
        self.review_capture_jobs = ReviewCaptureJobs()
        self._executor = ThreadPoolExecutor(
            max_workers=config.limits.dispatch_workers,
            thread_name_prefix="lc-tool",
        )
        self._analysis_executor = build_analysis_executor(config.limits.dispatch_workers)
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._draining = False
        self._legacy_dashboard_app: Any | None = None

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def begin_drain(self) -> None:
        self._draining = True

    async def wait_drained(self, timeout_s: float) -> bool:
        if self._in_flight == 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        return True

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except ServerError as exc:
            return json_response(exc.to_wire(), status=exc.status)
        except web.HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            correlation_id = uuid.uuid4().hex
            _LOG.exception("unhandled local server error correlation_id=%s", correlation_id)
            return json_response(
                error_payload(
                    ErrorCode.INTERNAL,
                    f"internal error (correlation_id={correlation_id})",
                    details={"correlation_id": correlation_id},
                ),
                status=500,
            )

    @web.middleware
    async def _lifecycle_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self._draining and request.path not in {"/healthz", "/readyz"}:
            raise ServerError(
                ErrorCode.SHUTTING_DOWN,
                "server is draining and is not accepting new requests",
                action=AgentAction.RETRY_LATER,
            )
        request[REQUEST_ID_KEY] = request.headers.get(HEADER_REQUEST_ID, "").strip() or f"req_{uuid.uuid4().hex}"
        self._in_flight += 1
        self._idle.clear()
        try:
            response = await handler(request)
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.set()
        if not response.prepared:
            response.headers[HEADER_REQUEST_ID] = request[REQUEST_ID_KEY]
        return response

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path.startswith(INTERNAL_PREFIX):
            # The services' back-channel: only a live service token reaches it, and
            # without one it is indistinguishable from a route that does not exist.
            services = self.services
            if services is None or not token_matches(request.headers.get(HEADER_SERVICE_TOKEN, ""), services.tokens()):
                raise ServerError(ErrorCode.TOOL_UNKNOWN, "no such route")
            request[PRINCIPAL_KEY] = self.local_principal
            internal: web.StreamResponse = await handler(request)
            return internal
        # Every other route, whatever the auth mode (--local-no-auth included):
        # the listener is loopback-only, so any other Host is a rebinding page.
        host_refusal = local_host_refusal(request)
        if host_refusal:
            raise ServerError(ErrorCode.FORBIDDEN, host_refusal)
        resource = request.match_info.route.resource
        canonical = "" if resource is None else resource.canonical
        if (
            request.path in _PUBLIC_PATHS
            or canonical in _PUBLIC_PATHS
            or self.review_surface.is_public_path(request.path)
        ):
            return await handler(request)
        if self.config.local_no_auth:
            request[PRINCIPAL_KEY] = self.local_principal
        else:
            browser_sessions = self.browser_sessions
            browser_token = request.headers.get(LOCAL_BROWSER_SESSION_HEADER, "").strip()
            if browser_sessions is not None and browser_token and browser_authorized_path(request.path):
                refusal = local_browser_refusal(
                    request,
                    require_same_origin_context=request.method not in _SAFE_BROWSER_METHODS,
                )
                if not refusal and browser_sessions.verify(browser_token, request.host):
                    request[PRINCIPAL_KEY] = self.browser_principal
                    return await handler(request)
            authenticator = self.authenticator
            if authenticator is None:  # configuration validates this before serving
                raise ServerError(ErrorCode.NOT_CONFIGURED, "local server authentication is not configured")
            request[PRINCIPAL_KEY] = authenticator.authenticate(request.headers.get("Authorization"))
        return await handler(request)

    def _principal(self, request: web.Request) -> Principal:
        principal = request.get(PRINCIPAL_KEY)
        if not isinstance(principal, Principal):
            raise ServerError(ErrorCode.UNAUTHENTICATED, "a bearer credential is required")
        return principal

    def _access(self, principal: Principal) -> SessionAccess:
        return SessionAccess(org_id=principal.org_id, subject=principal.subject)

    def _session(self, request: web.Request, principal: Principal, session_id: str | None = None) -> Session:
        sid = session_id or request.headers.get("X-LemonCrow-Session", "")
        if not sid:
            raise ServerError(
                ErrorCode.SESSION_UNKNOWN,
                "X-LemonCrow-Session is required",
                action=AgentAction.REOPEN_SESSION,
            )
        return self.sessions.touch(self._access(principal), sid)

    async def _owned_view(self, request: web.Request, principal: Principal, session: Session, view_id: str) -> None:
        del request, session
        owner = self.view_owners.owner(principal.org_id, view_id)
        if owner is None or owner.subject != principal.subject:
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "view is not available",
                details={"view_id": view_id},
                action=AgentAction.REOPEN_VIEW,
            )

    def _emit(
        self,
        event: str,
        decision: AuditDecision,
        tenant: TenantContext,
        *,
        tool: str = "",
        reason_code: str = "",
        counts: Mapping[str, int] | None = None,
    ) -> None:
        del event, decision, tenant, tool, reason_code, counts

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

    def _reclaim_view(self, owner: _LocalViewOwner) -> None:
        org_id, view_id = owner.org_id, owner.view_id
        cancel_deferred_view(self.backend, view_id)
        for forget in (
            lambda: self.backend.views.close(org_id, view_id),
            lambda: self.backend.links.forget(org_id, view_id),
            lambda: self.backend.semantic.forget(org_id, view_id),
        ):
            try:
                forget()
            except Exception:
                pass
        self._local_fs_grants.pop((org_id, view_id), None)
        self._local_fs_repo_fingerprints.pop((org_id, view_id), None)
        self._local_fs_complete_revisions.pop((org_id, view_id), None)
        if self.workspace is not None:
            try:
                forget_accelerator = getattr(self.dispatcher, "forget_workspace", None)
                if callable(forget_accelerator):
                    forget_accelerator(self.workspace.tree_path(org_id, view_id))
                self.workspace.forget(org_id, view_id)
            except Exception:
                pass

    def _review_source_root(self, repo_id: str, session: Any) -> Path | None:
        checkout_root = self.review_repository.checkout_root(repo_id)
        if checkout_root is not None:
            return checkout_root

        # Keep the last known checkout useful for historical reviews even after
        # a temporary worktree has been deleted. The marker is a local path hint,
        # not an assertion that the checkout still exists.
        marker = self.review_repository.store_root(repo_id) / ".checkout-root"
        try:
            raw_marker = marker.read_text(encoding="utf-8").strip()
        except OSError:
            raw_marker = ""
        if raw_marker:
            return Path(raw_marker).expanduser().resolve(strict=False)

        raw_repo_root = str(getattr(session, "repo_root", "") or "").strip()
        if raw_repo_root.startswith(("/", "~")):
            return Path(raw_repo_root).expanduser().resolve(strict=False)
        return None

    def _review_code_intelligence(self, repo_id: str, revision: Any) -> Any:
        """Current-index code intelligence for one Review revision."""
        from .review_code_intelligence import NativeReviewCodeIntelligence

        return NativeReviewCodeIntelligence(
            backend=self.backend,
            queries=self.queries,
            org_id=self.local_principal.org_id,
            repo_id=repo_id,
            subject=lambda: self.local_principal.subject,
            content_size_cap=self.config.limits.max_indexed_content_bytes,
            preferred_view_id=str(getattr(revision, "source_view_id", "") or ""),
            preferred_view_revision=int(getattr(revision, "source_view_revision", 0) or 0),
            restore_view=lambda: self._restore_review_view(repo_id, revision),
            keep_view_leased=self._renew_review_view,
        )

    def _scoped_code_intelligence(self, scope: Mapping[str, Any]) -> Any | None:
        project_root = scope.get("project_root")
        if isinstance(project_root, str) and project_root:
            return self._code_intelligence_for_root(Path(project_root))
        repo_id = validate_opaque_id(str(scope.get("repo_id") or ""), "repo_id")
        revision_id = validate_opaque_id(str(scope.get("revision_id") or ""), "revision_id")
        # Re-read per call: a restored View is recorded on the revision row.
        revision = self.review_repository.store(repo_id).get_revision(revision_id)
        return None if revision is None else self._review_code_intelligence(repo_id, revision)

    def _internal_code_intelligence_sync(
        self, scope: Mapping[str, Any], method: str, args: list[Any], kwargs: dict[str, Any]
    ) -> Any:
        provider = self._scoped_code_intelligence(scope)
        if method == "probe":
            return provider is not None
        if provider is None:
            raise ServerError(ErrorCode.VIEW_UNKNOWN, "no native View backs this code-intelligence scope")
        call = getattr(provider, method, None)
        if not callable(call):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, f"code intelligence has no {method}")
        if method == "file_relationships" and args and isinstance(args[0], list):
            args = [tuple(args[0]), *args[1:]]
        result = call(*args, **kwargs)
        return result.to_wire() if method == "readiness" else result

    async def internal_code_intelligence(self, request: web.Request) -> web.StreamResponse:
        """A service's code-intelligence call, answered from the index this process owns."""
        body = await self._body(request)
        scope = require_object(body, "scope")
        method = require_str(body, "method", max_len=64)
        if method not in _CODE_INTELLIGENCE_METHODS:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, f"code intelligence has no {method}")
        args = body.get("args", [])
        kwargs = body.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "args must be a list and kwargs an object")
        result = await self._in_worker(self._internal_code_intelligence_sync, scope, method, args, kwargs)
        return json_response({"result": jsonable(result)})

    async def internal_content(self, request: web.Request) -> web.StreamResponse:
        """Content-addressed bytes a Review needs; the store is keyed by digest alone."""
        digest = request.match_info["digest"]
        if not _DIGEST.fullmatch(digest):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "digest must be a sha256 hex string")
        data = await self._in_worker(self.backend.content.get, self.local_principal.org_id, digest)
        if data is None:
            return web.Response(status=404)
        return web.Response(body=data, content_type="application/octet-stream")

    def start_services(self, core_url: str) -> None:
        if self.services is not None:
            self.services.start(core_url)

    async def stop_services(self) -> None:
        if self.services is not None:
            await self.services.stop()

    def _served_by(
        self, service: str, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def route(request: web.Request) -> web.StreamResponse:
            if self.services is not None:
                return await self.services.forwarder(service)(request)
            return await handler(request)

        return route

    def _review_references(self) -> Mapping[tuple[str, str], frozenset[str]]:
        return self.review_repository.referenced_digests(self.local_principal.org_id)

    def _renew_review_view(self, view_id: str) -> None:
        """Keep a View the Reader navigates through alive while it is in use."""
        if not self._view_lease_due(view_id):
            return
        try:
            self.backend.views.renew(self.local_principal.org_id, view_id, self.config.view_lease_s)
        except ServerError:
            self._view_lease_renewals().pop(view_id, None)

    def _restore_review_view(self, repo_id: str, revision: Any) -> tuple[str, int] | None:
        """Reopen a Review revision's native View from its stored source tree.

        The View a Snapshot was captured through lapses with its lease, taking
        the Reader's code intelligence with it. The revision stores the exact
        tree it named, so opening that manifest again restores it: warm when
        the manifest root is still retained, indexed like any View otherwise.
        Content cleanup has lost makes the restored View report incomplete.
        """
        org_id = self.local_principal.org_id
        subject = self.local_principal.subject
        with self._review_restore_lock:
            store = self.review_repository.store(repo_id)
            current = store.get_revision(str(revision.id))
            if current is None:
                return None
            bound = current.source_view_id
            if bound and bound != getattr(revision, "source_view_id", ""):
                try:
                    # A concurrent request restored it already.
                    return bound, self.backend.views.get(org_id, bound).view_revision
                except ServerError:
                    pass
            tree = store.read_source_tree_artifact(current.review_id, current.id, side="new")
            if not tree:
                return None
            entries = tuple(
                ManifestEntry(
                    path,
                    str(row["content_digest"]),
                    profile_for_path(path),
                    int(row["mode"]),
                    int(row["size"]),
                )
                for path, row in sorted(tree.items())
            )
            chunks = chunk_manifest(entries)
            opened = self.backend.views.open(
                org_id=org_id,
                repo_id=repo_id,
                base_revision=current.head_sha or "review-snapshot",
                manifest_root=manifest_root(entries),
                chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
                lease_s=self.config.view_lease_s,
                content_subject=self._content_subject_for_view(self.local_principal, repo_id),
                owner_subject=subject,
            )
            view_id = opened.state.view_id
            by_id = {chunk.chunk_id: chunk for chunk in chunks}
            for chunk_id in opened.state.pending_chunks:
                self.backend.views.put_manifest_chunk(
                    org_id=org_id,
                    view_id=view_id,
                    chunk_id=chunk_id,
                    entries=by_id[chunk_id].entries,
                )
            self.view_owners.record(org_id=org_id, view_id=view_id, subject=subject)
            self._note_view_scope(org_id=org_id, view_id=view_id, repo_id=repo_id)
            self._note_view_leased(view_id)
            state = self.backend.views.get(org_id, view_id)
            if state.manifest_complete:
                enqueue_deferred_view(self.backend, org_id, view_id)
            store.set_revision_source_view(current.id, view_id=view_id, view_revision=state.view_revision)
            _LOG.info(
                "restored Review source view review=%s revision=%s view=%s", current.review_id, current.id, view_id
            )
            return view_id, state.view_revision

    def _require_local_view(self, principal: Principal, view_id: str) -> Any:
        owner = self.view_owners.owner(principal.org_id, view_id)
        if owner is None or owner.subject != principal.subject:
            raise ServerError(ErrorCode.VIEW_UNKNOWN, "view is not available", details={"view_id": view_id})
        return self.backend.views.get(principal.org_id, view_id)

    def _review_derivation_root(self) -> Path:
        """Private parent for derived-capture scratch repositories, beside the Review stores.

        Each derivation creates and removes its own temporary directory below it;
        nothing here is a Review store, so the stores' directory stays untouched.
        """

        # lc-debt: a process killed mid-derivation leaves its derive-* directory
        # (a copy of the two Views) here; sweep stale derive-* entries at startup.
        root = self.config.resolved_review_root.parent / "review-derive"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return root

    @staticmethod
    def _membership_fingerprint(membership: Mapping[str, Any]) -> str:
        payload = bytearray()
        for path, entry in sorted(membership.items()):
            payload.extend(path.encode("utf-8"))
            payload.extend(b"\0")
            payload.extend(entry.content_digest.encode("ascii"))
            payload.extend(b"\0")
            payload.extend(str(entry.mode).encode("ascii"))
            payload.extend(b"\0")
            payload.extend(str(entry.size).encode("ascii"))
            payload.extend(b"\0")
        return hashlib.sha256(bytes(payload)).hexdigest()

    async def review_capture(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        body, body_size = await self._body_with_size(request)
        draft_raw = request.match_info.get("draft_id")
        assembled, ack = self.review_drafts.accept(
            org_id=principal.org_id,
            subject=principal.subject,
            repo_id=repo_id,
            draft_id=validate_opaque_id(draft_raw, "draft_id") if draft_raw else None,
            body=body,
            size=body_size,
        )
        if assembled is None:
            return json_response(ack or {}, status=202)
        # Capture reads the whole source View and writes the Snapshot: seconds
        # of SQLite and hashing that must not stall the loop's other requests.
        if "respond-async" in request.headers.get("Prefer", "").lower():
            # The client asked not to hold a request open for the capture: answer
            # at once and let it poll, so a big diff on a busy host cannot hit a
            # client timeout. A client that does not ask gets the answer below.
            owner = (principal.org_id, principal.subject, repo_id)

            progress: dict[str, Any] = {}
            loop = asyncio.get_running_loop()

            def report(detail: str) -> None:
                # Worker feedback is applied on the loop that serves job polls.
                loop.call_soon_threadsafe(progress.update, {"detail": detail})

            async def run() -> tuple[int, dict[str, Any]]:
                return 201, await self._in_worker(self._review_capture_sync, principal, repo_id, assembled, report)

            job_id = self.review_capture_jobs.submit(owner, run, progress=progress)
            return json_response({"job_id": job_id, "state": "running", "poll_after_s": 1.0}, status=202)
        captured = await self._in_worker(self._review_capture_sync, principal, repo_id, assembled)
        return json_response(captured, status=201)

    async def review_capture_job(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        job_id = validate_opaque_id(request.match_info["job_id"], "job_id")
        state, status, payload = self.review_capture_jobs.get((principal.org_id, principal.subject, repo_id), job_id)
        if state == "running":
            return json_response({"job_id": job_id, "state": "running", "poll_after_s": 1.0, "progress": dict(payload)})
        if state == "done":
            return json_response({"job_id": job_id, "state": "done", "result": payload})
        return json_response({"job_id": job_id, "state": "failed", "error": payload, "status": status})

    def _review_capture_sync(
        self, principal: Principal, repo_id: str, body: dict[str, Any], progress: Callable[[str], None] | None = None
    ) -> dict[str, Any]:
        if progress is not None:
            progress("Validating snapshot source")
        # Version skew fails closed before anything is read or written.
        require_capture_mode(body, modes=LOCAL_CAPTURE_MODES)
        derive_packet = body.get("derive_packet", False)
        if not isinstance(derive_packet, bool):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "derive_packet must be a boolean",
                details={"field": "derive_packet"},
                action=AgentAction.FIX_REQUEST,
            )
        view_id = validate_opaque_id(require_str(body, "view_id", max_len=128), "view_id")
        view_revision = require_int(body, "view_revision")
        source_ref = require_str(body, "source_ref", max_len=1024)
        packet_raw: dict[str, Any] | None = None
        blobs_raw: Mapping[str, Any] = {}
        range_spec: RevRange | None = None
        base_gitlinks: dict[str, str] = {}
        head_gitlinks: dict[str, str] = {}
        with_impact = True
        if derive_packet:
            # The server builds the packet from the Views, exactly as hosted does;
            # a client-built packet or source blob alongside would be ambiguous.
            if body.get("packet") is not None or body.get("new_blobs") not in (None, {}):
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "derived Review capture must not supply a client-built packet or source blobs",
                    details={"field": "derive_packet"},
                    action=AgentAction.FIX_REQUEST,
                )
            range_spec = review_range_from_wire(require_object(body, "range"))
            base_gitlinks, head_gitlinks = gitlinks_from_wire(
                require_object(body, "gitlinks") if body.get("gitlinks") is not None else {}
            )
            with_impact_raw = body.get("with_impact", True)
            if not isinstance(with_impact_raw, bool):
                raise ServerError(
                    ErrorCode.PAYLOAD_INVALID,
                    "with_impact must be a boolean",
                    details={"field": "with_impact"},
                    action=AgentAction.FIX_REQUEST,
                )
            with_impact = with_impact_raw
        else:
            packet_raw = require_object(body, "packet")
            require_packet_schema(packet_raw)
            blobs_raw = require_object(body, "new_blobs")
        base_view_raw = body.get("base_view_id", "")
        if not isinstance(base_view_raw, str):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "base_view_id must be a string")
        base_view_id = validate_opaque_id(base_view_raw, "base_view_id") if base_view_raw else ""
        if range_spec is not None and range_spec.base_sha and not base_view_id:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "derived Review capture with a non-empty base revision requires base_view_id",
                details={"field": "base_view_id"},
                action=AgentAction.FIX_REQUEST,
            )
        base_revision_raw = body.get("base_view_revision", 0)
        if isinstance(base_revision_raw, bool) or not isinstance(base_revision_raw, int):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "base_view_revision must be an integer")
        restore_archived = body.get("restore_archived", False)
        if not isinstance(restore_archived, bool):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "restore_archived must be a boolean")
        force_new_review = body.get("force_new_review", False)
        if not isinstance(force_new_review, bool):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "force_new_review must be a boolean")
        continue_review_raw = body.get("continue_review_id", "")
        if not isinstance(continue_review_raw, str) or len(continue_review_raw) > 256:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "continue_review_id must be a short string")
        continue_review_id = continue_review_raw.strip()
        reuse_closed_review = body.get("reuse_closed_review", False)
        if not isinstance(reuse_closed_review, bool):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "reuse_closed_review must be a boolean")
        if force_new_review and continue_review_id:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID, "force_new_review and continue_review_id are mutually exclusive"
            )
        client_packet = None
        if packet_raw is not None:
            try:
                client_packet = review_packet_from_dict(packet_raw)
            except ValueError as exc:
                raise ServerError(ErrorCode.PAYLOAD_INVALID, "review packet is invalid") from exc

        view = self._require_local_view(principal, view_id)
        if view.repo_id != repo_id:
            raise ServerError(ErrorCode.VIEW_UNKNOWN, "view is not available", details={"view_id": view_id})
        if view.view_revision != view_revision:
            raise ServerError(
                ErrorCode.VIEW_REVISION_STALE,
                "the source view revision is no longer current",
                details={"claimed_view_revision": view_revision, "committed_view_revision": view.view_revision},
                action=AgentAction.REFRESH_VIEW_REVISION,
            )
        if not view.manifest_complete:
            raise ServerError(ErrorCode.MANIFEST_INCOMPLETE, "the source view manifest is incomplete")
        membership = self.backend.views.membership(principal.org_id, view_id)

        old_source_tree: dict[str, dict[str, int | str]] | None = None
        old_blobs: dict[str, str] = {}
        base_membership: Mapping[str, ManifestEntry] = {}
        if base_view_id:
            base_view = self._require_local_view(principal, base_view_id)
            if base_view.repo_id != repo_id:
                raise ServerError(ErrorCode.VIEW_UNKNOWN, "view is not available", details={"view_id": base_view_id})
            if base_view.view_revision != int(base_revision_raw):
                raise ServerError(ErrorCode.VIEW_REVISION_STALE, "the base source view revision is no longer current")
            if not base_view.manifest_complete:
                raise ServerError(ErrorCode.MANIFEST_INCOMPLETE, "the base source view manifest is incomplete")
            base_membership = self.backend.views.membership(principal.org_id, base_view_id)
            base_digests = tuple(
                sorted(
                    {
                        entry.content_digest
                        for entry in base_membership.values()
                        if entry.size <= self.config.limits.max_indexed_content_bytes
                    }
                )
            )
            if self.backend.content.missing(principal.org_id, base_digests):
                raise ServerError(ErrorCode.BLOB_MISSING, "the committed base View is incomplete for Review capture")
            old_source_tree = {
                path: {"content_digest": entry.content_digest, "mode": entry.mode, "size": entry.size}
                for path, entry in base_membership.items()
            }

        if client_packet is not None:
            packet = client_packet
        else:
            assert range_spec is not None
            # The one derivation hosted runs, over the two Views this principal
            # owns: their bytes, never a client revision string, are diffed.
            packet = derive_review_packet_from_views(
                org_id=principal.org_id,
                repo_id=repo_id,
                content=self.backend.content,
                base_membership=base_membership,
                head_membership=membership,
                range_spec=range_spec,
                scratch_root=self._review_derivation_root(),
                base_gitlinks=base_gitlinks,
                head_gitlinks=head_gitlinks,
                with_impact=with_impact,
                # The identity LocalReviewRepository records for the repository.
                repo_root=f"local:{repo_id}",
            ).packet

        if base_view_id:
            base_size_limit = self.config.limits.max_indexed_content_bytes
            base_payloads = self.backend.content.get_many(
                principal.org_id,
                tuple(
                    entry.content_digest
                    for changed in packet.files
                    if changed.status != "added" and not changed.is_binary and changed.submodule_pointer is None
                    if (entry := base_membership.get(changed.old_path or changed.path)) is not None
                    and entry.size <= base_size_limit
                ),
            )
            for changed in packet.files:
                if changed.status == "added" or changed.is_binary or changed.submodule_pointer is not None:
                    continue
                old_path = changed.old_path or changed.path
                entry = base_membership.get(old_path)
                if entry is None or entry.size > base_size_limit:
                    continue
                payload = base_payloads.get(entry.content_digest)
                if payload is None:
                    raise ServerError(
                        ErrorCode.BLOB_MISSING, "review base content is absent from the committed source view"
                    )
                try:
                    old_blobs[changed.path] = payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ServerError(ErrorCode.PAYLOAD_INVALID, "review base text file is not valid UTF-8") from exc

        new_blobs: dict[str, str] = {}
        binary_new_blobs: dict[str, bytes] = {}
        for key, value in blobs_raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ServerError(ErrorCode.PAYLOAD_INVALID, "new_blobs must map paths to UTF-8 text")
            new_blobs[key] = value
        size_limit = self.config.limits.max_indexed_content_bytes
        packet = replace(
            packet,
            files=tuple(
                (
                    replace(changed, hunks=(), diff_omitted_reason=oversized_diff_reason(size_limit))
                    if changed.path in membership and membership[changed.path].size > size_limit
                    else changed
                )
                for changed in packet.files
            ),
        )
        # One bounded bulk read avoids queuing every changed file separately
        # behind index work while the human waits for the Review to open.
        payloads = self.backend.content.get_many(
            principal.org_id,
            tuple(
                entry.content_digest
                for changed in packet.files
                if changed.status != "deleted" and changed.submodule_pointer is None
                if (entry := membership.get(changed.path)) is not None and entry.size <= size_limit
            ),
        )
        for changed in packet.files:
            entry = membership.get(changed.path)
            if changed.status == "deleted":
                if entry is not None:
                    raise ServerError(
                        ErrorCode.PAYLOAD_INVALID, "review deletion does not match the committed source view"
                    )
                continue
            if changed.submodule_pointer is not None:
                if entry is not None or changed.path in new_blobs:
                    raise ServerError(ErrorCode.PAYLOAD_INVALID, "submodule pointer must not have source-file content")
                continue
            if entry is None:
                raise ServerError(ErrorCode.PAYLOAD_INVALID, "review packet names a file absent from the source view")
            if entry.size > size_limit:
                new_blobs.pop(changed.path, None)
                continue
            payload = payloads.get(entry.content_digest)
            if payload is None:
                raise ServerError(ErrorCode.BLOB_MISSING, "review content is absent from the committed source view")
            if changed.is_binary:
                binary_new_blobs[changed.path] = payload
                continue
            text = new_blobs.get(changed.path)
            if text is None:
                try:
                    text = payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ServerError(ErrorCode.PAYLOAD_INVALID, "review text file is not valid UTF-8") from exc
                new_blobs[changed.path] = text
            if sha256_hex(text.encode("utf-8")) != entry.content_digest:
                raise ServerError(ErrorCode.BLOB_DIGEST_MISMATCH, "review content does not match the source view")

        source_digests = tuple(
            sorted({entry.content_digest for entry in membership.values() if entry.size <= size_limit})
        )
        if self.backend.content.missing(principal.org_id, source_digests):
            raise ServerError(ErrorCode.BLOB_MISSING, "the committed source View is incomplete for Review capture")
        source_tree = {
            path: {"content_digest": entry.content_digest, "mode": entry.mode, "size": entry.size}
            for path, entry in membership.items()
        }
        captured_source_fingerprint = body.get("source_fingerprint", "")
        if not isinstance(captured_source_fingerprint, str) or len(captured_source_fingerprint) > 256:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "source_fingerprint must be a short string")
        # The same source in the identity form Snapshots were recorded under before
        # the tree-id form; matched only while that compatibility lasts (gitdiff).
        legacy_source_fingerprint = body.get("legacy_source_fingerprint", "")
        if not isinstance(legacy_source_fingerprint, str) or len(legacy_source_fingerprint) > 256:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "legacy_source_fingerprint must be a short string")
        workspace_branch = body.get("workspace_branch", "")
        if (
            not isinstance(workspace_branch, str)
            or len(workspace_branch) > 256
            or "\n" in workspace_branch
            or "\r" in workspace_branch
        ):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "workspace_branch must be a short single-line string")
        workspace_branch = workspace_branch.strip()
        # A scoped Review (one agent session's files in a shared worktree) is its
        # own workstream: it must neither continue nor take over the branch's
        # Review, so it routes by a worktree key derived from the scope.
        review_scope = body.get("review_scope", "")
        if (
            not isinstance(review_scope, str)
            or len(review_scope) > 200
            or not re.fullmatch(r"[A-Za-z0-9._:@/-]*", review_scope)
        ):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "review_scope must be a short identifier")
        if review_scope:
            workspace_branch = ""
        supplied_title = body.get("title", "")
        if not isinstance(supplied_title, str):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "title must be a string")
        supplied_title = supplied_title.strip()
        if len(supplied_title) > 120 or any(ord(char) < 32 for char in supplied_title):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "title must be at most 120 characters on one line")
        local_grant = self._local_fs_grants.get((principal.org_id, view_id))
        worktree_root = str(local_grant.root) if local_grant is not None else ""
        worktree_key = (
            hashlib.sha256(f"{worktree_root}\0{review_scope}".encode("utf-8", errors="surrogateescape")).hexdigest()[
                :32
            ]
            if review_scope
            else (
                hashlib.sha256(worktree_root.encode("utf-8", errors="surrogateescape")).hexdigest()[:32]
                if local_grant is not None
                else ""
            )
        )
        capture = self.review_repository.capture(
            repo_id=repo_id,
            reviewer_id=principal.subject,
            source_ref=source_ref,
            packet=packet,
            new_blobs=new_blobs,
            old_blobs=old_blobs,
            binary_new_blobs=binary_new_blobs,
            source_tree=source_tree,
            old_source_tree=old_source_tree,
            source_fingerprint=self._membership_fingerprint(membership),
            source_state_fingerprint=captured_source_fingerprint,
            legacy_source_fingerprint=legacy_source_fingerprint,
            source_view_id=view_id,
            source_view_revision=view_revision,
            checkout_root=local_grant.root if local_grant is not None else None,
            title=str(body.get("title") or ""),
            restore_archived=restore_archived,
            workspace_branch=workspace_branch,
            worktree_key=worktree_key,
            force_new_review=force_new_review,
            continue_review_id=continue_review_id,
            reuse_closed_review=reuse_closed_review,
            progress=progress,
        )
        if supplied_title and supplied_title.lower() not in {"working tree", "staged changes"}:
            self.review_repository.set_author_snapshot_title(repo_id, capture.revision.id, supplied_title)
        self.review_repository.set_workspace_root_hint(repo_id, capture.session.id, packet.repo_root)
        if local_grant is not None:
            self.review_repository.set_review_checkout_root(repo_id, capture.session.id, local_grant.root)
        if workspace_branch:
            self.review_repository.set_workspace_branch_hint(repo_id, capture.session.id, workspace_branch)
        review = self.review_service.review_session_wire(repo_id, capture.session)
        return {
            "review": review,
            "revision": {
                "id": capture.revision.id,
                "short_id": self.review_repository.short_revision_reference(capture.revision.id),
                "revision_number": _public_snapshot_number(capture.revision),
                "created_at": capture.revision.created_at,
            },
            "revision_created": capture.revision_created,
        }

    async def review_attach_base(self, request: web.Request) -> web.StreamResponse:
        """Attach the exact old-side tree after a Review is already visible.

        This keeps base snapshot materialization off time-to-first-diff without
        weakening Review identity: the revision and its new-side bytes already
        exist, and this endpoint only adds the immutable tree needed by Compare
        and executable old-side surfaces.
        """

        principal = self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        revision_id = validate_opaque_id(request.match_info["revision_id"], "revision_id")
        body = await self._body(request)
        attached = await self._in_worker(
            self._review_attach_base_sync, principal, repo_id, review_id, revision_id, body
        )
        return json_response(attached)

    def _review_attach_base_sync(
        self, principal: Principal, repo_id: str, review_id: str, revision_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        store, session = self.review_repository.require_review(repo_id, review_id)
        revision = store.get_revision(revision_id)
        if revision is None or revision.review_id != session.id:
            raise ServerError(
                ErrorCode.REVIEW_UNKNOWN,
                "the Review revision does not exist",
                details={"review_id": review_id, "revision_id": revision_id},
            )

        base_view_id = validate_opaque_id(require_str(body, "base_view_id", max_len=128), "base_view_id")
        base_view_revision = require_int(body, "base_view_revision")
        base_view = self._require_local_view(principal, base_view_id)
        if base_view.repo_id != repo_id:
            raise ServerError(ErrorCode.VIEW_UNKNOWN, "base Review view is not available")
        if base_view.view_revision != base_view_revision:
            raise ServerError(ErrorCode.VIEW_REVISION_STALE, "the base Review view revision is no longer current")
        if not base_view.manifest_complete:
            raise ServerError(ErrorCode.MANIFEST_INCOMPLETE, "the base Review view manifest is incomplete")

        membership = self.backend.views.membership(principal.org_id, base_view_id)
        digests = tuple(
            sorted(
                {
                    entry.content_digest
                    for entry in membership.values()
                    if entry.size <= self.config.limits.max_indexed_content_bytes
                }
            )
        )
        if self.backend.content.missing(principal.org_id, digests):
            raise ServerError(ErrorCode.BLOB_MISSING, "the committed base Review View is incomplete")
        old_source_tree = {
            path: {"content_digest": entry.content_digest, "mode": entry.mode, "size": entry.size}
            for path, entry in membership.items()
        }
        store.write_source_tree_artifact(session.id, revision.id, old_source_tree, side="old")
        return {
            "review_id": session.id,
            "revision_id": revision.id,
            "base_view_id": base_view_id,
            "base_view_revision": base_view_revision,
            "attached": True,
        }

    @staticmethod
    def _mentions_lemoncrow(path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                return False
            return "lemoncrow" in path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            return False

    @staticmethod
    def _display_integration_path(path: Path, home: Path) -> str:
        try:
            return f"~/{path.relative_to(home).as_posix()}"
        except ValueError:
            return str(path)

    def _local_integration_component(
        self,
        *,
        component_id: str,
        label: str,
        description: str,
        paths: tuple[Path, ...],
        home: Path,
        match: str = "exists",
    ) -> dict[str, Any]:
        present_path: Path | None = None
        for path in paths:
            if match == "mentions":
                present = self._mentions_lemoncrow(path)
            elif match == "file":
                present = path.is_file()
            elif match == "dir":
                present = path.is_dir()
            else:
                present = path.exists()
            if present:
                present_path = path
                break
        display_path = present_path or (paths[0] if paths else None)
        return {
            "id": component_id,
            "label": label,
            "description": description,
            "present": present_path is not None,
            "path": self._display_integration_path(display_path, home) if display_path is not None else None,
        }

    def _local_integration_rows(self) -> tuple[list[dict[str, Any]], bool]:
        home = Path.home()
        status_path = home / ".lemoncrow" / "hosts" / "status.json"
        cached: dict[str, str] = {}
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cached = {str(key): str(value) for key, value in raw.items()}
        except (OSError, ValueError, TypeError):
            cached = {}

        xdg = home / ".config"
        specs = (
            (
                "claude",
                "Claude Code",
                "Claude plugin and MCP integration.",
                ("claude",),
                (home / ".claude.json", home / ".claude" / "settings.json"),
                "supported",
            ),
            (
                "codex",
                "Codex",
                "Codex MCP registration and LemonCrow plugin context.",
                ("codex",),
                (home / ".codex" / "config.toml", home / ".codex" / "plugins" / "lemoncrow" / ".mcp.json"),
                "supported",
            ),
            (
                "opencode",
                "OpenCode",
                "OpenCode MCP integration and imported-session support.",
                ("opencode",),
                (xdg / "opencode" / "opencode.json", xdg / "opencode" / "opencode.jsonc"),
                "supported",
            ),
            (
                "lemoncode",
                "LemonCode",
                "Public managed LemonCode host; Hosted execution chooses its frontend separately.",
                ("lemoncode",),
                (home / ".lemoncrow" / "bin" / "lemoncode-host", home / ".lemoncrow" / "bin" / "lemoncode-host.json"),
                "supported",
            ),
            (
                "pi",
                "Pi",
                "Default LemonCrow-managed coding frontend.",
                (),
                (home / ".lemoncrow" / "hosts" / "pi-host.json",),
                "supported",
            ),
            (
                "copilot",
                "VS Code / Copilot",
                "VS Code MCP integration and generated instructions.",
                ("code",),
                (xdg / "Code" / "User" / "mcp.json",),
                "supported",
            ),
            (
                "cursor",
                "Cursor",
                "Cursor MCP integration and project guidance.",
                ("cursor", "cursor-agent"),
                (home / ".cursor" / "mcp.json",),
                "supported",
            ),
            (
                "antigravity",
                "Antigravity",
                "Antigravity integration is not part of the current setup wizard yet.",
                ("antigravity", "agy"),
                (xdg / "Antigravity" / "User" / "mcp.json", xdg / "Code" / "User" / "mcp.json"),
                "coming_soon",
            ),
            (
                "hermes",
                "Hermes Agent",
                "Hermes integration is not part of the current setup wizard yet.",
                ("hermes",),
                (home / ".hermes" / "config.yaml",),
                "coming_soon",
            ),
        )
        component_specs: dict[str, tuple[tuple[str, str, str, tuple[Path, ...], str], ...]] = {
            "claude": (
                (
                    "mcp",
                    "MCP registration",
                    "Short-name lc MCP server registered with Claude.",
                    (home / ".claude.json", home / ".claude" / "settings.json"),
                    "mentions",
                ),
                (
                    "plugin",
                    "Plugin bundle",
                    "LemonCrow Claude plugin package.",
                    (home / ".lemoncrow" / "claude-plugin" / ".claude-plugin" / "plugin.json",),
                    "file",
                ),
                (
                    "hooks",
                    "Lifecycle hooks",
                    "Claude session and tool lifecycle hooks.",
                    (home / ".lemoncrow" / "claude-plugin" / "hooks" / "hooks.json",),
                    "file",
                ),
            ),
            "codex": (
                (
                    "mcp",
                    "MCP registration",
                    "LemonCrow MCP server exposed through the Codex plugin.",
                    (home / ".codex" / "plugins" / "lemoncrow" / ".mcp.json",),
                    "file",
                ),
                (
                    "plugin",
                    "Plugin bundle",
                    "Installed LemonCrow Codex plugin.",
                    (home / ".codex" / "plugins" / "lemoncrow" / ".codex-plugin" / "plugin.json",),
                    "file",
                ),
                (
                    "hooks",
                    "Lifecycle hooks",
                    "Codex session, prompt, tool, and stop hooks.",
                    (home / ".codex" / "plugins" / "lemoncrow" / "hooks" / "hooks.json",),
                    "file",
                ),
                (
                    "instructions",
                    "AGENTS instructions",
                    "LemonCrow guidance merged into Codex instructions.",
                    (home / ".codex" / "AGENTS.md",),
                    "mentions",
                ),
            ),
            "opencode": (
                (
                    "mcp",
                    "MCP registration",
                    "lc MCP server in the OpenCode config.",
                    (xdg / "opencode" / "opencode.json", xdg / "opencode" / "opencode.jsonc"),
                    "mentions",
                ),
                (
                    "plugin",
                    "Prompt plugin",
                    "Prompt-time LemonCrow nudge plugin.",
                    (xdg / "opencode" / "plugins" / "lemoncrow-nudge.js",),
                    "file",
                ),
            ),
            "lemoncode": (
                (
                    "binary",
                    "Managed host",
                    "LemonCode rollback host binary managed by LemonCrow.",
                    (home / ".lemoncrow" / "bin" / "lemoncode-host",),
                    "file",
                ),
                (
                    "metadata",
                    "Release metadata",
                    "Installed LemonCode host release metadata.",
                    (home / ".lemoncrow" / "bin" / "lemoncode-host.json",),
                    "file",
                ),
            ),
            "pi": (
                (
                    "binary",
                    "Managed host",
                    "Checksum-pinned Pi host binary installed by LemonCrow.",
                    (home / ".lemoncrow" / "bin" / "pi-host",),
                    "file",
                ),
                (
                    "metadata",
                    "Release metadata",
                    "Pinned Pi release and verification metadata.",
                    (home / ".lemoncrow" / "hosts" / "pi-host.json",),
                    "file",
                ),
                (
                    "settings",
                    "Isolated runtime settings",
                    "Fail-closed Pi settings created when the managed host runs.",
                    (home / ".lemoncrow" / "hosts" / "pi" / "config" / "settings.json",),
                    "file",
                ),
            ),
            "copilot": (
                (
                    "mcp",
                    "MCP registration",
                    "LemonCrow MCP server in VS Code.",
                    (xdg / "Code" / "User" / "mcp.json",),
                    "mentions",
                ),
                (
                    "instructions",
                    "Copilot instructions",
                    "LemonCrow instructions available to Copilot.",
                    (home / ".copilot" / "instructions" / "lemoncrow.instructions.md",),
                    "file",
                ),
                (
                    "tasks",
                    "VS Code tasks",
                    "LemonCrow task wiring in the VS Code task file.",
                    (xdg / "Code" / "User" / "tasks.json",),
                    "mentions",
                ),
            ),
            "cursor": (
                (
                    "mcp",
                    "MCP registration",
                    "Global LemonCrow MCP server used by Cursor IDE and CLI.",
                    (home / ".cursor" / "mcp.json",),
                    "mentions",
                ),
                (
                    "hooks",
                    "Lifecycle hooks",
                    "Cursor session-start and stop hooks.",
                    (home / ".cursor" / "hooks.json",),
                    "mentions",
                ),
            ),
        }

        rows: list[dict[str, Any]] = []
        for host_id, label, description, commands, _config_paths, support in specs:
            detected = any(shutil.which(command) is not None for command in commands)
            if host_id == "cursor":
                detected = detected or (home / ".cursor").is_dir()
            elif host_id == "pi":
                detected = (home / ".lemoncrow" / "bin" / "pi-host").is_file()
            elif host_id == "hermes":
                detected = detected or (home / ".hermes" / "config.yaml").is_file()

            cached_status = cached.get(host_id)
            components = [
                self._local_integration_component(
                    component_id=component_id,
                    label=component_label,
                    description=component_description,
                    paths=component_paths,
                    home=home,
                    match=match,
                )
                for component_id, component_label, component_description, component_paths, match in component_specs.get(
                    host_id, ()
                )
            ]
            if host_id == "opencode":
                agents_dir = xdg / "opencode" / "agents"
                agent_paths = sorted(agents_dir.glob("lemoncrow.*.md")) if agents_dir.is_dir() else []
                legacy_agent = agents_dir / "code.md"
                if not agent_paths and legacy_agent.is_file():
                    agent_paths = [legacy_agent]
                components.extend(
                    {
                        "id": f"agent:{path.stem}",
                        "label": f"Agent · {path.stem.removeprefix('lemoncrow.')}",
                        "description": "Installed LemonCrow OpenCode agent role.",
                        "present": True,
                        "path": self._display_integration_path(path, home),
                    }
                    for path in agent_paths
                )
            configured = support == "supported" and any(component["present"] for component in components)
            rows.append(
                {
                    "id": host_id,
                    "label": label,
                    "description": description,
                    "detected": detected,
                    "configured": configured,
                    "status": "configured" if configured else ("detected" if detected else "not_detected"),
                    "cached_status": cached_status,
                    "support": support,
                    "components": components,
                }
            )
        return rows, status_path.is_file()

    def _advertised_mcp_tools(self) -> list[dict[str, Any]]:
        # Runtime owns MCP advertisement policy. The Review-only engine does not
        # ship that policy module and never imports it.
        if not self.config.dashboard_enabled:
            return []
        from .runtime_product_surface import advertised_runtime_mcp_tools

        return advertised_runtime_mcp_tools()

    async def local_integrations(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        hosts, cached_status_available = self._local_integration_rows()
        tools = self._advertised_mcp_tools()
        return json_response(
            {
                "hosts": hosts,
                "tools": tools,
                # Runtime Lite serves tools inside each agent's own MCP
                # process, so an agent's connection never waits on this server.
                "dispatcher_available": True,
                "cached_status_available": cached_status_available,
                "internal_dispatchable_count": len(tools),
            }
        )

    async def local_system_status(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        ready = not self._draining
        if ready:
            try:
                await self._in_worker(self._check_readiness_sync)
            except Exception:
                ready = False
        identity = self._server_identity()
        advertised_tools = self._advertised_mcp_tools()
        return json_response(
            {
                "status": "draining" if self._draining else "ok",
                "ready": ready,
                "dispatcher_available": self.dispatcher.available,
                "protocol_version": str(identity.get("protocol_version") or ""),
                "capabilities": list(identity.get("capabilities") or []),
                "tool_count": len(advertised_tools),
                "review_count": await self._in_worker(self.review_service.review_count),
                "allow_local_fs": self.config.allow_local_fs,
                "listen": f"{self.config.listen_host}:{self.config.listen_port}",
            }
        )

    async def local_browser_pair(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        browser_sessions = self.browser_sessions
        if browser_sessions is None:
            raise ServerError(ErrorCode.UNAUTHENTICATED, "local browser pairing is not configured")
        refusal = local_browser_refusal(request)
        if refusal:
            raise ServerError(ErrorCode.FORBIDDEN, refusal, action=AgentAction.REQUEST_ACCESS)
        body = await self._body(request)
        path = require_str(body, "path", max_len=2048)
        if not self.review_surface.is_ui_path(path):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "local browser pairing path must be a Review UI path",
                action=AgentAction.FIX_REQUEST,
            )
        browser_sessions.arm(path)
        if path == "/":
            # ``/`` redirects before the SPA claims, so arm where it lands too.
            browser_sessions.arm(self.review_surface.landing)
        return json_response({"state": "armed", "expires_in": int(LOCAL_BROWSER_PAIR_TTL_S)})

    async def local_browser_claim(self, request: web.Request) -> web.StreamResponse:
        refusal = local_browser_refusal(request, require_same_origin_context=True)
        if refusal:
            raise ServerError(ErrorCode.FORBIDDEN, refusal, action=AgentAction.REQUEST_ACCESS)
        body = await self._body(request)
        path = require_str(body, "path", max_len=2048)
        if not self.review_surface.is_ui_path(path):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "local browser claim path must be a Review UI path",
                action=AgentAction.FIX_REQUEST,
            )
        browser_sessions = self.browser_sessions
        if browser_sessions is None:
            if self.config.local_no_auth:
                # Test/demo loopback servers deliberately have no machine
                # credential to derive an origin-bound browser capability from.
                # The middleware already trusts every request in this explicit
                # mode, so return a non-secret no-op capability that lets the
                # normal Reader bootstrap without a special frontend path.
                return json_response(
                    {
                        "token": "local-no-auth",
                        "expires_at": int(time.time() + LOCAL_BROWSER_SESSION_TTL_S),
                    }
                )
            raise ServerError(ErrorCode.UNAUTHENTICATED, "local browser pairing is not configured")
        # Headers alone cannot prove a browser: any local process can send a
        # loopback Origin. A fresh capability therefore needs either a CLI
        # pairing armed with the machine bearer in the last few seconds, or a
        # still-valid capability (renewal keeps reloads and long sessions
        # working without re-arming).
        presented = request.headers.get(LOCAL_BROWSER_SESSION_HEADER, "").strip()
        if presented:
            token, expires_at = browser_sessions.renew(presented, request.host)
        else:
            token, expires_at = browser_sessions.claim(path, request.host)
        return json_response({"token": token, "expires_at": expires_at})

    def _legacy_dashboard(self) -> Any:
        from .dashboard_service import default_dashboard_app

        if self._legacy_dashboard_app is None:
            self._legacy_dashboard_app = default_dashboard_app(self._code_intelligence_for_root)
        return self._legacy_dashboard_app

    def _code_intelligence_for_root(self, project_root: Path) -> Any | None:
        """The native View kept in sync for *project_root*, or ``None``.

        A View belongs to a worktree through the same-host grant its client
        proved for that directory; the newest grant wins. Without one, `/code`
        falls back to the legacy engine and labels it.
        """

        from .review_code_intelligence import NativeReviewCodeIntelligence

        org_id = self.local_principal.org_id
        subject = self.local_principal.subject
        try:
            target = project_root.resolve()
            owned = set(self.backend.views.view_ids_for_user(org_id, subject))
        except Exception:
            return None
        best: tuple[float, str, Any] | None = None
        for (grant_org, view_id), grant in list(self._local_fs_grants.items()):
            if grant_org != org_id or view_id not in owned:
                continue
            try:
                if Path(grant.root).resolve() != target:
                    continue
                state = self.backend.views.get(org_id, view_id)
            except Exception:
                continue
            if best is None or float(grant.granted_at) > best[0]:
                best = (float(grant.granted_at), view_id, state)
        if best is None:
            return None
        _granted, view_id, state = best
        return NativeReviewCodeIntelligence(
            backend=self.backend,
            queries=self.queries,
            org_id=org_id,
            repo_id=state.repo_id,
            subject=lambda: self.local_principal.subject,
            content_size_cap=self.config.limits.max_indexed_content_bytes,
            preferred_view_id=view_id,
            preferred_view_revision=int(state.view_revision),
        )

    async def legacy_dashboard_api(self, request: web.Request) -> web.StreamResponse:
        """The Runtime Dashboard service, when the Dashboard is enabled."""

        self._principal(request)
        if not self.config.dashboard_enabled or self.dashboard_service is None:
            raise web.HTTPNotFound(text="the LemonCrow Dashboard is not enabled")
        if self.services is not None:
            return await self.services.forwarder(SERVICE_DASHBOARD)(request)
        return await self.dashboard_service.handle(request)

    async def review_reader_api(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        if principal.auth_method != self.browser_principal.auth_method and is_approval_decision(
            request.method, request.path
        ):
            # P12-AGENT: approving an agent's risky action needs the human. Unlike
            # other judgment, an unresolved caller is refused (fail closed).
            host = await asyncio.to_thread(peer_agent_host, request.transport)
            if host != "":
                caller = f"a coding-agent process ({host})" if host else "a caller that could not be identified"
                raise ServerError(
                    ErrorCode.FORBIDDEN,
                    f"approving or denying an agent action cannot come from {caller}; "
                    "decide it in the Review Reader in your browser",
                    action=AgentAction.ABANDON,
                )
        elif principal.auth_method != self.browser_principal.auth_method and is_judgment_request(
            request.method, request.path
        ):
            host = await asyncio.to_thread(peer_agent_host, request.transport)
            if host is None:
                # lc-debt: fail-open where the caller cannot be resolved
                # (unsupported platform, restricted /proc, and on macOS a caller
                # that closes before lsof sees it); the CLI guard still applies
                # there. Upgrade: an OS peer-credential lookup.
                _LOG.debug("review judgment caller unresolved; allowing %s %s", request.method, request.path)
            elif host:
                raise ServerError(
                    ErrorCode.FORBIDDEN,
                    "this records human review judgment and cannot come from a coding-agent process "
                    f"({host}); use the Review Reader or your own terminal",
                    action=AgentAction.ABANDON,
                )
        tail = str(request.match_info.get("tail") or "")
        review_roots = ("reviews", "revisions", "annotations", "evidence", "proposals")
        if any(tail == root or tail.startswith(f"{root}/") for root in review_roots) or tail == "compare":
            if self.services is not None:
                return await self.services.forwarder(SERVICE_REVIEW)(request)
            return await self.review_service.reader_api(request)
        return await self.legacy_dashboard_api(request)

    def build(self) -> web.Application:
        app = web.Application(
            middlewares=[self._error_middleware, self._lifecycle_middleware, self._auth_middleware],
            client_max_size=self.config.limits.max_request_bytes * 2,
        )
        app.add_routes(list(self.core_route_defs(include_tools=False)))
        if self.review_surface.available:
            ui = self.review_surface.ui
            app.add_routes(
                [
                    web.get("/", self.review_surface.home),
                    web.get("/review", ui),
                    web.get("/reviews", ui),
                    web.get("/settings", ui),
                    web.get("/settings/{tail:.*}", ui),
                    web.get("/system", ui),
                    web.get("/system/{tail:.*}", ui),
                    web.get("/reviews/{tail:.*}", ui),
                    web.get("/r/{tail:.*}", ui),
                    web.get("/rr/{tail:.*}", ui),
                    web.get("/assets/{tail:.*}", self.review_surface.asset),
                    *self.review_surface.public_asset_route_defs(),
                ]
            )
            if self.config.dashboard_enabled:
                # Runtime Dashboard pages: never served when only Review runs.
                app.add_routes(
                    [
                        web.get("/home", ui),
                        web.get("/runs", ui),
                        web.get("/runs/{tail:.*}", ui),
                        web.get("/code", ui),
                        web.get("/knowledge", ui),
                        web.get("/knowledge/{tail:.*}", ui),
                        web.get("/usage", ui),
                    ]
                )
        product_routes: list[web.RouteDef] = [web.get("/api/local/system", self.local_system_status)]
        if self.config.dashboard_enabled:
            product_routes.append(web.get("/api/local/integrations", self.local_integrations))
        if self.config.review_enabled:
            product_routes.extend(
                [
                    web.post(LOCAL_BROWSER_PAIR_PATH, self.local_browser_pair),
                    web.post(LOCAL_BROWSER_CLAIM_PATH, self.local_browser_claim),
                    web.get("/v1/reviews/manifest", self.review_manifest),
                    web.post("/v1/repos/{repo_id}/reviews", self.review_capture),
                    web.post("/v1/repos/{repo_id}/reviews/drafts/{draft_id}", self.review_capture),
                    web.get("/v1/repos/{repo_id}/reviews/jobs/{job_id}", self.review_capture_job),
                    web.post(
                        "/v1/repos/{repo_id}/reviews/{review_id}/revisions/{revision_id}/base-view",
                        self.review_attach_base,
                    ),
                    *(
                        web.route(method, path, self._served_by(SERVICE_REVIEW, handler))
                        for method, path, handler in self.review_service.route_table()
                    ),
                    web.post(INTERNAL_PREFIX + "code-intelligence", self.internal_code_intelligence),
                    web.get(INTERNAL_PREFIX + "content/{digest}", self.internal_content),
                    web.route("*", "/api/{tail:.*}", self.review_reader_api),
                ]
            )
        elif self.config.dashboard_enabled:
            product_routes.append(web.route("*", "/api/{tail:.*}", self.legacy_dashboard_api))
        app.add_routes(product_routes)
        app.on_shutdown.append(self._on_shutdown)
        return app

    async def _on_shutdown(self, app: web.Application) -> None:
        del app
        if not self.draining:
            self.begin_drain()
            await self.wait_drained(self.config.shutdown_timeout_s)
        await self.review_capture_jobs.close(self.config.shutdown_timeout_s)
        index_worker = getattr(self, "index_worker", None)
        if index_worker is not None:
            await asyncio.to_thread(index_worker.close)
        # Cancelling an asyncio job does not stop its running worker thread.
        # Finish those writes before closing the dispatcher and workspace.
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
        if self._analysis_executor is not None:
            self._analysis_executor.shutdown(wait=True, cancel_futures=True)
            self._analysis_executor = None
        close_dispatcher = getattr(self.dispatcher, "close", None)
        if callable(close_dispatcher):
            close_dispatcher()
        if self.workspace is not None:
            self.workspace.close()


def build_local_app(
    *,
    config: LocalServerConfig,
    backend: IndexBackend,
    token: str | None,
    dispatcher: ToolDispatcher | None = None,
    workspace: ViewMaterializer | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[LocalServerApp, web.Application]:
    state = LocalServerApp(
        config=config,
        backend=backend,
        token=token,
        dispatcher=dispatcher,
        workspace=workspace,
        clock=clock,
    )
    return state, state.build()


def _isolated_services(config: LocalServerConfig) -> LocalServices:
    """Review and Dashboard as worker processes over the same state root."""

    assert config.state_root is not None
    extra = (
        "--state-root",
        str(config.state_root),
        "--review-root",
        str(config.resolved_review_root),
        "--max-request-bytes",
        str(config.limits.max_request_bytes),
        "--maintenance-interval-s",
        str(config.maintenance_interval_s),
    )
    names: list[str] = []
    if config.review_enabled:
        names.append(SERVICE_REVIEW)
    if config.dashboard_enabled:
        names.append(SERVICE_DASHBOARD)
    return LocalServices(ServiceProcess(name, service_command(name, extra=extra)) for name in names)
