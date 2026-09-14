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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.anchors import AnchorResolution, blob_sha, build_anchor, resolve_anchor
from lemoncrow.pro.capabilities.review.gitdiff import RevRange, source_state
from lemoncrow.pro.capabilities.review.models import ChangedFile, ImpactSite, ReviewPacket
from lemoncrow.pro.capabilities.review.packet import PacketBuild, attach_impact, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.revisions import Reconciliation, compute_frontier, reconcile
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
    ReviewFrontier,
    ReviewMark,
    ReviewRevision,
    ReviewSession,
    ReviewSessionStatus,
    ReviewSubjectType,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id
from lemoncrow.pro.capabilities.review.units import (
    derive_units,
    file_fingerprint,
    tree_fingerprint,
    unit_key,
    unknown_file_fingerprint,
)

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
}


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

    raw = store.read_packet_artifact(revision)
    if raw is None:
        return None
    try:
        decoded = json.loads(gzip.decompress(raw).decode("utf-8"))
    except (OSError, EOFError, UnicodeDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


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


def _canonical_named_ref(repo_root: Path, spec: str) -> str:
    """Return a canonical Git ref name when *spec* is genuinely a named ref.

    Relative expressions and raw SHAs intentionally return ``""``. ``HEAD`` is
    also excluded even though libgit2 can resolve it to the checked-out branch:
    a review keyed to the word HEAD would jump branches after checkout.
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


def _named_range_source_ref(repo_root: Path, rng: RevRange) -> str:
    if rng.mode != "commit_range":
        return ""
    base = _canonical_named_ref(repo_root, rng.base_rev)
    head = _canonical_named_ref(repo_root, rng.head_rev)
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
    as the branch moves.
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
    substantive_marks = sum(1 for item in marks if item.state != "unreviewed")
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


def open_or_create_session(
    store: ReviewStore,
    repo_root: Path,
    rng: RevRange,
    *,
    title: str = "",
    restore_archived: bool = False,
) -> ReviewSession:
    """Reopen this change's session, refusing a discarded review by default.

    ``archived`` is the store's disposable state. Merely opening the same range
    must not silently turn that tombstone back into mutable review state: the
    retention worker may be deleting it at the same time. An explicit restore
    changes the status to ``open`` first, which also refreshes ``updated_at``
    and therefore removes the session from the retention cutoff before any
    revision, mark, comment or evidence can be written to it.
    """

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

    resolved_root = str(repo_root.resolve())
    subject_type = _SUBJECT_BY_MODE.get(rng.mode, "local_change")
    ref = source_ref(rng, repo_root=repo_root)
    existing = store.find_session(
        repo_root=resolved_root,
        subject_type=subject_type,
        source_ref=ref,
        range_mode=rng.mode,
    )
    if existing is not None:
        return _usable(existing)
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
        return _usable(adopted)
    return store.create_session(
        ReviewSession(
            id="",
            subject_type=subject_type,
            repo_root=resolved_root,
            range_mode=rng.mode,
            title=title or rng.title,
            source_ref=ref,
        )
    )


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

    effective = effective_mark_state(unit, state)
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
        )
    )
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
        if source_state(repo_root, rng).fingerprint != revision.source_fingerprint:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return new_side_text(repo_root, session, path)


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
    title: str = "",
    evidence: Sequence[str] = (),
    confidence: float | None = None,
    new_text: str | None = None,
    repo_root: Path | None = None,
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
        raise ValueError(f"{path!r} is not a file in revision {revision.revision_number} of this review")

    if parent_id:
        parent = store.get_annotation(parent_id)
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
        detail = f"file-level comment on revision {revision.revision_number}"
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
        detail = f"anchored on revision {revision.revision_number}"

    record = store.add_annotation(
        Annotation(
            id="",
            review_id=session.id,
            revision_id=revision.id,
            anchor=anchor,
            body=body,
            kind=resolved_kind,
            parent_id=parent_id,
            created_by=created_by,
            created_by_actor=created_by_actor,
            source=resolved_source,
            source_id=source_id,
            title=title,
            evidence=tuple(str(item) for item in evidence if str(item)),
            confidence=confidence,
            anchor_method=method,
            anchor_detail=detail,
        )
    )
    # ``add_annotation`` writes the first rung of the audit trail in the same
    # transaction as the row, so there is nothing to append here.
    return record


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


def set_review_status(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision | None,
    *,
    status: ReviewSessionStatus = "finished",
    reviewer_id: str = "local",
) -> ReviewClosure:
    """Persist a human status choice and return the reader's exact target tally.

    This never issues a verdict and never blocks finishing because work remains.
    It records the human's choice and reports an explainable partition of the
    current review target denominator beside that choice.
    """

    if session.status != status:
        store.update_session(session.id, status=status)
    units = store.list_units(revision.id) if revision is not None else ()
    marks = store.list_marks(session.id, reviewer_id=reviewer_id)
    annotations = store.list_annotations(session.id)

    target_count = reviewed = unreviewed = changed = needs = unknown = 0
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
        changed = progress.changed_since_review
        needs = progress.needs_changes
        unknown = progress.unknown

    return ReviewClosure(
        review_id=session.id,
        status=status,
        target_count=target_count,
        reviewed_targets=reviewed,
        unreviewed_targets=unreviewed,
        changed_since_review=changed,
        needs_changes=needs,
        unknown_targets=unknown,
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


def range_for_session(repo_root: Path, session: ReviewSession) -> RevRange:
    """Rebuild the range a stored session reviews.

    Working-tree and staged sessions follow their mutable local source. Legacy
    commit-range sessions store resolved SHAs and therefore reopen the same
    snapshot forever. Named-range sessions intentionally store canonical Git
    refs so a branch review follows branch-head commits inside one ReviewSession.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range

    if session.range_mode == "staged":
        return resolve_rev_range(repo_root, staged=True)
    if session.range_mode == "working_tree":
        return resolve_rev_range(repo_root, working_tree=True)
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
    elif resolution.located and annotation.state == "orphaned":
        # An orphan that has been found again is an open comment once more.
        # Leaving it orphaned would bury a live objection in the list of things
        # the reviewer was told to ignore.
        fields["state"] = "open"
    store.update_annotation(annotation.id, **fields)
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
        seen = {mark.reviewed_revision_id for mark in recorded if mark.reviewed_revision_id}
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

    previous_units = {unit.path: unit for unit in store.list_units(previous.id) if unit.kind == "file"}
    reusable: set[str] = set()
    for item in current.packet.files:
        old_row = previous_files.get(item.path)
        old_unit = previous_units.get(item.path)
        if old_row is None or old_unit is None or item.is_binary:
            continue
        if str(old_row.get("status") or "") != item.status:
            continue
        old_old_path = old_row.get("old_path")
        if (str(old_old_path) if old_old_path is not None else None) != item.old_path:
            continue
        if item.status == "deleted":
            current_fingerprint = unknown_file_fingerprint(item.path, item.status)
        else:
            text = current.blobs.new.get(item.path)
            if text is None or old_unit.fingerprint_method != "blob_sha256":
                continue
            current_fingerprint = file_fingerprint(item.path, text)
        if current_fingerprint == old_unit.content_fingerprint:
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


def record_revision(
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
) -> RevisionRecording:
    """Persist the current state of *rng* **and** reconcile it. One operation.

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

    if build is None:
        previous_for_reuse = store.latest_revision(session.id)
        if previous_for_reuse is None:
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
            reuse = _incremental_impact_reuse(store, previous_for_reuse, base_build, rng)
            build = attach_impact(repo_root, rng, base_build, limit=limit, reuse=reuse)
    packet = build.packet
    blobs = build.blobs
    units = derive_units(packet, blobs.new)
    fingerprint = tree_fingerprint(units)
    try:
        watch_fingerprint = source_state(repo_root, rng).fingerprint
    except (OSError, RuntimeError, ValueError):
        # Change detection is advisory. A source we cannot fingerprint still
        # gets a fully reviewable revision; the workspace simply cannot offer
        # automatic refresh for that row.
        watch_fingerprint = ""
    # A file unit can only use a placeholder fingerprint for deleted, binary,
    # oversized or unreadable content. In that case the file-only tree digest is
    # not a content identity and cannot satisfy the review store's UNIQUE tree
    # constraint safely. Fold in the independent exact source identity when we
    # have it; parser/symbol projections remain excluded, while two different
    # large/binary source states can no longer collapse onto one revision row.
    has_unknown_file = any(unit.kind == "file" and unit.fingerprint_method == "unknown" for unit in units)
    if has_unknown_file and watch_fingerprint:
        payload = f"{fingerprint}\0source\0{watch_fingerprint}".encode("utf-8")
        fingerprint = hashlib.sha256(payload).hexdigest()

    # Read before write: after ``add_revision`` these are no longer "previous".
    marks = store.list_marks(session.id, reviewer_id=reviewer_id)
    baseline = baseline_revision(store, session.id, marks=marks, reviewer_id=reviewer_id)
    baseline_units = store.list_units(baseline.id) if baseline is not None else ()
    existing = store.find_revision_by_tree(session.id, fingerprint)
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
        # Reopening an older revision is also our chance to backfill the exact
        # text snapshot introduced after that revision was first recorded. Exact
        # tree or source identity proves these are the same reviewed bytes.
        if store.read_blob_artifact(session.id, record.id) is None:
            store.write_blob_artifact(session.id, record.id, blobs.new)
        created = False
        stored_units = store.list_units(record.id)
    else:
        revision_id = new_revision_id()
        rel_path, sha256, size = store.write_packet_artifact(session.id, revision_id, encode_packet(packet))
        store.write_blob_artifact(session.id, revision_id, blobs.new)
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
                degraded=tuple(sorted(set(packet.degraded) | set(blobs.degraded))),
                provenance_host=packet.provenance.host or "",
                provenance_model=packet.provenance.model or "",
                provenance_session_id=packet.provenance.session_id or "",
                provenance_certainty=packet.provenance.certainty,
                source_fingerprint=watch_fingerprint,
            ),
            units,
        )
        # `add_revision` may return a row this call did not create: another writer
        # committed the same tree first and this one adopted it (the tree race).
        # `created` describes the ROW, so it has to be read off what came back --
        # hard-coding True made an adopted revision claim it was fresh, and the
        # reconciliation below would then compare the rival's units against ours.
        created = record.id == revision_id
        stored_units = units if created else store.list_units(record.id)

        actor = _actor_for(packet.provenance.host, session.actor_type)
        if actor != session.actor_type:
            store.update_session(session.id, actor_type=actor)
    # saw; otherwise the comparison is against itself, which is exactly the
    # reading that reports "nothing ever changes". Either way ``reconcile``
    # decides on content fingerprints, so a rewrite reopens the mark whichever
    # side supplied the left hand.
    against = baseline_units if baseline is not None and record.id != baseline.id else stored_units
    result = reconcile(against, stored_units, marks, renames=_renames(packet))

    # Writes before deletes: a renamed unit's mark is inserted under its new key
    # and only then removed from the old one, so a crash between the two leaves
    # a duplicate verdict rather than no verdict at all.
    for mark in result.reopened:
        store.set_mark(mark)
    # The evidence before the deletion, for the same reason. A discarded verdict
    # is the one thing in this system that looking again cannot recover, so it
    # is written down before the row that proves it goes -- and the rename
    # migrations are filtered out first, because those verdicts were carried,
    # not thrown away.
    migrated = _rename_migrations(against, stored_units, _renames(packet))
    known = {unit.unit_key: unit for unit in against}
    discarded: list[DiscardedMark] = []
    for mark in result.dropped:
        if mark.unit_key in migrated:
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
    for mark in result.dropped:
        store.clear_mark(session.id, mark.reviewer_id, mark.unit_key)

    # Author rationale is captured out-of-band by the authoring agent and bound
    # to exact file bytes. Import it only *after* this revision exists: capture
    # must never advance a human's review frontier underneath them. Rationale is
    # optional enrichment, so a malformed/old sidecar cannot make review fail.
    try:
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

    # Author-produced screenshots/traces/previews follow the same trust rule as
    # rationale: capture happens out-of-band, import happens only after this
    # exact ReviewRevision exists and its code identity matches the sidecar.
    try:
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

    # Annotations whose stored geometry was *derived from this revision* -- not
    # merely ones that happen to own an event on it. The two differ exactly
    # where it matters: after an undo the tree fingerprints back to an older
    # revision the comment already has an origin event on, while the row itself
    # still carries the geometry of the revision in between. Re-anchoring is
    # pure and cheap; it is the append-only audit row that must not be
    # duplicated, and that is what this set actually prevents.
    anchored_here = store.annotations_anchored_at(record.id)
    for annotation in store.list_annotations(session.id):
        if annotation.state not in ("open", "orphaned") or annotation.id in anchored_here:
            continue
        if annotation.source != "human":
            _new_path, new_text, _status = _new_side(packet, blobs.new, annotation.anchor.path)
            same_blob = bool(
                new_text is not None and annotation.anchor.blob_sha and blob_sha(new_text) == annotation.anchor.blob_sha
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
                            f"{annotation.source} annotation is revision-bound; " "the underlying file content changed"
                        ),
                    ),
                )
                continue
            # Exact whole-blob identity is strong enough to carry non-human
            # context through a rename/path move; no heuristic text relocation.
            _persist_anchor(store, annotation, record, reanchor_annotation(annotation, packet, blobs.new))
            continue
        _persist_anchor(store, annotation, record, reanchor_annotation(annotation, packet, blobs.new))

    # Read back rather than collected from the writes above: the block a
    # reviewer sees has to describe this revision, not this process. Whichever
    # command first recorded the revision did the relocating, and a
    # ``COMMENT ANCHORS`` list that only shows up for that one caller is a
    # report about who typed what first.
    moves = store.anchor_moves_on(record.id)

    return RevisionRecording(
        revision=record,
        created=created,
        baseline_revision=baseline,
        reconciliation=result,
        moves=moves,
        discarded=tuple(discarded),
    )


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
) -> RefreshResult:
    """Record the current state and project the reviewer's marks onto it.

    :func:`record_revision` does the recording and the reconciling -- they are
    one operation and this function is not where they meet. What is added here
    is the *frontier*: the derived, never-stored answer to "what is left for me
    to look at", which every surface reads and none may disagree about.
    """

    if rng is None:
        rng = range_for_session(repo_root, session)

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
    "AnchorMove",
    "RefreshResult",
    "ReviewClosure",
    "RevisionRecording",
    "annotate",
    "annotation_counts",
    "baseline_revision",
    "coerce_anchor_side",
    "coerce_annotation_kind",
    "coerce_mark_state",
    "effective_mark_state",
    "encode_packet",
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
    "set_review_status",
    "snapshot_revision",
    "source_ref",
    "unseen_units",
]
