"""Single-user durable Review storage for the public loopback server.

This module deliberately stops at local Review semantics.  It has no tenant
routing, provider webhooks, participants, attention workflow, policy projection,
or notification plumbing; those remain enterprise composition concerns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from lemoncrow.pro.capabilities.review.api import register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import BlobPair, RevRange, source_state
from lemoncrow.pro.capabilities.review.models import ReviewPacket, review_packet_from_dict
from lemoncrow.pro.capabilities.review.packet import PacketBuild
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewSubjectType
from lemoncrow.pro.capabilities.review.sources.local import RevisionRecording, record_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

from .context import validate_opaque_id
from .errors import AgentAction, ErrorCode, ServerError
from .local_review import _dispatch_asgi

__all__ = ["LocalReviewCapture", "LocalReviewReader", "LocalReviewRepository"]

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

    __slots__ = ("_root", "_source_content_reader")

    def __init__(
        self,
        root: Path,
        *,
        source_content_reader: Callable[[str], bytes | None] | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._source_content_reader = source_content_reader

    @property
    def root(self) -> Path:
        return self._root

    def store_root(self, repo_id: str) -> Path:
        return self._root / validate_opaque_id(repo_id, "repo_id")

    def store(self, repo_id: str) -> ReviewStore:
        return ReviewStore(
            self.store_root(repo_id),
            source_content_reader=self._source_content_reader,
        )

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

    def list_repository_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(path.name for path in self._root.iterdir() if path.is_dir() and not path.name.startswith("."))
        )

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

    def require_review(self, repo_id: str, review_id: str) -> tuple[ReviewStore, ReviewSession]:
        session = self.get_review(repo_id, review_id)
        if session is None:
            raise ServerError(ErrorCode.REVIEW_UNKNOWN, "the Review does not exist", details={"review_id": review_id})
        return self.store(repo_id), session

    def capture(
        self,
        *,
        repo_id: str,
        reviewer_id: str,
        source_ref: str,
        packet: ReviewPacket | Mapping[str, Any],
        new_blobs: Mapping[str, str],
        binary_new_blobs: Mapping[str, bytes] | None = None,
        source_tree: Mapping[str, Mapping[str, Any]] | None = None,
        old_source_tree: Mapping[str, Mapping[str, Any]] | None = None,
        source_fingerprint: str,
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
        build = PacketBuild(
            packet=parsed,
            blobs=BlobPair(old={}, new=dict(new_blobs), degraded=(), binary_new=dict(binary_new_blobs or {})),
        )
        watch_fingerprint = source_fingerprint
        if checkout_root is not None:
            try:
                watch_fingerprint = source_state(checkout_root, rng).fingerprint
            except (OSError, RuntimeError, ValueError):
                # The committed View remains an exact immutable identity even if
                # the advisory same-host source probe cannot be established.
                pass
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
            )
            if first is None:
                first = current
        if first is None:
            raise RuntimeError("local Review capture reconciled no reviewer")
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

    __slots__ = ("_apps", "_listen_port", "_repository")

    def __init__(self, repository: LocalReviewRepository, *, listen_port: int) -> None:
        self._repository = repository
        self._listen_port = int(listen_port)
        self._apps: dict[str, Any] = {}

    def _resolve_review(self, review_id: str) -> _ResolvedReader | None:
        matches: list[_ResolvedReader] = []
        for repo_id in self._repository.list_repository_ids():
            if self._repository.get_review(repo_id, review_id) is not None:
                matches.append(_ResolvedReader(repo_id, self._repository.store(repo_id)))
        if len(matches) > 1:
            raise ServerError(ErrorCode.REVIEW_STATE_CONFLICT, "the Review reference is ambiguous")
        return matches[0] if matches else None

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
        )
        self._apps[cache_key] = app
        return app

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.path == "/api/compare":
            resolved = self.resolve_compare(
                from_ref=request.query.get("from_ref", ""),
                to_ref=request.query.get("to_ref", ""),
                scope=request.query.get("scope", ""),
            )
        else:
            resolved = self.resolve(request.path)
        return await _dispatch_asgi(self._app(resolved), request)
