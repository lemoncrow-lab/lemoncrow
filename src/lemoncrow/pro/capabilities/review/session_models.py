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

ReviewBindingSubjectKind = Literal["branch", "pull_request", "named_range", "detached"]
REVIEW_BINDING_SUBJECT_KINDS: tuple[str, ...] = ("branch", "pull_request", "named_range", "detached")
ReviewCaptureKind = Literal["legacy", "draft", "commit", "revision"]
REVIEW_CAPTURE_KINDS: tuple[str, ...] = ("legacy", "draft", "commit", "revision")
ReviewCommitState = Literal["active", "superseded"]
REVIEW_COMMIT_STATES: tuple[str, ...] = ("active", "superseded")

ReviewUnitKind = Literal["file", "symbol", "hunk", "document_section", "dependency"]
UNIT_KINDS: tuple[str, ...] = ("file", "symbol", "hunk", "document_section", "dependency")
AttributionScope = Literal["revision", "file"]
AttributionRole = Literal["primary", "contributor"]
ATTRIBUTION_SCOPES: tuple[str, ...] = ("revision", "file")
ATTRIBUTION_ROLES: tuple[str, ...] = ("primary", "contributor")

MarkState = Literal["unreviewed", "reviewed", "needs_changes", "changed_since_review", "unknown", "later"]
ReviewMarkEventKind = Literal["judgment", "reconciled", "discarded", "migrated"]
REVIEW_MARK_EVENT_KINDS: tuple[str, ...] = ("judgment", "reconciled", "discarded", "migrated")

ReviewOutcomeKind = Literal["comment", "lgtm", "changes_requested"]
REVIEW_OUTCOME_KINDS: tuple[str, ...] = ("comment", "lgtm", "changes_requested")

ReviewAskRole = Literal["human", "assistant"]
REVIEW_ASK_ROLES: tuple[str, ...] = ("human", "assistant")
ReviewAskIntent = Literal["question", "change_request"]
REVIEW_ASK_INTENTS: tuple[str, ...] = ("question", "change_request")
ReviewAskHandoffState = Literal["prepared", "dispatching", "sent", "blocked", "failed", "uncertain"]
REVIEW_ASK_HANDOFF_STATES: tuple[str, ...] = (
    "prepared",
    "dispatching",
    "sent",
    "blocked",
    "failed",
    "uncertain",
)

ThreadTurnOwnerKind = Literal["none", "author", "reviewer"]
THREAD_TURN_OWNER_KINDS: tuple[str, ...] = ("none", "author", "reviewer")

ReviewActivityKind = Literal[
    "review.created",
    "review.status_changed",
    "revision.recorded",
    "snapshot.published",
    "mark.judgment",
    "mark.reconciled",
    "mark.discarded",
    "mark.migrated",
    "annotation.created",
    "annotation.updated",
    "annotation.state_changed",
    "annotation.author_response",
    "annotation.thread_reply",
    "proposal.created",
    "proposal.applied",
    "proposal.conflicted",
    "proposal.dismissed",
    "outcome.recorded",
    "development.contributor",
    "development.handoff",
    "development.session_ended",
    "priority.override_set",
    "priority.override_cleared",
    "landing.revalidated",
    "landing.revalidation_deferred",
    "landing.withdrawn",
    "policy.generation_changed",
    "finding.static_disposition",
    "post_merge.action",
    "automation.refused",
    "annotation.provider_synced",
    "annotation.provider_sync_failed",
    "review.discard_acknowledged",
    "premerge.waiver_granted",
    "premerge.waiver_refused",
    "premerge.intent_rebound",
    "premerge.intent_rebind_refused",
    "premerge.intent_record_refused",
    "record.exported",
    "record.not_emitted",
    "record.retracted",
    "review.coverage_seen",
    "review.coverage_skipped",
    "deployment.observed",
    "deployment.summarized",
    "observation.contradicted",
    "observation.summarized",
    "incident.linked",
    "outcome.linked",
    "landing.auto_approved",
    "landing.auto_approval_withdrawn",
    "landing.break_glass",
    "landing.break_glass_refused",
]
REVIEW_ACTIVITY_KINDS: tuple[str, ...] = (
    "review.created",
    "review.status_changed",
    "revision.recorded",
    "snapshot.published",
    "mark.judgment",
    "mark.reconciled",
    "mark.discarded",
    "mark.migrated",
    "annotation.created",
    "annotation.updated",
    "annotation.state_changed",
    "annotation.author_response",
    "annotation.thread_reply",
    "annotation.finding_disposition",
    "proposal.created",
    "proposal.applied",
    "proposal.conflicted",
    "proposal.dismissed",
    "outcome.recorded",
    "development.contributor",
    "development.handoff",
    "development.session_ended",
    "priority.override_set",
    "priority.override_cleared",
    "landing.revalidated",
    "landing.revalidation_deferred",
    "landing.withdrawn",
    "policy.generation_changed",
    # A human's dismissal/confirmation of one imported static finding (P11-FED).
    "finding.static_disposition",
    # P12-AUTO: post-merge follow-ups (human or rule, with the rule id), refused
    # automation, provider sync of published feedback, a confirmed discard.
    "post_merge.action",
    "automation.refused",
    "annotation.provider_synced",
    "annotation.provider_sync_failed",
    "review.discard_acknowledged",
    # Pre-merge check waivers (granted or refused) and intent binding (P10).
    "premerge.waiver_granted",
    "premerge.waiver_refused",
    "premerge.intent_rebound",
    "premerge.intent_rebind_refused",
    "premerge.intent_record_refused",
    # A signed (or explicitly unsigned) review record was emitted (P10-SIGN).
    "record.exported",
    # A provider-observed merge emitted no record: nothing may sign it here (P10-SIGN).
    "record.not_emitted",
    # A person retracted a signed verdict inside the 24-hour window (P6-2).
    "record.retracted",
    # D7: reviewer-reported exposure and explicit skipped-coverage disclosure.
    "review.coverage_seen",
    "review.coverage_skipped",
    # P15-DEPLOY: a merged change's deployment state moved in one environment, and a
    # deployment's observation window closed (summary kept, raw signal expired).
    "deployment.observed",
    "deployment.summarized",
    "observation.contradicted",
    "observation.summarized",
    "incident.linked",
    "outcome.linked",
    # P10-LAND: policy auto-approval (made / withdrawn) and break-glass (used / refused).
    "landing.auto_approved",
    "landing.auto_approval_withdrawn",
    "landing.break_glass",
    "landing.break_glass_refused",
)

