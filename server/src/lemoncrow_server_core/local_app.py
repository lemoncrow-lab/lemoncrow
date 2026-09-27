"""Concrete single-user loopback composition for the public LemonCrow server."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from aiohttp import web

from lemoncrow.pro.capabilities.review.models import oversized_diff_reason, review_packet_from_dict
from lemoncrow.pro.capabilities.review.relationships import change_context_payload
from lemoncrow.pro.capabilities.review.revisions import compute_frontier
from lemoncrow.pro.capabilities.review.sources.local import read_packet_json
from lemoncrow.pro.capabilities.review.targets import derive_review_targets, review_progress

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
from .dispatch import ToolDispatcher
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
from .local_agent_guard import is_judgment_request, peer_agent_host
from .local_browser_auth import (
    LOCAL_BROWSER_CLAIM_PATH,
    LOCAL_BROWSER_PAIR_PATH,
    LOCAL_BROWSER_PAIR_TTL_S,
    LOCAL_BROWSER_SESSION_HEADER,
    LocalBrowserSessions,
    browser_authorized_path,
    local_browser_refusal,
)
from .local_review import LocalReviewSurface
from .local_reviews import LocalReviewReader, LocalReviewRepository
from .registry_dispatch import PublicRegistryDispatcher
from .result_reuse import ResultReuseValidator
from .review_drafts import ReviewDrafts
from .sessions import Session, SessionAccess, SessionRegistry
from .workspace import ViewMaterializer

__all__ = ["LocalServerApp", "build_local_app"]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.server.local")
_PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {"/healthz", "/readyz", "/.well-known/lemoncrow-server.json", "/v1/handshake", LOCAL_BROWSER_CLAIM_PATH}
)
_SAFE_BROWSER_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})
PRINCIPAL_KEY: Final[web.RequestKey[Principal]] = web.RequestKey("lemoncrow_local_principal", Principal)


def _git_stdout(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _public_snapshot_number(revision: Any | None) -> int:
    """Translate one-based persistence ordinals to the public Snapshot number."""

    if revision is None:
        return 0
    return max(0, int(getattr(revision, "revision_number", 0) or 0) - 1)


def _review_location_hint(root: str) -> dict[str, Any]:
    """Project a packet workspace path without treating it as readable source.

    The path is client-supplied display metadata.  Do not resolve it, stat it, or
    run Git against it here: only a negotiated local-fs grant can authorize
    filesystem reads.
    """

    path = Path(root).expanduser()
    if not path.is_absolute():
        return {}
    name = path.name or str(path)
    return {
        "directory": str(path.parent),
        "project": name,
        "project_root": str(path),
        "worktree": name,
        "worktree_root": str(path),
        "branch": "",
        "revision": "",
        "detached": False,
    }


def _review_location_metadata(root: Path) -> dict[str, Any]:
    """Describe the exact proven checkout/worktree behind a local review."""

    resolved = root.expanduser().resolve(strict=False)
    branch = ""
    revision = ""
    project_root = resolved
    if resolved.is_dir():
        branch = _git_stdout(resolved, "branch", "--show-current")
        revision = _git_stdout(resolved, "rev-parse", "--short", "HEAD")
        common_dir_raw = _git_stdout(resolved, "rev-parse", "--git-common-dir")
        if common_dir_raw:
            common_dir = Path(common_dir_raw)
            if not common_dir.is_absolute():
                common_dir = (resolved / common_dir).resolve(strict=False)
            if common_dir.name == ".git":
                project_root = common_dir.parent

    return {
        "directory": str(resolved.parent),
        "project": project_root.name,
        "project_root": str(project_root),
        "worktree": resolved.name,
        "worktree_root": str(resolved),
        "branch": branch or (f"detached@{revision}" if revision else "unknown"),
        "revision": revision,
        "detached": not bool(branch),
    }


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


class _LocalScope:
    def shares_repository(self, principal: Principal, org_id: str, repo_id: str) -> bool:
        del repo_id
        return principal.org_id == org_id

    def note_view(self, *, org_id: str, view_id: str, repo_id: str) -> None:
        del org_id, view_id, repo_id

    def forget_view(self, org_id: str, view_id: str) -> None:
        del org_id, view_id


class _LocalAuthorizer:
    """Single-principal authorization: tenancy is impossible on loopback local mode."""

    def __init__(self, dispatcher: ToolDispatcher) -> None:
        self._dispatcher = dispatcher
        self.scoped = _LocalScope()

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
        payload["product_capabilities"] = {
            "review": {
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
            }
        }
        return payload

    """Runnable public composition: one loopback user, one shared engine."""

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
        self.dispatcher = dispatcher if dispatcher is not None else PublicRegistryDispatcher()
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
        from .review_code_intelligence import NativeReviewCodeIntelligence

        self.review_reader = LocalReviewReader(
            self.review_repository,
            listen_port=config.listen_port,
            code_intelligence_factory=lambda repo_id, revision: NativeReviewCodeIntelligence(
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
            ),
        )
        self._review_restore_lock = threading.Lock()
        self.review_surface = LocalReviewSurface(
            config.resolved_review_root,
            port=config.listen_port,
            frontend_dir=config.frontend_dir,
        )
        self.review_drafts = ReviewDrafts(
            max_drafts=config.limits.max_review_drafts,
            max_bytes=config.limits.max_review_draft_bytes,
        )
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

    def _access(self, principal: Principal, *, cross_subject: bool = False) -> SessionAccess:
        return SessionAccess(org_id=principal.org_id, subject=principal.subject, cross_subject=cross_subject)

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

    def _review_location_wire(self, repo_id: str, session: Any) -> dict[str, Any]:
        """Human identity for a Change, independent of source-read authority."""

        hint = self.review_repository.workspace_root_hint(repo_id, str(session.id))
        branch_hint = self.review_repository.workspace_branch_hint(repo_id, str(session.id))
        trusted = self.review_repository.checkout_root(repo_id)
        if hint:
            if trusted is not None:
                try:
                    if trusted == Path(hint).expanduser().resolve(strict=False):
                        return _review_location_metadata(trusted)
                except (OSError, ValueError):
                    pass
            hinted = _review_location_hint(hint)
            if hinted:
                if branch_hint:
                    hinted["branch"] = branch_hint
                return hinted
        if trusted is not None:
            return _review_location_metadata(trusted)
        return {
            "directory": "",
            "project": "",
            "project_root": "",
            "worktree": "",
            "worktree_root": "",
            "branch": "",
            "revision": "",
            "detached": False,
        }

    def _review_session_wire(
        self,
        repo_id: str,
        session: Any,
        *,
        location: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        revision = self.review_repository.store(repo_id).latest_revision(session.id)
        short_id = self.review_repository.short_review_reference(session.id)
        if location is None:
            location = self._review_location_wire(repo_id, session)
        return {
            "id": session.id,
            "short_id": short_id,
            "ref": f"r/{short_id}",
            "object": "change",
            "change_ref": f"r/{short_id}",
            "review_path": f"/r/{short_id}",
            "repo_id": repo_id,
            "title": session.title,
            "subject_type": session.subject_type,
            "source_ref": session.source_ref,
            "range_mode": session.range_mode,
            "actor_type": session.actor_type,
            "status": session.status,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "revision_number": _public_snapshot_number(revision),
            "revision_id": "" if revision is None else revision.id,
            **location,
        }

    def _review_progress_wire(self, repo_id: str, session: Any, revision: Any | None) -> dict[str, int]:
        if revision is None:
            return {
                "target_count": 0,
                "reviewed": 0,
                "changed_since_review": 0,
                "needs_changes": 0,
                "unreviewed": 0,
                "unknown": 0,
                "mechanical": 0,
            }
        store = self.review_repository.store(repo_id)
        units = store.list_units(revision.id)
        marks = store.list_marks(session.id, reviewer_id=session.reviewer_id)
        annotations = store.list_annotations(session.id)
        frontier = compute_frontier(
            session.id,
            session.reviewer_id,
            revision,
            units,
            marks,
            annotations=annotations,
        )
        progress = review_progress(
            derive_review_targets(
                units,
                frontier.entries,
                read_packet_json(store, revision),
                annotations=annotations,
            )
        )
        return {
            "target_count": progress.target_count,
            "reviewed": progress.reviewed,
            "changed_since_review": progress.changed_since_review,
            "needs_changes": progress.needs_changes,
            "unreviewed": progress.unreviewed,
            "unknown": progress.unknown,
            "mechanical": progress.mechanical,
        }

    def _review_rows(self, *, status: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for repo_id in self.review_repository.list_repository_ids():
            sessions = self.review_repository.list_reviews(repo_id, status=status, limit=None)
            if not sessions:
                continue
            rows.extend(self._review_session_wire(repo_id, session) for session in sessions)
        rows.sort(key=lambda row: (str(row["updated_at"]), str(row["id"])), reverse=True)
        return rows

    async def review_inbox(self, request: web.Request) -> web.StreamResponse:
        """List local Reviews without projecting hosted collaboration state."""

        self._principal(request)
        status = request.query.get("status", "all").strip() or "all"
        if status not in {"all", "open", "finished", "archived"}:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "status must be one of all, open, finished, archived")
        query = request.query.get("q", "").strip().lower()
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError as exc:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "limit must be an integer") from exc
        limit = max(1, min(limit, 100))
        # Listing opens every repository's Review store: off the loop.
        page_rows = await self._in_worker(self._review_inbox_page, status, query, limit)
        return json_response(
            {
                "reviews": page_rows,
                "status": status,
                "query": query,
                "next_cursor": "",
            }
        )

    def _review_inbox_page(self, status: str, query: str, limit: int) -> list[dict[str, Any]]:
        rows = self._review_rows(status="" if status == "all" else status)
        if query:
            terms = query.split()
            search_keys = (
                "id",
                "repo_id",
                "title",
                "source_ref",
                "directory",
                "project",
                "worktree",
                "branch",
                "worktree_root",
            )
            rows = [
                row
                for row in rows
                if all(term in " ".join(str(row.get(key) or "") for key in search_keys).lower() for term in terms)
            ]
        page_rows: list[dict[str, Any]] = []
        for row in rows[:limit]:
            store, session = self.review_repository.require_review(str(row["repo_id"]), str(row["id"]))
            revision = store.latest_revision(session.id)
            projected = dict(row)
            projected["progress"] = self._review_progress_wire(str(row["repo_id"]), session, revision)
            page_rows.append(projected)
        return page_rows

    async def review_list(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        return json_response({"reviews": await self._in_worker(self._review_list_rows, repo_id)})

    def _review_list_rows(self, repo_id: str) -> list[dict[str, Any]]:
        return [
            self._review_session_wire(repo_id, row) for row in self.review_repository.list_reviews(repo_id, limit=200)
        ]

    async def review_get(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        return json_response(await self._in_worker(self._review_get_payload, repo_id, review_id))

    def _review_get_payload(self, repo_id: str, review_id: str) -> dict[str, Any]:
        store, session = self.review_repository.require_review(repo_id, review_id)
        revision = store.latest_revision(session.id)
        return {
            "review": self._review_session_wire(repo_id, session),
            "change": change_context_payload(store, session),
            "revision": (
                None
                if revision is None
                else {
                    "id": revision.id,
                    "object": "snapshot",
                    "snapshot_ref": f"rr/{revision.id}",
                    "revision_number": _public_snapshot_number(revision),
                    "base_sha": revision.base_sha,
                    "head_sha": revision.head_sha,
                    "merge_base_sha": revision.merge_base_sha,
                    "source_fingerprint": revision.source_fingerprint,
                    "dirty": revision.dirty,
                    "created_at": revision.created_at,
                }
            ),
            "revision_count": len(store.list_revisions(session.id)),
        }

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
            shared = self.authorizer.scoped.shares_repository(self.local_principal, org_id, repo_id)
            opened = self.backend.views.open(
                org_id=org_id,
                repo_id=repo_id,
                base_revision=current.head_sha or "review-snapshot",
                manifest_root=manifest_root(entries),
                chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
                lease_s=self.config.view_lease_s,
                content_subject="" if shared else subject,
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
            self.authorizer.scoped.note_view(org_id=org_id, view_id=view_id, repo_id=repo_id)
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
        captured = await self._in_worker(self._review_capture_sync, principal, repo_id, assembled)
        return json_response(captured, status=201)

    def _review_capture_sync(self, principal: Principal, repo_id: str, body: dict[str, Any]) -> dict[str, Any]:
        view_id = validate_opaque_id(require_str(body, "view_id", max_len=128), "view_id")
        view_revision = require_int(body, "view_revision")
        source_ref = require_str(body, "source_ref", max_len=1024)
        packet_raw = require_object(body, "packet")
        blobs_raw = require_object(body, "new_blobs")
        base_view_raw = body.get("base_view_id", "")
        if not isinstance(base_view_raw, str):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "base_view_id must be a string")
        base_view_id = validate_opaque_id(base_view_raw, "base_view_id") if base_view_raw else ""
        base_revision_raw = body.get("base_view_revision", 0)
        if isinstance(base_revision_raw, bool) or not isinstance(base_revision_raw, int):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "base_view_revision must be an integer")
        restore_archived = body.get("restore_archived", False)
        if not isinstance(restore_archived, bool):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "restore_archived must be a boolean")
        try:
            packet = review_packet_from_dict(packet_raw)
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
            base_size_limit = self.config.limits.max_indexed_content_bytes
            for changed in packet.files:
                if changed.status == "added" or changed.is_binary or changed.submodule_pointer is not None:
                    continue
                old_path = changed.old_path or changed.path
                entry = base_membership.get(old_path)
                if entry is None or entry.size > base_size_limit:
                    continue
                payload = self.backend.content.get(principal.org_id, entry.content_digest)
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
            payload = self.backend.content.get(principal.org_id, entry.content_digest)
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
        workspace_branch = body.get("workspace_branch", "")
        if (
            not isinstance(workspace_branch, str)
            or len(workspace_branch) > 256
            or "\n" in workspace_branch
            or "\r" in workspace_branch
        ):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "workspace_branch must be a short single-line string")
        workspace_branch = workspace_branch.strip()
        local_grant = self._local_fs_grants.get((principal.org_id, view_id))
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
            source_view_id=view_id,
            source_view_revision=view_revision,
            checkout_root=local_grant.root if local_grant is not None else None,
            title=str(body.get("title") or ""),
            restore_archived=restore_archived,
        )
        self.review_repository.set_workspace_root_hint(repo_id, capture.session.id, packet.repo_root)
        if workspace_branch:
            self.review_repository.set_workspace_branch_hint(repo_id, capture.session.id, workspace_branch)
        review = self._review_session_wire(repo_id, capture.session)
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

    async def review_enrich_projection(self, request: web.Request) -> web.StreamResponse:
        """Identity-safe metadata enrichment for an already-visible revision."""

        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        revision_id = validate_opaque_id(request.match_info["revision_id"], "revision_id")
        body = await self._body(request)
        packet = require_object(body, "packet")
        revision = await self._in_worker(
            lambda: self.review_repository.enrich_projection(
                repo_id=repo_id,
                review_id=review_id,
                revision_id=revision_id,
                packet=packet,
            )
        )
        return json_response(
            {
                "review_id": review_id,
                "revision_id": revision.id,
                "revision_number": _public_snapshot_number(revision),
                "enriched": True,
            }
        )

    async def review_update_preparation(self, request: web.Request) -> web.StreamResponse:
        """Publish source-first preparation state for the waiting Reader."""

        self._principal(request)
        repo_id = validate_opaque_id(request.match_info["repo_id"], "repo_id")
        review_id = validate_opaque_id(request.match_info["review_id"], "review_id")
        body = await self._body(request)
        preparation_id = require_str(body, "preparation_id", max_len=128).strip()
        stage = require_str(body, "state", max_len=64).strip()
        detail_raw = body.get("detail", "")
        if not isinstance(detail_raw, str):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "detail must be a string")
        await self._in_worker(
            lambda: self.review_repository.update_preparation(
                repo_id=repo_id,
                review_id=review_id,
                preparation_id=preparation_id,
                stage=stage,
                detail=detail_raw,
            )
        )
        return json_response({"review_id": review_id, "preparation_id": preparation_id, "state": stage})

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

        xdg = Path.home() / ".config"
        specs: tuple[tuple[str, str, str, tuple[str, ...], tuple[Path, ...]], ...] = (
            (
                "claude",
                "Claude Code",
                "Claude plugin and MCP integration.",
                ("claude",),
                (home / ".claude.json", home / ".claude" / "settings.json"),
            ),
            (
                "codex",
                "Codex",
                "Codex MCP registration and LemonCrow plugin context.",
                ("codex",),
                (home / ".codex" / "config.toml",),
            ),
            (
                "opencode",
                "OpenCode",
                "OpenCode MCP integration and imported-session support.",
                ("opencode",),
                (xdg / "opencode" / "opencode.json", xdg / "opencode" / "opencode.jsonc"),
            ),
            (
                "lemoncode",
                "LemonCode",
                "LemonCrow-managed coding host.",
                ("lemoncode",),
                (xdg / "lemoncode" / "opencode.json", xdg / "lemoncode" / "opencode.jsonc"),
            ),
            (
                "copilot",
                "VS Code / Copilot",
                "VS Code MCP integration and generated instructions.",
                ("code",),
                (xdg / "Code" / "User" / "mcp.json",),
            ),
            (
                "antigravity",
                "Antigravity",
                "Antigravity MCP integration and companion workflow.",
                ("antigravity", "agy"),
                (xdg / "Antigravity" / "User" / "mcp.json", xdg / "Code" / "User" / "mcp.json"),
            ),
            (
                "cursor",
                "Cursor",
                "Cursor MCP integration and project guidance.",
                ("cursor", "cursor-agent"),
                (home / ".cursor" / "mcp.json",),
            ),
            (
                "hermes",
                "Hermes Agent",
                "Global Hermes MCP registration.",
                ("hermes",),
                (home / ".hermes" / "config.yaml",),
            ),
        )

        rows: list[dict[str, Any]] = []
        for host_id, label, description, commands, config_paths in specs:
            detected = any(shutil.which(command) is not None for command in commands)
            if host_id == "cursor":
                detected = detected or (home / ".cursor").is_dir()
            if host_id == "hermes":
                detected = detected or (home / ".hermes" / "config.yaml").is_file()
            cached_status = cached.get(host_id)
            configured = cached_status == "installed" or any(self._mentions_lemoncrow(path) for path in config_paths)
            rows.append(
                {
                    "id": host_id,
                    "label": label,
                    "description": description,
                    "detected": detected,
                    "configured": configured,
                    "status": "configured" if configured else ("detected" if detected else "not_detected"),
                    "cached_status": cached_status,
                }
            )
        return rows, status_path.is_file()

    @staticmethod
    def _advertised_mcp_tools() -> list[dict[str, Any]]:
        # The server execution matrix is deliberately broader than the MCP
        # surface shown to coding agents. Reuse the canonical MCP advertisement
        # policy so this UI matches tools/list exactly instead of leaking hidden
        # implementation capabilities into the operator view.
        from lemoncrow.gateway.tools.registry import advertised_tools

        return [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
            }
            for tool in advertised_tools()
        ]

    async def local_integrations(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        hosts, cached_status_available = self._local_integration_rows()
        return json_response(
            {
                "hosts": hosts,
                "tools": self._advertised_mcp_tools(),
                "dispatcher_available": self.dispatcher.available,
                "cached_status_available": cached_status_available,
                "internal_dispatchable_count": len(self.dispatcher.tools),
            }
        )

    async def local_system_status(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        ready = not self._draining and self.dispatcher.available
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
                "review_count": len(await self._in_worker(self._review_rows)),
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
        return json_response({"state": "armed", "expires_in": int(LOCAL_BROWSER_PAIR_TTL_S)})

    async def local_browser_claim(self, request: web.Request) -> web.StreamResponse:
        browser_sessions = self.browser_sessions
        if browser_sessions is None:
            raise ServerError(ErrorCode.UNAUTHENTICATED, "local browser pairing is not configured")
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
        # Every UI route is served by this loopback origin. A Review deep link
        # needs the same browser access as the directory, including on reload
        # after its previous capability has expired.
        token, expires_at = browser_sessions.issue(request.host)
        return json_response({"token": token, "expires_at": expires_at})

    def _legacy_dashboard(self) -> Any:
        if self._legacy_dashboard_app is None:
            from lemoncrow.core.service.api import create_app

            self._legacy_dashboard_app = create_app()
        return self._legacy_dashboard_app

    async def legacy_dashboard_api(self, request: web.Request) -> web.StreamResponse:
        """Bridge the existing dashboard API into the one-process loopback server.

        Browser authentication terminates at this aiohttp server. The legacy
        FastAPI application is invoked in-process against the same LemonCrow
        store; no second listener or daemon is started.
        """

        self._principal(request)
        app = self._legacy_dashboard()
        body = await request.read()
        tail = str(request.match_info.get("tail") or "")
        path = "/" + tail.lstrip("/")

        headers: list[tuple[bytes, bytes]] = []
        for key, value in request.raw_headers:
            lowered = key.lower()
            if lowered in {
                b"authorization",
                b"x-lemoncrow-browser-session",
                b"host",
                b"content-length",
                b"connection",
            }:
                continue
            headers.append((lowered, value))

        from lemoncrow.core.service.config import cfg as legacy_cfg

        if legacy_cfg.require_auth and legacy_cfg.api_key:
            headers.append((b"authorization", f"Bearer {legacy_cfg.api_key}".encode()))
        headers.append((b"host", request.host.encode("latin-1", errors="ignore")))

        transport = request.transport
        peer = transport.get_extra_info("peername") if transport is not None else None
        sock = transport.get_extra_info("sockname") if transport is not None else None
        client = (str(peer[0]), int(peer[1])) if isinstance(peer, tuple) and len(peer) >= 2 else None
        server = (str(sock[0]), int(sock[1])) if isinstance(sock, tuple) and len(sock) >= 2 else None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": f"{request.version.major}.{request.version.minor}",
            "method": request.method,
            "scheme": request.scheme,
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": request.query_string.encode("latin-1"),
            "root_path": "",
            "headers": headers,
            "client": client,
            "server": server,
            "state": {},
        }
        received = False

        async def receive() -> dict[str, Any]:
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}

        status = 500
        response_headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, response_headers
            message_type = message.get("type")
            if message_type == "http.response.start":
                status = int(message.get("status", 500))
                response_headers = list(message.get("headers") or [])
            elif message_type == "http.response.body":
                chunks.append(bytes(message.get("body") or b""))

        await app(scope, receive, send)
        outgoing: dict[str, str] = {}
        for key, value in response_headers:
            name = key.decode("latin-1")
            if name.lower() in {"content-length", "connection", "transfer-encoding"}:
                continue
            outgoing[name] = value.decode("latin-1")
        return web.Response(status=status, body=b"".join(chunks), headers=outgoing)

    async def review_reader_api(self, request: web.Request) -> web.StreamResponse:
        principal = self._principal(request)
        if principal.auth_method != self.browser_principal.auth_method and is_judgment_request(
            request.method, request.path
        ):
            host = await asyncio.to_thread(peer_agent_host, request.transport)
            if host is None:
                # lc-debt: fail-open where the caller cannot be resolved
                # (unsupported platform, restricted /proc); the CLI guard still
                # applies there. Upgrade: an OS peer-credential lookup.
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
        if any(tail == root or tail.startswith(f"{root}/") for root in review_roots):
            return await self.review_reader.handle(request)
        if tail == "compare":
            return await self.review_reader.handle(request)
        return await self.legacy_dashboard_api(request)

    async def review_api_directory(self, request: web.Request) -> web.StreamResponse:
        self._principal(request)
        rows = await self._in_worker(self._review_rows)
        return json_response({"reviews": rows, "repo_root": "", "status_filter": "all"})

    def build(self) -> web.Application:
        app = web.Application(
            middlewares=[self._error_middleware, self._lifecycle_middleware, self._auth_middleware],
            client_max_size=self.config.limits.max_request_bytes * 2,
        )
        app.add_routes(list(self.core_route_defs()))
        if self.review_surface.available:
            app.add_routes(
                [
                    web.get("/", self.review_surface.home),
                    web.get("/home", self.review_surface.ui),
                    web.get("/runs", self.review_surface.ui),
                    web.get("/runs/{tail:.*}", self.review_surface.ui),
                    web.get("/code", self.review_surface.ui),
                    web.get("/knowledge", self.review_surface.ui),
                    web.get("/knowledge/{tail:.*}", self.review_surface.ui),
                    web.get("/usage", self.review_surface.ui),
                    web.get("/review", self.review_surface.ui),
                    web.get("/reviews", self.review_surface.ui),
                    web.get("/settings", self.review_surface.ui),
                    web.get("/settings/{tail:.*}", self.review_surface.ui),
                    web.get("/system", self.review_surface.ui),
                    web.get("/system/{tail:.*}", self.review_surface.ui),
                    web.get("/reviews/{tail:.*}", self.review_surface.ui),
                    web.get("/r/{tail:.*}", self.review_surface.ui),
                    web.get("/rr/{tail:.*}", self.review_surface.ui),
                    web.get("/assets/{tail:.*}", self.review_surface.asset),
                    web.get("/favicon.svg", self.review_surface.root_asset),
                    web.get("/lemoncrow-card.png", self.review_surface.root_asset),
                    web.get("/lemoncrow-card.svg", self.review_surface.root_asset),
                    web.get("/site.webmanifest", self.review_surface.root_asset),
                    web.get("/robots.txt", self.review_surface.root_asset),
                ]
            )
        app.add_routes(
            [
                web.post(LOCAL_BROWSER_PAIR_PATH, self.local_browser_pair),
                web.post(LOCAL_BROWSER_CLAIM_PATH, self.local_browser_claim),
                web.get("/v1/reviews", self.review_inbox),
                web.get("/v1/repos/{repo_id}/reviews", self.review_list),
                web.post("/v1/repos/{repo_id}/reviews", self.review_capture),
                web.post("/v1/repos/{repo_id}/reviews/drafts/{draft_id}", self.review_capture),
                web.get("/v1/repos/{repo_id}/reviews/{review_id}", self.review_get),
                web.post(
                    "/v1/repos/{repo_id}/reviews/{review_id}/revisions/{revision_id}/projection",
                    self.review_enrich_projection,
                ),
                web.post(
                    "/v1/repos/{repo_id}/reviews/{review_id}/preparation",
                    self.review_update_preparation,
                ),
                web.post(
                    "/v1/repos/{repo_id}/reviews/{review_id}/revisions/{revision_id}/base-view",
                    self.review_attach_base,
                ),
                web.get("/api/local/integrations", self.local_integrations),
                web.get("/api/local/system", self.local_system_status),
                web.get("/api/reviews", self.review_api_directory),
                web.route("*", "/api/{tail:.*}", self.review_reader_api),
            ]
        )
        app.on_shutdown.append(self._on_shutdown)
        return app

    async def _on_shutdown(self, app: web.Application) -> None:
        del app
        if not self.draining:
            self.begin_drain()
            await self.wait_drained(self.config.shutdown_timeout_s)
        index_worker = getattr(self, "index_worker", None)
        if index_worker is not None:
            await asyncio.to_thread(index_worker.close)
        self._executor.shutdown(wait=False, cancel_futures=True)
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
