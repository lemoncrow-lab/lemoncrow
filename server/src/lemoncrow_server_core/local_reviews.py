"""Single-user durable Review storage for the public loopback server.

This module deliberately stops at local Review semantics.  It has no tenant
routing, provider webhooks, participants, attention workflow, policy projection,
or notification plumbing; those are not part of the public engine.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import logging
import re
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from aiohttp import web
from multidict import CIMultiDict

from lemoncrow.core.foundation.paths import default_store_root
from lemoncrow.pro.capabilities.review.api import register_review_api
from lemoncrow.pro.capabilities.review.change_sets import CHANGE_SET_KIND
from lemoncrow.pro.capabilities.review.development import sync_ledgers_before_capture
from lemoncrow.pro.capabilities.review.gitdiff import BlobPair, RevRange, load_blobs, resolve_rev_range, source_state
from lemoncrow.pro.capabilities.review.models import ReviewPacket, review_packet_from_dict
from lemoncrow.pro.capabilities.review.packet import PacketBuild
from lemoncrow.pro.capabilities.review.readiness import SnapshotProgress
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewRevision,
    ReviewSession,
    ReviewSubjectType,
)
from lemoncrow.pro.capabilities.review.sources.local import (
    MUTABLE_RANGE_MODES,
    ReviewRoutingError,
    RevisionRecording,
    attach_review,
    branch_review_base_for,
    checkpoint_range_for,
    claim_review,
    enrich_revision_projection,
    freeze_branch_base,
    record_revision,
    record_scm_relationship,
    review_subject,
    select_review,
    sync_commit_checkpoints,
)
from lemoncrow.pro.capabilities.review.store import DB_NAME, ReviewStore, ShortReferenceIndex
from lemoncrow.pro.capabilities.review.store import (
    short_revision_reference as make_short_revision_reference,
)

from .context import validate_opaque_id
from .errors import AgentAction, ErrorCode, ServerError
from .local_review import _dispatch_asgi, run_asgi_get_isolated

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

    __slots__ = (
        "_landing_revalidator",
        "_merge_recorder",
        "_referenced",
        "_root",
        "_source_content_reader",
        "_stores",
        "_stores_lock",
    )

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
        # repo -> the (revision, side) source trees read so far and the union of
        # the digests they name: each distinct digest held once, not per revision.
        self._referenced: dict[str, tuple[frozenset[tuple[str, str]], frozenset[str]]] = {}
        # (repo_id, review_id) -> the Reader's readiness revalidation of one
        # armed intent; set by the Reader composed over this repository.
        self._landing_revalidator: Callable[[str, str], object] | None = None
        # (repo_id, review_id) -> the Reader's automatic review record of a
        # provider-observed merge (P10-SIGN); set by the Reader composed here.
        self._merge_recorder: Callable[[str, str], Mapping[str, Any]] | None = None

    @property
    def root(self) -> Path:
        return self._root

    def set_landing_revalidator(self, revalidate: Callable[[str, str], object]) -> None:
        """Let the landing watch re-evaluate readiness (policy generation changes) through the Reader."""

        self._landing_revalidator = revalidate

    def set_merge_recorder(self, record: Callable[[str, str], Mapping[str, Any]]) -> None:
        """Let the landing watch emit the signed review record of a merge it observes, through the Reader."""

        self._merge_recorder = record

    def _record_observed_merge(self, repo_id: str, review_id: str) -> None:
        # The record (or the noted absence of a signing key) is idempotent per
        # Snapshot in the Reader, so a repeated observation is harmless.
        # lc-debt: only the pass that observes the merge emits; a crash between
        # recording the merge and emitting leaves it to an explicit export.
        # Upgrade path: a durable emission marker outside the landing row.
        recorder = self._merge_recorder
        if recorder is None:
            return
        try:
            outcome = recorder(repo_id, review_id)
        except Exception:
            _LOG.exception("review record of the merge of Change %s in repository %s failed", review_id, repo_id)
            return
        _LOG.info("merge of Change %s: review record %s", review_id, str(outcome.get("status") or ""))

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
        surfaces, previews and native View from content still held. A stored
        tree never changes, so only trees new since the last call are read; a
        removed revision re-derives its repository's union.
        """
        out: dict[tuple[str, str], frozenset[str]] = {}
        repo_ids = self.list_repository_ids()
        for repo_id in repo_ids:
            store = self.store(repo_id)
            trees = [
                (session.id, revision.id, side)
                for session in store.list_sessions(status="", limit=None)
                for revision in store.list_revisions(session.id)
                for side in ("new", "old")
            ]
            read, digests = self._referenced.get(repo_id, (frozenset(), frozenset()))
            if not read <= {(revision_id, side) for _session_id, revision_id, side in trees}:
                read, digests = frozenset(), frozenset()
            unread = [tree for tree in trees if tree[1:] not in read]
            if unread:
                grown, now_read = set(digests), set(read)
                for session_id, revision_id, side in unread:
                    rows = store.read_source_tree_artifact(session_id, revision_id, side=side)
                    if rows is None:
                        continue  # the old side may still be attached later
                    grown.update(str(row["content_digest"]) for row in rows.values())
                    now_read.add((revision_id, side))
                read, digests = frozenset(now_read), frozenset(grown)
            self._referenced[repo_id] = (read, digests)
            if digests:
                out[(org_id, repo_id)] = digests
        live = set(repo_ids)
        for stale in [repo_id for repo_id in self._referenced if repo_id not in live]:
            del self._referenced[stale]
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
        from lemoncrow.pro.capabilities.review.fix_lines import FIX_WORKTREE_DIR

        current = self.checkout_root(repo_id)
        if current is not None and resolved.is_relative_to(current / FIX_WORKTREE_DIR):
            # A fix worktree never replaces the checkout it fixes.
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

    def set_review_checkout_root(self, repo_id: str, review_id: str, root: Path) -> None:
        """Record the proven checkout one Review reads its source from."""

        review = validate_opaque_id(review_id, "review_id")
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            return
        marker = self.store_root(repo_id) / ".checkout-roots" / review
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(resolved) + "\n", encoding="utf-8")
        marker.chmod(0o600)

    def review_checkout_root(self, repo_id: str, review_id: str) -> Path | None:
        marker = self.store_root(repo_id) / ".checkout-roots" / validate_opaque_id(review_id, "review_id")
        try:
            resolved = Path(marker.read_text(encoding="utf-8").strip()).expanduser().resolve(strict=True)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_dir() else None

    def source_root_for(self, repo_id: str, review_id: str) -> Path | None:
        """Return this Review's proven checkout, never another Review's checkout."""

        marker = self.store_root(repo_id) / ".checkout-roots" / validate_opaque_id(review_id, "review_id")
        if marker.exists():
            return self.review_checkout_root(repo_id, review_id)
        return self.checkout_root(repo_id)

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

    def manual_review_title(self, repo_id: str, review_id: str) -> str:
        marker = self.store_root(repo_id) / ".review-titles" / validate_opaque_id(review_id, "review_id")
        try:
            return marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set_manual_review_title(self, repo_id: str, review_id: str, title: str) -> None:
        marker = self.store_root(repo_id) / ".review-titles" / validate_opaque_id(review_id, "review_id")
        if not title:
            marker.unlink(missing_ok=True)
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(title + "\n", encoding="utf-8")
        marker.chmod(0o600)

    def author_snapshot_title(self, repo_id: str, revision_id: str) -> str:
        marker = self.store_root(repo_id) / ".author-titles" / validate_opaque_id(revision_id, "revision_id")
        try:
            return marker.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set_author_snapshot_title(self, repo_id: str, revision_id: str, title: str) -> None:
        resolved = validate_opaque_id(revision_id, "revision_id")
        marker = self.store_root(repo_id) / ".author-titles" / resolved
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(title + "\n", encoding="utf-8")
        marker.chmod(0o600)
        store = self.store(repo_id)
        revision = store.get_revision(resolved)
        if revision is not None:
            store.set_revision_title(revision.review_id, resolved, title)

    def list_repository_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(path.name for path in self._root.iterdir() if path.is_dir() and not path.name.startswith("."))
        )

    def reconcile_landings(self) -> None:
        """Advance every armed GitHub Merge-when-ready intent once.

        Runs from local maintenance, so an armed intent lands only while this
        server runs. Each intent is isolated: one broken Change or repository
        never stops the others. The same pass watches deployments of merged
        Changes (P15-DEPLOY) and closes their observation windows.
        """

        from lemoncrow.pro.capabilities.review.landing import (
            ARMED_LANDING_STATES,
            landing_metadata,
            reconcile_local_github_landing,
        )
        from lemoncrow.pro.capabilities.review.outcomes.local_deployments import (
            load_local_deployment_settings,
            watch_local_deployments,
        )
        from lemoncrow.pro.capabilities.review.outcomes.local_observability import (
            load_local_observability_settings,
            watch_local_observations,
        )

        deployment_settings = load_local_deployment_settings()
        observability_settings = load_local_observability_settings()
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
                if landing.relation != "merge_when_ready":
                    continue
                # An owed provider cancel is retried here too, armed or not.
                owes_cancel = landing_metadata(landing).get("provider_cancel") == "pending"
                if landing.status not in ARMED_LANDING_STATES and not owes_cancel:
                    continue
                try:
                    session = store.get_session(landing.review_id)
                    if session is None or not session.source_ref.startswith("github:"):
                        continue
                    revalidator = self._landing_revalidator
                    settled = reconcile_local_github_landing(
                        store,
                        session,
                        revalidate=(
                            None if revalidator is None else lambda r=repo_id, s=session.id, v=revalidator: v(r, s)  # type: ignore[misc]
                        ),
                    )
                    if settled is not None and settled.status == "merged":
                        if landing.status != "merged":
                            # P10-SIGN: the signed review record of the merge (or its noted absence).
                            self._record_observed_merge(repo_id, session.id)
                        # P12-AUTO: the provider merged it; run enabled post-merge rules once.
                        from lemoncrow.pro.capabilities.review.post_merge_actions import run_watch_merge_automation

                        run_watch_merge_automation(store, session)
                except Exception:
                    _LOG.exception("landing watch failed for Change %s in repository %s", landing.review_id, repo_id)
            try:
                watch_local_deployments(store, settings=deployment_settings)
            except Exception:
                _LOG.exception("deployment watch failed for repository %s", repo_id)
            try:
                # P15-OBS: production observation runs on this machine (the local locus), never elsewhere.
                watch_local_observations(store, settings=observability_settings)
            except Exception:
                _LOG.warning("production observation watch failed for repository %s", repo_id)

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

    def review_ids(self) -> tuple[str, ...]:
        """Every Review id across repositories: the uniqueness domain of short references."""
        return tuple(
            session.id
            for repo_id in self.list_repository_ids()
            for session in self.list_reviews(repo_id, status="", limit=None)
        )

    def review_references(self) -> ShortReferenceIndex:
        """Every Review's short-reference domain: read and hashed once to name many Reviews."""
        return ShortReferenceIndex(self.review_ids(), revision=False)

    def short_review_reference(
        self, review_id: str, *, min_chars: int = 8, references: ShortReferenceIndex | None = None
    ) -> str:
        if references is None:
            references = self.review_references()
        return references.shortest(review_id, min_chars=min_chars)

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
        legacy_source_fingerprint: str = "",
        source_view_id: str = "",
        source_view_revision: int = 0,
        checkout_root: Path | None = None,
        title: str = "",
        restore_archived: bool = False,
        workspace_branch: str = "",
        worktree_key: str = "",
        force_new_review: bool = False,
        continue_review_id: str = "",
        reuse_closed_review: bool = False,
        progress: Callable[[str], None] | None = None,
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
        subject = review_subject(
            repo_identity,
            branch=workspace_branch,
            worktree_key=worktree_key,
            range_mode=parsed.range_mode,
        )
        branch_key = subject.subject_key.removesuffix("#staged") if subject and subject.subject_kind == "branch" else ""
        try:
            session = select_review(
                store,
                subject=subject,
                repo_root_key=repo_identity,
                subject_type=subject_type,
                source_ref=source_ref,
                range_mode=parsed.range_mode,
                force_new=force_new_review,
                continue_review_id=continue_review_id,
                restore_archived=restore_archived,
                reuse_closed=reuse_closed_review,
            )
        except ReviewRoutingError as exc:
            raise ServerError(
                ErrorCode.REVIEW_STATE_CONFLICT,
                str(exc),
                details=dict(exc.details),
                action=AgentAction.FIX_REQUEST,
            ) from exc
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
        # An explicit --new/--continue takes the branch over only once its
        # capture is recorded: a failed attempt must not strand the Review the
        # branch already had.
        deferred_binding = False
        if session is None:
            template = ReviewSession(
                id="",
                subject_type=subject_type,
                repo_root=repo_identity,
                range_mode=parsed.range_mode,
                title=title or parsed.title,
                source_ref=source_ref,
                review_base_sha=(
                    branch_review_base_for(subject, parsed.base_sha)
                    if parsed.range_mode in MUTABLE_RANGE_MODES
                    else parsed.base_sha
                ),
                actor_type="human",
                reviewer_id=reviewer,
            )
            if force_new_review:
                session = store.create_session(template)
                deferred_binding = subject is not None
            else:
                session = claim_review(store, template, subject)
        elif continue_review_id:
            session = freeze_branch_base(store, session, subject, parsed.base_sha)
            deferred_binding = subject is not None
        else:
            session = attach_review(store, session, subject, fallback_base=parsed.base_sha)
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
        legacy_watch = legacy_source_fingerprint if source_state_fingerprint else ""
        if checkout_root is not None and not source_state_fingerprint:
            try:
                # A local live-View can publish the current Snapshot before its
                # base View is attached. The range still names an immutable Git
                # base, so read only the changed old sides needed by semantic
                # Review units when the request did not carry them.
                if not captured_old_blobs:
                    captured_old_blobs = dict(load_blobs(checkout_root, rng, parsed.files).old)
                state = source_state(checkout_root, rng)
                watch_fingerprint, legacy_watch = state.fingerprint, state.legacy_fingerprint
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
        record_rng = rng
        record_build: PacketBuild | None = build
        record_root = self.scratch_root(repo)
        record_source_fingerprint = watch_fingerprint
        record_legacy_fingerprint = legacy_watch
        if checkout_root is not None and parsed.range_mode in MUTABLE_RANGE_MODES:
            # Catch Development up from this machine's agent ledgers (the root the
            # Reader ingests) before any Snapshot below takes its watermark.
            sync_ledgers_before_capture(store, session, repo_root=checkout_root, source_store_root=default_store_root())
        if parsed.range_mode in MUTABLE_RANGE_MODES and session.review_base_sha and checkout_root is not None:
            if progress is not None:
                progress("Synchronizing commit checkpoints")
            sync_commit_checkpoints(
                store,
                session,
                checkout_root,
                store_root=root,
                limit=5000,
                reviewer_id=reviewer,
                progress=progress,
            )
            if not parsed.dirty:
                current_head = resolve_rev_range(checkout_root, working_tree=True).base_sha
                checkpoint = store.find_commit(session.id, current_head) if current_head else None
                if checkpoint is not None:
                    record_rng = checkpoint_range_for(checkout_root, session, current_head)
                    record_build = None
                    record_root = checkout_root
                    record_source_fingerprint = record_legacy_fingerprint = ""

        reviewers = list(store.list_reviewer_ids(session.id))
        if reviewer not in reviewers:
            reviewers.insert(0, reviewer)
        elif reviewers and reviewers[0] != reviewer:
            reviewers.remove(reviewer)
            reviewers.insert(0, reviewer)

        first: RevisionRecording | None = None
        for current_reviewer in reviewers:
            current = record_revision(
                store,
                session,
                record_root,
                record_rng,
                store_root=root,
                source_store_root=default_store_root() if checkout_root is not None else None,
                build=record_build,
                reviewer_id=current_reviewer,
                source_fingerprint_override=record_source_fingerprint,
                legacy_source_fingerprint=record_legacy_fingerprint,
                source_view_id=source_view_id,
                source_view_revision=source_view_revision,
                branch_ref_override=branch_key,
                worktree_key_override=worktree_key,
                progress=progress,
            )
            if first is None:
                first = current
        if first is None:
            raise RuntimeError("local Review capture reconciled no reviewer")
        if deferred_binding:
            session = attach_review(
                store, session, subject, move=bool(continue_review_id), fallback_base=parsed.base_sha
            )
            # A force-new/continue capture intentionally binds only after the
            # Snapshot is safely recorded. Re-run visible-message import now
            # that ownership exists so pre-Review agent output can attach to
            # this first Snapshot without weakening that failure-safety rule.
            if checkout_root is not None and parsed.range_mode in MUTABLE_RANGE_MODES:
                try:
                    from lemoncrow.pro.capabilities.review.session_comments import ingestor_for

                    ingestor_for(store, default_store_root()).sync(session, force=True)
                except (OSError, ValueError, RuntimeError):
                    pass
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
    review_id: str = ""


