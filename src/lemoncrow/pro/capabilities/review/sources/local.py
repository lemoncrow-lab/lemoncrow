"""The local review source: git range → packet → units → a persisted revision.

This is the only module that knows how a working tree becomes durable review
state. Everything under it is pure: ``packet.py`` composes analyses,
``units.py`` derives identity and fingerprints, ``store.py`` writes rows. The
orchestration — which session to reopen, whether this content is a new revision
at all, what gets written to disk as the record — lives here and nowhere else,
so the CLI and (later) the workspace API drive exactly the same sequence.

Two invariants it exists to hold:

**Reopen, never re-create.** A second ``lc review`` on the same repository and
the same range finds the session that already exists. A review whose marks reset
on every invocation is not review state, it is a cache.

**Identical content is not a new revision.** The packet is fingerprinted into a
``tree_fingerprint`` before anything is written; if that fingerprint is already
on file, the existing revision is returned and no row, no artifact and no mark
moves. Without this, running the command twice would strand every mark made
against the first run.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow_client.review_words import revision_name

from lemoncrow.core.foundation.optional_modules import module_absent, skip_if_absent
from lemoncrow.pro.capabilities.review import viewer_lens
from lemoncrow.pro.capabilities.review.anchors import AnchorResolution, blob_sha, build_anchor, resolve_anchor
from lemoncrow.pro.capabilities.review.gitdiff import EMPTY_TREE_SHA, RevRange, source_state
from lemoncrow.pro.capabilities.review.gitdiff import legacy_source_fingerprint as legacy_source_identity
from lemoncrow.pro.capabilities.review.models import ChangedFile, ImpactSite, ReviewPacket, review_packet_from_dict
from lemoncrow.pro.capabilities.review.packet import (
    PacketBuild,
    attach_changed_symbols,
    attach_impact,
    attach_provenance,
    build_review_packet_with_blobs,
)
from lemoncrow.pro.capabilities.review.packet_index import decode_packet_bytes
from lemoncrow.pro.capabilities.review.relationships import scm_identity_from_source_ref
from lemoncrow.pro.capabilities.review.revisions import (
    Reconciliation,
    compute_frontier,
    file_revision_delta,
    reconcile,
    symbol_move_candidates,
)
from lemoncrow.pro.capabilities.review.session_models import (
    ANCHOR_SIDES,
    ANNOTATION_KINDS,
    ANNOTATION_SOURCES,
    MARK_STATES,
    ActorType,
    AnchorMethod,
    AnchorMove,
    AnchorSide,
    Annotation,
    AnnotationAnchor,
    AnnotationKind,
    AnnotationSource,
    AnnotationState,
    DiscardedMark,
    MarkState,
    ReviewActivityEvent,
    ReviewAttribution,
    ReviewBinding,
    ReviewBindingSubjectKind,
    ReviewCaptureKind,
    ReviewCommit,
    ReviewFrontier,
    ReviewMark,
    ReviewRelationship,
    ReviewRevision,
    ReviewSession,
    ReviewSessionStatus,
    ReviewSubjectType,
    ReviewUnit,
    ThreadTurnOwnerKind,
)
from lemoncrow.pro.capabilities.review.store import AnnotationHistoryContext, ReviewStore, new_revision_id
from lemoncrow.pro.capabilities.review.units import (
    bare_fingerprint,
    derive_units,
    owns_deleted_text,
    split_anchor,
    symbol_body_digest,
    symbol_header,
    tree_fingerprint,
    unit_key,
)
from lemoncrow.pro.capabilities.review.validity import mark_event_reason

if TYPE_CHECKING:  # `from __future__ import annotations` keeps this import out of the runtime path.
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

# A working tree and a staged index are both "the change on this machine right
# now"; only a commit range names something already written down.
_SUBJECT_BY_MODE: dict[str, ReviewSubjectType] = {
    "working_tree": "local_change",
    "staged": "local_change",
    "commit_range": "commit_range",
}

_MARK_STATE_BY_NAME: dict[str, MarkState] = {
    "unreviewed": "unreviewed",
    "reviewed": "reviewed",
    "needs_changes": "needs_changes",
    "changed_since_review": "changed_since_review",
    "unknown": "unknown",
    "later": "later",
}


def _marked_generated(store: ReviewStore, repo_root: str, base_sha: str, packet: ReviewPacket) -> frozenset[str]:
    """Paths the trusted base marks generated, for stamping; nothing where the base is not locally trusted.

    A hosted store takes its trusted base from a provider-proven capture, never from the base a client
    pushed, so a hosted capture accepts nothing by the generated rule until that base is wired in (P7).
    """

    if callable(getattr(store, "trusted_base_authority", None)):
        return frozenset()
    from lemoncrow.pro.capabilities.review.review_roles import marked_generated_paths

    return marked_generated_paths(
        repo_root, base_sha, [(item.path, item.old_path, item.status) for item in packet.files]
    )


def encode_packet(packet: ReviewPacket) -> bytes:
    """Serialise a packet into the bytes the artifact store holds.

    gzip, because the store names the file ``packet.json.gz`` and a name that
    lies about its contents is a trap for the next reader. ``mtime=0`` so the
    same packet produces the same bytes -- a revision's ``packet_sha256`` must
    identify the packet, not the second it was written.
    """

    raw = json.dumps(packet.to_dict(), ensure_ascii=False, default=str).encode("utf-8")
    return gzip.compress(raw, mtime=0)


def read_packet_json(store: ReviewStore, revision: ReviewRevision) -> dict[str, Any] | None:
    """The stored packet as plain data, or ``None`` when it is not readable.

    Returned as a dict rather than a ``ReviewPacket``: an artifact written under
    an older ``packet_schema_version`` is still worth reading, and rehydrating it
    into today's dataclasses would either fail or quietly invent defaults for
    fields the capture never had.

    Never raises. A pruned, missing or corrupt artifact reads as ``None`` -- the
    caller still has the revision row and must be able to say so.
    """

    return decode_packet_bytes(store.read_packet_artifact(revision))


def coerce_mark_state(value: str) -> MarkState:
    """Narrow a caller-supplied string to a ``MarkState``, or refuse it.

    A silently-defaulted mark state is a reviewer who believes they recorded
    something they did not, so an unrecognised name raises rather than falling
    back to ``unreviewed``.
    """

    state = _MARK_STATE_BY_NAME.get(value)
    if state is None:
        raise ValueError(f"unknown mark state {value!r}; expected one of {', '.join(MARK_STATES)}")
    return state


_NAMED_RANGE_PREFIX = "named:"
# Readers of what Runtime writes (agent sidecars, hook-prepared builds); the
# Review-only engine ships none of them (server/review-engine-optional.txt).
_RATIONALE = "lemoncrow.pro.capabilities.review.rationale"
_EVIDENCE_CAPTURE = "lemoncrow.pro.capabilities.review.evidence_capture"
_PRECOMPUTE = "lemoncrow.pro.capabilities.review.precompute"


def _canonical_named_ref(repo_root: Path, spec: str) -> str:
    """Return a canonical Git ref name when *spec* is genuinely a named ref.

    Relative expressions and raw SHAs intentionally return ``""``. So does the
    word ``HEAD``: a review keyed to it would jump branches after checkout. The
    head of a range spelled with ``HEAD`` goes through ``_checked_out_branch``.
    """

    if not spec or spec == "HEAD":
        return ""
    try:
        import pygit2

        repo = pygit2.Repository(str(repo_root.resolve()))
        ref = repo.lookup_reference_dwim(spec)
    except Exception:  # libgit2 has several invalid-spec/not-found exceptions.
        return ""
    name = str(getattr(ref, "name", "") or "")
    return name if name.startswith("refs/") else ""


def _checked_out_branch(repo_root: Path) -> str:
    """The branch ``HEAD`` is attached to, or ``""`` when detached or unborn."""

    try:
        import pygit2

        repo = pygit2.Repository(str(repo_root.resolve()))
        if repo.head_is_detached or repo.head_is_unborn:
            return ""
        name = str(repo.head.name or "")
    except Exception:  # libgit2 has several invalid-spec/not-found exceptions.
        return ""
    return name if name.startswith("refs/heads/") else ""


def _named_range_source_ref(repo_root: Path, rng: RevRange) -> str:
    if rng.mode != "commit_range":
        return ""
    base = _canonical_named_ref(repo_root, rng.base_rev)
    # ``main...HEAD`` on a checked-out branch is that branch's review, under the
    # branch's name, so it is the same Review as ``main...feature``.
    head = _checked_out_branch(repo_root) if rng.head_rev == "HEAD" else _canonical_named_ref(repo_root, rng.head_rev)
    if not base or not head:
        return ""
    relation = "..." if rng.merge_base_sha else ".."
    return f"{_NAMED_RANGE_PREFIX}{base}{relation}{head}"


def source_ref(rng: RevRange, *, repo_root: Path | None = None) -> str:
    """The session's reopen key for this range.

    Working-tree and staged reviews have one durable session per repository.
    Commit ranges normally stay snapshot-isolated by resolved SHA: ``HEAD~1``
    names a different change after every commit and must not inherit yesterday's
    verdicts.

    A range whose two endpoints are proven named Git refs is different:
    ``main...feature`` is one evolving review. In that case the session key uses
    canonical ref names so the same ReviewSession receives new ReviewRevisions
    as the branch moves. ``main...HEAD`` counts when ``HEAD`` is on a branch.
    """

    if rng.mode != "commit_range":
        return ""
    if repo_root is not None:
        named = _named_range_source_ref(repo_root, rng)
        if named:
            return named
    return f"{rng.base_sha}..{rng.head_sha}"


def _legacy_head_is_ancestor(repo_root: Path, old_head: str, current_head: str) -> bool:
    if not old_head or not current_head:
        return False
    if old_head == current_head:
        return True
    try:
        import pygit2

        repo = pygit2.Repository(str(repo_root.resolve()))
        return bool(repo.descendant_of(pygit2.Oid(hex=current_head), pygit2.Oid(hex=old_head)))
    except Exception:
        return False


def _legacy_session_score(store: ReviewStore, session: ReviewSession) -> tuple[int, int, int, int, str]:
    """Prefer scarce human judgment over newer auto-populated sessions."""

    marks = store.list_marks(session.id)
    human_annotations = sum(1 for item in store.list_annotations(session.id) if item.source == "human")
    # ``later`` is a human setting a part aside, not a judgment: it is not scarce judgment worth preferring.
    substantive_marks = sum(1 for item in marks if item.state not in ("unreviewed", "later"))
    return (
        human_annotations + substantive_marks,
        human_annotations,
        substantive_marks,
        len(store.list_evidence(session.id)),
        session.updated_at,
    )


def _adopt_legacy_named_session(
    store: ReviewStore,
    repo_root: Path,
    rng: RevRange,
    *,
    resolved_root: str,
    subject_type: ReviewSubjectType,
    ref: str,
    title: str,
    restore_archived: bool = False,
) -> ReviewSession | None:
    """Re-key one compatible pre-named-range session without losing review work.

    Older builds keyed commit ranges only by resolved SHAs, so the same feature
    branch accumulated rival sessions as its head advanced. Adoption is
    conservative: same repository/mode/subject, same reviewed base, and an old
    head that is an ancestor of today's named head. Among compatible sessions,
    human judgment wins over evidence and recency. Other rows stay untouched.

    A discarded (``archived``) row is never re-keyed unless the caller has
    explicitly asked to restore one. Adopting a tombstone re-keys it *before*
    ``_usable`` gets to refuse it, which tells the reviewer that a review they
    have never opened is discarded -- and, because the new named key now
    resolves to that row, refuses it again on every retry while the discarded
    review it stole is no longer reachable under its own key.
    """

    if not ref.startswith(_NAMED_RANGE_PREFIX):
        return None
    candidates: list[ReviewSession] = []
    for candidate in store.list_sessions(status="", limit=200):
        if (
            candidate.repo_root != resolved_root
            or candidate.subject_type != subject_type
            or candidate.range_mode != rng.mode
            or candidate.source_ref.startswith(_NAMED_RANGE_PREFIX)
            or (candidate.status == "archived" and not restore_archived)
        ):
            continue
        revision = store.latest_revision(candidate.id)
        if revision is None or revision.base_sha != rng.base_sha:
            continue
        if not _legacy_head_is_ancestor(repo_root, revision.head_sha, rng.head_sha):
            continue
        candidates.append(candidate)
    if not candidates:
        return None
    chosen = max(candidates, key=lambda item: _legacy_session_score(store, item))
    store.update_session(chosen.id, source_ref=ref, title=title or rng.title)
    return store.get_session(chosen.id)


def record_scm_relationship(store: ReviewStore, session: ReviewSession, ref: str) -> None:
    """Attach source-control identity without making it the Change identity."""

    identity = scm_identity_from_source_ref(ref, range_mode=session.range_mode)
    if identity is None:
        return
    provider, external_id, url = identity
    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=session.id,
            kind="scm",
            relation="tracks",
            provider=provider,
            external_id=external_id,
            url=url,
            source="imported",
        )
    )


MUTABLE_RANGE_MODES: frozenset[str] = frozenset({"working_tree", "staged"})

# lc-debt: a branch that owns more commits than this since its Review base gets
# no checkpoint backfill at all (each checkpoint is a full packet build); page
# the backfill across captures instead of skipping it.
_MAX_CHECKPOINT_BACKFILL = 50


def worktree_key(repo_root: Path) -> str:
    """Stable identity of one checkout directory."""

    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8", errors="surrogateescape")).hexdigest()[:32]


@dataclass(frozen=True)
class ReviewSubject:
    """What a mutable Review is routed by: at most one active Review per subject."""

    repository_identity: str
    subject_kind: ReviewBindingSubjectKind
    subject_key: str
    worktree_key: str = ""

    def binding(self, review_id: str = "") -> ReviewBinding:
        return ReviewBinding(
            id="",
            repository_identity=self.repository_identity,
            subject_kind=self.subject_kind,
            subject_key=self.subject_key,
            review_id=review_id,
            worktree_key=self.worktree_key,
        )


def review_subject(
    repository_identity: str,
    *,
    branch: str,
    worktree_key: str,
    range_mode: str,
) -> ReviewSubject | None:
    """Route a mutable capture by its branch, else by its detached worktree.

    Staged and working-tree Reviews of one branch are separate subjects. A
    detached HEAD has no branch workstream: it routes by worktree and its Review
    never freezes a branch base (see :func:`branch_review_base_for`).
    """

    if range_mode not in MUTABLE_RANGE_MODES or not repository_identity:
        return None
    suffix = "#staged" if range_mode == "staged" else ""
    ref = branch.strip()
    if ref and not ref.startswith("refs/heads/"):
        ref = f"refs/heads/{ref}"
    if ref:
        return ReviewSubject(repository_identity, "branch", f"{ref}{suffix}", worktree_key)
    if worktree_key:
        return ReviewSubject(repository_identity, "detached", f"worktree:{worktree_key}{suffix}", worktree_key)
    return None


def local_review_subject(repo_root: Path, range_mode: str) -> ReviewSubject | None:
    """The subject of a capture read straight from *repo_root*'s Git state."""

    import pygit2

    from lemoncrow.pro.capabilities.review.gitdiff import current_branch_ref

    resolved = repo_root.resolve()
    try:
        repo = pygit2.Repository(str(resolved))
        common = Path(getattr(repo, "commondir", "") or repo.path).resolve(strict=False)
    except Exception:
        return None
    repository_identity = hashlib.sha256(str(common).encode("utf-8", errors="surrogateescape")).hexdigest()
    return review_subject(
        repository_identity,
        branch=current_branch_ref(resolved),
        worktree_key=worktree_key(resolved),
        range_mode=range_mode,
    )


def branch_review_base_for(subject: ReviewSubject | None, base_sha: str) -> str:
    """The base a new Review freezes: only branch Reviews own a commit workstream."""

    return base_sha if subject is not None and subject.subject_kind == "branch" else ""


def review_base_for_adoption(store: ReviewStore, session: ReviewSession, fallback: str) -> str:
    """Recover a legacy Review's original base from its earliest frozen revision."""

    for revision in store.list_revisions(session.id):
        if revision.base_sha:
            return revision.base_sha
    return fallback


class ReviewRoutingError(ValueError):
    """A capture asked to join a Review it cannot join."""

    def __init__(self, message: str, **details: str) -> None:
        super().__init__(message)
        self.details: dict[str, str] = details


