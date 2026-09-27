"""Single-user durable Review storage for the public loopback server.

This module deliberately stops at local Review semantics.  It has no tenant
routing, provider webhooks, participants, attention workflow, policy projection,
or notification plumbing; those remain enterprise composition concerns.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from lemoncrow.pro.capabilities.review.api import register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import BlobPair, RevRange, load_blobs, source_state
from lemoncrow.pro.capabilities.review.models import ReviewPacket, review_packet_from_dict
from lemoncrow.pro.capabilities.review.packet import PacketBuild
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewSubjectType
from lemoncrow.pro.capabilities.review.sources.local import (
    RevisionRecording,
    enrich_revision_projection,
    record_revision,
    record_scm_relationship,
)
from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore
from lemoncrow.pro.capabilities.review.store import (
    short_review_reference as make_short_review_reference,
)
from lemoncrow.pro.capabilities.review.store import (
    short_revision_reference as make_short_revision_reference,
)

from .context import validate_opaque_id
from .errors import AgentAction, ErrorCode, ServerError
from .local_review import _dispatch_asgi

__all__ = ["LocalReviewCapture", "LocalReviewReader", "LocalReviewRepository"]

_LOG = logging.getLogger("lemoncrow.server.local_reviews")

_REVIEW_PATH = re.compile(r"^/api/reviews/(?P<id>[A-Za-z0-9_.:-]+)(?:/|$)")
_REVISION_PATH = re.compile(r"^/api/revisions/(?P<id>[A-Za-z0-9_.:-]+)(?:/|$)")
_ANNOTATION_PATH = re.compile(r"^/api/annotations/(?P<id>[A-Za-z0-9_.:-]+)(?:/|$)")
_EVIDENCE_PATH = re.compile(r"^/api/evidence/(?P<id>[A-Za-z0-9_.:-]+)(?:/|$)")


@dataclass(frozen=True, slots=True)
class LocalReviewCapture:
    session: ReviewSession
    revision: ReviewRevision
    revision_created: bool


class LocalReviewRepository:
    """Repository-partitioned ReviewStore collection for one local principal."""

    __slots__ = ("_root", "_source_content_reader", "_stores", "_stores_lock", "_tree_digests")

    def __init__(
        self,
        root: Path,
        *,
        source_content_reader: Callable[[str], bytes | None] | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._source_content_reader = source_content_reader
        # One store per repository for the process: a fresh one re-ran schema
        # setup and migration checks, and resolving a Reader request touches
        # every repository. Bounded by the repositories on disk.
        self._stores: dict[str, ReviewStore] = {}
        self._stores_lock = threading.Lock()
        # (repo, revision, side) -> digests its immutable source tree names.
        self._tree_digests: dict[tuple[str, str, str], frozenset[str]] = {}

    @property
    def root(self) -> Path:
        return self._root

    def store_root(self, repo_id: str) -> Path:
        return self._root / validate_opaque_id(repo_id, "repo_id")

    def store(self, repo_id: str) -> ReviewStore:
        root = self.store_root(repo_id)
        with self._stores_lock:
            cached = self._stores.get(repo_id)
            # A store whose database was removed underneath is set up again.
            if cached is not None and cached.db_path.exists():
                return cached
            store = ReviewStore(root, source_content_reader=self._source_content_reader)
            self._stores[repo_id] = store
            return store

    def referenced_digests(self, org_id: str) -> dict[tuple[str, str], frozenset[str]]:
        """Content each repository's stored Review revisions name, for cleanup.

        Every revision's source trees -- both sides, archived Reviews too,
        which can be restored -- so reopening a Review can rebuild its
        surfaces, previews and native View from content still held.
        """
        seen: set[tuple[str, str, str]] = set()
        out: dict[tuple[str, str], frozenset[str]] = {}
        for repo_id in self.list_repository_ids():
            store = self.store(repo_id)
            digests: set[str] = set()
            for session in store.list_sessions(status="", limit=None):
                for revision in store.list_revisions(session.id):
                    for side in ("new", "old"):
                        key = (repo_id, revision.id, side)
                        seen.add(key)
                        tree = self._tree_digests.get(key)
                        if tree is None:
                            rows = store.read_source_tree_artifact(session.id, revision.id, side=side)
                            if rows is None:
                                continue  # the old side may still be attached later
                            tree = frozenset(str(row["content_digest"]) for row in rows.values())
                            self._tree_digests[key] = tree
                        digests |= tree
            if digests:
                out[(org_id, repo_id)] = frozenset(digests)
        for stale in [key for key in self._tree_digests if key not in seen]:
            del self._tree_digests[stale]
        return out

    def scratch_root(self, repo_id: str) -> Path:
        root = self.store_root(repo_id) / "_source"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def set_checkout_root(self, repo_id: str, root: Path | None) -> None:
        marker = self.store_root(repo_id) / ".checkout-root"
        if root is None:
            marker.unlink(missing_ok=True)
            return
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(resolved) + "\n", encoding="utf-8")
        marker.chmod(0o600)

    def checkout_root(self, repo_id: str) -> Path | None:
        marker = self.store_root(repo_id) / ".checkout-root"
        try:
            raw = marker.read_text(encoding="utf-8").strip()
            resolved = Path(raw).expanduser().resolve(strict=True)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_dir() else None

    def set_workspace_root_hint(self, repo_id: str, review_id: str, root: str) -> None:
        """Persist a display-only workspace path for one durable Change.

        This hint is deliberately separate from ``.checkout-root``.  It comes
        from the Review packet and is safe to use for identity/display, but it
        is *not* proof that the server may read that filesystem path.  Source
        refresh and editor/runtime operations continue to require the proven
        same-host checkout grant.
        """

        review = validate_opaque_id(review_id, "review_id")
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            return
        marker = self.store_root(repo_id) / ".workspace-roots" / review
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(candidate) + "\n", encoding="utf-8")
        marker.chmod(0o600)

    def workspace_root_hint(self, repo_id: str, review_id: str) -> str:
        marker = self.store_root(repo_id) / ".workspace-roots" / validate_opaque_id(review_id, "review_id")
        try:
            raw = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return raw if raw and Path(raw).expanduser().is_absolute() else ""

    def set_workspace_branch_hint(self, repo_id: str, review_id: str, branch: str) -> None:
        """Persist display-only branch identity separately from source authority."""

        review = validate_opaque_id(review_id, "review_id")
        value = branch.strip()
        if not value or len(value) > 256 or "\n" in value or "\r" in value:
            return
        marker = self.store_root(repo_id) / ".workspace-branches" / review
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(value + "\n", encoding="utf-8")
        marker.chmod(0o600)

    def workspace_branch_hint(self, repo_id: str, review_id: str) -> str:
        marker = self.store_root(repo_id) / ".workspace-branches" / validate_opaque_id(review_id, "review_id")
        try:
            raw = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return raw if raw and len(raw) <= 256 and "\n" not in raw and "\r" not in raw else ""

    def list_repository_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(path.name for path in self._root.iterdir() if path.is_dir() and not path.name.startswith("."))
        )

    def reconcile_landings(self) -> None:
        """Advance every armed GitHub Merge-when-ready intent once.

        Runs from local maintenance, so an armed intent lands only while this
        server runs. Each intent is isolated: one broken Change or repository
        never stops the others.
        """

        from lemoncrow.pro.capabilities.review.landing import ARMED_LANDING_STATES, reconcile_local_github_landing

        for repo_id in self.list_repository_ids():
            try:
                if not (self.store_root(repo_id) / DB_NAME).is_file():
                    continue
                store = self.store(repo_id)
                landings = store.list_relationships_by_kind("landing")
            except Exception:
                _LOG.exception("landing watch could not read repository %s", repo_id)
                continue
            for landing in landings:
                if landing.relation != "merge_when_ready" or landing.status not in ARMED_LANDING_STATES:
                    continue
                try:
                    session = store.get_session(landing.review_id)
                    if session is None or not session.source_ref.startswith("github:"):
                        continue
                    reconcile_local_github_landing(store, session)
                except Exception:
                    _LOG.exception("landing watch failed for Change %s in repository %s", landing.review_id, repo_id)

    def list_reviews(self, repo_id: str, *, status: str = "", limit: int | None = 100) -> tuple[ReviewSession, ...]:
        root = self.store_root(repo_id)
        if not root.exists():
            return ()
        return self.store(repo_id).list_sessions(status=status, limit=limit)

    def get_review(self, repo_id: str, review_id: str) -> ReviewSession | None:
        root = self.store_root(repo_id)
        if not root.exists():
            return None
        return self.store(repo_id).resolve_session(review_id)

    def short_review_reference(self, review_id: str, *, min_chars: int = 8) -> str:
        candidates = [
            session.id
            for repo_id in self.list_repository_ids()
            for session in self.list_reviews(repo_id, status="", limit=None)
        ]
        return make_short_review_reference(review_id, candidates, min_chars=min_chars)

    def short_revision_reference(self, revision_id: str, *, min_chars: int = 8) -> str:
        candidates: list[str] = []
        for repo_id in self.list_repository_ids():
            store = self.store(repo_id)
            for session in store.list_sessions(status="", limit=None):
                candidates.extend(item.id for item in store.list_revisions(session.id))
        return make_short_revision_reference(revision_id, candidates, min_chars=min_chars)

    def require_review(self, repo_id: str, review_id: str) -> tuple[ReviewStore, ReviewSession]:
        session = self.get_review(repo_id, review_id)
        if session is None:
            raise ServerError(ErrorCode.REVIEW_UNKNOWN, "the Review does not exist", details={"review_id": review_id})
        return self.store(repo_id), session

    def enrich_projection(
        self,
        *,
        repo_id: str,
        review_id: str,
        revision_id: str,
        packet: ReviewPacket | Mapping[str, Any],
    ) -> ReviewRevision:
        """Enrich one exact visible revision without changing source/target identity."""

        store, session = self.require_review(repo_id, review_id)
        revision = store.get_revision(validate_opaque_id(revision_id, "revision_id"))
        if revision is None or revision.review_id != session.id:
            raise ServerError(
                ErrorCode.REVIEW_UNKNOWN,
                "the Review revision does not exist",
                details={"review_id": review_id, "revision_id": revision_id},
            )
        try:
            parsed = packet if isinstance(packet, ReviewPacket) else review_packet_from_dict(packet)
        except ValueError as exc:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "review packet is invalid",
                details={"field": "packet"},
            ) from exc
        blobs = store.read_blob_artifact(session.id, revision.id)
        if blobs is None:
            raise ServerError(
                ErrorCode.REVIEW_STATE_CONFLICT,
                "the Review revision has no frozen text snapshot for safe enrichment",
                details={"review_id": review_id, "revision_id": revision.id},
            )
        try:
            record = enrich_revision_projection(
                store,
                session,
                revision,
                PacketBuild(packet=parsed, blobs=BlobPair(old={}, new=blobs, degraded=())),
            )
            try:
                from lemoncrow.pro.capabilities.review.preparation import project_lemoncrow_annotations

                project_lemoncrow_annotations(
                    store,
                    session,
                    record,
                    parsed,
                    new_blobs=blobs,
                )
            except (OSError, RuntimeError, ValueError):
                pass
            return record
        except ValueError as exc:
            raise ServerError(
                ErrorCode.REVIEW_STATE_CONFLICT,
                str(exc),
                details={"review_id": review_id, "revision_id": revision.id},
            ) from exc

    def update_preparation(
        self,
        *,
        repo_id: str,
        review_id: str,
        preparation_id: str,
        stage: str,
        detail: str = "",
    ) -> None:
        """Publish progressive preparation state in the Review's own store."""

        store, session = self.require_review(repo_id, review_id)
        prep = validate_opaque_id(preparation_id, "preparation_id")
        if stage not in {"impact_background", "finalizing_background", "ready", "enrichment_failed", "failed"}:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "invalid Review preparation state")
        if len(detail) > 2048:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "preparation detail is too long")
        from lemoncrow.pro.capabilities.review.workspace import write_review_preparation

        write_review_preparation(store.root, session.id, prep, stage, detail=detail)

    def capture(
        self,
        *,
        repo_id: str,
        reviewer_id: str,
        source_ref: str,
        packet: ReviewPacket | Mapping[str, Any],
        new_blobs: Mapping[str, str],
        old_blobs: Mapping[str, str] | None = None,
        binary_new_blobs: Mapping[str, bytes] | None = None,
        source_tree: Mapping[str, Mapping[str, Any]] | None = None,
        old_source_tree: Mapping[str, Mapping[str, Any]] | None = None,
        source_fingerprint: str,
        source_state_fingerprint: str = "",
        source_view_id: str = "",
        source_view_revision: int = 0,
        checkout_root: Path | None = None,
        title: str = "",
        restore_archived: bool = False,
    ) -> LocalReviewCapture:
        repo = validate_opaque_id(repo_id, "repo_id")
        reviewer = validate_opaque_id(reviewer_id, "reviewer_id")
        if not source_ref.strip():
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "source_ref is required", details={"field": "source_ref"})
        if not source_fingerprint:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "source_fingerprint is required")
        try:
            parsed = packet if isinstance(packet, ReviewPacket) else review_packet_from_dict(packet)
        except ValueError as exc:
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID, "review packet is invalid", details={"field": "packet"}
            ) from exc

        root = self.store_root(repo)
        store = self.store(repo)
        if checkout_root is not None:
            self.set_checkout_root(repo, checkout_root)
        repo_identity = f"local:{repo}"
        subject_type: ReviewSubjectType = "commit_range" if parsed.range_mode == "commit_range" else "local_change"
        session = store.find_session(
            repo_root=repo_identity,
            subject_type=subject_type,
            source_ref=source_ref,
            range_mode=parsed.range_mode,
        )
        if session is not None and session.status == "archived":
            if not restore_archived:
                raise ServerError(
                    ErrorCode.REVIEW_STATE_CONFLICT,
                    "review is discarded and read-only; run lc review --reopen-review to restore it before making changes",
                    details={"review_id": session.id, "status": session.status},
                    action=AgentAction.FIX_REQUEST,
                )
            store.update_session(session.id, status="open")
            session = store.get_session(session.id)
        if session is None:
            session = store.create_session(
                ReviewSession(
                    id="",
                    subject_type=subject_type,
                    repo_root=repo_identity,
                    range_mode=parsed.range_mode,
                    title=title or parsed.title,
                    source_ref=source_ref,
                    actor_type="human",
                    reviewer_id=reviewer,
                )
            )
        record_scm_relationship(store, session, source_ref)

        rng = RevRange(
            mode=parsed.range_mode,
            base_rev=parsed.base_rev,
            head_rev=parsed.head_rev,
            base_sha=parsed.base_sha,
            head_sha=parsed.head_sha,
            merge_base_sha=parsed.merge_base_sha,
            dirty=parsed.dirty,
            title=parsed.title,
        )
        captured_old_blobs = dict(old_blobs or {})
        watch_fingerprint = source_state_fingerprint or source_fingerprint
        if checkout_root is not None and not source_state_fingerprint:
            try:
                # A local live-View can publish the current Snapshot before its
                # base View is attached. The range still names an immutable Git
                # base, so read only the changed old sides needed by semantic
                # Review units when the request did not carry them.
                if not captured_old_blobs:
                    captured_old_blobs = dict(load_blobs(checkout_root, rng, parsed.files).old)
                watch_fingerprint = source_state(checkout_root, rng).fingerprint
            except (OSError, RuntimeError, ValueError):
                # The committed View remains an exact immutable identity even if
                # the advisory same-host source probe cannot be established.
                pass
        build = PacketBuild(
            packet=parsed,
            blobs=BlobPair(
                old=captured_old_blobs,
                new=dict(new_blobs),
                degraded=(),
                binary_new=dict(binary_new_blobs or {}),
            ),
        )
        reviewers = list(store.list_reviewer_ids(session.id))
        if reviewer not in reviewers:
            reviewers.insert(0, reviewer)
        elif reviewers and reviewers[0] != reviewer:
            reviewers.remove(reviewer)
            reviewers.insert(0, reviewer)

        first: RevisionRecording | None = None
        runtime_root = self.scratch_root(repo)
        for current_reviewer in reviewers:
            current = record_revision(
                store,
                session,
                runtime_root,
                rng,
                store_root=root,
                build=build,
                reviewer_id=current_reviewer,
                source_fingerprint_override=watch_fingerprint,
                source_view_id=source_view_id,
                source_view_revision=source_view_revision,
            )
            if first is None:
                first = current
        if first is None:
            raise RuntimeError("local Review capture reconciled no reviewer")
        if first.created and session.source_ref.startswith("github:"):
            # record_revision marked an armed Merge-when-ready stale; this Reader
            # delegates GitHub landing through gh, so settle the owed cancel now.
            from lemoncrow.pro.capabilities.review.landing import (
                complete_pending_landing_cancel,
                landing_metadata,
                landing_relationship,
                local_github_landing,
            )

            landing = landing_relationship(store, session.id)
            armed_id = str(landing_metadata(landing).get("revision_id") or "")
            armed_revision = store.get_revision(armed_id) if armed_id else None
            complete_pending_landing_cancel(
                store,
                session.id,
                lambda: local_github_landing(session, armed_revision or first.revision, action="cancel"),
            )
        if source_tree is not None:
            store.write_source_tree_artifact(session.id, first.revision.id, source_tree, side="new")
        if old_source_tree is not None:
            store.write_source_tree_artifact(session.id, first.revision.id, old_source_tree, side="old")
        return LocalReviewCapture(
            session=store.get_session(session.id) or session,
            revision=first.revision,
            revision_created=first.created,
        )