AnnotationVersionKind = Literal[
    "created",
    "edited",
    "state_changed",
    "author_response",
    "thread_reply",
    "finding_disposition",
]
ANNOTATION_VERSION_KINDS: tuple[str, ...] = (
    "created",
    "edited",
    "state_changed",
    "author_response",
    "thread_reply",
    "finding_disposition",
)
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
    "later",
)
"""``later`` is "Decide later": the reviewer saw the part and set it aside.

It is **not a judgment**. It is carried with the same weight as ``unreviewed``
everywhere a count or a gate asks whether a part has been judged, and it is
shown as still open. Only the states in :data:`JUDGED_MARK_STATES` attest that
a human decided something about the content.
"""
JUDGED_MARK_STATES: frozenset[str] = frozenset({"reviewed", "needs_changes"})
"""The marks that record a human decision about the part's content on its fingerprint."""

AnswerCategory = Literal["decision", "quick_check", "accepted_by_rule"]
ANSWER_CATEGORIES: tuple[str, ...] = ("decision", "quick_check", "accepted_by_rule")
"""How a part is asked of a person.

``decision``: a person must answer it. ``quick_check``: a person glances and
confirms. ``accepted_by_rule``: a named rule accepts it, it is listed, and it is
never recorded as reviewed or quick-checked. Computed by
:mod:`~lemoncrow.pro.capabilities.review.answer_category` from checked facts only.
"""

AnnotationKind = Literal["comment", "request_change", "suggestion", "looks_good"]
ANNOTATION_KINDS: tuple[str, ...] = ("comment", "request_change", "suggestion", "looks_good")

AnnotationSource = Literal["human", "author", "lemoncrow", "ai_review"]
ANNOTATION_SOURCES: tuple[str, ...] = ("human", "author", "lemoncrow", "ai_review")

AnnotationState = Literal["open", "resolved", "orphaned", "obsolete"]
FindingDisposition = Literal["", "open", "confirmed_by_reviewer", "dismissed", "fixed", "superseded_stale"]
FINDING_DISPOSITIONS: tuple[str, ...] = (
    "",
    "open",
    "confirmed_by_reviewer",
    "dismissed",
    "fixed",
    "superseded_stale",
)
FindingDismissalReason = Literal["", "incorrect", "accepted_risk", "duplicate", "not_applicable"]
FINDING_DISMISSAL_REASONS: tuple[str, ...] = ("", "incorrect", "accepted_risk", "duplicate", "not_applicable")
ANNOTATION_STATES: tuple[str, ...] = ("open", "resolved", "orphaned", "obsolete")
AuthorResponseState = Literal["none", "addressed"]
AUTHOR_RESPONSE_STATES: tuple[str, ...] = ("none", "addressed")