def select_review(
    store: ReviewStore,
    *,
    subject: ReviewSubject | None,
    repo_root_key: str,
    subject_type: str,
    source_ref: str,
    range_mode: str,
    force_new: bool = False,
    continue_review_id: str = "",
    restore_archived: bool = False,
    reuse_closed: bool = False,
    adopt_unbound: bool = True,
) -> ReviewSession | None:
    """The existing Review a capture belongs to, or ``None`` to create one. Never writes.

    Order: an explicit ``--continue`` target, the subject's active Review, the
    subject's latest released Review when reopening, then a Review with the
    same reopen key. For mutable captures that last step only adopts Reviews no
    binding ever owned (pre-binding rows): the reopen key is shared by every
    branch, so a Review released by ``--new`` or ``--continue`` must never leak
    into another branch through it. ``adopt_unbound=False`` skips that step for
    a routed subject: hosted leaves pre-binding Reviews as they are.
    """

    if continue_review_id:
        candidate = store.resolve_session(continue_review_id)
        if candidate is None or candidate.repo_root != repo_root_key:
            raise ReviewRoutingError(
                "requested Review does not belong to this repository", review_id=continue_review_id
            )
        if candidate.status != "open":
            raise ReviewRoutingError("requested Review is not open", review_id=candidate.id, status=candidate.status)
        if candidate.range_mode != range_mode:
            raise ReviewRoutingError(
                f"requested Review uses {candidate.range_mode}; current capture uses {range_mode}",
                review_id=candidate.id,
                review_range_mode=candidate.range_mode,
                capture_range_mode=range_mode,
            )
        return candidate
    if force_new:
        return None
    allowed = {"open"}
    if reuse_closed or restore_archived:
        allowed.add("finished")
    if restore_archived:
        allowed.add("archived")
    mutable = range_mode in MUTABLE_RANGE_MODES
    if mutable and subject is not None:
        key = {
            "repository_identity": subject.repository_identity,
            "subject_kind": subject.subject_kind,
            "subject_key": subject.subject_key,
        }
        active = store.active_binding(**key)
        if active is not None:
            bound = store.get_session(active.review_id)
            # A discarded Review keeps its binding so ordinary recapture hits it
            # and stays read-only until explicitly restored.
            if bound is not None and bound.status in {"open", "archived"}:
                return bound
        if reuse_closed or restore_archived:
            latest = store.latest_binding(**key)
            released = None if latest is None else store.get_session(latest.review_id)
            if released is not None and released.status in allowed:
                return released
        if not adopt_unbound:
            return None
    legacy = store.find_session(
        repo_root=repo_root_key,
        subject_type=subject_type,
        source_ref=source_ref,
        range_mode=range_mode,
        never_bound=mutable,
    )
    return legacy if legacy is not None and legacy.status in allowed else None


def claim_review(
    store: ReviewStore,
    template: ReviewSession,
    subject: ReviewSubject | None,
    *,
    take_over: bool = False,
) -> ReviewSession:
    """Create the subject's Review, converging with a concurrent capture.

    ``take_over`` (``--new``) always creates and displaces the subject's
    current Review, which stays open and reachable with ``--continue``.
    """

    if subject is None:
        if template.range_mode in MUTABLE_RANGE_MODES:
            return store.create_session(template)
        # An immutable range has no binding to converge on; a racing capture of
        # the same range must still join one Review.
        session, _created = store.find_or_create_session(template, reuse=("open",))
        return session
    if take_over:
        created = store.create_session(template)
        store.bind_subject(subject.binding(created.id))
        return created
    session, _created = store.create_bound_session(template, subject.binding())
    return session


def attach_review(
    store: ReviewStore,
    session: ReviewSession,
    subject: ReviewSubject | None,
    *,
    move: bool = False,
    fallback_base: str = "",
) -> ReviewSession:
    """Bind an existing Review to *subject* and freeze its branch base if it has none.

    ``move`` (``--continue``) releases the Review's other subjects first, so it
    follows the reviewer instead of staying active on two branches.
    """

    if subject is None:
        return session
    if move:
        store.unbind_review(session.id)
    store.bind_subject(subject.binding(session.id))
    return freeze_branch_base(store, session, subject, fallback_base)


def freeze_branch_base(
    store: ReviewStore,
    session: ReviewSession,
    subject: ReviewSubject | None,
    fallback_base: str,
) -> ReviewSession:
    """Give a Review joining a branch its frozen base if it has none yet."""

    if subject is None or subject.subject_kind != "branch" or session.review_base_sha:
        return session
    store.update_session(session.id, review_base_sha=review_base_for_adoption(store, session, fallback_base))
    return store.get_session(session.id) or session


def open_or_create_session(
    store: ReviewStore,
    repo_root: Path,
    rng: RevRange,
    *,
    title: str = "",
    restore_archived: bool = False,
    force_new: bool = False,
    continue_review_id: str = "",
    reuse_closed: bool = False,
) -> ReviewSession:
    """Reopen this durable Change, refusing a discarded one by default."""

    ref = source_ref(rng, repo_root=repo_root)

    def _usable(session: ReviewSession) -> ReviewSession:
        if session.status != "archived":
            return session
        if not restore_archived:
            raise ValueError(
                "this review is discarded and read-only; use --reopen-review to restore it before continuing"
            )
        store.update_session(session.id, status="open")
        restored = store.get_session(session.id)
        if restored is None:  # pragma: no cover - update/get share one local store
            raise RuntimeError("discarded review disappeared while it was being restored")
        return restored

    def _with_source_context(session: ReviewSession) -> ReviewSession:
        usable = _usable(session)
        record_scm_relationship(store, usable, ref)
        return usable

    resolved_root = str(repo_root.resolve())
    subject_type = _SUBJECT_BY_MODE.get(rng.mode, "local_change")
    if rng.mode in MUTABLE_RANGE_MODES:
        subject = local_review_subject(repo_root, rng.mode)
        selected = select_review(
            store,
            subject=subject,
            repo_root_key=resolved_root,
            subject_type=subject_type,
            source_ref=ref,
            range_mode=rng.mode,
            force_new=force_new,
            continue_review_id=continue_review_id,
            restore_archived=restore_archived,
            reuse_closed=reuse_closed,
        )
        if selected is None:
            session = claim_review(
                store,
                ReviewSession(
                    id="",
                    subject_type=subject_type,
                    repo_root=resolved_root,
                    range_mode=rng.mode,
                    title=title or rng.title,
                    source_ref=ref,
                    review_base_sha=branch_review_base_for(subject, rng.base_sha),
                    review_base_policy="fixed" if force_new else "integrated",
                ),
                subject,
                take_over=force_new,
            )
        else:
            session = attach_review(store, selected, subject, move=bool(continue_review_id), fallback_base=rng.base_sha)
        return _with_source_context(session)

    existing = store.find_session(
        repo_root=resolved_root,
        subject_type=subject_type,
        source_ref=ref,
        range_mode=rng.mode,
    )
    if existing is not None:
        return _with_source_context(existing)
    adopted = _adopt_legacy_named_session(
        store,
        repo_root,
        rng,
        resolved_root=resolved_root,
        subject_type=subject_type,
        ref=ref,
        title=title,
        restore_archived=restore_archived,
    )
    if adopted is not None:
        return _with_source_context(adopted)
    created, _new = store.find_or_create_session(
        ReviewSession(
            id="",
            subject_type=subject_type,
            repo_root=resolved_root,
            range_mode=rng.mode,
            title=title or rng.title,
            source_ref=ref,
            review_base_sha=rng.base_sha,
        )
    )
    return _with_source_context(created)


def snapshot_revision(
    store: ReviewStore,
    session: ReviewSession,
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    session_id: str | None = None,
    limit: int = 40,
    build: PacketBuild | None = None,
    reviewer_id: str = "local",
) -> ReviewRevision:
    """Record the current state of *rng* as a revision of *session*.

    Thin by design: :func:`record_revision` is the one operation, and this is
    the name for callers that only want the revision back. It cannot be used to
    take a snapshot *without* reconciling, because there is no such operation --
    see :func:`record_revision` for why that separation was the defect.
    """

    return record_revision(
        store,
        session,
        repo_root,
        rng,
        store_root=store_root,
        session_id=session_id,
        limit=limit,
        build=build,
        reviewer_id=reviewer_id,
    ).revision


def _revision_attributions(packet: ReviewPacket, revision_id: str) -> tuple[ReviewAttribution, ...]:
    """Project packet provenance into append-only durable attribution rows."""

    rows: list[ReviewAttribution] = []
    primary = packet.provenance
    if primary.certainty == "exact" and primary.host and primary.session_id:
        rows.append(
            ReviewAttribution(
                revision_id=revision_id,
                scope="revision",
                scope_key="",
                host=primary.host.strip().lower(),
                session_id=primary.session_id.strip(),
                role="primary",
                model=primary.model or "",
                certainty="exact",
                method=primary.match_reason or "revision_exact",
            )
        )
    primary_key = ((primary.host or "").strip().lower(), (primary.session_id or "").strip())
    for item in packet.attributions:
        if item.certainty != "exact" or item.scope != "file" or not item.host or not item.session_id:
            continue
        session_key = (item.host.strip().lower(), item.session_id.strip())
        rows.append(
            ReviewAttribution(
                revision_id=revision_id,
                scope="file",
                scope_key=item.scope_key,
                host=session_key[0],
                session_id=session_key[1],
                role="primary" if primary_key == session_key and all(primary_key) else "contributor",
                model=item.model,
                certainty="exact",
                method=item.method,
            )
        )
    return tuple(rows)


def _actor_for(host: str | None, current: ActorType) -> ActorType:
    """Roll the revision's provenance up onto the session.

    Only the one promotion this scope can justify: a revision the correlator
    attributed to a named agent host makes the session agent-authored. ``mixed``
    exists in the vocabulary for when human-authored revisions become
    distinguishable, which nothing records yet — claiming it now would be a
    guess wearing a Literal.
    """

    if not host:
        return current
    return "agent" if current in ("unknown", "agent") else "mixed"


def resolve_unit(units: Sequence[ReviewUnit], target: str) -> ReviewUnit | None:
    """Find the unit a human meant by *target*: a ``unit_key`` or a file path.

    Paths resolve to the file unit, never to a hunk or symbol inside it — "I read
    this file" is the claim a path makes, and silently marking one of its symbols
    instead would record a narrower claim than the reviewer made.

    ``None`` rather than a guess when nothing matches: the caller reports it.
    """

    for unit in units:
        if unit.unit_key == target:
            return unit
    for unit in units:
        if unit.kind == "file" and unit.path == target:
            return unit
    return None


@dataclass(frozen=True)
class MarkResolution:
    """Where a verdict on one typed spelling lands, and what that spelling outranked.

    ``units`` is empty when nothing matched at all. ``shadowed`` holds the other
    reading of the same string -- the one that lost -- so a caller can say out
    loud which of the two questions it answered. It exists because a resolution
    that drops a reading silently is the whole defect: the reviewer types one
    word, two different judgments answer to it, and only one verdict is written.
    """

    units: tuple[ReviewUnit, ...] = ()
    shadowed: tuple[ReviewUnit, ...] = ()


def resolve_mark_units(
    units: Sequence[ReviewUnit],
    targets: Sequence[ReviewTarget],
    target: str,
) -> MarkResolution:
    """The units a verdict on *target* has to be written to, in target order.

    *target* is a ``unit_key``, a repo-relative path, or a target label -- every
    spelling any surface prints, so a reviewer can type back the word they were
    just shown (reader spec §29.3, "accept target labels/unit keys cleanly").

    A ``hun:``/``sym:`` key and a target label each name one judgment and are
    left exactly as typed: they are the precise forms, and widening one would
    store verdicts the reviewer never asked for.

    A repo-relative path and the file's ``fil:`` key are the same imprecise
    claim -- "I read this file" -- and both expand to every current
    :class:`~lemoncrow.pro.capabilities.review.targets.ReviewTarget` on that
    path. :func:`resolve_unit` answers both with the file unit, but no target is
    keyed to a file unit unless the whole file *is* the one target -- so a
    verdict parked there moves no progress count and no closure count, and the
    only mark either spelling could ever create was invisible to every number
    printed beside it. Expanding puts the same claim on the judgments the file
    actually contains.

    **A real path outranks a label that merely reads like one.** Labels are
    path-shaped (``src/app.py#0``, ``src/app.py::two``), so nothing stops a
    repository from containing a changed file spelled exactly like some other
    file's target label. Trying the label first sent a verdict on the file the
    reviewer named into a judgment inside a different file, and the widening
    disclosure -- which fires only when the resolution widened -- made that
    silent. The file the repository actually contains wins, and the label it beat
    comes back in :attr:`MarkResolution.shadowed` so the caller can name what it
    did not do. A label colliding with no path still resolves to its one target.

    Expansion is against *targets* as this revision derives them. A later
    revision that splits the path into more targets leaves the new ones
    unreviewed, which is correct: nobody has looked at them.

    Empty ``units`` when nothing matches, so the caller can refuse a stale path
    instead of recording silence. A caller holding no target view at all --
    *targets* empty, or derived against some other revision -- still gets the
    file unit back for a path this revision contains, because a match must never
    be a no-op.
    """

    by_key = {unit.unit_key: unit for unit in units}
    named = by_key.get(target)
    if named is not None and named.kind != "file":
        return MarkResolution((named,))
    labelled = tuple(by_key[item.unit_key] for item in targets if item.label == target and item.unit_key in by_key)
    if named is None:
        named = next((unit for unit in units if unit.kind == "file" and unit.path == target), None)
    if named is None:
        # No key and no path: the label is the only reading left, and it names
        # one judgment. Empty when nothing was labelled either -- the refusal.
        return MarkResolution(labelled)
    covered = tuple(by_key[item.unit_key] for item in targets if item.path == named.path and item.unit_key in by_key)
    covered = covered or (named,)
    landed = {unit.unit_key for unit in covered}
    return MarkResolution(covered, tuple(unit for unit in labelled if unit.unit_key not in landed))


LATER_UNAVAILABLE = "Decide later is not available on hosted reviews yet; mark the part reviewed or needs_changes"


class LaterMarkUnavailable(ValueError):
    """The store cannot record ``later`` yet (a hosted store that an older build may still serve)."""


def effective_mark_state(unit: ReviewUnit, requested: MarkState) -> MarkState:
    """The state that will actually be stored for *requested* on *unit*.

    Public so a surface can say what it is about to do *before* it does it. A
    downgrade the user only discovers in an aggregate count is a downgrade they
    never see: they asked for ``reviewed`` and the tool must say out loud that
    it recorded something weaker.
    """

    if requested == "reviewed" and unit.fingerprint_method == "unknown":
        return "unknown"
    return requested


def mark_downgrade_note(unit: ReviewUnit, requested: MarkState, recorded: MarkState) -> str:
    """Why *requested* was not what got stored, in one sentence, or ``""``.

    Lives next to the rule it explains so the wording cannot drift away from
    the condition that triggers it.
    """

    if recorded == requested:
        return ""
    return (
        f"recorded as {recorded}, not {requested}: this {unit.kind}'s content could not be "
        f"fingerprinted (method {unit.fingerprint_method}), so nothing can attest what was reviewed"
    )


def mark_unit(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    unit: ReviewUnit,
    *,
    state: MarkState = "reviewed",
    reviewer_id: str = "local",
    actor_type: ActorType = "human",
    note: str = "",
    expected_current_revision_id: str = "",
) -> ReviewMark:
    """Record a verdict on *unit*, bound to the content it was made against.

    The mark carries ``unit.content_fingerprint``, not just the revision id: a
    later revision that did not touch this unit leaves the mark valid, and one
    that rewrote it invalidates the mark even where the line numbers still match.

    A unit whose fingerprint could not be computed is never recorded as
    ``reviewed``; it is downgraded to ``unknown``, because a surface that
    promotes an uncomputable unit to reviewed tells the reader a human looked at
    something nobody can prove they looked at.

    Recording a verdict also advances the reviewer's durable frontier. This is
    the one door that does: a frontier moved by merely *running* a command is
    ``latest_revision`` under another name. It is written separately from the
    mark rather than read back off it because reconciliation is entitled to
    delete a mark -- the unit it attested to can leave the review -- and the
    fact that a human looked at revision N must outlive any particular verdict
    they formed there. See :meth:`ReviewStore.record_frontier`.
    """

    if state == "later" and not store.later_mark_supported():
        raise LaterMarkUnavailable(LATER_UNAVAILABLE)
    effective = effective_mark_state(unit, state)
    with store.atomic_review_transition():
        if expected_current_revision_id:
            store.require_current_revision(session.id, expected_current_revision_id)
        recorded = store.set_mark(
            ReviewMark(
                review_id=session.id,
                unit_key=unit.unit_key,
                state=effective,
                reviewed_revision_id=revision.id,
                content_fingerprint=unit.content_fingerprint,
                reviewer_id=reviewer_id,
                actor_type=actor_type,
                note=note,
            ),
            event_kind="judgment",
            event_revision_id=revision.id,
            event_reason=note,
        )
        # Setting a part aside is not a judgment, so it does not move "the revision I last reviewed".
        if effective != "later":
            store.record_frontier(session.id, reviewer_id, revision.id)
    return recorded