class _LocalChangeSetDirectory:
    """Change-set members across this machine's repositories (Phase 9R item 42).

    The local server runs as its one principal, who reads and manages every
    local repository; a member resolves through its own repository's Reader.
    """

    __slots__ = ("_reader", "_repo_id")

    def __init__(self, reader: LocalReviewReader, repo_id: str) -> None:
        self._reader = reader
        self._repo_id = repo_id

    @property
    def repository(self) -> str:
        return self._repo_id

    def member(self, ref: Any) -> Any:
        repository = self._reader._repository
        if ref.repository not in repository.list_repository_ids():
            return None
        session = repository.get_review(ref.repository, ref.review_id)
        if session is None:
            return None
        app = self._reader._app(_ResolvedReader(ref.repository, repository.store(ref.repository), session.id))
        return app.state.review_change_set_member(session.id)

    def may_link(self, repository: str) -> bool:
        return repository in self._reader._repository.list_repository_ids()

    def write_lock(self) -> Any:
        """Every local repository is one tenant: one lock file serializes their change-set writes."""

        from lemoncrow.pro.capabilities.review.change_sets import file_write_lock

        return file_write_lock(self._reader._repository.root / "change-sets.lock")


def _local_approval_authority() -> Any:
    """The local reviewer: ``local_app`` admits an approval decision only from the
    paired browser or a resolved non-agent process (P12-AGENT, fail closed)."""

    from lemoncrow.pro.capabilities.review.action_approval_api import ApprovalPrincipal

    return ApprovalPrincipal("human", "local")


