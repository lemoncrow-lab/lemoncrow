"""Durable review state -- pure data, zero I/O.

``models.py`` describes one *packet*: what a single ``lc review`` invocation
computed, thrown away when the process exits. This module describes the thing
that outlives every invocation -- the review itself: which change a human is
working through, which revisions of it they have seen, which units they marked
reviewed and against which content, and where their comments are anchored.

There is exactly one authoritative review state and it is this one. Renderers
(a browser pane, a terminal diff viewer, an agent surface) are projections of
it; none of them is a source of truth to sync *from*.

Discipline, mirrored verbatim from ``models.py``:

* ``@dataclass(frozen=True)`` throughout -- a mark that can be mutated in place
  is a mark whose fingerprint no longer describes what was reviewed.
* Every collection field defaults to ``()`` and every unknown scalar to a
  sentinel, so a partially-filled record is valid rather than a crash.
* Fields are **appended, never inserted or renamed**, and the SQL columns behind
  them are added with a default. See ``SESSION_SCHEMA_VERSION``.
* ``Literal`` + a module-level ``_VALUES`` tuple instead of ``enum.Enum``: the
  release wheel compiles this package with mypyc, which ``enum`` does not
  survive (``tests/test_mypyc_compile_safety.py`` enforces it).

Sync-friendliness is a design constraint, not an aspiration. A hosted team
service must be able to replicate these rows without a rewrite, so: ids are
client-generated and globally unique (UUIDv7 text behind a 3-letter prefix,
sortable by creation time), timestamps are aware-UTC ISO-8601 text, and **no
primary key contains a local filesystem path**. ``repo_root`` is an ordinary
attribute of a session, never part of its identity.

No I/O lives here, and nothing in this module imports ``store.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lemoncrow.pro.capabilities.review.models import (
    FileCategory,
    ProvenanceCertainty,
    RangeMode,
)

# Bump only on a field removal or type change. Additive changes keep version 1;
# readers must ignore unknown keys and writers must never reorder a column.
# ``store.ReviewStore`` persists this number in ``review_meta`` and refuses to
# open a database written by a build with a higher one.
SESSION_SCHEMA_VERSION = 1

ReviewSubjectType = Literal["local_change", "commit_range", "pull_request", "document"]
SUBJECT_TYPES: tuple[str, ...] = ("local_change", "commit_range", "pull_request", "document")

ActorType = Literal["human", "agent", "mixed", "unknown"]
ACTOR_TYPES: tuple[str, ...] = ("human", "agent", "mixed", "unknown")

ReviewSessionStatus = Literal["open", "finished", "archived"]
SESSION_STATUSES: tuple[str, ...] = ("open", "finished", "archived")

ReviewUnitKind = Literal["file", "symbol", "hunk", "document_section"]
UNIT_KINDS: tuple[str, ...] = ("file", "symbol", "hunk", "document_section")

MarkState = Literal["unreviewed", "reviewed", "needs_changes", "changed_since_review", "unknown"]
"""``unknown`` is a first-class state, not a gap.

It is what a unit whose fingerprint could not be computed *is*, and it must
never render, sort or count as ``reviewed``. A surface that quietly promotes
an uncomputable unit to reviewed tells the reader a human looked at something
nobody looked at.
"""
MARK_STATES: tuple[str, ...] = (
    "unreviewed",
    "reviewed",
    "needs_changes",
    "changed_since_review",
    "unknown",
)

AnnotationKind = Literal["comment", "request_change", "suggestion", "looks_good"]
ANNOTATION_KINDS: tuple[str, ...] = ("comment", "request_change", "suggestion", "looks_good")

AnnotationSource = Literal["human", "author", "lemoncrow", "ai_review"]
ANNOTATION_SOURCES: tuple[str, ...] = ("human", "author", "lemoncrow", "ai_review")

AnnotationState = Literal["open", "resolved", "orphaned", "obsolete"]
ANNOTATION_STATES: tuple[str, ...] = ("open", "resolved", "orphaned", "obsolete")
AuthorResponseState = Literal["none", "addressed"]
AUTHOR_RESPONSE_STATES: tuple[str, ...] = ("none", "addressed")

ReviewEvidenceKind = Literal["screenshot", "image", "video", "playwright_trace", "document", "live_preview"]
REVIEW_EVIDENCE_KINDS: tuple[str, ...] = (
    "screenshot",
    "image",
    "video",
    "playwright_trace",
    "document",
    "live_preview",
)
ReviewEvidenceSource = Literal["human", "agent", "test", "ci", "external"]
REVIEW_EVIDENCE_SOURCES: tuple[str, ...] = ("human", "agent", "test", "ci", "external")

AnchorSide = Literal["old", "new", "document"]
ANCHOR_SIDES: tuple[str, ...] = ("old", "new", "document")

AnchorMethod = Literal[
    "file",
    "identical_blob",
    "exact_text_context",
    "symbol_text",
    "unique_text",
    "unique_context",
    "orphaned",
    "removed",
    "unresolved",
    "stale",
]
"""How an annotation's position was established on the current revision.