# --------------------------------------------------------------------------- #
# annotations: a selected line range becomes a durable anchor
# --------------------------------------------------------------------------- #


_ANNOTATION_KIND_BY_NAME: dict[str, AnnotationKind] = {
    "comment": "comment",
    "request_change": "request_change",
    "suggestion": "suggestion",
    "looks_good": "looks_good",
}

_ANNOTATION_SOURCE_BY_NAME: dict[str, AnnotationSource] = {
    "human": "human",
    "author": "author",
    "lemoncrow": "lemoncrow",
    "ai_review": "ai_review",
}

# `path:L10` or `path:L10-L20`. The `L` is required on both ends so a Windows
# drive letter or a `file:80` port can never be read as a line range.
_LINE_TARGET = re.compile(r"^(?P<path>.+?):L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")

# Diff sides as `@pierre/diffs` names them, mapped onto the anchor's own
# vocabulary. The browser's word for a side must never become the stored word:
# `AnchorSide` is a review concept and outlives whichever viewer produced it.
_SIDE_BY_NAME: dict[str, AnchorSide] = {
    "new": "new",
    "old": "old",
    "document": "document",
    "additions": "new",
    "deletions": "old",
}


def coerce_annotation_kind(value: str) -> AnnotationKind:
    """Narrow a string to an :data:`AnnotationKind`, or raise.

    Plan SS5.5 caps the dispositions at four. A fifth is refused here rather
    than stored and rendered as a raw identifier: a review tool whose verdict
    vocabulary can be extended by a caller has no vocabulary.
    """

    kind = _ANNOTATION_KIND_BY_NAME.get(value)
    if kind is None:
        raise ValueError(f"unknown annotation kind {value!r}; expected one of {', '.join(ANNOTATION_KINDS)}")
    return kind


def coerce_annotation_source(value: str) -> AnnotationSource:
    """Narrow an annotation source, because source is a trust boundary."""

    source = _ANNOTATION_SOURCE_BY_NAME.get(value)
    if source is None:
        raise ValueError(f"unknown annotation source {value!r}; expected one of {', '.join(ANNOTATION_SOURCES)}")
    return source


def coerce_anchor_side(value: str) -> AnchorSide:
    """Narrow a side name -- ours or ``@pierre/diffs``' -- to an :data:`AnchorSide`."""

    side = _SIDE_BY_NAME.get(value)
    if side is None:
        raise ValueError(f"unknown side {value!r}; expected one of {', '.join(ANCHOR_SIDES)}")
    return side


def parse_line_target(target: str) -> tuple[str, int, int]:
    """``"src/app.py:L10-L20"`` -> ``("src/app.py", 10, 20)``.

    The terminal spelling of a selection. A bare path is refused rather than
    defaulted to line 1, because a comment silently attached to the top of a
    file is a comment about something the reviewer never read.
    """

    match = _LINE_TARGET.match(target.strip())
    if match is None:
        raise ValueError(f"{target!r} is not a line target; write PATH:L10 or PATH:L10-L20")
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1:
        raise ValueError(f"{target!r} starts before line 1")
    if end < start:
        raise ValueError(f"{target!r} ends before it starts")
    return match.group("path"), start, end


def owning_symbol_unit(units: Sequence[ReviewUnit], path: str, start_line: int, end_line: int) -> ReviewUnit | None:
    """The tightest symbol unit in *path* that contains ``[start_line, end_line]``.

    Tightest, not first: a method inside a class is the definition the comment
    is actually about, and recording the class instead would make the anchor's
    rung 3 search a body large enough to contain the selected text twice.

    ``None`` when the range spans a symbol boundary or sits between definitions.
    That is a real answer -- the anchor then carries no symbol and relies on the
    text rungs -- not a failure to find one.
    """

    best: ReviewUnit | None = None
    for unit in units:
        if unit.kind != "symbol" or unit.path != path:
            continue
        if unit.start_line > start_line or unit.end_line < end_line:
            continue
        if best is None or (unit.end_line - unit.start_line) < (best.end_line - best.start_line):
            best = unit
    return best