ReviewChangeProposalState = Literal["proposed", "applied", "conflicted", "dismissed", "superseded"]
REVIEW_CHANGE_PROPOSAL_STATES: tuple[str, ...] = (
    "proposed",
    "applied",
    "conflicted",
    "dismissed",
    "superseded",
)

ReviewEvidenceKind = Literal[
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
]
REVIEW_EVIDENCE_KINDS: tuple[str, ...] = (
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
    # A human's reasoned override of one blocking check on one exact Snapshot.
    "waiver",
    # An immutable capture of one linked intent source or the PR description (P10).
    "intent",
    # A stored, validated model evaluation of one pre-merge check (never a verification row).
    "premerge_check",
    # A provider/CD observation of a merged change's deployment (P15-DEPLOY); post-merge only.
    "deployment",
)
ReviewEvidenceSource = Literal["human", "agent", "test", "ci", "external"]
REVIEW_EVIDENCE_SOURCES: tuple[str, ...] = ("human", "agent", "test", "ci", "external")

CheckAttestationClass = Literal[
    "provider_ci",
    "lemoncrow_verification",
    "trusted_security_feed",
    # A DSSE-signed in-toto result verified with a key the trusted base names (Trust T4).
    "signed_attestation",
    "human_observation",
    "human_waiver",
]
CHECK_ATTESTATION_CLASSES: tuple[str, ...] = (
    "provider_ci",
    "lemoncrow_verification",
    "trusted_security_feed",
    "signed_attestation",
    "human_observation",
    "human_waiver",
)

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