The first five are the ordered rungs of the relocation ladder, strongest
first; the last three are terminal states. ``orphaned`` and ``unresolved`` are
reported, never hidden -- an annotation silently moved to a plausible-looking
line is worse than one that admits it lost its footing.
"""
ANCHOR_METHODS: tuple[str, ...] = (
    "file",
    "identical_blob",
    "exact_text_context",
    "symbol_text",
    "unique_text",
    "unique_context",
    "orphaned",
    "removed",
    "unresolved",
    "stale",
)

ANCHOR_METHOD_LABELS: dict[str, str] = {
    "file": "file-level comment",
    "identical_blob": "file unchanged since the comment",
    "exact_text_context": "re-found by its text and both neighbours",
    "symbol_text": "re-found inside its definition",
    "unique_text": "re-found by its text alone",
    "unique_context": "text gone; placed by surrounding context",
    "orphaned": "not relocated",
    "removed": "file removed from this revision",
    "unresolved": "could not be checked",
    "stale": "underlying content changed",
}
"""Each rung in the words a reviewer reads, never the identifier.

Every surface that shows a surviving comment shows one of these, because a
comment re-found by rung 4 and a comment whose file never changed are two very
different claims and drawing them alike is how a heuristic passes for a fact.
Total over :data:`ANCHOR_METHODS`, pinned by a test, so an unlabelled method is
impossible rather than rendered as a raw code.

``unresolved`` reads "could not be checked" and deliberately not "not looked
at". The rung answers ``unresolved`` in two situations: the new side could not
be read at all, *and* the anchor carried nothing to search with -- no selected
text, no surviving context -- which happens on files that did change. "Not
looked at" was true of only the first, and told the reader of the second that
their changed file had been skipped.
"""

EXACT_ANCHOR_METHODS: tuple[str, ...] = ("identical_blob",)
"""The only rung whose stored line numbers were never re-derived.

Rung 1 compares whole-blob hashes: nothing in the file moved, so the line the
reviewer chose is still literally that line. Every other rung *searched* for the
text again and is a re-find, however confident -- which is why an anchor status
of ``unchanged`` belongs to this rung alone.
"""

HEURISTIC_ANCHOR_METHODS: tuple[str, ...] = (
    "exact_text_context",
    "symbol_text",
    "unique_text",
    "unique_context",
)
"""Rungs 2-5: the comment survived, but only because we looked for it again."""

LOST_ANCHOR_METHODS: tuple[str, ...] = ("orphaned", "removed", "unresolved", "stale")
"""Rungs that located nothing on this revision.