@dataclass(frozen=True, slots=True)
class _ResolvedReader:
    repo_id: str
    store: ReviewStore


class LocalReviewReader:
    """Resolve a local Review object to its repository store and shared Reader API."""

    __slots__ = (
        "_apps",
        "_apps_lock",
        "_code_intelligence_factory",
        "_listen_port",
        "_repository",
        "_review_repos",
    )

    def __init__(
        self,
        repository: LocalReviewRepository,
        *,
        listen_port: int,
        code_intelligence_factory: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._repository = repository
        self._listen_port = int(listen_port)
        self._code_intelligence_factory = code_intelligence_factory
        self._apps: dict[str, Any] = {}
        self._apps_lock = threading.Lock()
        # Review reference -> repository it resolved in, so the Reader's polling
        # does not search every repository per request. Only references that
        # resolved are kept, and each hit is re-checked in its repository.
        self._review_repos: dict[str, str] = {}

    def _resolve_review(self, review_id: str) -> _ResolvedReader | None:
        known = self._review_repos.get(review_id)
        if known is not None and self._repository.get_review(known, review_id) is not None:
            return _ResolvedReader(known, self._repository.store(known))
        matches: list[_ResolvedReader] = []
        for repo_id in self._repository.list_repository_ids():
            if self._repository.get_review(repo_id, review_id) is not None:
                matches.append(_ResolvedReader(repo_id, self._repository.store(repo_id)))
        if len(matches) > 1:
            raise ServerError(ErrorCode.REVIEW_STATE_CONFLICT, "the Review reference is ambiguous")
        if not matches:
            return None
        self._review_repos[review_id] = matches[0].repo_id
        return matches[0]

    def _resolve_revision(self, revision_id: str) -> _ResolvedReader | None:
        matches: list[_ResolvedReader] = []
        for repo_id in self._repository.list_repository_ids():
            store = self._repository.store(repo_id)
            revision = store.resolve_revision(revision_id)
            if revision is not None and store.get_session(revision.review_id) is not None:
                matches.append(_ResolvedReader(repo_id, store))
        if len(matches) > 1:
            raise ServerError(ErrorCode.REVIEW_STATE_CONFLICT, "the Review revision id is duplicated")
        return matches[0] if matches else None

    def resolve(self, path: str) -> _ResolvedReader:
        match = _REVIEW_PATH.match(path)
        resolved = self._resolve_review(match.group("id")) if match else None
        if resolved is None:
            match = _REVISION_PATH.match(path)
            resolved = self._resolve_revision(match.group("id")) if match else None
        if resolved is None:
            match = _ANNOTATION_PATH.match(path)
            if match:
                annotation_id = match.group("id")
                for repo_id in self._repository.list_repository_ids():
                    store = self._repository.store(repo_id)
                    row = store.get_annotation(annotation_id)
                    if row is not None and store.get_session(row.review_id) is not None:
                        resolved = _ResolvedReader(repo_id, store)
                        break
        if resolved is None:
            match = _EVIDENCE_PATH.match(path)
            if match:
                evidence_id = match.group("id")
                for repo_id in self._repository.list_repository_ids():
                    store = self._repository.store(repo_id)
                    row = store.get_evidence(evidence_id)
                    if row is not None and store.get_session(row.review_id) is not None:
                        resolved = _ResolvedReader(repo_id, store)
                        break
        if resolved is None:
            raise ServerError(
                ErrorCode.REVIEW_UNKNOWN, "the Review object is not available", action=AgentAction.ABANDON
            )
        return resolved

    def resolve_compare(self, *, from_ref: str, to_ref: str, scope: str = "") -> _ResolvedReader:
        candidates: list[_ResolvedReader] = []
        if scope:
            resolved_scope = self._resolve_review(scope)
            if resolved_scope is None:
                raise ServerError(ErrorCode.REVIEW_UNKNOWN, "the comparison scope Review is not available")
            candidates.append(resolved_scope)

        for source_ref in (from_ref, to_ref):
            resolved: _ResolvedReader | None = None
            if source_ref.startswith("rr/"):
                resolved = self._resolve_revision(source_ref)
            elif source_ref.startswith("r/"):
                resolved = self._resolve_review(source_ref)
            if resolved is not None:
                candidates.append(resolved)
            elif source_ref.startswith(("rr/", "r/")):
                raise ServerError(
                    ErrorCode.REVIEW_UNKNOWN,
                    "the comparison Review source is not available",
                    details={"source_ref": source_ref},
                    action=AgentAction.ABANDON,
                )

        if not candidates:
            repo_ids = self._repository.list_repository_ids()
            if len(repo_ids) == 1:
                repo_id = repo_ids[0]
                return _ResolvedReader(repo_id, self._repository.store(repo_id))
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "repository scope is required when comparing Git-only sources",
                details={"field": "scope"},
                action=AgentAction.FIX_REQUEST,
            )
        repo_ids = {item.repo_id for item in candidates}
        if len(repo_ids) != 1:
            raise ServerError(
                ErrorCode.REVIEW_STATE_CONFLICT,
                "comparison sources belong to different repositories",
                action=AgentAction.FIX_REQUEST,
            )
        return candidates[0]

    def _app(self, resolved: _ResolvedReader) -> Any:
        with self._apps_lock:
            return self._app_locked(resolved)

    def _app_locked(self, resolved: _ResolvedReader) -> Any:
        checkout_root = self._repository.checkout_root(resolved.repo_id)
        cache_key = f"{resolved.repo_id}:{checkout_root or ''}"
        cached = self._apps.get(cache_key)
        if cached is not None:
            return cached
        from fastapi import FastAPI

        async def _outer_server_authenticated() -> None:
            return None

        app = FastAPI(title="LemonCrow Local Review", docs_url=None, redoc_url=None, openapi_url=None)
        from lemoncrow.core.foundation.paths import default_store_root

        code_intelligence = None
        code_intelligence_factory = self._code_intelligence_factory
        if code_intelligence_factory is not None:

            def _review_code_intelligence(_session: Any, revision: Any) -> Any:
                return code_intelligence_factory(resolved.repo_id, revision)

            code_intelligence = _review_code_intelligence
        register_review_api(
            app,
            resolved.store,
            auth_dependency=_outer_server_authenticated,
            repo_root=self._repository.scratch_root(resolved.repo_id),
            port=self._listen_port,
            session_repo_identity=f"local:{resolved.repo_id}",
            enforce_origin_guard=False,
            surface_workspace_root=self._repository.scratch_root(resolved.repo_id),
            dependency_repo_root=checkout_root,
            source_repo_root=checkout_root,
            source_store_root=default_store_root(),
            source_refresh_enabled=checkout_root is not None,
            code_intelligence=code_intelligence,
            # The local server runs as the reviewer, so their `gh` login is the
            # one a GitHub pull request Review publishes through.
            github_publish_enabled=True,
        )
        self._apps[cache_key] = app
        return app

    def _resolved_app(self, path: str, query: Mapping[str, str]) -> Any:
        if path == "/api/compare":
            resolved = self.resolve_compare(
                from_ref=query.get("from_ref", ""),
                to_ref=query.get("to_ref", ""),
                scope=query.get("scope", ""),
            )
        else:
            resolved = self.resolve(path)
        return self._app(resolved)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        # Resolution opens Review stores; the Reader polls, so keep it off the
        # event loop that also answers every other request.
        app = await asyncio.to_thread(self._resolved_app, request.path, dict(request.query))
        return await _dispatch_asgi(app, request)