def _prime_asgi_route_caches(app: Any) -> None:
    """Build lazy included-router tables before Reader requests fan out to threads.

    Some FastAPI versions in LemonCrow's supported range populate included-router
    match tables lazily. Reader requests run on a shared four-thread ASGI pool, so
    first-use route-cache construction must happen while the app is still protected
    by ``LocalReviewReader._apps_lock`` rather than concurrently in that pool.
    """

    for route in tuple(getattr(app, "routes", ())):
        for name in ("effective_candidates", "effective_low_priority_routes"):
            build = getattr(route, name, None)
            if callable(build):
                build()


class LocalReviewReader:
    """Resolve a local Review object to its repository store and shared Reader API."""

    __slots__ = (
        "_apps",
        "_apps_lock",
        "_ask_enabled",
        "_code_intelligence_factory",
        "_discussion_summaries",
        "_inflight",
        "_listen_port",
        "_repository",
        "_responses",
        "_review_repos",
        "_review_workflow",
    )

    def __init__(
        self,
        repository: LocalReviewRepository,
        *,
        listen_port: int,
        code_intelligence_factory: Callable[[str, Any], Any] | None = None,
        ask_enabled: bool | None = None,
    ) -> None:
        self._repository = repository
        self._listen_port = int(listen_port)
        self._code_intelligence_factory = code_intelligence_factory
        if ask_enabled is None:
            try:
                ask_available = importlib.util.find_spec("lemoncrow.pro.capabilities.review.ask") is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                ask_available = False
            self._ask_enabled = ask_available
        else:
            self._ask_enabled = bool(ask_enabled)
        from lemoncrow.pro.capabilities.review.discussion_summary import local_discussion_summary_host

        # DS0: one scheduler for every Review app here (refreshes queue into this machine's jobs table).
        self._discussion_summaries = local_discussion_summary_host()
        self._apps: dict[str, Any] = {}
        self._apps_lock = threading.Lock()
        # Review reference -> repository it resolved in, so the Reader's polling
        # does not search every repository per request. Only references that
        # resolved are kept, and each hit is re-checked in its repository.
        self._review_repos: dict[str, str] = {}
        # Finished GET responses of the heaviest Reader reads, keyed by request and
        # the review store's on-disk state, plus the identical reads in flight.
        self._responses: OrderedDict[tuple[Any, ...], tuple[int, tuple[tuple[str, str], ...], bytes]] = OrderedDict()
        self._inflight: dict[tuple[Any, ...], asyncio.Future[web.StreamResponse]] = {}
        # Trust T2: the local Inbox's workflow and next action for one Review (set by the service).
        self._review_workflow: (
            Callable[[str, ReviewStore, Any, datetime, SnapshotProgress | None], Mapping[str, Any] | None] | None
        ) = None
        repository.set_landing_revalidator(self.revalidate_landing)
        repository.set_merge_recorder(self.record_merge)

    def set_review_workflow(
        self,
        provider: Callable[[str, ReviewStore, Any, datetime, SnapshotProgress | None], Mapping[str, Any] | None] | None,
    ) -> None:
        """Where Review Status reads the local Inbox's workflow and next action (Trust T2)."""

        self._review_workflow = provider

    def revalidate_landing(self, repo_id: str, review_id: str) -> object:
        """Re-evaluate one armed intent's readiness through the Reader (local landing watch)."""

        app = self._app(_ResolvedReader(repo_id, self._repository.store(repo_id), review_id))
        return app.state.review_landing_revalidator(review_id, origin="watch")

    def record_merge(self, repo_id: str, review_id: str) -> Mapping[str, Any]:
        """Emit the review record of a provider-observed merge (signed, or the absence noted)."""

        app = self._app(_ResolvedReader(repo_id, self._repository.store(repo_id), review_id))
        return cast(Mapping[str, Any], app.state.review_record_on_merge(review_id))

    @property
    def ask_enabled(self) -> bool:
        return self._ask_enabled

    def _resolve_review(self, review_id: str) -> _ResolvedReader | None:
        known = self._review_repos.get(review_id)
        if known is not None:
            session = self._repository.get_review(known, review_id)
            if session is not None:
                return _ResolvedReader(known, self._repository.store(known), session.id)
        matches: list[_ResolvedReader] = []
        for repo_id in self._repository.list_repository_ids():
            session = self._repository.get_review(repo_id, review_id)
            if session is not None:
                matches.append(_ResolvedReader(repo_id, self._repository.store(repo_id), session.id))
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
                matches.append(_ResolvedReader(repo_id, store, revision.review_id))
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
                    annotation = store.get_annotation(annotation_id)
                    if annotation is not None and store.get_session(annotation.review_id) is not None:
                        resolved = _ResolvedReader(repo_id, store, annotation.review_id)
                        break
        if resolved is None:
            match = _EVIDENCE_PATH.match(path)
            if match:
                evidence_id = match.group("id")
                for repo_id in self._repository.list_repository_ids():
                    store = self._repository.store(repo_id)
                    evidence = store.get_evidence(evidence_id)
                    if evidence is not None and store.get_session(evidence.review_id) is not None:
                        resolved = _ResolvedReader(repo_id, store, evidence.review_id)
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
        resolved_repo_ids = {item.repo_id for item in candidates}
        if len(resolved_repo_ids) != 1:
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
        # lc-debt: apps are cached per (repo, checkout) and never evicted; drop
        # entries whose checkout is gone if fix lines pile up.
        checkout_root = (
            self._repository.source_root_for(resolved.repo_id, resolved.review_id)
            if resolved.review_id
            else self._repository.checkout_root(resolved.repo_id)
        )
        cache_key = f"{resolved.repo_id}:{resolved.review_id}:{checkout_root or ''}"
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

        def _review_workflow(
            session: Any, as_of: datetime, progress: SnapshotProgress | None
        ) -> Mapping[str, Any] | None:
            # Read at call time: the service sets its provider after this Reader exists.
            provider = self._review_workflow
            return None if provider is None else provider(resolved.repo_id, resolved.store, session, as_of, progress)

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
            ask_enabled=self._ask_enabled,
            change_set_directory=_LocalChangeSetDirectory(self, resolved.repo_id),
            # P12-AGENT: the local server is the one human reviewer; the agent's
            # own process is refused (fail closed) by ``local_app`` before a
            # decision reaches this app, so requester separation is not needed.
            approval_authority=_local_approval_authority,
            approval_separate_requester=False,
            discussion_summaries=self._discussion_summaries,
            review_workflow=_review_workflow,
        )
        _prime_asgi_route_caches(app)
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

    # What the Reader's first paint waits on (overview + targets), plus the
    # proofs/readiness and projection it asks for next. Each fills a per-Snapshot
    # cache in this process, so the reviewer's first request is a hit. History
    # goes first: it stores the new Snapshot's row (a store write), which must
    # land before the reads cached against the store's write generation.
    _WARM_PATHS = ("/revisions", "", "/targets", "/readiness", "/projection")

    # lc-debt: shutdown does not cancel a running warm-up, so a read that outlives the
    # core logs CoreUnavailable (exit stays 0); cancel warm tasks before the core stops.
    async def warm(self, review_id: str) -> None:
        """Run the Reader's first-load reads once, ahead of the reviewer. Best effort."""

        try:
            app, store = await asyncio.to_thread(self._resolved_pair, f"/api/reviews/{review_id}", {})
        except ServerError:
            return
        for suffix in self._WARM_PATHS:
            path = f"/api/reviews/{review_id}{suffix}"
            try:
                # On the Reader pool, never on this loop (seconds of blocking work),
                # and through the response cache the Reader's own reads hit.
                await self._cached_get(app, store, path, "", functools.partial(run_asgi_get_isolated, app, path))
            except Exception:
                _LOG.exception("Review warm-up read failed review=%s path=%s", review_id, suffix)
        # A capture is the durable event that originates the run plan's planned
        # pre-merge checks (never a browser read); the scheduler is plan-gated.
        scheduler = getattr(getattr(app, "state", None), "review_premerge_scheduler", None)
        if callable(scheduler):
            try:
                await asyncio.to_thread(scheduler, review_id)
            except Exception:
                _LOG.exception("Review pre-merge scheduling failed review=%s", review_id)

    def _resolved_pair(self, path: str, query: Mapping[str, str]) -> tuple[Any, ReviewStore | None]:
        if path == "/api/compare":
            return self._resolved_app(path, query), None
        resolved = self.resolve(path)
        return self._app(resolved), resolved.store

    async def handle(self, request: web.Request) -> web.StreamResponse:
        # Resolution opens Review stores; the Reader polls, so keep it off the
        # event loop that also answers every other request.
        app, store = await asyncio.to_thread(self._resolved_pair, request.path, dict(request.query))
        match = _CACHEABLE_READ.match(request.path) if request.method == "GET" else None
        if store is None or match is None or match.group("suffix") not in _CACHEABLE_SUFFIXES:
            return await _dispatch_asgi(app, request)
        return await self._cached_get(
            app, store, request.path, request.query_string, lambda: _dispatch_asgi(app, request)
        )

    async def _cached_get(
        self,
        app: Any,
        store: ReviewStore | None,
        path: str,
        query_string: str,
        compute: Callable[[], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        match = _CACHEABLE_READ.match(path)
        if store is None or match is None or match.group("suffix") not in _CACHEABLE_SUFFIXES:
            return await compute()
        stamp = await asyncio.to_thread(self._read_stamp, store)
        if stamp is None:
            return await compute()
        key = (id(app), path, query_string, stamp)
        hit = self._responses.get(key)
        if hit is not None:
            self._responses.move_to_end(key)
            return _cached_response(hit)
        pending = self._inflight.get(key)
        if pending is not None:
            # The same read of the same stored state is already being computed.
            try:
                shared = await asyncio.shield(pending)
            except BaseException:
                if not pending.done():
                    raise  # this request itself was cancelled
                shared = None  # the leader failed or went away: read for ourselves
            if isinstance(shared, web.Response) and isinstance(shared.body, bytes):
                return web.Response(status=shared.status, headers=shared.headers.copy(), body=shared.body)
            return await compute()
        future: asyncio.Future[web.StreamResponse] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            response = await compute()
            future.set_result(response)
        except BaseException:
            future.cancel()  # waiters fall back to their own read
            raise
        finally:
            self._inflight.pop(key, None)
        body = response.body if isinstance(response, web.Response) else None
        # Remembered only when nothing was written while it was computed.
        if (
            response.status == 200
            and isinstance(body, bytes)
            and await asyncio.to_thread(self._read_stamp, store) == stamp
        ):
            self._responses[key] = (response.status, tuple(response.headers.items()), body)
            while len(self._responses) > _RESPONSE_CACHE_MAX:
                self._responses.popitem(last=False)
        return response

    def _read_stamp(self, store: ReviewStore) -> tuple[Any, ...] | None:
        """The store's write generation; with change sets in it, every local store's too.

        A change-set member's readiness reads the other members' stores, so a
        write to any of them must miss the cached read.
        """

        own = _store_stamp(store)
        if own is None or not store.list_relationships_by_kind(CHANGE_SET_KIND):
            return own
        members: list[tuple[Any, ...]] = []
        for repo_id in self._repository.list_repository_ids():
            other = self._repository.store(repo_id)
            if other is store:
                continue
            stamp = _store_stamp(other)
            if stamp is None:
                return None
            members.append(stamp)
        return (*own, tuple(members))


# Reads that are pure functions of the review store (SQLite rows plus immutable
# per-Snapshot artifacts): a refresh of an unchanged Review is served from memory.
# Anything that also reads the working tree, live agent state or the clock
# (source-state, journal, live-state, projection's synthesis expiry) is excluded.
_CACHEABLE_READ = re.compile(r"^/api/reviews/(?P<id>[A-Za-z0-9_.:-]+)(?P<suffix>/[a-z-]+)?$")
_CACHEABLE_SUFFIXES = frozenset({None, "/targets", "/frontier", "/readiness"})
_RESPONSE_CACHE_MAX = 24  # a few Reviews' first-load reads; each is up to a few MB


# lc-debt: durable table + triggers installed into each local review store for the
# Reader cache; harmless if left behind, drop them if this cache is ever removed.
_GENERATION_TABLE = "lc_reader_generation"
# Agent-timeline ingestion appends here every few seconds while an agent works;
# no cached read depends on it, so it must not invalidate them. review_derived
# stores exact results of pure derivations: writing one never changes a response.
_UNTRACKED_TABLE_PREFIXES = ("development_", "review_derived")
_generation_ready: dict[str, int] = {}  # db path -> schema object count when installed
_generation_lock = threading.Lock()


def _ensure_generation(db_path: str) -> bool:
    """Install the write-generation counter; False (never cache) when it cannot be trusted."""

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        rows = conn.execute("SELECT type, name, COALESCE(sql, '') FROM sqlite_master").fetchall()
        tables = {name: sql for kind, name, sql in rows if kind == "table"}
        triggers = {name for kind, name, _sql in rows if kind == "trigger"}
        virtual = [name for name, sql in tables.items() if sql.upper().startswith("CREATE VIRTUAL TABLE")]
        tracked = [
            name
            for name in tables
            if not name.startswith(("sqlite_", _GENERATION_TABLE, *_UNTRACKED_TABLE_PREFIXES)) and name not in virtual
            # A virtual table's shadow tables change only with their base table's rows.
            and not any(name.startswith(f"{base}_") for base in virtual)
        ]
        wanted = {(name, op): f"lc_gen_{name}_{op}" for name in tracked for op in ("insert", "update", "delete")}
        missing = [key for key, trigger in wanted.items() if trigger not in triggers]
        # Triggers on a table that is no longer tracked would keep bumping it.
        stale = sorted({trigger for trigger in triggers if trigger.startswith("lc_gen_")} - set(wanted.values()))
        if stale:
            with conn:
                for trigger in stale:
                    conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        if missing or _GENERATION_TABLE not in tables:
            with conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_GENERATION_TABLE} "
                    "(id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER NOT NULL)"
                )
                conn.execute(f"INSERT OR IGNORE INTO {_GENERATION_TABLE} (id, n) VALUES (1, 0)")
                for name, op in missing:
                    conn.execute(
                        f'CREATE TRIGGER IF NOT EXISTS "lc_gen_{name}_{op}" AFTER {op.upper()} ON "{name}" '
                        f"BEGIN UPDATE {_GENERATION_TABLE} SET n = n + 1 WHERE id = 1; END"
                    )
        return True
    except sqlite3.Error:
        _LOG.warning("Reader cache disabled for %s: write generation unavailable", db_path, exc_info=True)
        return False
    finally:
        conn.close()


def _read_generation(db_path: str) -> tuple[int, int] | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            objects = int(conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0])
            row = conn.execute(f"SELECT n FROM {_GENERATION_TABLE} WHERE id = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return None if row is None else (objects, int(row[0]))


def _store_stamp(store: ReviewStore) -> tuple[Any, ...] | None:
    """Write generation of a review store: any commit by any process to a table a
    cached read depends on changes it. ``None`` means do not cache."""

    db_path = str(store.db_path)
    with _generation_lock:
        installed = _generation_ready.get(db_path)
    current = _read_generation(db_path) if installed else None
    if current is None or current[0] != installed:
        # First use, or the schema changed (a migration added a table): (re)install
        # so no table a cached read depends on goes untracked.
        with _generation_lock:
            if not _ensure_generation(db_path):
                _generation_ready[db_path] = 0
                return None
            current = _read_generation(db_path)
            if current is None:
                return None
            _generation_ready[db_path] = current[0]
    return (db_path, current[0], current[1])


def _cached_response(hit: tuple[int, tuple[tuple[str, str], ...], bytes]) -> web.Response:
    status, headers, body = hit
    return web.Response(status=status, headers=CIMultiDict(headers), body=body)