def new_side_text(repo_root: Path, session: ReviewSession, path: str, *, status: str = "modified") -> str | None:
    """The new-side text of *path* as this session's range sees it, or ``None``.

    Deliberately the same :func:`~...gitdiff.load_blobs` the packet build uses,
    so the text an anchor is built from is byte-identical to the text the
    relocation ladder will later be handed. Two different reads of "the file"
    would produce a ``blob_sha`` that can never match, which would silently
    demote every rung-1 hit to a search.

    ``None`` -- never ``""`` -- when there is nothing to read. An empty file is
    a file we looked at.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import load_blobs

    if status == "deleted":
        return None
    probe = ChangedFile(path=path, old_path=None, status="modified")
    try:
        blobs = load_blobs(repo_root, range_for_session(repo_root, session), (probe,))
    except (OSError, ValueError, RuntimeError):
        # A repository we cannot open is a reason we did not look, and the
        # caller turns that into an unanchored comment rather than a failure.
        return None
    return blobs.new.get(path)


def source_paths_since_revision(
    store: ReviewStore,
    repo_root: Path,
    revision: ReviewRevision,
    rng: RevRange,
    paths: Sequence[str],
) -> tuple[str, ...]:
    """Compare mutable source with frozen bytes, not with the Git diff base.

    Include the previous patch's paths so reverting or removing a captured file
    is still counted. Missing frozen content is counted conservatively.
    """
    from lemoncrow.pro.capabilities.review.gitdiff import (
        MAX_REVIEW_MEDIA_BYTES,
        _blob_from_tree,
        _decode,
        _new_side_bytes,
        _open_repo,
        _tree_for_sha,
    )

    packet = read_packet_json(store, revision) or {}
    rows = {str(row["path"]): row for row in packet.get("files", ()) if isinstance(row, Mapping) and row.get("path")}
    deleted = {path for path, row in rows.items() if row.get("status") == "deleted"}
    deleted.update(
        str(row["old_path"]) for row in rows.values() if row.get("status") == "renamed" and row.get("old_path")
    )
    candidates = set(paths) | set(rows) | deleted
    tree = store.read_source_tree_artifact(revision.review_id, revision.id)
    blobs = store.read_blob_artifact(revision.review_id, revision.id) or {}
    media = store.read_media_artifact(revision.review_id, revision.id) or {}
    repo = _open_repo(repo_root)
    base = _tree_for_sha(repo, revision.base_sha)
    changed: list[str] = []
    for path in sorted(candidates):
        current, reason = _new_side_bytes(repo, repo_root, rng, None, path, max_bytes=MAX_REVIEW_MEDIA_BYTES)
        # Missing paths are real deletions; other read failures remain uncertain.
        if rng.mode == "staged":
            try:
                repo.index[path]
                missing = False
            except KeyError:
                missing = True
        else:
            candidate = repo_root / path
            missing = not candidate.exists() and not candidate.is_symlink()
        if reason and not missing:
            changed.append(path)
            continue
        if tree is not None:
            entry = tree.get(path)
            differs = (
                (not missing)
                if entry is None
                else (missing or hashlib.sha256(current or b"").hexdigest() != entry["content_digest"])
            )
        elif path in deleted:
            differs = not missing
        elif path in media:
            differs = missing or current != media[path]
        elif path in blobs:
            differs = missing or _decode(current) != blobs[path]
        elif path in rows:
            # A binary/oversized/gitlink without frozen bytes cannot be certified.
            differs = True
        else:
            frozen, frozen_reason = _blob_from_tree(repo, base, path, max_bytes=MAX_REVIEW_MEDIA_BYTES)
            try:
                frozen_missing = base is None
                if base is not None:
                    base / path
                    frozen_missing = False
            except KeyError:
                frozen_missing = True
            differs = missing != frozen_missing or (not missing and (bool(frozen_reason) or current != frozen))
        if differs:
            changed.append(path)
    return tuple(changed)


def revision_new_side_text(
    store: ReviewStore,
    repo_root: Path,
    session: ReviewSession,
    revision: ReviewRevision,
    path: str,
) -> str | None:
    """Exact new-side text the reviewer sees for *revision*, when available.

    New tracked revisions persist the blob snapshot beside their packet. Older
    revisions may predate that artifact. A commit range is immutable and can be
    read from Git safely; a local/staged review may only fall back to the live
    source when its cheap source fingerprint still matches the stored revision.
    Otherwise ``None`` is returned so the annotation stays explicitly
    unresolved instead of being anchored to newer bytes.
    """

    frozen = store.read_blob_artifact(session.id, revision.id)
    if frozen is not None:
        return frozen.get(path)

    if session.range_mode == "commit_range":
        return new_side_text(repo_root, session, path)

    if not revision.source_fingerprint:
        return None
    try:
        rng = range_for_session(repo_root, session)
        if not source_state(repo_root, rng).matches(revision.source_fingerprint):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return new_side_text(repo_root, session, path)


def _snapshot_number(revision: ReviewRevision) -> int:
    """The 0-based Snapshot number every reader-facing surface shows."""

    return max(0, revision.revision_number - 1)


def annotate(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    path: str,
    start_line: int,
    end_line: int,
    body: str,
    kind: str = "comment",
    side: str = "new",
    parent_id: str = "",
    created_by: str = "local",
    created_by_actor: ActorType = "human",
    source: str = "human",
    source_id: str = "",
    agent_role: str = "unknown",
    agent_host: str = "",
    agent_model: str = "",
    parent_session_id: str = "",
    source_message_id: str = "",
    created_at: str = "",
    title: str = "",
    evidence: Sequence[str] = (),
    confidence: float | None = None,
    turn_owner_kind: ThreadTurnOwnerKind = "none",
    turn_owner_id: str = "",
    new_text: str | None = None,
    repo_root: Path | None = None,
    annotation_id: str = "",
    expected_current_revision_id: str = "",
    reconsidered_after_structure: bool = False,
    structure_revision_id: str = "",
    hidden_authors: Collection[str] = (),
) -> Annotation:
    """Attach a comment to a line range, and capture everything to find it again.

    The anchor is built **here**, server-side, from the file's own text -- never
    from anything the browser computed. A viewer's line number is a rendering
    detail of one revision; the moment it becomes the anchor of record the
    review frontier is dead (spec SS5.4).

    *new_text* is the file's new-side text when the caller already has it (the
    CLI holds the exact blobs its packet was built from). When it is ``None``
    and *repo_root* is given, the text is read the same way the packet read it.
    When there is no text at all -- a binary file, an old-side selection -- the
    comment is still stored, with an anchor that carries a line number and says
    so. Losing a reviewer's words because we could not hash the file around
    them would be the worse failure, and
    :func:`~...anchors.resolve_anchor` treats such an anchor as ``unresolved``
    forever rather than orphaning it.

    Raises ``ValueError`` for the things a caller genuinely got wrong: a kind
    outside the four, a path this revision does not contain, a parent in another
    review, a line past the end of the file.
    """

    resolved_kind = coerce_annotation_kind(kind)
    resolved_source = coerce_annotation_source(source)
    resolved_side = coerce_anchor_side(side)
    file_level = int(start_line) < 1
    start = 0 if file_level else max(1, int(start_line))
    end = 0 if file_level else max(start, int(end_line))

    units = store.list_units(revision.id)
    file_unit = next((unit for unit in units if unit.kind == "file" and unit.path == path), None)
    if file_unit is None:
        raise ValueError(f"{path!r} is not a file in {revision_name(_snapshot_number(revision))} of this review")

    if parent_id:
        # A parent whose author this person may not read yet is no parent (SPEC 7.1 P9-4 A): a reply would also
        # reopen and re-version a note she cannot name.
        parent = viewer_lens.reachable(hidden_authors, store.get_annotation(parent_id), lambda note: note.created_by)
        if parent is None or parent.review_id != session.id:
            raise ValueError(f"no comment {parent_id!r} in this review to reply to")

    if new_text is None and repo_root is not None and resolved_side == "new":
        new_text = new_side_text(repo_root, session, path)

    symbol_unit = None if file_level else owning_symbol_unit(units, path, start, end)
    unit_key = symbol_unit.unit_key if symbol_unit is not None else file_unit.unit_key

    if file_level:
        anchor = AnnotationAnchor(
            path=path,
            side=resolved_side,
            start_line=0,
            end_line=0,
            blob_sha=blob_sha(new_text) if new_text is not None else "",
            unit_key=file_unit.unit_key,
        )
        method: AnchorMethod = "file"
        detail = f"file-level comment on {revision_name(_snapshot_number(revision))}"
    elif new_text is None:
        anchor = AnnotationAnchor(
            path=path,
            side=resolved_side,
            start_line=start,
            end_line=end,
            symbol_qualified_name=symbol_unit.symbol if symbol_unit is not None else "",
            symbol_fingerprint=symbol_unit.content_fingerprint if symbol_unit is not None else "",
            unit_key=unit_key,
        )
        method = "unresolved"
        detail = (
            "no new-side text to anchor to (binary, unreadable, or an old-side selection); "
            "this comment carries a line number and will not be relocated"
        )
    else:
        total = len(new_text.splitlines())
        if start > total:
            raise ValueError(f"{path} has {total} line(s); line {start} is past the end of the file")
        anchor = build_anchor(
            path=path,
            text=new_text,
            start_line=start,
            end_line=min(end, total),
            side=resolved_side,
            unit_key=unit_key,
            symbol_qualified_name=symbol_unit.symbol if symbol_unit is not None else "",
            symbol_fingerprint=symbol_unit.content_fingerprint if symbol_unit is not None else "",
        )
        method = "identical_blob"
        detail = f"anchored on {revision_name(_snapshot_number(revision))}"

    with store.atomic_review_transition():
        if expected_current_revision_id:
            store.require_current_revision(session.id, expected_current_revision_id)
        record = store.add_annotation(
            Annotation(
                id=annotation_id,
                review_id=session.id,
                revision_id=revision.id,
                anchor=anchor,
                body=body,
                reconsidered_after_structure=reconsidered_after_structure,
                structure_revision_id=structure_revision_id,
                kind=resolved_kind,
                parent_id=parent_id,
                created_by=created_by,
                created_by_actor=created_by_actor,
                source=resolved_source,
                source_id=source_id,
                agent_role=agent_role,
                agent_host=agent_host,
                agent_model=agent_model,
                parent_session_id=parent_session_id,
                source_message_id=source_message_id,
                created_at=created_at,
                title=title,
                evidence=tuple(str(item) for item in evidence if str(item)),
                confidence=confidence,
                turn_owner_kind=turn_owner_kind,
                turn_owner_id=turn_owner_id,
                anchor_method=method,
                anchor_detail=detail,
            )
        )
    # ``add_annotation`` writes the first rung of the audit trail in the same
    # transaction as the row, so there is nothing to append here.
    return record


def _machine_finding_fingerprint(
    *,
    source: AnnotationSource,
    source_id: str,
    path: str,
    unit_key: str,
    kind: AnnotationKind,
    title: str,
    body: str,
) -> str:
    """Stable semantic identity for one non-human review finding.

    ``source_id`` identifies the producer/session/skill, not necessarily one
    finding.  The finding key therefore also includes normalized claim text and
    the owning code unit.  Line numbers are deliberately absent so rerunning a
    reviewer after lines move within the same symbol does not duplicate the
    thread.  Unit fingerprints make materially rewritten code a fresh finding.
    """

    def normalized(value: str) -> str:
        return " ".join(str(value or "").split())

    raw = "\0".join(
        (
            source,
            normalized(source_id),
            path,
            unit_key,
            kind,
            normalized(title),
            normalized(body),
        )
    )
    return hashlib.sha256(raw.encode("utf-8", errors="surrogateescape")).hexdigest()


def annotate_machine_finding(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    path: str,
    start_line: int,
    end_line: int,
    body: str,
    source: AnnotationSource,
    source_id: str,
    created_by: str,
    created_by_actor: ActorType,
    kind: str = "comment",
    side: str = "new",
    title: str = "",
    evidence: Sequence[str] = (),
    confidence: float | None = None,
    new_text: str | None = None,
    repo_root: Path | None = None,
) -> tuple[Annotation, bool]:
    """Create one idempotent author/LemonCrow/AI finding.

    Returns ``(annotation, created)``. Human comments intentionally do not use
    this path: two humans writing the same sentence are two independent review
    acts. Machine producers, however, may rerun after every edit or reconnect;
    identical findings must remain one durable thread instead of flooding the
    reviewer.
    """

    resolved_source = coerce_annotation_source(source)
    if resolved_source == "human":
        raise ValueError("machine finding annotations cannot use source='human'")
    resolved_kind = coerce_annotation_kind(kind)
    file_level = int(start_line) < 1
    start = 0 if file_level else max(1, int(start_line))
    end = 0 if file_level else max(start, int(end_line))
    units = store.list_units(revision.id)
    file_unit = next((unit for unit in units if unit.kind == "file" and unit.path == path), None)
    if file_unit is None:
        raise ValueError(f"{path!r} is not a file in {revision_name(_snapshot_number(revision))} of this review")
    symbol_unit = None if file_level else owning_symbol_unit(units, path, start, end)
    owner = symbol_unit or file_unit
    finding = _machine_finding_fingerprint(
        source=resolved_source,
        source_id=source_id,
        path=path,
        unit_key=owner.unit_key + "@" + owner.content_fingerprint,
        kind=resolved_kind,
        title=title,
        body=body,
    )
    finding_ref = f"finding:{finding}"
    for existing in store.list_annotations(session.id):
        if existing.source == resolved_source and finding_ref in existing.evidence:
            return existing, False

    merged_evidence = tuple(dict.fromkeys((finding_ref, *(str(item) for item in evidence if str(item)))))
    annotation_id = (
        "ann-" + hashlib.sha256(f"{session.id}\0{finding}".encode("utf-8", errors="surrogateescape")).hexdigest()[:32]
    )
    try:
        created = annotate(
            store,
            session,
            revision,
            path=path,
            start_line=start,
            end_line=end,
            body=body,
            kind=resolved_kind,
            side=side,
            created_by=created_by,
            created_by_actor=created_by_actor,
            source=resolved_source,
            source_id=source_id,
            title=title,
            evidence=merged_evidence,
            confidence=confidence,
            new_text=new_text,
            repo_root=repo_root,
            annotation_id=annotation_id,
        )
    except sqlite3.IntegrityError:
        # Two concurrent machine producers may race after the read-side scan.
        # The deterministic primary key makes one insert the winner; the loser
        # returns the same durable thread rather than surfacing a duplicate.
        raced = store.get_annotation(annotation_id)
        if raced is None:
            raise
        return raced, False
    return created, True


@dataclass(frozen=True)
class ReviewClosure:
    """What the target-based review looked like when the human changed status.

    Completion is defined by the non-overlapping ``ReviewTarget`` denominator
    that the reader actually asks the human to judge. Raw file/hunk/symbol units
    remain the durable fingerprint substrate but never contribute independent
    completion counts here.
    """

    review_id: str
    status: ReviewSessionStatus
    target_count: int
    reviewed_targets: int
    unreviewed_targets: int
    changed_since_review: int
    needs_changes: int
    unknown_targets: int
    open_comments: int
    orphaned_comments: int
    later_targets: int = 0
    """Parts the reviewer left for later: not judged, so they are part of what is still open."""


def _owe_reopen_withdrawal(store: ReviewStore, session: ReviewSession) -> None:
    """Reopening a finished Review owes the withdrawal of an armed Merge when ready.

    Merge when ready is offered only on a finished Review. The withdrawal is
    recorded as owed before any provider is asked, here where every status
    change (Reader, CLI, hosted) lands, so a provider failure leaves it owed and
    retried; the caller holding a provider delegate settles it.
    """

    from lemoncrow.pro.capabilities.review.landing import landing_metadata, owe_landing_withdrawal

    owed = owe_landing_withdrawal(store, session.id, reason="reopened")
    if owed is None:
        return
    store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=session.id,
            revision_id=str(landing_metadata(owed).get("revision_id") or ""),
            kind="landing.withdrawn",
            actor_id="lemoncrow",
            actor_type="unknown",
            subject_type="landing",
            subject_id=owed.id,
            summary="Merge when ready withdrawn: the review was reopened",
            detail_json=json.dumps({"origin": "reopen", "reason": "reopened"}, sort_keys=True),
        )
    )


def set_review_status(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision | None,
    *,
    status: ReviewSessionStatus = "finished",
    reviewer_id: str = "local",
    expected_revision_id: str = "",
) -> ReviewClosure:
    """Persist a human status choice and return the reader's exact target tally.

    This never issues a verdict and never blocks finishing because work remains.
    It records the human's choice and reports an explainable partition of the
    current review target denominator beside that choice.
    """

    prior_status = session.status
    status_changed = prior_status != status
    if expected_revision_id:
        session, status_changed = store.update_session_status_if_current_revision(
            session.id, expected_revision_id, status
        )
    elif status_changed:
        store.update_session(session.id, status=status)
        updated = store.get_session(session.id)
        if updated is not None:
            session = updated
    # A finished Review releases the branch so the next change can start a new
    # Review. A discarded Review deliberately keeps its branch binding: ordinary
    # recapture must hit the archived row and stay read-only until the reviewer
    # explicitly restores it with --reopen-review.
    if status_changed and status == "finished":
        store.unbind_review(session.id)
    # Reopening it takes the branch back, unless a newer Review now holds it.
    if status_changed and status == "open" and prior_status == "finished":
        store.restore_binding(session.id)
    if status_changed:
        store.record_activity(
            ReviewActivityEvent(
                id="",
                review_id=session.id,
                revision_id=revision.id if revision is not None else "",
                kind="review.status_changed",
                actor_id=reviewer_id,
                actor_type="human",
                subject_type="review",
                subject_id=session.id,
                summary=f"Review status: {prior_status} → {status}",
            )
        )
    if status_changed and status == "open" and prior_status == "finished":
        _owe_reopen_withdrawal(store, session)
    units = store.list_units(revision.id) if revision is not None else ()
    marks = store.list_marks(session.id, reviewer_id=reviewer_id)
    annotations = store.list_annotations(session.id)

    target_count = reviewed = unreviewed = changed_count = needs = unknown = later = 0
    if revision is not None:
        from lemoncrow.pro.capabilities.review.targets import derive_review_targets, review_progress

        frontier = compute_frontier(
            session.id,
            reviewer_id,
            revision,
            units,
            marks,
            annotations=annotations,
        )
        targets = derive_review_targets(
            units,
            frontier.entries,
            read_packet_json(store, revision),
            annotations=annotations,
        )
        progress = review_progress(targets)
        target_count = progress.target_count
        reviewed = progress.reviewed
        unreviewed = progress.unreviewed
        changed_count = progress.changed_since_review
        needs = progress.needs_changes
        unknown = progress.unknown
        later = progress.later

    return ReviewClosure(
        review_id=session.id,
        status=status,
        target_count=target_count,
        reviewed_targets=reviewed,
        unreviewed_targets=unreviewed,
        changed_since_review=changed_count,
        needs_changes=needs,
        unknown_targets=unknown,
        later_targets=later,
        open_comments=sum(
            1 for item in annotations if item.source == "human" and not item.parent_id and item.state == "open"
        ),
        orphaned_comments=sum(
            1 for item in annotations if item.source == "human" and not item.parent_id and item.state == "orphaned"
        ),
    )


def annotation_counts(annotations: Sequence[Annotation]) -> dict[str, int]:
    """Outstanding *human* comments per state.

    Author rationale, LemonCrow evidence and AI hypotheses share the annotation
    substrate, but none is reviewer judgment. Counting them as open comments
    would turn automatic preparation into fake unresolved review work.
    """

    counts: dict[str, int] = {}
    for annotation in annotations:
        if annotation.source != "human":
            continue
        counts[annotation.state] = counts.get(annotation.state, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# refresh: a new revision, reconciled against what the reviewer already saw
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RevisionRecording:
    """What recording a revision did -- the write *and* its reconciliation.

    They are one result because they are one operation. ``created`` is ``False``
    when the tree fingerprinted to a revision already on file; it describes the
    revision row, and nothing else keys off it. Marks are reconciled and
    annotations re-anchored on the strength of what is actually stored against
    this revision, never on the strength of this flag.
    """

    revision: ReviewRevision
    created: bool
    baseline_revision: ReviewRevision | None
    """The revision this reviewer last actually looked at; ``None`` before their
    first mark. Not ``latest_revision``: any surface may advance that one, so
    keying "since my review" on it makes the answer depend on who ran what
    first. This is the left-hand side of the reconciliation and of every
    "new since my last review" claim."""
    reconciliation: Reconciliation
    moves: tuple[AnchorMove, ...] = ()
    discarded: tuple[DiscardedMark, ...] = ()
    """Verdicts this reconciliation deleted, named so a surface can read them
    out. Not ``Reconciliation.dropped``: that tuple also carries the stale
    old-key row of every mark a rename re-keyed, and those verdicts were
    carried forward, not thrown away."""


@dataclass(frozen=True)
class RefreshResult:
    """Everything a surface needs to answer "what changed since I reviewed?".

    ``created`` is ``False`` when the tree fingerprinted to a revision that was
    already stored: no new row and no new artifact. It is a fact about the
    revision, not a permission slip -- reconciliation happened either way, in
    :func:`record_revision`, before this result existed.
    """

    session: ReviewSession
    revision: ReviewRevision
    created: bool
    reconciliation: Reconciliation
    frontier: ReviewFrontier
    previous_revision: ReviewRevision | None = None
    """The revision this reviewer last looked at -- see
    :attr:`RevisionRecording.baseline_revision`. Named for what it is *to the
    reviewer*, which is the only reading of "previous" a frontier can use."""
    moves: tuple[AnchorMove, ...] = ()
    discarded: tuple[DiscardedMark, ...] = ()
    """See :attr:`RevisionRecording.discarded`. Every surface reads this one
    rather than counting ``Reconciliation.dropped`` for itself."""


def _with_scope(rng: RevRange, scope_paths: Sequence[str]) -> RevRange:
    from dataclasses import replace as replace_range

    return replace_range(rng, paths=tuple(sorted(set(scope_paths)))) if scope_paths else rng


def session_scope_paths(store: ReviewStore, session: ReviewSession) -> tuple[str, ...]:
    """The paths a scoped Review (one agent session's files) covers; ``()`` is the whole range.

    A scoped capture records its scope in each Snapshot's packet, so a later
    refresh that only knows the session (the Reader's) stays scoped instead of
    taking over the whole worktree.
    """

    if session.range_mode not in MUTABLE_RANGE_MODES:
        return ()
    revision = store.latest_revision(session.id)
    packet = read_packet_json(store, revision) if revision is not None else None
    raw = packet.get("scope_paths") if packet is not None else None
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({item for item in raw if isinstance(item, str) and item}))


def range_for_session(repo_root: Path, session: ReviewSession, *, scope_paths: Sequence[str] = ()) -> RevRange:
    """Rebuild the range a stored session reviews.

    Working-tree and staged sessions follow their mutable local source, limited
    to *scope_paths* for a scoped Review (see :func:`session_scope_paths`).
    Legacy commit-range sessions store resolved SHAs and therefore reopen the
    same snapshot forever. Named-range sessions intentionally store canonical
    Git refs so a branch review follows branch-head commits inside one
    ReviewSession.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, with_review_base

    if session.range_mode == "staged":
        staged = with_review_base(
            repo_root,
            session.review_base_sha,
            resolve_rev_range(repo_root, staged=True),
            advance=session.review_base_policy == "integrated",
        )
        return _with_scope(staged, scope_paths)
    if session.range_mode == "working_tree":
        working = with_review_base(
            repo_root,
            session.review_base_sha,
            resolve_rev_range(repo_root, working_tree=True),
            advance=session.review_base_policy == "integrated",
        )
        return _with_scope(working, scope_paths)
    if session.source_ref.startswith(_NAMED_RANGE_PREFIX):
        spec = session.source_ref[len(_NAMED_RANGE_PREFIX) :]
        return resolve_rev_range(repo_root, spec)
    base, _, head = session.source_ref.partition("..")
    if head and not base:
        # An empty-tree base -- a root commit, or a shallow clone whose parent
        # was never fetched -- is stored as "..<sha>". Reopening that with
        # ``base=None`` asks git for "<sha>~1", which is precisely the commit
        # that does not exist, so every refresh of the session would fail
        # forever.
        #
        # Ask the no-argument fallback first: on a root commit it produces this
        # exact range, and it is the only caller that knows the range was the
        # one the submodule-dirt discount chose. Rebuilding the range by hand
        # instead would drop that disclosure, so the reader would stop showing
        # a warning the CLI had printed over the very same diff.
        fallback = resolve_rev_range(repo_root)
        if fallback.base_sha == "" and fallback.head_sha == head:
            return fallback
        resolved = resolve_rev_range(repo_root, base=head, head=head)
        return RevRange(
            mode=resolved.mode,
            base_rev="(empty tree)",
            head_rev=resolved.head_rev,
            base_sha="",
            head_sha=resolved.head_sha,
            merge_base_sha="",
            dirty=resolved.dirty,
            title=resolved.title,
            submodule_dirt_discounted=resolved.submodule_dirt_discounted,
        )
    return resolve_rev_range(repo_root, base=base or None, head=head or None)


def _renames(packet: ReviewPacket) -> tuple[tuple[str, str], ...]:
    """``(old_path, new_path)`` for each file the diff reports as renamed.

    Copies are excluded on purpose: a copy leaves the original in place, so
    migrating its marks would move a verdict off a file that still exists.
    """

    return tuple((item.old_path, item.path) for item in packet.files if item.status == "renamed" and item.old_path)


def _new_side(packet: ReviewPacket, blobs: Mapping[str, str], path: str) -> tuple[str, str | None, str]:
    """Locate *path* in the new revision: ``(new_path, new_text, status)``.

    Follows a rename so an annotation is not lost to a ``git mv``, and returns
    ``None`` text -- never ``""`` -- when there is nothing to read. The two are
    not the same: an empty file is a file we looked at.
    """

    match: ChangedFile | None = None
    for item in packet.files:
        if item.path == path or (item.old_path or "") == path:
            match = item
            break
    if match is None:
        return path, None, "absent"
    text = blobs.get(match.path)
    if match.is_binary or match.status == "deleted":
        text = None
    return match.path, text, match.status


def reanchor_annotation(
    annotation: Annotation,
    packet: ReviewPacket,
    blobs: Mapping[str, str],
) -> AnchorResolution:
    """Run the ladder for one annotation against a freshly captured packet.

    A file that has dropped out of the change set entirely resolves as
    ``unresolved``, not ``orphaned``: the review no longer covers it, which is
    a reason we did not look rather than a reason the comment lost its footing.
    """

    new_path, new_text, status = _new_side(packet, blobs, annotation.anchor.path)
    if status == "absent":
        return AnchorResolution(
            status="unresolved",
            method="unresolved",
            anchor=annotation.anchor,
            detail="file is not part of this revision's change set",
        )
    if annotation.anchor.start_line == 0 or annotation.anchor_method == "file":
        if status == "deleted":
            return AnchorResolution(
                status="removed",
                method="removed",
                anchor=AnnotationAnchor(
                    path=new_path,
                    side=annotation.anchor.side,
                    start_line=0,
                    end_line=0,
                    unit_key=annotation.anchor.unit_key,
                ),
                detail="file removed from this revision",
            )
        if new_text is None:
            return AnchorResolution(
                status="unresolved",
                method="unresolved",
                anchor=annotation.anchor,
                detail="file-level comment could not read this file on the new revision",
            )
        return AnchorResolution(
            status="relocated",
            method="file",
            anchor=AnnotationAnchor(
                path=new_path,
                side=annotation.anchor.side,
                start_line=0,
                end_line=0,
                blob_sha=blob_sha(new_text),
                unit_key=annotation.anchor.unit_key,
            ),
            detail="file-level comment remains attached to the file",
        )
    return resolve_anchor(annotation.anchor, new_text=new_text, path=new_path, file_status=status)


_STATE_FOR_STATUS: dict[str, AnnotationState] = {
    "orphaned": "orphaned",
    "removed": "obsolete",
    "stale": "obsolete",
}


def _persist_anchor(
    store: ReviewStore,
    annotation: Annotation,
    revision: ReviewRevision,
    resolution: AnchorResolution,
) -> AnchorMove:
    """Write one resolution: the audit row first, then the annotation itself.

    The event is appended for **every** attempt, including the ones that moved
    nothing. An annotation whose position changed without a recorded rung is a
    move nobody can audit, and the cheapest way to guarantee that never happens
    is to make the row unconditional rather than to make it conditional on a
    correctly-computed "did it move".
    """

    store.record_anchor_event(
        annotation.id,
        revision.id,
        resolution.method,
        resolution.status,
        resolution.detail,
        resolution.anchor,
    )
    fields: dict[str, object] = {
        "anchor": resolution.anchor,
        "anchor_method": resolution.method,
        "anchor_detail": resolution.detail,
    }
    state = _STATE_FOR_STATUS.get(resolution.status)
    if state is not None:
        fields["state"] = state
        # Binding loss is a finding lifecycle event only for unresolved AI
        # findings. Explicit reviewer confirmation/dismissal/fixed disposition
        # remains durable and auditable instead of being overwritten by a generic
        # source move. A later exact AI revalidation may reopen superseded_stale.
        if (
            annotation.source == "ai_review"
            and annotation.finding_disposition in {"", "open"}
            and resolution.status in {"orphaned", "removed", "stale"}
        ):
            fields["finding_disposition"] = "superseded_stale"
            fields["finding_disposition_reason"] = ""
            fields["finding_disposition_revision_id"] = revision.id
            fields["finding_disposition_reviewer_id"] = ""
            fields["finding_disposition_evidence_json"] = "[]"
    elif resolution.located and annotation.state == "orphaned":
        # An orphan that has been found again is an open comment once more.
        # Leaving it orphaned would bury a live objection in the list of things
        # the reviewer was told to ignore.
        fields["state"] = "open"
    store.update_annotation(
        annotation.id,
        AnnotationHistoryContext(
            revision_id=revision.id,
            changed_by="lemoncrow",
            changed_by_actor="unknown",
        ),
        **fields,
    )
    return AnchorMove(
        annotation_id=annotation.id,
        path=resolution.anchor.path,
        status=resolution.status,
        method=resolution.method,
        detail=resolution.detail,
        from_line=annotation.anchor.start_line,
        to_line=resolution.anchor.start_line,
    )


def _rename_migrations(
    previous_units: Sequence[ReviewUnit],
    next_units: Sequence[ReviewUnit],
    renames: Sequence[tuple[str, str]],
) -> frozenset[str]:
    """Old unit keys whose verdict a rename *moved* rather than deleted.

    ``Reconciliation.dropped`` carries two different events under one name: a
    mark whose unit left the review, and the stale old-key row of a mark that a
    ``git mv`` re-keyed and which is being re-inserted under the new path in the
    same breath. Only the first is a discarded verdict. Announcing the second as
    one tells a reviewer their approval was thrown away in precisely the case
    where it was carried forward.

    Mirrors ``revisions._rename_aliases`` including its one restriction: an
    alias only counts when it lands on a unit of the new revision, because a
    symbol deleted *during* the rename genuinely left the review.
    """

    moved = {old: new for old, new in renames if old and new and old != new}
    if not moved:
        return frozenset()
    arrivals = {unit.unit_key for unit in next_units}
    return frozenset(
        unit.unit_key
        for unit in previous_units
        if unit.path in moved and unit_key(unit.kind, moved[unit.path], unit.symbol, unit.ordinal) in arrivals
    )


@dataclass(frozen=True)
class _SymbolTexts:
    """What reading the churned symbols' text yields: name-masked body digests and the decorator lines above each."""

    digests: dict[str, str]
    headers: dict[str, str]


def _symbol_body_digests(
    store: ReviewStore,
    review_id: str,
    previous_revision_id: str,
    previous_units: Sequence[ReviewUnit],
    next_blobs: Mapping[str, str],
    next_units: Sequence[ReviewUnit],
) -> _SymbolTexts:
    """Name-masked body digests and decorator headers of the symbols that left or arrived.

    Only the :func:`symbol_move_candidates` are read, and only when there is at least one on each
    side, so a revision with no symbol churn reads no frozen text. The new revision's text is the
    *next_blobs* already in memory; only the previous revision's frozen artifact is read from the
    store. A unit whose text is missing or whose window is empty has no digest and simply cannot be
    recognised as renamed; one with no readable header cannot be carried as moved.
    """

    removed, added = symbol_move_candidates(previous_units, next_units)
    if not removed or not added:
        return _SymbolTexts({}, {})
    digests: dict[str, str] = {}
    headers: dict[str, str] = {}
    previous_blobs = store.read_blob_artifact(review_id, previous_revision_id) or {}
    for blobs, units in ((previous_blobs, removed), (next_blobs, added)):
        lines_by_path: dict[str, list[str]] = {}
        for unit in units:
            text = blobs.get(unit.path)
            if text is None or unit.fingerprint_method == "unknown" or not 0 < unit.start_line <= unit.end_line:
                continue
            lines = lines_by_path.setdefault(unit.path, text.splitlines())
            digest = symbol_body_digest("symbol", unit.symbol, "\n".join(lines[unit.start_line - 1 : unit.end_line]))
            if digest:
                digests[unit.unit_key] = digest
            header = symbol_header(lines, unit.start_line)
            if header is not None:
                headers[unit.unit_key] = header
    return _SymbolTexts(digests, headers)


def _prefetch_symbol_digests(
    store: ReviewStore,
    review_id: str,
    reviewer_id: str,
    record_id: str,
    next_blobs: Mapping[str, str],
    next_units: Sequence[ReviewUnit],
) -> tuple[str, _SymbolTexts]:
    """``(baseline revision id, texts)`` computed *before* the transition lock is taken.

    The previous revision's frozen artifact is gzip + JSON: reading it while holding the write lock
    stalls every other writer for as long as that takes. The baseline is read again under the lock
    (a competing refresh may have moved it), and these digests are used only if it is the same
    revision -- both sides are immutable, so then they are exactly what the lock would have computed.
    """

    if not any(unit.kind == "symbol" for unit in next_units):
        return "", _SymbolTexts({}, {})  # nothing can have arrived as a symbol: read neither marks nor the baseline
    baseline = baseline_revision(
        store, review_id, marks=store.list_marks(review_id, reviewer_id=reviewer_id), reviewer_id=reviewer_id
    )
    if baseline is None or baseline.id == record_id:
        return "", _SymbolTexts({}, {})
    return baseline.id, _symbol_body_digests(
        store, review_id, baseline.id, store.list_units(baseline.id), next_blobs, next_units
    )


def unseen_units(
    store: ReviewStore,
    review_id: str,
    units: Sequence[ReviewUnit],
    added: Sequence[str],
    *,
    reviewer_id: str = "local",
) -> tuple[str, ...]:
    """*added*, minus every unit whose content this reviewer has already seen.

    ``new`` has to mean "you have not looked at this content". *added* alone
    means "this key was not in the revision you last reviewed", which is a
    claim about a comparison and not about the reviewer. The two part company
    the moment the tree goes backwards: an undo restores a symbol byte for byte,
    the revision they last reviewed did not contain it, and the unit they
    approved two revisions ago is presented to them as brand new work.

    Keyed on ``(unit_key, content_fingerprint)`` -- the same pair every other
    carry-forward decision in the system is keyed on -- so a *changed* unit
    under a familiar key is still new content and still says so.

    Deliberately does not restore the verdict. The mark was discarded because
    its unit left the review, the reviewer was told, and resurrecting it here
    would be this function issuing a verdict nobody recorded. Not new and not
    reviewed is exactly what the unit is.
    """

    if not added:
        return ()
    seen = store.attested_fingerprints(review_id, reviewer_id=reviewer_id)
    if not seen:
        return tuple(added)
    fingerprints = {unit.unit_key: unit.content_fingerprint for unit in units}
    return tuple(key for key in added if (key, fingerprints.get(key, "")) not in seen)


def baseline_revision(
    store: ReviewStore,
    review_id: str,
    *,
    marks: Sequence[ReviewMark] | None = None,
    reviewer_id: str = "local",
) -> ReviewRevision | None:
    """The newest revision *this reviewer* actually looked at, or ``None``.

    Public because it is the left-hand side of *every* "since I reviewed"
    answer, and the surfaces that render one -- the terminal, the HTTP overview,
    the refresh response -- have to scope their lists to the same revision the
    header they print is counting from. A surface that derives its own baseline
    is a surface that will eventually name a different window than the frontier
    beside it, which is how a destroyed verdict came to be reported on exactly
    one revision. *marks* is an optimisation for a caller that has already read
    them; leaving it out reads this reviewer's marks from the store.

    Not ``latest_revision``: that one is advanced by whoever ran the last
    command, and a question phrased "what changed since **I** reviewed" cannot
    be answered with it. Reading the frontier off the store's newest row is what
    made ``lc review --comments`` and ``lc review --since-my-review`` answer
    differently depending on which one the developer typed first.

    The evidence is :meth:`ReviewStore.frontier_revision` -- a row written when
    the reviewer records a verdict, and never by merely looking. It **wins
    outright** when it is there. The marks are a fallback for a store written
    before that table existed, and only that: taking the newer of the two was a
    second high-water mark over ``revision_number``, and after an undo it names
    a revision the reviewer has moved on from -- a mark recorded against
    revision 2 outvoting the frontier row that says their last look was
    revision 1, and every unit revision 2 lacked then reported as new.

    The marks alone are **not** enough, and that is the bug this signature
    exists to close. Reconciliation deletes a mark whose unit left the review --
    rename the one symbol a reviewer approved and their only mark is cleared --
    so a baseline derived from marks alone evaporates at the exact moment the
    reviewer most needs to be told what moved. Whichever command reconciled
    first ate their history, and every later command then reported "nothing
    reviewed yet" about code they had reviewed.

    ``None`` before their first verdict, and that is the honest answer: a
    reviewer who has recorded nothing has seen no revision, so every unit is new
    to them and none is stale.
    """

    durable = store.frontier_revision(review_id, reviewer_id)
    if durable:
        seen = {durable}
    else:
        recorded = store.list_marks(review_id, reviewer_id=reviewer_id) if marks is None else marks
        # A part left for later is not a look at the revision in the sense this baseline means (SPEC P1-2 B).
        seen = {mark.reviewed_revision_id for mark in recorded if mark.reviewed_revision_id and mark.state != "later"}
    if not seen:
        return None
    baseline: ReviewRevision | None = None
    for revision in store.list_revisions(review_id):
        if revision.id in seen and (baseline is None or revision.revision_number > baseline.revision_number):
            baseline = revision
    return baseline


_INCREMENTAL_DETECTOR_BLOCKERS = frozenset(
    {
        "astgrep_unavailable",
        "contract_literal_impact",
        "decorator_contract_impact",
        "impact_failed",
        "impact_unavailable",
        "signature_change_impact",
        "symbol_contract_impact",
    }
)
# A token meaning *a detector failed* blocks reuse above: its cached sites are
# not evidence of anything. A token meaning *this pass was partial* must not
# block -- the sites that pass did find are still true, and a second pass would
# hit the same cap -- but it has to ride into the packet reuse produces, or the
# reuse launders a capped scan into one that looks exhaustive.
# ``contract_literal_scan_truncated`` (the batched ast-grep cap) makes exactly
# the claim ``contract_literal_unparsed`` makes -- "an empty result here means
# not looked at everywhere, not not present" -- so it is classified with it.
_INCREMENTAL_DETECTOR_CARRY = frozenset(
    {
        "contract_literal_scan_truncated",
        "contract_literal_unparsed",
        "cross_repository_sites",
    }
)


def _prepared_build(store_root: Path, repo_root: Path, rng: RevRange) -> PacketBuild | None:
    """The background-prepared build of exactly the current working-tree source.

    It is keyed by repository, range and source fingerprint, so a hit is
    byte-for-byte the tree a fresh build would read now.
    """

    if rng.mode != "working_tree" or rng.paths:
        return None
    try:
        from lemoncrow.pro.capabilities.review.precompute import load_source_build
    except ModuleNotFoundError as exc:
        # Authoring hooks prepare builds; a build without them (no Runtime) has none.
        if not module_absent(exc, _PRECOMPUTE):
            raise
        return None
    return load_source_build(store_root, repo_root, rng, source_state(repo_root, rng).fingerprint)


def _incremental_impact_reuse(
    store: ReviewStore,
    previous: ReviewRevision,
    current: PacketBuild,
    rng: RevRange,
) -> object | None:
    """Build the narrow detector cache proven safe for *current*.

    The cache is keyed by persisted file-unit fingerprints plus an unchanged
    diff base. It carries detector sites only; graph facts are intentionally
    recomputed. If a path left the patch, a detector failure occurred, or a
    previous site cannot be attributed to one source edit, reuse is refused.
    """

    if (
        previous.range_mode != rng.mode
        or previous.base_sha != rng.base_sha
        or previous.merge_base_sha != rng.merge_base_sha
    ):
        return None
    packet = read_packet_json(store, previous)
    if packet is None:
        return None
    degraded = {str(item) for item in (packet.get("degraded") or ()) if isinstance(item, str)}
    if degraded & _INCREMENTAL_DETECTOR_BLOCKERS:
        return None

    previous_file_rows = packet.get("files")
    if not isinstance(previous_file_rows, list):
        return None
    previous_files = {
        str(row.get("path")): row
        for row in previous_file_rows
        if isinstance(row, Mapping) and isinstance(row.get("path"), str) and row.get("path")
    }
    previous_paths = frozenset(previous_files)
    current_paths = frozenset(item.path for item in current.packet.files)
    # A path that leaves the patch becomes an outside consumer again. Cached
    # detector results excluded it previously, so a full pass is required.
    if not previous_paths or not previous_paths <= current_paths:
        return None

    previous_units = tuple(unit for unit in store.list_units(previous.id) if unit.kind == "file")
    current_units = derive_units(current.packet, current.blobs.new, old_blobs=current.blobs.old)
    transition = file_revision_delta(previous_units, current_units, renames=_renames(current.packet))
    exact_preserved = set(transition.preserved)

    # FileRevisionDelta is now the shared exact-content gate used by both the
    # Reader cache and semantic reuse. Detector reuse adds two stricter metadata
    # checks because equal bytes reached through a different delta status or old
    # path can change what a contract detector is allowed to infer.
    reusable: set[str] = set()
    for item in current.packet.files:
        old_row = previous_files.get(item.path)
        if item.path not in exact_preserved or old_row is None or item.is_binary:
            continue
        if str(old_row.get("status") or "") != item.status:
            continue
        old_old_path = old_row.get("old_path")
        if (str(old_old_path) if old_old_path is not None else None) != item.old_path:
            continue
        reusable.add(item.path)
    if not reusable:
        return None

    detector_sites: list[ImpactSite] = []
    impact_rows = packet.get("impact")
    if isinstance(impact_rows, list):
        for row in impact_rows:
            if not isinstance(row, Mapping) or str(row.get("kind") or "") == "untouched_caller":
                continue
            source_path = str(row.get("source_path") or "")
            if not source_path:
                return None
            new_value = row.get("new")
            detector_sites.append(
                ImpactSite(
                    kind=str(row.get("kind") or "contract_literal"),  # type: ignore[arg-type]
                    path=str(row.get("path") or ""),
                    old=str(row.get("old") or ""),
                    new=str(new_value) if new_value is not None else None,
                    snippet=str(row.get("snippet") or ""),
                    in_patch=bool(row.get("in_patch")),
                    inspected_by_agent=None,
                    source_path=source_path,
                    uncertainty=str(row.get("uncertainty") or ""),
                )
            )

    from lemoncrow.pro.capabilities.review.impact import ImpactReuse

    return ImpactReuse(
        detector_sites=tuple(detector_sites),
        reusable_source_paths=frozenset(reusable),
        previous_touched_paths=previous_paths,
        carried_degraded=tuple(sorted(degraded & _INCREMENTAL_DETECTOR_CARRY)),
    )


_BASE_ONLY_CARRY_REASON = (
    "base-only movement: the diff base moved and the authored patch is equivalent on the new base; judgment kept"
)


def _base_only_unit_keys(
    store: ReviewStore,
    baseline: ReviewRevision | None,
    baseline_units: Sequence[ReviewUnit],
    record: ReviewRevision,
    stored_units: Sequence[ReviewUnit],
    marks: Sequence[ReviewMark],
) -> frozenset[str]:
    """Unit keys whose judgments survive a rebase/base move (Phase 9 item 34).

    Only for a reviewer with marks and a different baseline Snapshot. Any
    failure is "none": the marks then reconcile by fingerprint, which reopens.
    """

    if baseline is None or baseline.id == record.id or not marks:
        return frozenset()
    from lemoncrow.pro.capabilities.review.rebase_classification import classify_movement

    try:
        classification = classify_movement(
            baseline,
            baseline_units,
            read_packet_json(store, baseline),
            record,
            stored_units,
            read_packet_json(store, record),
        )
    except Exception:  # conservative: unknown equivalence reopens
        logging.getLogger(__name__).warning("base-only classification failed for %s", record.id, exc_info=True)
        return frozenset()
    return classification.base_only_unit_keys


def record_revision(
    store: ReviewStore,
    session: ReviewSession,
    repo_root: Path,
    rng: RevRange,
    *,
    store_root: Path,
    source_store_root: Path | None = None,
    session_id: str | None = None,
    limit: int = 40,
    build: PacketBuild | None = None,
    reviewer_id: str = "local",
    project_annotations: bool = True,
    source_fingerprint_override: str = "",
    legacy_source_fingerprint: str = "",
    source_view_id: str = "",
    source_view_revision: int = 0,
    branch_ref_override: str = "",
    worktree_key_override: str = "",
    checkpoint_history: bool = False,
    progress: Callable[[str], None] | None = None,
) -> RevisionRecording:
    """Persist the current state of *rng* **and** reconcile it. One operation.

    *checkpoint_history* records a commit checkpoint the working tree has
    already moved past (a partial commit, say). Such a checkpoint lacks the
    still-uncommitted files, which have not left the Review, so a mark whose
    unit is merely absent from it is kept for the draft capture that follows
    instead of being discarded.

    *legacy_source_fingerprint* is the commit-id form of the source identity
    *source_fingerprint_override* names (see ``gitdiff.SourceState``), so a
    Snapshot recorded before the tree-id identity still counts as unchanged.

    This is the only function in the system that writes a revision, and it is
    the only one that carries marks and comment anchors onto it. That is not a
    convenience: splitting the two is the defect this shape exists to make
    unrepresentable. When a plain ``lc review`` could record revision 2 without
    reconciling, the *next* command found ``latest_revision`` already equal to
    the revision it was about to take, concluded nothing had happened, and left
    a human's ``reviewed`` standing over rewritten code and a comment labelled
    "file unchanged since the comment" on a file that had been rewritten. The
    stale verdict was then permanent, because no later run would ever see a
    transition to reconcile. There is no snapshot-without-reconcile door left to
    walk through: :func:`snapshot_revision` calls this and throws away the rest.

    Order is the point. The baseline revision's units and the reviewer's marks
    are read **before** the write, because the write is what makes them
    previous; reading them afterwards compares the new revision against itself
    and reports that nothing ever changes.

    The baseline is the revision *this reviewer last looked at* -- see
    :func:`baseline_revision` -- and not the store's newest row. Now that every
    path records, "the previous revision" is whatever the last command happened
    to do, and a question phrased "what changed since **I** reviewed" cannot be
    answered with it.

    Idempotent, and not by way of a "did I just create it" flag:

    * Identical content fingerprints to the revision already on file, so no row
      and no artifact are written.
    * Reconciliation is decided on ``content_fingerprint``, so re-running it
      over the marks it just produced is a fixed point -- every mark comes back
      ``carried`` and nothing is written.
    * Re-anchoring is skipped only for an annotation whose stored geometry was
      *derived from this revision* -- its newest anchor event is one recorded
      here -- so the append-only audit table grows only when something actually
      resolved, and the row is never left describing a different revision. The
      weaker test ("any event on this revision") is what a revert defeats:
      content that changed and changed back resolves to an *older* stored
      revision the comment already owns its origin event on, and the comment
      would then keep the geometry of the revision in between -- a line and a
      function name that exist nowhere in the tree. A ``created`` flag is no
      better: it reads false forever on exactly that revision.

    The packet is persisted as an artifact rather than rebuilt on demand. A
    working-tree new-side blob is gone the instant the file is saved again, so a
    rebuild would silently re-anchor the review to different content and every
    mark would then be describing something nobody reviewed.

    ``with_patch_text=True`` is what makes hunk fingerprints content-derived
    rather than geometry-derived; it costs the diff walk one string per hunk and
    is off everywhere else.

    *build* lets a caller that has **already built and displayed** a packet hand
    it over instead of paying for a second impact pass. It carries the packet
    *and* the blob texts that produced it, and the pairing is the point: a
    packet alone would force this function to re-read the working tree, and any
    file saved between the two reads would be stored as content A under
    fingerprints describing content B. A reviewer must never mark a packet they
    were not shown. Pass a build made with ``with_patch_text=True``; one without
    hunk bodies still snapshots, with ``unknown`` hunk fingerprints.
    """

    supplied_build = build is not None
    if build is None:
        previous_for_reuse = store.latest_revision(session.id)
        # Background preparation (``live_runtime``) may already hold this exact
        # source's diff and bytes; an explicit Snapshot takes that build over.
        prepared = _prepared_build(store_root, repo_root, rng)
        if previous_for_reuse is None and prepared is None:
            build = build_review_packet_with_blobs(
                repo_root,
                rng,
                store_root=store_root,
                session_id=session_id,
                limit=limit,
                with_patch_text=True,
                unbounded_patch_text=True,
            )
        else:
            # Capture the diff/blob snapshot once without semantic impact, use
            # its exact bytes to decide reuse, then attach fresh impact to that
            # same snapshot. No second worktree read is allowed between them.
            if prepared is not None:
                base_build = attach_provenance(store_root, repo_root, rng, prepared, session_id=session_id)
            else:
                base_build = build_review_packet_with_blobs(
                    repo_root,
                    rng,
                    store_root=store_root,
                    with_impact=False,
                    session_id=session_id,
                    limit=limit,
                    with_patch_text=True,
                    unbounded_patch_text=True,
                )
            reuse = (
                _incremental_impact_reuse(store, previous_for_reuse, base_build, rng)
                if previous_for_reuse is not None
                else None
            )
            build = attach_impact(repo_root, rng, base_build, limit=limit, reuse=reuse)
    packet = build.packet
    blobs = build.blobs
    nested_snapshot_payload: bytes | None = None
    nested_snapshot_fingerprint = ""
    nested_snapshot_degraded: set[str] = set()
    if rng.mode == "working_tree" and not source_fingerprint_override:
        from lemoncrow.pro.capabilities.review.snapshot import (
            ReviewSnapshotUnavailable,
            capture_dirty_submodule_snapshots,
        )

        try:
            nested_snapshot_payload, nested_snapshot_fingerprint, _nested_paths = capture_dirty_submodule_snapshots(
                repo_root
            )
        except ReviewSnapshotUnavailable:
            nested_snapshot_degraded.add("submodule_snapshot_unavailable")

    previous_projection = store.latest_revision(session.id)
    previous_packet = None
    previous_units: tuple[ReviewUnit, ...] = ()
    previous_blobs = None
    if previous_projection is not None:
        previous_payload = read_packet_json(store, previous_projection)
        if previous_payload is not None:
            try:
                previous_packet = review_packet_from_dict(previous_payload)
            except (TypeError, ValueError):
                pass  # A missing/legacy projection falls back to fresh derivation.
            else:
                previous_units = store.list_units(previous_projection.id)
                previous_blobs = store.read_blob_artifact(session.id, previous_projection.id)
    units = derive_units(
        packet,
        blobs.new,
        previous_packet=previous_packet,
        previous_units=previous_units,
        previous_blobs=previous_blobs,
        old_blobs=blobs.old,
        progress=(
            (lambda completed, total: progress(f"Finding changed parts · {completed}/{total} files"))
            if progress is not None
            else None
        ),
    )
    if progress is not None:
        progress("Deriving dependency targets")
    from lemoncrow.pro.capabilities.review.dependencies import dependency_capture_gaps, derive_dependency_units
    from lemoncrow.pro.capabilities.review.units import stamp_answers

    changed_paths = tuple(item.path for item in packet.files)
    # An existing side whose text is absent was never read (over the cap,
    # unreadable). Only added/deleted files legitimately lack a side.
    base_expected = tuple(item.path for item in packet.files if item.status != "added")
    head_expected = tuple(item.path for item in packet.files if item.status != "deleted")
    dependency_units = derive_dependency_units(
        blobs.old,
        blobs.new,
        changed_paths,
        base_expected=base_expected,
        head_expected=head_expected,
    )
    if dependency_units:
        units = (*units, *dependency_units)
    units = stamp_answers(
        units,
        packet,
        marked_generated=_marked_generated(store, str(repo_root), rng.base_sha, packet),
    )
    capture_degraded = set(nested_snapshot_degraded)
    if dependency_capture_gaps(
        blobs.old, blobs.new, changed_paths, base_expected=base_expected, head_expected=head_expected
    ):
        capture_degraded.add("dependency_side_unavailable")
    fingerprint = tree_fingerprint(units)
    if nested_snapshot_fingerprint:
        fingerprint = hashlib.sha256(f"{fingerprint}\0submodules\0{nested_snapshot_fingerprint}".encode()).hexdigest()
    legacy_watch = ""
    if source_fingerprint_override:
        # A server-backed Review has an exact source identity already: the
        # committed View revision/manifest it verified the packet against.
        watch_fingerprint = source_fingerprint_override
        legacy_watch = legacy_source_fingerprint
    elif build.source_fingerprint:
        # Persist exactly the identity proven around the diff/blob capture. Do
        # not re-read a mutable worktree after the packet has already frozen it.
        watch_fingerprint = build.source_fingerprint
        legacy_watch = legacy_source_fingerprint or legacy_source_identity(repo_root, rng, watch_fingerprint)
    else:
        try:
            state = source_state(repo_root, rng)
            watch_fingerprint, legacy_watch = state.fingerprint, state.legacy_fingerprint
        except (OSError, RuntimeError, ValueError):
            # Change detection is advisory. A source we cannot fingerprint still
            # gets a fully reviewable revision; the workspace simply cannot offer
            # automatic refresh for that row.
            watch_fingerprint = ""
    # A file unit can only use a placeholder fingerprint for deleted, binary,
    # oversized or unreadable content. In that case the file-only tree digest is
    # not a semantic content identity. Fold in the independent exact source
    # generation when we have it; parser/symbol projections remain excluded,
    # while two different large/binary source states can never be described as
    # semantically equivalent merely because both were unreadable.
    has_unknown_file = any(unit.kind == "file" and unit.fingerprint_method == "unknown" for unit in units)
    semantic_fingerprint = fingerprint
    if has_unknown_file and watch_fingerprint:
        payload = f"{fingerprint}\0source\0{watch_fingerprint}".encode()
        fingerprint = hashlib.sha256(payload).hexdigest()

    # Remember which Review head this capture started from. The immutable row
    # may be recorded optimistically, but reconciliation may only publish it if
    # no competing refresh advanced the human-visible head first.
    expected_current = store.latest_revision(session.id)
    expected_current_revision_id = expected_current.id if expected_current is not None else ""
    if watch_fingerprint and not supplied_build and not source_fingerprint_override and rng.mode != "commit_range":
        try:
            if source_state(repo_root, rng).fingerprint != watch_fingerprint:
                raise ValueError("source changed after Review capture; retry against the settled source")
        except (OSError, RuntimeError) as exc:
            raise ValueError("could not verify captured Review source before publication") from exc

    if progress is not None:
        progress("Saving revision and source artifacts")
    existing = store.find_revision_by_snapshot_identity(session.id, fingerprint, watch_fingerprint)
    if existing is None and watch_fingerprint and legacy_watch and legacy_watch != watch_fingerprint:
        # lc-debt: a Snapshot recorded before the tree-id source identity (2026-10-13)
        # stores the commit-id form; the same source in that form is unchanged, so an
        # existing Review takes no extra Snapshot after the upgrade. Remove with
        # SourceState.legacy_fingerprint no earlier than 2027-01-13 (see gitdiff).
        legacy_tree = (
            hashlib.sha256(f"{semantic_fingerprint}\0source\0{legacy_watch}".encode()).hexdigest()
            if has_unknown_file
            else fingerprint
        )
        existing = store.find_revision_by_snapshot_identity(session.id, legacy_tree, legacy_watch)
    # File units with an `unknown` fingerprint carry a deterministic placeholder
    # (deleted/binary/oversized/unreadable), not exact bytes. Require the
    # independent source fingerprint to agree before reusing such a revision;
    # otherwise two different source states can inherit one stored packet and a
    # stale reviewed verdict.
    if existing is None and watch_fingerprint:
        # Compatibility with revisions captured when tree identity included
        # parser-derived symbol/hunk projections. Exact source identity is
        # analysis-independent, so the same code must not become a new revision
        # merely because LemonCrow learned a better symbol name.
        existing = store.find_latest_revision_by_source_fingerprint(session.id, watch_fingerprint)
    if existing is not None:
        record = existing
        if watch_fingerprint and not record.source_fingerprint:
            updated = store.set_revision_source_fingerprint(record.id, watch_fingerprint)
            if updated is not None:
                record = updated
        if source_view_id and not record.source_view_id:
            # Exact Snapshot recapture may come from another principal/View.
            # Preserve the first durable binding rather than making current-index
            # context jump between worktrees/users merely because identical bytes
            # were published again later.
            updated = store.set_revision_source_view(
                record.id,
                view_id=source_view_id,
                view_revision=source_view_revision,
            )
            if updated is not None:
                record = updated

        stored_units = store.list_units(record.id)
        stored_packet = read_packet_json(store, record)
        stored_files = stored_packet.get("files") if isinstance(stored_packet, dict) else None
        if packet.files and isinstance(stored_packet, dict) and isinstance(stored_files, list) and not stored_files:
            # Early hosted captures could persist the durable target rows but a
            # packet artifact with no file entries. Re-capturing the same exact
            # source is the one safe repair point: keep the revision's existing
            # analysis/provenance projection, restore only its source-file rows,
            # and let update_revision_projection refresh the packet hash/size.
            repaired_packet = dict(stored_packet)
            repaired_packet["files"] = packet.to_dict()["files"]
            repaired_payload = gzip.compress(
                json.dumps(repaired_packet, ensure_ascii=False, default=str).encode("utf-8"),
                mtime=0,
            )
            record = store.update_revision_projection(
                record.id,
                packet_payload=repaired_payload,
                packet_schema_version=record.packet_schema_version,
                degraded=record.degraded,
                provenance_host=record.provenance_host,
                provenance_model=record.provenance_model,
                provenance_session_id=record.provenance_session_id,
                provenance_certainty=record.provenance_certainty,
                units=stored_units,
            )

        # Reopening an older revision is also our chance to backfill the exact
        # text snapshot introduced after that revision was first recorded. Exact
        # tree or source identity proves these are the same reviewed bytes.
        if store.read_blob_artifact(session.id, record.id) is None:
            store.write_blob_artifact(session.id, record.id, blobs.new)
        if blobs.binary_new and store.read_media_artifact(session.id, record.id) is None:
            store.write_media_artifact(session.id, record.id, blobs.binary_new)
        if nested_snapshot_payload is not None and store.read_submodule_artifact(session.id, record.id) is None:
            store.write_submodule_artifact(session.id, record.id, nested_snapshot_payload)
        created = False
        stored_units = store.list_units(record.id)
    else:
        # A new Snapshot freezes Development at the event watermark taken in
        # add_revision's transaction; catch up on observable agent work first
        # so edits that produced this source are not reported as "since".
        from lemoncrow.pro.capabilities.review.development import sync_ledgers_before_capture

        sync_ledgers_before_capture(store, session, repo_root=repo_root, source_store_root=store_root)
        capture_kind: ReviewCaptureKind = "revision"
        if session.range_mode in MUTABLE_RANGE_MODES:
            capture_kind = "commit" if rng.mode == "commit_range" and bool(rng.head_sha) else "draft"
        branch_ref = branch_ref_override.strip()
        revision_worktree_key = worktree_key_override.strip()
        if not branch_ref and capture_kind != "revision":
            from lemoncrow.pro.capabilities.review.gitdiff import current_branch_ref

            branch_ref = current_branch_ref(repo_root)
        if capture_kind == "draft" and not revision_worktree_key:
            revision_worktree_key = worktree_key(repo_root)
        if capture_kind != "draft":
            revision_worktree_key = ""
        revision_id = new_revision_id()
        # Prepare the packet while the caller freezes the independent source
        # artifacts. Only pure encoding runs in the worker; artifact backends
        # and SQLite stay on this thread, and publication waits for both.
        # Every artifact written here is handed to add_revision, whose insert claims
        # them all against hosted orphan GC (a stalled capture commits nothing dangling).
        written: list[str] = []
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-packet") as pool:
            packet_payload = pool.submit(encode_packet, packet)
            written.append(store.write_blob_artifact(session.id, revision_id, blobs.new)[0])
            if blobs.binary_new:
                written.append(store.write_media_artifact(session.id, revision_id, blobs.binary_new)[0])
            if nested_snapshot_payload is not None:
                written.append(store.write_submodule_artifact(session.id, revision_id, nested_snapshot_payload)[0])
            rel_path, sha256, size = store.write_packet_artifact(session.id, revision_id, packet_payload.result())
        record = store.add_revision(
            ReviewRevision(
                id=revision_id,
                review_id=session.id,
                revision_number=0,
                range_mode=rng.mode,
                tree_fingerprint=fingerprint,
                packet_schema_version=packet.schema_version,
                base_sha=rng.base_sha,
                head_sha=rng.head_sha,
                merge_base_sha=rng.merge_base_sha,
                dirty=rng.dirty,
                packet_path=rel_path,
                packet_sha256=sha256,
                packet_bytes=size,
                degraded=tuple(sorted(set(packet.degraded) | set(blobs.degraded) | capture_degraded)),
                provenance_host=packet.provenance.host or "",
                provenance_model=packet.provenance.model or "",
                provenance_session_id=packet.provenance.session_id or "",
                provenance_certainty=packet.provenance.certainty,
                source_fingerprint=watch_fingerprint,
                source_view_id=source_view_id,
                source_view_revision=max(0, int(source_view_revision)),
                capture_kind=capture_kind,
                commit_sha=rng.head_sha if capture_kind == "commit" else "",
                branch_ref=branch_ref,
                worktree_key=revision_worktree_key,
            ),
            units,
            artifact_keys=tuple(written),
        )
        # `add_revision` may return a row this call did not create: another writer
        # committed the same exact Snapshot identity first and this one adopted it.
        # `created` describes the ROW, so it has to be read off what came back --
        # hard-coding True made an adopted revision claim it was fresh, and the
        # reconciliation below would then compare the rival's units against ours.
        created = record.id == revision_id
        stored_units = units if created else store.list_units(record.id)

    if progress is not None:
        progress("Reconciling review marks and comments")
    store.add_revision_attributions(record.id, _revision_attributions(packet, record.id))
    if (
        record.capture_kind == "commit"
        and record.commit_sha
        and store.find_commit(session.id, record.commit_sha) is None
    ):
        parent_sha = ""
        try:
            import pygit2

            repo = pygit2.Repository(str(repo_root.resolve()))
            commit = repo.revparse_single(record.commit_sha)
            parents = getattr(commit, "parents", ())
            if parents:
                parent_sha = str(parents[0].id)
        except Exception:
            parent_sha = ""
        checkpoint = store.record_commit(
            ReviewCommit(
                review_id=session.id,
                commit_number=0,
                commit_sha=record.commit_sha,
                revision_id=record.id,
                parent_sha=parent_sha,
            )
        )
        _write_review_commit_note(store, repo_root, session, checkpoint)

    prefetched_baseline, prefetched_texts = _prefetch_symbol_digests(
        store, session.id, reviewer_id, record.id, blobs.new, stored_units
    )
    with store.atomic_review_transition():
        current_session = store.get_session(session.id)
        current_revision_id = current_session.current_revision_id if current_session is not None else ""
        if current_revision_id not in {expected_current_revision_id, record.id}:
            raise ValueError(
                "Review advanced while this source generation was being reconciled; retry the current source"
            )
        if watch_fingerprint and not supplied_build and not source_fingerprint_override and rng.mode != "commit_range":
            try:
                if source_state(repo_root, rng).fingerprint != watch_fingerprint:
                    raise ValueError("source changed before Review publication; retry against the settled source")
            except (OSError, RuntimeError) as exc:
                raise ValueError("could not verify captured Review source before publication") from exc

        # Baseline and marks are read only after winning the transition lock, so
        # a human judgment or competing refresh committed while packet analysis
        # ran is included rather than overwritten by an old snapshot.
        marks = store.list_marks(session.id, reviewer_id=reviewer_id)
        baseline = baseline_revision(store, session.id, marks=marks, reviewer_id=reviewer_id)
        baseline_units = store.list_units(baseline.id) if baseline is not None else ()
        # saw; otherwise the comparison is against itself, which is exactly the
        # reading that reports "nothing ever changes". Either way ``reconcile``
        # decides on content fingerprints, so a rewrite reopens the mark whichever
        # side supplied the left hand.
        against = baseline_units if baseline is not None and record.id != baseline.id else stored_units
        base_only = _base_only_unit_keys(store, baseline, baseline_units, record, stored_units, marks)
        previous_revision_id = baseline.id if baseline is not None and record.id != baseline.id else record.id
        texts = (
            prefetched_texts
            if prefetched_baseline and prefetched_baseline == previous_revision_id
            else _symbol_body_digests(store, session.id, previous_revision_id, against, blobs.new, stored_units)
        )
        result = reconcile(
            against,
            stored_units,
            marks,
            renames=_renames(packet),
            base_only=base_only,
            body_digests=texts.digests,
            headers=texts.headers,
        )
        moves_by_new = {(move.reviewer_id, move.new_key): move for move in result.moves}
        followed_old = {(move.reviewer_id, move.old_key): move for move in result.moves}

        # Writes before deletes: a renamed unit's mark is inserted under its new key
        # and only then removed from the old one, so a crash between the two leaves
        # a duplicate verdict rather than no verdict at all.
        # File-rename aliases only: a symbol move names its source per reviewer (``moves_by_new``).
        previous_key_by_current = {
            new: old for old, new in result.aliases if (old, new) not in {(m.old_key, m.new_key) for m in result.moves}
        }
        for mark in result.rebased:
            store.set_mark(
                mark,
                event_kind="reconciled",
                event_revision_id=record.id,
                event_reason=_BASE_ONLY_CARRY_REASON,
            )
        # Trust T1: the audited reason names the rule that decided each mark.
        reason_codes = {(reviewer, key): code for reviewer, key, code in result.reasons}
        for mark in result.restamped:
            # Kept, not reopened and not a base-only carry: the part shows less than the verdict covered.
            store.set_mark(
                mark,
                event_kind="reconciled",
                event_revision_id=record.id,
                event_reason=mark_event_reason(reason_codes.get((mark.reviewer_id, mark.unit_key), "")),
            )
        for mark in result.reopened:
            move = moves_by_new.get((mark.reviewer_id, mark.unit_key))
            prior_key = previous_key_by_current.get(mark.unit_key, "") or (move.old_key if move is not None else "")
            code = reason_codes.get((mark.reviewer_id, mark.unit_key), "")
            store.set_mark(
                mark,
                event_kind="reconciled",
                event_revision_id=record.id,
                event_reason=(
                    mark_event_reason(code, symbol_move=(move.kind, move.previous_path, move.previous_symbol))
                    if code and move is not None
                    else (
                        mark_event_reason(code, renamed=bool(prior_key))
                        if code
                        else (
                            "unit identity migrated across rename and its judgment was reconciled"
                            if prior_key
                            else "revision reconciliation changed the judgment state"
                        )
                    )
                ),
                previous_unit_key=prior_key,
            )
        # The evidence before the deletion, for the same reason. A discarded verdict
        # is the one thing in this system that looking again cannot recover, so it
        # is written down before the row that proves it goes -- and the rename
        # migrations are filtered out first, because those verdicts were carried,
        # not thrown away.
        migrated = _rename_migrations(against, stored_units, _renames(packet))
        known = {unit.unit_key: unit for unit in against}
        dropped = result.dropped
        if checkpoint_history:
            # Only a unit the checkpoint still carries (under a rename alias) moves
            # its mark; one it merely lacks keeps the mark for the next draft.
            present = {unit.unit_key for unit in stored_units}
            alias = dict(result.aliases)

            def _lands_on(mark: ReviewMark) -> str:
                move = followed_old.get((mark.reviewer_id, mark.unit_key))
                return move.new_key if move is not None else alias.get(mark.unit_key, mark.unit_key)

            dropped = tuple(mark for mark in dropped if _lands_on(mark) in present)
        discarded: list[DiscardedMark] = []
        for mark in dropped:
            if mark.unit_key in migrated or (mark.reviewer_id, mark.unit_key) in followed_old:
                continue
            unit = known.get(mark.unit_key)
            discarded.append(
                store.record_discarded_mark(
                    DiscardedMark(
                        review_id=session.id,
                        unit_key=mark.unit_key,
                        discarded_revision_id=record.id,
                        state=mark.state,
                        content_fingerprint=mark.content_fingerprint,
                        reviewer_id=mark.reviewer_id,
                        reviewed_revision_id=mark.reviewed_revision_id,
                        kind=unit.kind if unit is not None else "file",
                        path=unit.path if unit is not None else "",
                        symbol=unit.symbol if unit is not None else "",
                        ordinal=unit.ordinal if unit is not None else 0,
                        start_line=unit.start_line if unit is not None else 0,
                        reason="the unit it attested to is not in this revision",
                    )
                )
            )
        for mark in dropped:
            moved = mark.unit_key in migrated or (mark.reviewer_id, mark.unit_key) in followed_old
            store.clear_mark(
                session.id,
                mark.reviewer_id,
                mark.unit_key,
                event_kind="migrated" if moved else "discarded",
                event_revision_id=record.id,
                event_reason=(
                    "old unit key cleared after rename migration"
                    if moved
                    else "the unit it attested to is not in this revision"
                ),
            )

        # Annotations whose stored geometry was *derived from this revision* -- not
        # merely ones that happen to own an event on it. Reconcile in a retry loop:
        # set_current_revision() takes SQLite's write lock and refuses the pointer
        # advance if a comment arrived after our last pass, so a current revision is
        # never published with an open comment whose geometry still describes older
        # bytes.
        def _reconcile_annotation(annotation: Annotation) -> None:
            if annotation.source != "human":
                _new_path, new_text, _status = _new_side(packet, blobs.new, annotation.anchor.path)
                same_blob = bool(
                    new_text is not None
                    and annotation.anchor.blob_sha
                    and blob_sha(new_text) == annotation.anchor.blob_sha
                )
                if not same_blob:
                    _persist_anchor(
                        store,
                        annotation,
                        record,
                        AnchorResolution(
                            status="stale",
                            method="stale",
                            anchor=annotation.anchor,
                            detail=(
                                f"{annotation.source} annotation is revision-bound; the underlying file content changed"
                            ),
                        ),
                    )
                    return
            _persist_anchor(store, annotation, record, reanchor_annotation(annotation, packet, blobs.new))

        for _attempt in range(16):
            for annotation in store.annotations_needing_anchor_at(session.id, record.id):
                _reconcile_annotation(annotation)
            try:
                store.set_current_revision(session.id, record.id)
                break
            except ValueError as exc:
                if "open annotation is not reconciled" not in str(exc):
                    raise
        else:
            raise RuntimeError(
                "Review source kept changing concurrently with human comments; refusing to publish an unreconciled revision"
            )

        # Read back rather than collected from the writes above: the block a
        # reviewer sees has to describe this revision, not this process. Whichever
        # command first recorded the revision did the relocating, and a
        # ``COMMENT ANCHORS`` list that only shows up for that one caller is a
        # report about who typed what first.
        moves = store.anchor_moves_on(record.id)

    actor = _actor_for(packet.provenance.host, session.actor_type)
    if actor != session.actor_type:
        store.update_session(session.id, actor_type=actor)

    # Author rationale is captured out-of-band by the authoring agent and bound
    # to exact file bytes. Import it only *after* this revision exists: capture
    # must never advance a human's review frontier underneath them. Rationale is
    # optional enrichment, so a malformed/old sidecar cannot make review fail.
    # The agent-written sidecars (rationale, evidence) and their readers are
    # Runtime's: a build without a reader skips that step.
    try:
        with skip_if_absent(_RATIONALE):
            from lemoncrow.pro.capabilities.review.rationale import import_author_rationales

            import_author_rationales(
                store,
                session,
                record,
                store_root=store_root,
                repo_root=repo_root,
                new_blobs=blobs.new,
            )
    except (OSError, ValueError, RuntimeError):
        pass

    # Author guidance (spec §6) follows the same trust rule: recorded
    # out-of-band, imported only after this revision exists, never a human state.
    try:
        from lemoncrow.pro.capabilities.review.guidance import import_author_guidance

        import_author_guidance(
            store,
            session,
            record,
            store_root=store_root,
            repo_root=repo_root,
            changed_paths={path for item in packet.files for path in (item.path, item.old_path or "") if path},
        )
    except (OSError, ValueError, RuntimeError):
        pass

    # Author-produced screenshots/traces/previews follow the same trust rule as
    # rationale: capture happens out-of-band, import happens only after this
    # exact ReviewRevision exists and its code identity matches the sidecar.
    try:
        with skip_if_absent(_EVIDENCE_CAPTURE):
            from lemoncrow.pro.capabilities.review.evidence_capture import import_agent_evidence

            import_agent_evidence(
                store,
                session,
                record,
                store_root=store_root,
                repo_root=repo_root,
            )
    except (OSError, ValueError, RuntimeError):
        pass

    # Prepare a small deterministic evidence layer before the human arrives.
    # These annotations explain *why LemonCrow ranked a file*, never whether the
    # change is correct.  They are content-bound and idempotent, so re-opening
    # the same revision does not manufacture a second queue of things to read.
    if project_annotations:
        try:
            from lemoncrow.pro.capabilities.review.preparation import project_lemoncrow_annotations

            project_lemoncrow_annotations(
                store,
                session,
                record,
                packet,
                new_blobs=blobs.new,
            )
        except (OSError, ValueError, RuntimeError):
            pass

    # Completed host replies are optional enrichment, imported without moving
    # the human frontier. Standalone planners/reviewers need no UI handoff.
    try:
        from lemoncrow.pro.capabilities.review.session_comments import ingestor_for

        ingestor_for(store, source_store_root or store_root).sync(session, force=True)
    except (OSError, ValueError, RuntimeError):
        pass

    if created:
        # Every capture path (CLI, live runtime, local server, hosted) funnels
        # through here, so a semantically new Snapshot can never leave an armed
        # Merge-when-ready intent looking current. Provider cancellation is
        # recorded as owed and completed where the landing delegate lives.
        from lemoncrow.pro.capabilities.review.landing import mark_landing_stale_for_revision

        mark_landing_stale_for_revision(store, session.id, record)

    return RevisionRecording(
        revision=record,
        created=created,
        baseline_revision=baseline,
        reconciliation=result,
        moves=moves,
        discarded=tuple(discarded),
    )


def carry_recorded_anchors(
    derived: Sequence[ReviewUnit], recorded: Sequence[ReviewUnit], old_blobs: Mapping[str, str]
) -> list[ReviewUnit]:
    """*derived*, with a unit that owns deleted text, or sits in an ordered file, keeping its recorded fingerprint.

    Such a unit is anchored in the exact base blob (its deleted text, or its place among the base lines of an ordered
    file). An enrichment does not carry the blob, and without it the
    fingerprint is the ``unanchored:`` one, which never equals the anchored one and would rewrite every owned-text
    part's identity.
    The recorded value is kept only for the same ``unit_key`` whose body digest (the bare part) is unchanged and
    whose path has no base blob at hand; anything else keeps what was derived.
    """

    recorded_by_key = {unit.unit_key: unit.content_fingerprint for unit in recorded}
    out: list[ReviewUnit] = []
    for unit in derived:
        stored = recorded_by_key.get(unit.unit_key)
        if (
            (owns_deleted_text(unit.content_fingerprint) or bool(split_anchor(unit.content_fingerprint)[1]))
            and unit.path not in old_blobs
            and stored is not None
            and bare_fingerprint(stored) == bare_fingerprint(unit.content_fingerprint)
        ):
            unit = replace(unit, content_fingerprint=stored)
        out.append(unit)
    return out


# Degraded signals only a capture can observe; an enrichment must carry them.
_CAPTURE_ONLY_DEGRADED = frozenset({"dependency_side_unavailable", "submodule_snapshot_unavailable"})


def enrich_revision_projection(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    build: PacketBuild,
) -> ReviewRevision:
    """Attach expensive evidence to an already-visible revision, identity-safe."""

    packet = build.packet
    # The expensive enrichment that produced ``build`` already happened outside
    # this transition. Only the final monotonic merge + packet/unit projection is
    # serialized, so two enrichers cannot both decide from the same stale packet
    # and let the slower one downgrade or flip exact attribution.
    with store.atomic_review_transition():
        stored_packet = read_packet_json(store, revision)
        if isinstance(stored_packet, dict):
            stored_provenance = stored_packet.get("provenance")
            incoming = packet.to_dict()

            # Exact scoped contributor evidence is append-only even when the
            # revision-primary author must remain unchanged. A later enrichment
            # may discover another session's edit-time claim without being
            # allowed to rewrite the established primary/journal identity.
            merged_attributions: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for raw in (*tuple(stored_packet.get("attributions") or ()), *tuple(incoming.get("attributions") or ())):
                if not isinstance(raw, Mapping):
                    continue
                scope = str(raw.get("scope") or "")
                scope_key = str(raw.get("scope_key") or "")
                host = str(raw.get("host") or "").strip().lower()
                session_id = str(raw.get("session_id") or "").strip()
                certainty = str(raw.get("certainty") or "")
                if scope != "file" or not scope_key or not host or not session_id or certainty != "exact":
                    continue
                merged_attributions.setdefault((scope, scope_key, host, session_id), dict(raw))
            incoming["attributions"] = [merged_attributions[key] for key in sorted(merged_attributions)]

            incoming_provenance = incoming.get("provenance")
            if isinstance(stored_provenance, Mapping) and isinstance(incoming_provenance, Mapping):
                rank = {"none": 0, "possible": 1, "probable": 2, "exact": 3}

                def strength(row: Mapping[str, Any]) -> tuple[int, float]:
                    certainty = str(row.get("certainty") or "none")
                    return rank.get(certainty, 0), float(row.get("match_confidence") or 0.0)

                stored_strength = strength(stored_provenance)
                incoming_strength = strength(incoming_provenance)
                stored_exact_session = (
                    str(stored_provenance.get("host") or ""),
                    str(stored_provenance.get("session_id") or ""),
                )
                incoming_exact_session = (
                    str(incoming_provenance.get("host") or ""),
                    str(incoming_provenance.get("session_id") or ""),
                )
                keep_stored = stored_strength > incoming_strength or (
                    stored_strength[0] == rank["exact"]
                    and incoming_strength[0] == rank["exact"]
                    and stored_exact_session != incoming_exact_session
                    and all(stored_exact_session)
                )
                if keep_stored:
                    # Provenance, observable journal, and provenance evidence are one
                    # attribution bundle. Never keep author A's exact identity while
                    # a racing enrichment substitutes author B's session context.
                    incoming["provenance"] = dict(stored_provenance)
                    incoming["journal"] = list(stored_packet.get("journal") or [])
                    incoming["evidence"] = list(stored_packet.get("evidence") or [])
                    packet = review_packet_from_dict(incoming)
                elif merged_attributions:
                    packet = review_packet_from_dict(incoming)

        # Dependency targets were derived once, at capture, from both exact
        # sides. Enrichment sees only the new side, so it carries them as
        # recorded instead of re-deriving (or dropping) them -- and it keeps the
        # capture-time gaps it has no way to recompute.
        from lemoncrow.pro.capabilities.review.units import stamp_answers

        stored_units = store.list_units(revision.id)
        derived_units = carry_recorded_anchors(
            derive_units(packet, build.blobs.new, old_blobs=build.blobs.old), stored_units, build.blobs.old
        )
        units = stamp_answers(
            (
                *derived_units,
                *(unit for unit in stored_units if unit.kind == "dependency"),
            ),
            packet,
            marked_generated=_marked_generated(store, session.repo_root, revision.base_sha, packet),
        )
        capture_degraded = set(revision.degraded) & _CAPTURE_ONLY_DEGRADED
        record = store.update_revision_projection(
            revision.id,
            packet_payload=encode_packet(packet),
            packet_schema_version=packet.schema_version,
            degraded=tuple(sorted(set(packet.degraded) | set(build.blobs.degraded) | capture_degraded)),
            provenance_host=packet.provenance.host or "",
            provenance_model=packet.provenance.model or "",
            provenance_session_id=packet.provenance.session_id or "",
            provenance_certainty=packet.provenance.certainty,
            units=units,
        )
        store.add_revision_attributions(record.id, _revision_attributions(packet, record.id))
    actor = _actor_for(packet.provenance.host, session.actor_type)
    if actor != session.actor_type:
        store.update_session(session.id, actor_type=actor)
    return record


def _write_review_commit_note(
    store: ReviewStore, repo_root: Path, session: ReviewSession, checkpoint: ReviewCommit
) -> None:
    """Best-effort Git backlink that never participates in Review correctness."""

    import subprocess

    try:
        review_ref = f"r/{store.short_session_reference(session.id)}"
        note = f"LemonCrow-Review: {review_ref}\nLemonCrow-Commit: {checkpoint.commit_number}"
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "notes",
                "--ref=refs/notes/lemoncrow",
                "add",
                "-f",
                "-m",
                note,
                checkpoint.commit_sha,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return


def commit_checkpoint_range(repo_root: Path, base_sha: str, head_sha: str) -> RevRange:
    """Return one immutable Review checkpoint range, including an empty-tree base."""

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range

    if base_sha == EMPTY_TREE_SHA:
        return RevRange(
            mode="commit_range",
            base_rev="(empty tree)",
            head_rev=head_sha,
            base_sha=EMPTY_TREE_SHA,
            head_sha=head_sha,
            merge_base_sha="",
            dirty=False,
            title=f"commit {head_sha[:12]}",
        )
    resolved = resolve_rev_range(repo_root, base=base_sha, head=head_sha)
    return RevRange(
        mode=resolved.mode,
        base_rev=resolved.base_rev,
        head_rev=resolved.head_rev,
        base_sha=resolved.base_sha,
        head_sha=resolved.head_sha,
        merge_base_sha=resolved.merge_base_sha,
        dirty=False,
        title=resolved.title,
        submodule_dirt_discounted=resolved.submodule_dirt_discounted,
    )


def checkpoint_range_for(repo_root: Path, session: ReviewSession, commit_sha: str) -> RevRange:
    """The immutable range a branch Review checkpoints *commit_sha* as."""

    from lemoncrow.pro.capabilities.review.gitdiff import review_branch_base

    base = session.review_base_sha
    if session.review_base_policy == "integrated":
        base = review_branch_base(repo_root, base, head=commit_sha) or base
    return commit_checkpoint_range(repo_root, base, commit_sha)


def sync_commit_checkpoints(
    store: ReviewStore,
    session: ReviewSession,
    repo_root: Path,
    *,
    store_root: Path,
    session_id: str | None = None,
    limit: int = 40,
    reviewer_id: str = "local",
    scope_paths: Sequence[str] = (),
    progress: Callable[[str], None] | None = None,
    completed_checkpoints: dict[str, tuple[RevRange, RevisionRecording]] | None = None,
    finalize_commit_sha: str = "",
    final_project_annotations: bool = True,
) -> tuple[ReviewCommit, ...]:
    """Capture the branch's own commits that this Review has not checkpointed yet.

    Checkpoints are cumulative ``branch base -> commit`` states. Commits pulled,
    merged or rebased in from an integration branch are not this Review's
    work and never become checkpoints. A checkpoint whose commit is no longer
    in ``HEAD`` (amend, rebase, reset) is kept as ``superseded``. Existing
    commit rows make this idempotent; the mutable draft capture stays current.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import commits_in_head, review_branch_commits

    if session.range_mode not in MUTABLE_RANGE_MODES or not session.review_base_sha:
        return ()
    try:
        existing = store.list_commits(session.id)
        present = commits_in_head(repo_root, [checkpoint.commit_sha for checkpoint in existing])
        for checkpoint in existing:
            state = "active" if checkpoint.commit_sha in present else "superseded"
            if checkpoint.state != state:
                store.set_commit_state(session.id, checkpoint.commit_sha, state)
        owned = review_branch_commits(
            repo_root,
            session.review_base_sha,
            limit=_MAX_CHECKPOINT_BACKFILL,
            advance=session.review_base_policy == "integrated",
        )
    except Exception:
        # Checkpoints are best-effort history (shallow clones, missing objects);
        # the draft capture that follows stays authoritative.
        return ()
    if not owned:
        return ()

    recorded: list[ReviewCommit] = []
    for index, (sha, base) in enumerate(owned, 1):
        found = store.find_commit(session.id, sha)
        if found is not None:
            recorded.append(found)
            continue
        if progress is not None:
            progress(f"Recording commit checkpoint {index}/{len(owned)}")
        rng = _with_scope(commit_checkpoint_range(repo_root, base, sha), scope_paths)
        checkpoint_build = build_review_packet_with_blobs(
            repo_root,
            rng,
            store_root=store_root,
            with_impact=False,
            with_provenance=False,
            session_id=session_id,
            limit=limit,
            with_patch_text=True,
            unbounded_patch_text=True,
        )
        if sha == finalize_commit_sha:
            # When a clean refresh will immediately expose this HEAD as its
            # final Review, persist complete provenance/impact *now*. A later
            # idempotent record_revision cannot enrich an existing packet.
            checkpoint_build = attach_provenance(store_root, repo_root, rng, checkpoint_build, session_id=session_id)
            previous = store.latest_revision(session.id)
            reuse = _incremental_impact_reuse(store, previous, checkpoint_build, rng) if previous is not None else None
            checkpoint_build = attach_impact(repo_root, rng, checkpoint_build, limit=limit, reuse=reuse)
        else:
            checkpoint_build = attach_changed_symbols(repo_root, checkpoint_build, limit=limit)
        checkpoint_label = f"Commit checkpoint {index}/{len(owned)}"

        def checkpoint_progress(detail: str, prefix: str = checkpoint_label) -> None:
            if progress is not None:
                progress(f"{prefix} · {detail}")

        recording = record_revision(
            store,
            session,
            repo_root,
            rng,
            store_root=store_root,
            session_id=session_id,
            limit=limit,
            build=checkpoint_build,
            reviewer_id=reviewer_id,
            project_annotations=final_project_annotations if sha == finalize_commit_sha else False,
            checkpoint_history=sha != finalize_commit_sha,
            progress=checkpoint_progress if progress is not None else None,
        )
        item = store.find_commit(session.id, sha)
        if item is not None:
            recorded.append(item)
            if (
                completed_checkpoints is not None
                and sha == finalize_commit_sha
                and index == len(owned)
                and recording.created
                and recording.revision.id == item.revision_id
            ):
                # Fully reconciled the clean HEAD with authoritative evidence.
                # Return this exact recording to the caller; repeating it adds
                # no evidence and would repeat all of the database reads.
                completed_checkpoints[sha] = (rng, recording)
    return tuple(recorded)


def refresh(
    store: ReviewStore,
    session: ReviewSession,
    repo_root: Path,
    *,
    store_root: Path,
    rng: RevRange | None = None,
    session_id: str | None = None,
    limit: int = 40,
    reviewer_id: str = "local",
    build: PacketBuild | None = None,
    project_annotations: bool = True,
) -> RefreshResult:
    """Record the current state and project the reviewer's marks onto it.

    :func:`record_revision` does the recording and the reconciling -- they are
    one operation and this function is not where they meet. What is added here
    is the *frontier*: the derived, never-stored answer to "what is left for me
    to look at", which every surface reads and none may disagree about.
    """

    # A scoped Review stays scoped whoever refreshes it.
    scope = tuple(rng.paths) if rng is not None and rng.paths else session_scope_paths(store, session)
    if rng is None:
        rng = range_for_session(repo_root, session, scope_paths=scope)

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range

    completed_checkpoints: dict[str, tuple[RevRange, RevisionRecording]] = {}
    recording: RevisionRecording | None = None
    if session.range_mode in MUTABLE_RANGE_MODES and session.review_base_sha:
        sync_commit_checkpoints(
            store,
            session,
            repo_root,
            store_root=store_root,
            session_id=session_id,
            limit=limit,
            reviewer_id=reviewer_id,
            scope_paths=scope,
            completed_checkpoints=completed_checkpoints,
            final_project_annotations=project_annotations,
            # Mutable working-tree ranges have head_sha="" (WORKDIR); use
            # the real HEAD commit as the only checkpoint eligible for
            # complete, immutable-commit enrichment.
            finalize_commit_sha=(
                resolve_rev_range(repo_root, working_tree=True).base_sha
                if rng.mode == "working_tree" and not rng.dirty
                else ""
            ),
        )
        rng = range_for_session(repo_root, session, scope_paths=scope)
        if rng.mode in MUTABLE_RANGE_MODES and not rng.dirty:
            current_head = resolve_rev_range(repo_root, working_tree=True).base_sha
            checkpoint = store.find_commit(session.id, current_head) if current_head else None
            if checkpoint is not None:
                rng = _with_scope(checkpoint_range_for(repo_root, session, current_head), scope)
                # A supplied mutable-side packet does not describe this immutable
                # commit range. A *newly recorded* commit checkpoint does: it
                # already froze this same commit-range's exact diff and blobs.
                # Reuse only if its range (base/head/scope) matches exactly.
                # Existing checkpoints or a raced/advanced branch fall back to
                # the ordinary fresh packet, never a stale cached snapshot.
                build = None
                completed = completed_checkpoints.get(current_head)
                if completed is not None and completed[0] == rng:
                    # Commit-range Snapshot and mark/annotation reconciliation
                    # were completed once above. Never reconstruct a second
                    # record for a just-persisted identical commit.
                    recording = completed[1]

    if recording is None:
        recording = record_revision(
            store,
            session,
            repo_root,
            rng,
            store_root=store_root,
            session_id=session_id,
            limit=limit,
            build=build,
            reviewer_id=reviewer_id,
            project_annotations=project_annotations,
        )
    revision = recording.revision

    frontier = compute_frontier(
        session.id,
        reviewer_id,
        revision,
        store.list_units(revision.id),
        store.list_marks(session.id, reviewer_id=reviewer_id),
        annotations=store.list_annotations(session.id),
    )
    return RefreshResult(
        session=session,
        revision=revision,
        created=recording.created,
        reconciliation=recording.reconciliation,
        frontier=frontier,
        previous_revision=recording.baseline_revision,
        moves=recording.moves,
        discarded=recording.discarded,
    )


__all__ = [
    "LATER_UNAVAILABLE",
    "AnchorMove",
    "LaterMarkUnavailable",
    "RefreshResult",
    "ReviewClosure",
    "RevisionRecording",
    "annotate",
    "annotate_machine_finding",
    "annotation_counts",
    "baseline_revision",
    "coerce_anchor_side",
    "coerce_annotation_kind",
    "coerce_mark_state",
    "effective_mark_state",
    "encode_packet",
    "enrich_revision_projection",
    "mark_downgrade_note",
    "mark_unit",
    "new_side_text",
    "open_or_create_session",
    "owning_symbol_unit",
    "parse_line_target",
    "range_for_session",
    "read_packet_json",
    "reanchor_annotation",
    "record_revision",
    "refresh",
    "resolve_mark_units",
    "resolve_unit",
    "revision_new_side_text",
    "session_scope_paths",
    "set_review_status",
    "snapshot_revision",
    "source_ref",
    "unseen_units",
]