FingerprintMethod = Literal["blob_sha256", "symbol_body_sha256", "hunk_patch_sha256", "semantic", "unknown"]
FINGERPRINT_METHODS: tuple[str, ...] = (
    "blob_sha256",
    "symbol_body_sha256",
    "hunk_patch_sha256",
    "semantic",
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
    """One logical change a reviewer is working through across many captures.

    ``source_ref`` remains external/source-control context for compatibility.
    Local Review routing uses explicit :class:`ReviewBinding` rows so a branch
    can have one active Review without making branch identity the Review itself.
    ``review_base_sha`` freezes the Git base of the logical change; HEAD may
    advance through several commits without moving that base.
    """

    id: str
    """Bare 32-hex UUIDv7 payload. Public refs add the ``r/`` namespace."""
    subject_type: ReviewSubjectType
    repo_root: str
    """Absolute and ``resolve()``d. The only filesystem root a review service
    may ever read. An attribute of the session, deliberately not part of its
    primary key -- the same review must survive being replicated to a host
    where that path does not exist."""
    range_mode: RangeMode
    title: str = ""
    source_ref: str = ""
    """External/source-control context. New local routing uses ReviewBinding."""
    review_base_sha: str = ""
    """Immutable Git base for this logical Review. Blank means legacy/unadopted."""
    actor_type: ActorType = "unknown"
    """Rolled up from the revisions' provenance. ``mixed`` is a correct answer,
    not an unresolved one."""
    status: ReviewSessionStatus = "open"
    reviewer_id: str = "local"
    current_revision_id: str = ""
    """Revision whose source state is active now. History remains append-only,
    but an undo may legitimately point back to an older revision number."""
    created_at: str = ""
    """Aware-UTC ISO-8601. Blank means "not yet stamped"; the store fills it."""
    updated_at: str = ""
    review_base_policy: Literal["integrated", "fixed"] = "integrated"
    """Explicit new Reviews keep their original base; older Reviews follow integration."""


@dataclass(frozen=True)
class ReviewBinding:
    """Durable ownership of one logical source subject by a Review."""

    id: str
    repository_identity: str
    subject_kind: ReviewBindingSubjectKind
    subject_key: str
    review_id: str
    worktree_key: str = ""
    active: bool = True
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ReviewCommit:
    """One immutable Git commit checkpoint inside a logical Review."""

    review_id: str
    commit_number: int
    commit_sha: str
    revision_id: str
    parent_sha: str = ""
    state: ReviewCommitState = "active"
    created_at: str = ""


@dataclass(frozen=True)
class ReviewRevision:
    """One captured state of the change under review.

    ``tree_fingerprint`` is the semantic content identity. Exact Snapshot
    idempotency is ``(tree_fingerprint, source_fingerprint)``: an untouched
    source generation reuses its Snapshot, while SCM/base movement may record a
    new Snapshot even when the semantic tree is equivalent. Human judgment
    validity is still decided per ReviewTarget content fingerprint, never by a
    commit/tree/source generation identifier.
    """

    id: str
    """Bare 32-hex UUIDv7 payload. Public refs add the ``rr/`` namespace."""
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
    """Source-generation identity used for change detection and revision history.

    Empty on reviews captured before R14. Together with ``tree_fingerprint`` it
    prevents duplicate captures of the same source generation while allowing a
    rebase/base move to remain visible as a new revision. It never decides human
    review validity; target reconciliation remains authoritative.
    """
    source_view_id: str = ""
    """Native View that supplied this revision, when capture came through the server.

    Used only to bind later ``current_index`` context to the same logical View;
    it never participates in revision or human-judgment identity.
    """
    source_view_revision: int = 0
    """Committed native View revision verified when this revision was captured."""
    capture_kind: ReviewCaptureKind = "legacy"
    """User-facing classification. Branch Reviews capture ``draft`` and ``commit``
    checkpoints; PR and named-range Reviews capture ``revision``; ``legacy`` is
    a pre-migration row."""
    commit_sha: str = ""
    """Exact Git commit represented by a ``commit`` capture; blank for drafts/legacy rows."""
    branch_ref: str = ""
    """Canonical branch ref observed when this immutable capture was created."""
    worktree_key: str = ""
    """Stable local worktree identity for draft provenance; commits may leave it blank."""
    title: str = ""
    """Human-readable revision headline derived from captured author output."""


@dataclass(frozen=True)
class ReviewAttribution:
    """Durable author/contributor evidence for one immutable revision scope."""

    revision_id: str
    scope: AttributionScope
    scope_key: str
    host: str
    session_id: str
    role: AttributionRole = "contributor"
    model: str = ""
    certainty: ProvenanceCertainty = "none"
    method: str = ""
    created_at: str = ""


ReviewDestinationKind = Literal["auto", "session", "new_session", "github_pr", "export"]
REVIEW_DESTINATION_KINDS: tuple[str, ...] = ("auto", "session", "new_session", "github_pr", "export")


@dataclass(frozen=True)
class ReviewDestination:
    """Where this Change's review feedback goes when the reviewer sends it.

    ``auto`` is the default and is never stored: it routes each comment to the
    exact session that wrote it. A stored row pins one destination for every
    comment until the reviewer changes it.
    """

    review_id: str
    kind: ReviewDestinationKind = "auto"
    host: str = ""
    session_id: str = ""
    external_ref: str = ""
    set_by: str = ""
    updated_at: str = ""


HostedActionKind = Literal["mark", "comment", "reply", "outcome", "request_complete"]
HOSTED_ACTION_KINDS: tuple[str, ...] = ("mark", "comment", "reply", "outcome", "request_complete")
HostedActionState = Literal["pending", "applied", "conflict", "discarded"]
HOSTED_ACTION_STATES: tuple[str, ...] = ("pending", "applied", "conflict", "discarded")
#: States that still need the reviewer or a reconnect; they count against the queue bound.
HOSTED_ACTION_UNRESOLVED: tuple[str, ...] = ("pending", "conflict")


@dataclass(frozen=True)
class HostedAttachment:
    """An explicit link from this local Review to one hosted Review (G5-SYNC).

    While it exists hosted owns comments, judgments, requests, outcomes and
    policy; the local Review keeps ``projection_json`` (the hosted state last
    read) plus a queue of pending actions. ``principal_id`` is the hosted
    identity every queued action is replayed as.
    """

    review_id: str
    origin: str
    hosted_repo_id: str
    hosted_review_id: str
    principal_id: str
    projection_json: str = "{}"
    attached_at: str = ""
    synced_at: str = ""


@dataclass(frozen=True)
class HostedAction:
    """One explicitly queued local action, bound to the hosted Snapshot it was made on."""

    id: str
    review_id: str
    kind: HostedActionKind
    snapshot_id: str
    payload_json: str
    preconditions_json: str
    sequence: int = 0
    supersede_key: str = ""
    state: HostedActionState = "pending"
    reason: str = ""
    result_json: str = "{}"
    created_at: str = ""
    resolved_at: str = ""


@dataclass(frozen=True)
class ReviewRelationship:
    """Minimal durable context attached to one Change.

    One shape intentionally covers external SCM identity, Change dependencies,
    work-item links and connected-SCM requirements. External systems remain
    authoritative; LemonCrow persists only the relationship needed to keep the
    durable Change coherent across source-control identity movement.
    """

    id: str
    review_id: str
    kind: str
    relation: str
    provider: str = ""
    external_id: str = ""
    target_review_id: str = ""
    url: str = ""
    title: str = ""
    status: str = ""
    source: str = "explicit"
    metadata_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""


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
    answer_category: AnswerCategory | Literal[""] = ""
    """The structural category stamped when the revision was captured (path, file kind, special Git
    change); ``""`` on a unit stored before the stamp existed. The part's final category adds the
    facts only the queue knows (see ``answer_category.effective_answer``) and can only raise this to
    ``decision``."""
    category_rule: str = ""
    """The rule that accepted this unit (``generated_files`` or ``lockfiles``); ``""`` otherwise."""


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
class ReviewMarkEvent:
    """Append-only transition of one reviewer's judgment on one semantic unit."""

    id: str
    review_id: str
    reviewer_id: str
    unit_key: str
    revision_id: str
    reviewed_revision_id: str
    event_kind: ReviewMarkEventKind
    from_state: MarkState | Literal[""]
    to_state: MarkState | Literal[""]
    content_fingerprint: str
    previous_unit_key: str = ""
    actor_type: ActorType = "human"
    note: str = ""
    reason: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ReviewActivityEvent:
    """Human-visible product history; deliberately separate from security audit."""

    id: str
    review_id: str
    revision_id: str
    kind: ReviewActivityKind
    actor_id: str = ""
    actor_type: ActorType = "unknown"
    subject_type: str = ""
    subject_id: str = ""
    summary: str = ""
    detail_json: str = "{}"
    created_at: str = ""


@dataclass(frozen=True)
class ReviewAskConversationRecord:
    """One durable, Review-scoped Ask conversation shown in quick history."""

    id: str
    review_id: str
    title: str = ""
    origin_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""
    #: Who asked (the acting reviewer). ``""`` for a row from before this was recorded.
    created_by: str = ""


@dataclass(frozen=True)
class ReviewAskMessage:
    """One immutable conversational turn inside a Review-scoped Ask conversation."""

    id: str
    review_id: str
    revision_id: str
    role: ReviewAskRole
    intent: ReviewAskIntent
    body: str
    target_unit_key: str = ""
    path: str = ""
    symbol: str = ""
    side: str = ""
    start_line: int = 0
    end_line: int = 0
    context_json: str = "{}"
    references_json: str = "[]"
    action_json: str = "{}"
    created_at: str = ""
    conversation_id: str = ""
    #: Who asked, for an answer too (it belongs to the asker's conversation). ``""`` for a row from before this was recorded.
    created_by: str = ""


@dataclass(frozen=True)
class ReviewAskHandoff:
    """Mutable dispatch state for one assistant change-request preview."""

    message_id: str
    operation_id: str
    instruction_hash: str
    state: ReviewAskHandoffState = "prepared"
    target_ref: str = ""
    remote_ref: str = ""
    last_error: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ReviewOutcome:
    """One reviewer-level verdict bound to one immutable review revision."""

    id: str
    review_id: str
    revision_id: str
    reviewer_id: str
    outcome: ReviewOutcomeKind
    summary: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ReviewChangeProposal:
    """One reviewer-authored source change proposed against an immutable revision.

    The proposal is not the revision and never mutates one. ``base_file_sha256``
    is the compare-and-swap guard for applying it to a trusted mutable checkout;
    ``applied_source_fingerprint`` links that write to the later ReviewRevision
    whose source fingerprint captured the same working tree.
    """

    id: str
    review_id: str
    base_revision_id: str
    path: str
    start_line: int
    end_line: int
    original_text: str
    replacement_text: str
    patch_text: str
    base_file_sha256: str
    state: ReviewChangeProposalState = "proposed"
    target_unit_key: str = ""
    annotation_id: str = ""
    intent: str = ""
    conflict_reason: str = ""
    created_by: str = "local"
    created_at: str = ""
    updated_at: str = ""
    applied_at: str = ""
    applied_source_fingerprint: str = ""
    result_revision_id: str = ""


@dataclass(frozen=True)
class ReviewDeletionTombstone:
    """Durable record of one permanent Review deletion (P12-AUTO item 12).

    Written by the store in the same transaction as the deletion and never a
    child of the deleted Review, so it outlives every row it describes. It
    names the Review, never its content: the confirmation the deleter typed is
    kept only as a SHA-256. Tombstones are append-only and chained per store
    (``previous_sha256`` -> ``record_sha256``), so an edited or removed
    tombstone is detectable. The store fills ``id``, ``sequence``, ``counts``,
    ``deleted_at`` and the digests; callers supply who, where and the request.
    """

    review_id: str
    repo_id: str
    actor_id: str
    request_id: str
    confirmation_sha256: str
    confirmed_by: Literal["id", "title"]
    id: str = ""
    sequence: int = 0
    counts: tuple[tuple[str, int], ...] = ()
    """``(table, rows deleted)`` for the Review and its counted children."""
    deleted_at: str = ""
    previous_sha256: str = ""
    record_sha256: str = ""

    def digest_fields(self) -> dict[str, object]:
        """Every field the chain digest covers (all but ``record_sha256``)."""

        return {
            "id": self.id,
            "sequence": self.sequence,
            "review_id": self.review_id,
            "repo_id": self.repo_id,
            "actor_id": self.actor_id,
            "request_id": self.request_id,
            "confirmation_sha256": self.confirmation_sha256,
            "confirmed_by": self.confirmed_by,
            "counts": {table: count for table, count in self.counts},
            "deleted_at": self.deleted_at,
            "previous_sha256": self.previous_sha256,
        }

    def to_wire(self) -> dict[str, object]:
        return {**self.digest_fields(), "record_sha256": self.record_sha256}


@dataclass(frozen=True)
class AnnotationVersion:
    """Append-only semantic snapshot of one comment, excluding anchor movement."""

    id: str
    annotation_id: str
    review_id: str
    revision_id: str
    version_number: int
    body: str
    kind: AnnotationKind
    state: AnnotationState
    author_response: AuthorResponseState = "none"
    author_response_source_id: str = ""
    author_response_at: str = ""
    resolved_revision_id: str = ""
    changed_by: str = ""
    changed_by_actor: ActorType = "unknown"
    change_kind: AnnotationVersionKind = "edited"
    turn_owner_kind: ThreadTurnOwnerKind = "none"
    turn_owner_id: str = ""
    finding_disposition: FindingDisposition = ""
    finding_disposition_reason: FindingDismissalReason = ""
    finding_disposition_revision_id: str = ""
    finding_disposition_reviewer_id: str = ""
    finding_disposition_evidence: tuple[str, ...] = ()
    created_at: str = ""


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
    reconsidered_after_structure: bool = False
    """Human-reported reconsideration, not a claim that relationships are proven."""
    structure_revision_id: str = ""
    """Revision on which the reconsideration was recorded; required with the flag."""
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
    agent_role: str = "unknown"
    agent_host: str = ""
    agent_model: str = ""
    parent_session_id: str = ""
    source_message_id: str = ""
    comment_scope: Literal["code", "change"] = "code"
    """Discussion has no code anchor; an empty revision_id means pending draft ownership."""
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
    turn_owner_kind: ThreadTurnOwnerKind = "none"
    """Whose turn the root thread is waiting on. Replies normally leave this none."""
    turn_owner_id: str = ""
    """Exact reviewer principal for reviewer turns; empty means role-level author ownership."""
    rule_refs: tuple[str, ...] = ()
    """Canonical ReviewRule identities that made an AI finding policy-relevant; distinct from evidence."""
    finding_disposition: FindingDisposition = ""
    """Reviewer-facing lifecycle for AI findings; never a human Review mark."""
    finding_disposition_reason: FindingDismissalReason = ""
    """Required taxonomy only when the finding is dismissed."""
    finding_disposition_revision_id: str = ""
    """Revision on which the reviewer or revalidation recorded this disposition."""
    finding_disposition_reviewer_id: str = ""
    """Human principal that recorded the disposition; empty for automatic stale transitions."""
    finding_disposition_evidence: tuple[str, ...] = ()
    """Evidence/revalidation refs that justify fixed; never inferred from nearby edits."""


def validate_annotation_semantics(annotation: Annotation) -> None:
    """Reject source/claim combinations that could blur human judgment.

    ``source`` is a trust boundary, not presentation metadata. In particular,
    only a human reviewer may create review dispositions such as request-change
    or looks-good. Author and AI annotations can explain or suggest, but they
    cannot quietly become a human verdict by choosing the same ``kind``.
    """

    if annotation.reconsidered_after_structure and (
        annotation.source != "human"
        or annotation.created_by_actor != "human"
        or annotation.kind != "request_change"
        or annotation.parent_id
        or annotation.structure_revision_id != annotation.revision_id
    ):
        raise ValueError("structure reconsideration requires a human root change request on its own revision")
    if not annotation.reconsidered_after_structure and annotation.structure_revision_id:
        raise ValueError("ordinary annotation cannot claim a structure revision")
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
    if annotation.turn_owner_kind not in THREAD_TURN_OWNER_KINDS:
        raise ValueError(f"unknown thread turn owner {annotation.turn_owner_kind!r}")
    if annotation.finding_disposition not in FINDING_DISPOSITIONS:
        raise ValueError(f"unknown AI finding disposition {annotation.finding_disposition!r}")
    if annotation.finding_disposition_reason not in FINDING_DISMISSAL_REASONS:
        raise ValueError(f"unknown AI finding dismissal reason {annotation.finding_disposition_reason!r}")
    if annotation.source != "ai_review" and annotation.finding_disposition:
        raise ValueError("finding dispositions may only be attached to AI review annotations")
    if annotation.finding_disposition == "dismissed" and not annotation.finding_disposition_reason:
        raise ValueError("dismissed AI findings require a dismissal reason")
    if annotation.finding_disposition != "dismissed" and annotation.finding_disposition_reason:
        raise ValueError("dismissal reason is only valid for dismissed AI findings")
    if annotation.finding_disposition == "fixed" and not annotation.finding_disposition_evidence:
        raise ValueError("fixed AI findings require current revalidation evidence")
    if annotation.parent_id and annotation.turn_owner_kind != "none":
        raise ValueError("only root annotations may own a thread turn")
    if annotation.turn_owner_kind == "none" and annotation.turn_owner_id:
        raise ValueError("a thread with no owner kind cannot name an owner id")
    if annotation.turn_owner_kind == "reviewer" and not annotation.turn_owner_id:
        raise ValueError("reviewer-owned thread turns require a reviewer principal id")


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
    operation_id: str = ""
    revision_id: str = ""
    feedback_hash: str = ""
    annotation_version: int = 0


@dataclass(frozen=True)
class AuthorGuidanceRow:
    """One imported AuthorGuidance document, stored with its Change (spec §6).

    Raw, attributed, versioned author input. ``revision_id`` is the Snapshot
    it was first imported on; read time resolves it against any Snapshot.
    """

    review_id: str
    id: str
    revision_id: str
    producer_host: str
    producer_session_id: str
    producer_model: str
    attribution: str
    repo_root: str
    schema_version: int
    source_identity_json: str
    payload_json: str
    created_at: str
    imported_at: str = ""


DecisionAction = Literal["accept", "reconfirm", "request_change", "defer", "reopen"]
DecisionDisposition = Literal["open", "accepted", "change_requested", "deferred"]


@dataclass(frozen=True)
class ReviewDecisionEvent:
    """One human disposition of one author-keyed decision (spec §5).

    Append-only. ``scope_json``/``implementation_json`` are the canonical
    documents the fingerprints hash, kept so validity can name which input
    changed. No event ever changes a source review mark.
    """

    id: str
    review_id: str
    decision_key: str
    reviewer_id: str
    revision_id: str
    action: DecisionAction
    disposition: DecisionDisposition
    option_key: str = ""
    definition_ref: str = ""
    semantic_fingerprint: str = ""
    implementation_fingerprint: str = ""
    scope_json: str = "{}"
    implementation_json: str = "{}"
    reason: str = ""
    trigger_kind: str = ""
    trigger_check_key: str = ""
    trigger_note: str = ""
    feedback_annotation_id: str = ""
    prior_event_id: str = ""
    actor_type: ActorType = "human"
    created_at: str = ""


ReviewIntelligenceRunStatus = Literal["pending", "ready", "failed", "cancelled"]


@dataclass(frozen=True)
class ReviewIntelligenceRun:
    """One durable, Snapshot-bound Review Intelligence execution/cache row.

    Identity contains only semantic/digest fields. No local materialization path,
    prompt body, hidden reasoning, or human judgment belongs in this record.
    """

    review_id: str
    id: str
    revision_id: str
    snapshot_identity: str
    capability: str
    run_key: str
    scope_digest: str
    context_digest: str
    policy_digest: str
    schema_digest: str
    driver: str
    model: str
    prompt_version: int
    schema_version: int
    status: ReviewIntelligenceRunStatus
    result_json: str = "{}"
    usage_json: str = "{}"
    error: str = ""
    model_calls: int = 0
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""


SynthesisStatus = Literal["pending", "ready", "failed"]


@dataclass(frozen=True)
class ReviewSynthesisRow:
    """One cached plan synthesis, keyed by its exact inputs (spec §10, §11).

    A ``pending`` row is a claim: exactly one caller runs the model for a
    key, and completion is compare-and-set on that pending row. The payload
    is validated model output; it never carries a human judgment.
    """

    review_id: str
    id: str
    revision_id: str
    synthesis_key: str
    schema_version: int
    prompt_version: int
    model: str
    status: SynthesisStatus
    payload_json: str = "{}"
    error: str = ""
    created_at: str = ""
    completed_at: str = ""
    inputs_digest: str = ""
    """Cheap identity of the request's inputs (``synthesis_inputs_digest``):
    how every reader surface finds this row without rebuilding the request."""


@dataclass(frozen=True)
class ReviewEvidence:
    """One artifact/check observation bound to an exact ReviewRevision.

    Current versus stale is derived by the reader from revision_id. Typed
    attestation fields are durable authority, while title and source_ref remain
    presentation/deduplication metadata only. Empty attestation fields denote
    legacy/untyped context and can never satisfy a typed required check.
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
    attestation_class: str = ""
    stable_key: str = ""
    provider: str = ""
    integration_id: str = ""
    provider_revision: str = ""
    branch: str = ""
    environment: str = ""
    attestation_scope: str = ""
    attested_by: str = ""
    remote_identity: str = ""
    observed_at: str = ""
    waived_requirement_key: str = ""
    execution_intent: str = ""
    # Who executed reviewed code for this evidence: ``author_machine``,
    # ``customer_worker`` or ``managed_sandbox`` (execution.ExecutorClass).
    # Stamped by the dispatcher, never by the executor; empty means the
    # evidence is not an execution result.
    executor_class: str = ""
    # Canonical JSON of the result's verification statement (where it ran,
    # image digest, network, integrations not verified); empty when the
    # evidence carries no typed execution record.
    execution_verification: str = ""


@dataclass(frozen=True)
class CheckAttestation:
    """Typed authority projected from one ReviewEvidence row."""

    stable_key: str
    attestation_class: CheckAttestationClass
    state: str
    review_id: str
    revision_id: str
    provider: str = ""
    integration_id: str = ""
    provider_revision: str = ""
    branch: str = ""
    environment: str = ""
    scope: str = ""
    attested_by: str = ""
    remote_identity: str = ""
    observed_at: str = ""
    evidence_id: str = ""
    waived_requirement_key: str = ""
    executor_class: str = ""

    @classmethod
    def from_evidence(cls, evidence: ReviewEvidence) -> CheckAttestation | None:
        if evidence.attestation_class not in CHECK_ATTESTATION_CLASSES or not evidence.stable_key:
            return None
        return cls(
            stable_key=evidence.stable_key,
            attestation_class=evidence.attestation_class,  # type: ignore[arg-type]
            state=evidence.verification_status,
            review_id=evidence.review_id,
            revision_id=evidence.revision_id,
            provider=evidence.provider,
            integration_id=evidence.integration_id,
            provider_revision=evidence.provider_revision,
            branch=evidence.branch,
            environment=evidence.environment,
            scope=evidence.attestation_scope,
            attested_by=evidence.attested_by,
            remote_identity=evidence.remote_identity,
            observed_at=evidence.observed_at or evidence.created_at,
            evidence_id=evidence.id,
            waived_requirement_key=evidence.waived_requirement_key,
            executor_class=evidence.executor_class,
        )


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
    "ANSWER_CATEGORIES",
    "EXACT_ANCHOR_METHODS",
    "FEEDBACK_TARGET_TYPES",
    "FINGERPRINT_METHODS",
    "HEURISTIC_ANCHOR_METHODS",
    "HOSTED_ACTION_KINDS",
    "HOSTED_ACTION_STATES",
    "HOSTED_ACTION_UNRESOLVED",
    "JUDGED_MARK_STATES",
    "LOST_ANCHOR_METHODS",
    "MARK_STATES",
    "REVIEW_ASK_HANDOFF_STATES",
    "REVIEW_ASK_INTENTS",
    "REVIEW_ASK_ROLES",
    "REVIEW_BINDING_SUBJECT_KINDS",
    "REVIEW_CAPTURE_KINDS",
    "REVIEW_COMMIT_STATES",
    "REVIEW_DESTINATION_KINDS",
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
    "AnswerCategory",
    "DecisionAction",
    "DecisionDisposition",
    "DeliveryRecord",
    "DiscardedMark",
    "FeedbackTargetType",
    "FingerprintMethod",
    "FrontierEntry",
    "HostedAction",
    "HostedActionKind",
    "HostedActionState",
    "HostedAttachment",
    "MarkState",
    "ReviewAskConversationRecord",
    "ReviewAskHandoff",
    "ReviewAskHandoffState",
    "ReviewAskIntent",
    "ReviewAskMessage",
    "ReviewAskRole",
    "ReviewAttribution",
    "ReviewBinding",
    "ReviewBindingSubjectKind",
    "ReviewCaptureKind",
    "ReviewCommit",
    "ReviewCommitState",
    "ReviewDecisionEvent",
    "ReviewDeletionTombstone",
    "ReviewDestination",
    "ReviewDestinationKind",
    "ReviewEvidence",
    "ReviewEvidenceKind",
    "ReviewEvidenceSource",
    "ReviewFrontier",
    "ReviewMark",
    "ReviewRevision",
    "ReviewSession",
    "ReviewSessionStatus",
    "ReviewSubjectType",
    "ReviewSynthesisRow",
    "ReviewUnit",
    "ReviewUnitKind",
    "SynthesisStatus",
    "anchor_symbol_claim",
]