A comment on one of these still carries its last known path, line range and
symbol name -- for display, and so a reader can go and look at where it was
written. None of that is a claim that any of it still exists.
"""


def anchor_symbol_claim(method: str, symbol: str) -> tuple[str, str]:
    """Split a stored symbol name into ``(current, origin)``.

    At most one is ever non-empty, and that is the whole point. An annotation's
    anchor keeps the symbol name whether or not the relocation ladder found
    anything, so a surface that renders ``anchor.symbol_qualified_name``
    unconditionally prints *not relocated* and *in `gone_forever`* side by side
    -- one sentence saying the comment lost its footing and the next naming the
    definition it supposedly sits in. ``_orphan``'s contract is that it claims
    nothing; this is how the surfaces keep it.

    *current* is where the comment is **now**, and is only ever set for a rung
    that actually located it. *origin* is where it was **written**, to be
    rendered as "originally in ..." and never as "in ...".
    """

    if not symbol:
        return "", ""
    if method in LOST_ANCHOR_METHODS:
        return "", symbol
    return symbol, ""


FingerprintMethod = Literal["blob_sha256", "symbol_body_sha256", "hunk_patch_sha256", "unknown"]
FINGERPRINT_METHODS: tuple[str, ...] = (
    "blob_sha256",
    "symbol_body_sha256",
    "hunk_patch_sha256",
    "unknown",
)

FeedbackTargetType = Literal["agent_session", "github_review", "clipboard", "markdown_bundle"]
FEEDBACK_TARGET_TYPES: tuple[str, ...] = (
    "agent_session",
    "github_review",
    "clipboard",
    "markdown_bundle",
)

# Verbatim anchor text is capped so a pathological selection cannot turn one
# comment into a megabyte row. Rungs 2, 4 and 5 *search* for this text, which
# is why it is stored alongside its hash rather than replaced by it.
_ANCHOR_TEXT_CAP = 4096
_ANCHOR_CONTEXT_LINES = 3


@dataclass(frozen=True)
class ReviewSession:
    """One change a reviewer is working through, across every revision of it.

    Identity for *reopening* is ``(repo_root, subject_type, source_ref,
    range_mode)`` -- enforced by a unique index in ``store.py``, not by this
    dataclass. A second ``lc review --open`` in the same repository reopens
    this session; it never starts a rival one, because a review whose marks
    reset on every invocation is not review state, it is a cache.
    """

    id: str
    """``rev-<uuid7>``. Client-generated, globally unique, sortable by creation."""
    subject_type: ReviewSubjectType
    repo_root: str
    """Absolute and ``resolve()``d. The only filesystem root a review service
    may ever read. An attribute of the session, deliberately not part of its
    primary key -- the same review must survive being replicated to a host
    where that path does not exist."""
    range_mode: RangeMode
    title: str = ""
    source_ref: str = ""
    """Reopen key: empty for mutable local reviews; exact SHAs or canonical named refs for commit ranges."""
    actor_type: ActorType = "unknown"
    """Rolled up from the revisions' provenance. ``mixed`` is a correct answer,
    not an unresolved one."""
    status: ReviewSessionStatus = "open"
    reviewer_id: str = "local"
    created_at: str = ""
    """Aware-UTC ISO-8601. Blank means "not yet stamped"; the store fills it."""
    updated_at: str = ""


@dataclass(frozen=True)
class ReviewRevision:
    """One captured state of the change under review.

    ``tree_fingerprint`` is both the content identity and the idempotency key:
    re-running ``lc review`` on an untouched tree must find this revision, not
    manufacture a second one that would strand every mark made against the
    first.
    """

    id: str
    """``rrv-<uuid7>``."""
    review_id: str
    revision_number: int
    """1-based and dense within a review."""
    range_mode: RangeMode
    tree_fingerprint: str
    packet_schema_version: int
    """``models.SCHEMA_VERSION`` as it was at capture time, so a packet
    artifact is always read back with the contract it was written under."""
    base_sha: str = ""
    head_sha: str = ""
    """``""`` in ``working_tree`` and ``staged`` mode. Empty is the normal
    case there, never an error."""
    merge_base_sha: str = ""
    dirty: bool = False
    packet_path: str = ""
    """Relative to the store root, never absolute -- an absolute path in a row
    is a row that cannot be moved, backed up, or synced."""
    packet_sha256: str = ""
    packet_bytes: int = 0
    degraded: tuple[str, ...] = ()
    """Every reason this revision knows less than a perfect one would.

    Persisted rather than recomputed: a packet rebuilt later that drops these
    silently claims better recall than the original capture actually had."""
    provenance_host: str = ""
    provenance_model: str = ""
    provenance_session_id: str = ""
    provenance_certainty: ProvenanceCertainty = "none"
    created_at: str = ""
    source_fingerprint: str = ""
    """Cheap identity used only for read-only new-revision detection.

    Empty on reviews captured before R14. It never decides review state; a
    mismatch merely offers the human a refresh, whose full reconciliation is
    still authoritative.
    """


@dataclass(frozen=True)
class ReviewUnit:
    """One reviewable thing inside a revision: a file, a symbol, or a hunk.

    ``unit_key`` is stable across revisions and **position-independent**.
    Inserting ten lines above a function must not change the identity of that
    function, or every mark below an import block would evaporate on the next
    refresh. ``start_line``/``end_line`` are presentation only and are never
    identity components.
    """

    revision_id: str
    unit_key: str
    kind: ReviewUnitKind
    path: str
    """Repo-relative new-side path (old path when the file was deleted), the
    same convention as ``ChangedFile.path``."""
    content_fingerprint: str
    """What the reviewer actually saw. ``""`` is illegal -- a unit whose
    fingerprint could not be computed carries a real placeholder value and
    ``fingerprint_method="unknown"``, so it can never be mistaken for a match."""
    fingerprint_method: FingerprintMethod = "unknown"
    symbol: str = ""
    ordinal: int = 0
    """Disambiguates same-named siblings and hunks within one file."""
    start_line: int = 0
    end_line: int = 0
    attention_rank: int = 0
    """``ReviewOrderEntry.rank``; 0 means unranked."""
    attention_group: FileCategory = "production"
    reasons: tuple[str, ...] = ()
    """Why this unit ranked where it did. Every ranked item must carry an
    explanation -- an unexplained score is a number the reader cannot argue
    with, which is the same as a number they cannot trust."""


@dataclass(frozen=True)
class ReviewMark:
    """A reviewer's verdict on one unit, at one content fingerprint.

    Carry-forward is decided by ``content_fingerprint``, not by
    ``reviewed_revision_id``: a new revision that did not touch this unit
    leaves the mark valid, and a revision that rewrote it invalidates the mark
    even if the line numbers happen to match.

    ``actor_type`` is on the mark, not on the session, because "an agent said
    this looks fine" and "a human said this looks fine" are different claims
    and only the second one can close a review.
    """

    review_id: str
    unit_key: str
    state: MarkState
    reviewed_revision_id: str
    content_fingerprint: str
    reviewer_id: str = "local"
    actor_type: ActorType = "human"
    note: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AnnotationAnchor:
    """Where a comment is attached, described richly enough to be found again.

    Line numbers alone are not an anchor; they are a guess that survives until
    the first insertion above them. The hashes prove a candidate is the right
    text, and the verbatim copies exist because the relocation ladder has to
    *search* for that text on a later revision, not merely compare it.

    All of it is repository content the reviewer already has open on screen.
    """

    path: str
    side: AnchorSide
    start_line: int
    end_line: int
    selected_text_hash: str = ""
    """sha256 of the normalized selected text."""
    selected_text: str = ""
    """Verbatim, capped at ``_ANCHOR_TEXT_CAP`` characters."""
    before_context_hash: str = ""
    after_context_hash: str = ""
    before_context: str = ""
    """The ``_ANCHOR_CONTEXT_LINES`` lines above the selection, verbatim."""
    after_context: str = ""
    symbol_qualified_name: str = ""
    symbol_fingerprint: str = ""
    blob_sha: str = ""
    unit_key: str = ""
    """The owning unit. This is how an annotation follows a mark."""


@dataclass(frozen=True)
class Annotation:
    """One comment, at one anchor, on one revision.

    ``anchor_method`` and ``anchor_detail`` record how the *current* position
    was arrived at. The full attempt history is append-only in
    ``annotation_anchor_events`` -- a relocation that cannot be explained after
    the fact is a relocation nobody can audit.
    """

    id: str
    """``ann-<uuid7>``."""
    review_id: str
    revision_id: str
    """The revision this annotation was *created* on."""
    anchor: AnnotationAnchor
    body: str = ""
    kind: AnnotationKind = "comment"
    state: AnnotationState = "open"
    parent_id: str = ""
    """Threading; ``""`` for a root comment."""
    created_by: str = "local"
    created_by_actor: ActorType = "human"
    resolved_revision_id: str = ""
    anchor_method: AnchorMethod = "identical_blob"
    anchor_detail: str = ""
    created_at: str = ""
    updated_at: str = ""
    source: AnnotationSource = "human"
    """Who is making the claim. This is separate from ``kind``: an AI hint may
    be a suggestion, but it is never a human review judgment."""
    source_id: str = ""
    """Stable provenance within the source: agent session id, review skill id, reviewer id."""
    title: str = ""
    """Short progressive-disclosure label. Human comments normally leave this empty."""
    evidence: tuple[str, ...] = ()
    """Opaque evidence references understood by the source/consumer; never rendered as proof by itself."""
    confidence: float | None = None
    """Optional source confidence. Absence is different from 0 and is the default."""
    author_response: AuthorResponseState = "none"
    """What the author claims happened to this human comment. Never a reviewer verdict."""
    author_response_source_id: str = ""
    """Exact authoring session that made the response claim."""
    author_response_at: str = ""
    """When the author made the claim; empty until one exists."""


def validate_annotation_semantics(annotation: Annotation) -> None:
    """Reject source/claim combinations that could blur human judgment.

    ``source`` is a trust boundary, not presentation metadata. In particular,
    only a human reviewer may create review dispositions such as request-change
    or looks-good. Author and AI annotations can explain or suggest, but they
    cannot quietly become a human verdict by choosing the same ``kind``.
    """

    if annotation.source not in ANNOTATION_SOURCES:
        raise ValueError(f"unknown annotation source {annotation.source!r}")
    if annotation.source == "human" and annotation.created_by_actor != "human":
        raise ValueError("human review annotations must be created by a human actor")
    if annotation.source == "ai_review" and annotation.created_by_actor != "agent":
        raise ValueError("AI review annotations must be created by an agent actor")
    if annotation.source == "lemoncrow" and annotation.created_by_actor != "unknown":
        raise ValueError("LemonCrow evidence annotations use actor_type='unknown'; source carries the system identity")
    if annotation.source != "human" and annotation.kind in ("request_change", "looks_good"):
        raise ValueError(
            f"{annotation.source} annotations cannot record the human review disposition {annotation.kind!r}"
        )
    if annotation.confidence is not None and not 0.0 <= annotation.confidence <= 1.0:
        raise ValueError("annotation confidence must be between 0 and 1")
    if annotation.author_response not in AUTHOR_RESPONSE_STATES:
        raise ValueError(f"unknown author response {annotation.author_response!r}")
    if annotation.source != "human" and annotation.author_response != "none":
        raise ValueError("author responses may only be attached to human review annotations")


@dataclass(frozen=True)
class AnchorMove:
    """One annotation's fate on one revision, in the shape a reader can audit.

    Carries both line numbers because "it moved" is the only claim a reviewer
    has to take on trust, and showing where from and where to is what turns it
    back into something they can check.

    Reconstructed from ``annotation_anchor_events`` rather than returned by the
    call that wrote it, so it describes the *revision* and not the invocation:
    which command happened to record the revision is an accident of typing
    order and must not decide what a reviewer is shown.
    """

    annotation_id: str
    path: str
    status: str
    method: str
    detail: str
    from_line: int
    to_line: int

    @property
    def moved(self) -> bool:
        """Whether the annotation now points at a different line than before."""

        return self.from_line != self.to_line


@dataclass(frozen=True)
class DiscardedMark:
    """A verdict reconciliation deleted, kept as evidence that it existed.

    A mark whose unit left the review is cleared, and correctly: there is
    nothing left to attest to. What must not be cleared with it is the fact
    that a human looked at that content. Two questions depend on it and both
    are answered wrongly by an empty table:

    * *Was my approval thrown away?* -- announced at the moment it happens, and
      recoverable afterwards from here rather than only from a scrollback.
    * *Is this unit new to me?* -- ``new`` means "you have not seen this
      content", and the content fingerprint on this row is the only surviving
      proof that the reviewer has. Delete it and an undo that brings back a
      symbol they approved two revisions ago presents it as brand new work.

    The unit's descriptive fields are copied in rather than looked up later:
    the unit is, by definition, not in the revision that discarded it, so a
    surface reading this row a week later has nowhere else to find its name.
    """

    review_id: str
    unit_key: str
    discarded_revision_id: str
    """The revision whose reconciliation deleted the mark."""
    state: MarkState
    content_fingerprint: str
    """What the reviewer actually had on screen when they recorded the verdict."""
    reviewer_id: str = "local"
    reviewed_revision_id: str = ""
    kind: ReviewUnitKind = "file"
    path: str = ""
    symbol: str = ""
    ordinal: int = 0
    start_line: int = 0
    reason: str = ""
    """Why it went, in the words a reviewer is shown."""
    discarded_at: str = ""


@dataclass(frozen=True)
class DeliveryRecord:
    """One attempt to hand an annotation to something outside LemonCrow.

    Modelled now, delivered later: the schema exists so that adding a delivery
    target is a feature, not a migration of live review state.
    """

    id: str
    annotation_id: str
    target_type: FeedbackTargetType
    target_ref: str = ""
    state: str = "pending"
    remote_ref: str = ""
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ReviewEvidence:
    """One screenshot/trace/video/preview bound to an exact ReviewRevision.

    ``current`` versus ``stale`` is derived by the reader from ``revision_id``;
    it is intentionally not stored. A newer code revision therefore cannot
    leave an older screenshot silently looking like current proof.

    ``verification_status`` is an optional observed check result. Empty means
    this artifact is proof/context only (for example a screenshot). PASS/FAIL/
    NOT_RUN/UNKNOWN are rendered literally; the presence of an artifact never
    implies PASS.
    """

    id: str
    review_id: str
    revision_id: str
    kind: ReviewEvidenceKind
    title: str
    path: str = ""
    artifact_path: str = ""
    url: str = ""
    content_hash: str = ""
    mime_type: str = ""
    bytes: int = 0
    source: ReviewEvidenceSource = "human"
    source_ref: str = ""
    verification_status: str = ""
    detail: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class FrontierEntry:
    """One unit's standing in the reviewer's frontier."""

    unit_key: str
    kind: ReviewUnitKind
    path: str
    symbol: str = ""
    state: MarkState = "unreviewed"
    attention_rank: int = 0
    reasons: tuple[str, ...] = ()
    changed_since_mark: bool = False
    reviewed_revision_id: str = ""
    ordinal: int = 0
    """The unit's ordinal, carried so a hunk entry can be named ``path#2``.

    Three marks on one file are three different claims about three different
    amounts of reading; printed without the ordinal they render as identical
    rows and per-unit review looks broken."""
    start_line: int = 0
    """Presentation only, and carried for exactly one purpose: telling two
    same-named definitions in one file apart on screen.

    Never an identity component -- ``unit_key`` still contains no line number.
    But two units both labelled ``svc.py::run`` are two rows a reader cannot
    act on, and when nesting has not already separated them (``Reader.run`` vs
    ``Writer.run``) the line is the only thing left that will."""


@dataclass(frozen=True)
class ReviewFrontier:
    """What is left to look at, and what changed since you last looked.

    **Derived on every read, never stored.** Computed from ``review_units`` and
    ``review_marks`` it cannot disagree with the marks; persisted, it would
    become a second opinion that drifts and then has to be reconciled.

    ``reviewed_unit_fingerprints`` is a tuple of pairs rather than a mapping so
    this dataclass stays frozen and hashable.
    """

    review_id: str
    reviewer_id: str = "local"
    last_seen_revision: str = ""
    entries: tuple[FrontierEntry, ...] = ()
    unresolved_annotation_ids: tuple[str, ...] = ()
    orphaned_annotation_ids: tuple[str, ...] = ()
    reviewed_unit_fingerprints: tuple[tuple[str, str], ...] = ()


__all__ = [
    "ACTOR_TYPES",
    "ANCHOR_METHODS",
    "ANCHOR_METHOD_LABELS",
    "ANCHOR_SIDES",
    "ANNOTATION_KINDS",
    "ANNOTATION_STATES",
    "EXACT_ANCHOR_METHODS",
    "FEEDBACK_TARGET_TYPES",
    "FINGERPRINT_METHODS",
    "HEURISTIC_ANCHOR_METHODS",
    "LOST_ANCHOR_METHODS",
    "MARK_STATES",
    "REVIEW_EVIDENCE_KINDS",
    "REVIEW_EVIDENCE_SOURCES",
    "SESSION_SCHEMA_VERSION",
    "SESSION_STATUSES",
    "SUBJECT_TYPES",
    "UNIT_KINDS",
    "ActorType",
    "AnchorMethod",
    "AnchorMove",
    "AnchorSide",
    "Annotation",
    "AnnotationAnchor",
    "AnnotationKind",
    "AnnotationState",
    "DeliveryRecord",
    "DiscardedMark",
    "FeedbackTargetType",
    "FingerprintMethod",
    "FrontierEntry",
    "MarkState",
    "ReviewEvidence",
    "ReviewEvidenceKind",
    "ReviewEvidenceSource",
    "ReviewFrontier",
    "ReviewMark",
    "ReviewRevision",
    "ReviewSession",
    "ReviewSessionStatus",
    "ReviewSubjectType",
    "ReviewUnit",
    "ReviewUnitKind",
    "anchor_symbol_claim",
]
