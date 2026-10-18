"""Linked intent sources and immutable intent snapshots (Phase 10 items 1-2).

A Change may name the intent it implements: an issue/work item, a
specification/document, a plan, or an incident/change request. Sources are
the provider's linked issues (GitHub closing references and the linked-issues
API), explicit labeled links in the PR description, and ``lc review --intent``
for local Reviews.

Every capture is stored as an append-only ``ReviewEvidence`` row
(:data:`INTENT_EVIDENCE_KIND`) bound to the Snapshot it was captured on, with a
content digest, fetched-at timestamp, source revision and bounded text. The
first capture of a reference *binds* it: pre-merge checks read the bound text.
A later capture whose digest differs is recorded as an *observation*; the
Review then shows "intent changed since Snapshot N" and never silently
re-binds. Only an explicit human action (:func:`rebind_intent`) binds the newer
text.

The Change's own description (PR title/body) is different: it is part of the
Change under review, so the latest recorded description is what checks read.
Each description version is still its own immutable row with a digest.

All text here is untrusted data: PR descriptions and issue bodies are parsed
only by fixed patterns, never interpreted as instructions, and arbitrary links
are never fetched (only same-repository provider issues are).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence

IntentKind = Literal["issue", "spec", "plan", "incident"]
INTENT_KINDS: Final[tuple[str, ...]] = ("issue", "spec", "plan", "incident")
IntentOrigin = Literal["provider_linked", "closing_reference", "body_link", "cli", "human"]
INTENT_ORIGINS: Final[tuple[str, ...]] = ("provider_linked", "closing_reference", "body_link", "cli", "human")
IntentRole = Literal["bound", "observed", "description"]
#: ``ReviewEvidence.kind`` of every intent row (never a verification row).
INTENT_EVIDENCE_KIND: Final[str] = "intent"
#: ``source_ref`` prefix; keeps intent identities apart from check source refs.
INTENT_SOURCE_PREFIX: Final[str] = "intent:"
#: The Change's own description (PR title/body) identity.
DESCRIPTION_REF: Final[str] = "change:description"
MAX_INTENT_TEXT: Final[int] = 8000
MAX_INTENT_TITLE: Final[int] = 300
MAX_INTENT_SOURCES: Final[int] = 8
#: Only this much of a description is scanned for links (bounded parsing).
_MAX_SCAN: Final[int] = 65536

_ISSUE_REF = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d{1,9}"
_GITHUB_ISSUE_URL = r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d{1,9}"
_CLOSING = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?[ \t]+(?P<ref>" + _GITHUB_ISSUE_URL + "|" + _ISSUE_REF + r")\b"
)
_LABELED = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]*)?(?:\*\*)?(?P<label>specification|spec|design(?:[ \t]+doc)?|rfc|plan|incident|"
    r"change[ \t]+request|issue|work[ \t]+item|ticket)(?:\*\*)?[ \t]*:[ \t]*(?:\*\*)?[ \t]*(?P<ref>\S+)"
)
_LABEL_KIND: Final[Mapping[str, str]] = {
    "specification": "spec",
    "spec": "spec",
    "design": "spec",
    "design doc": "spec",
    "rfc": "spec",
    "plan": "plan",
    "incident": "incident",
    "change request": "incident",
    "issue": "issue",
    "work item": "issue",
    "ticket": "issue",
}
_SAFE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_./-]{1,300}$")


# --------------------------------------------------------------------------- #
# references
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IntentReference:
    """One declared intent link before anything was fetched.

    ``ref`` is the stable identity (``github:owner/repo#12``, ``url:https://…``,
    ``file:docs/spec.md`` or ``issue:#12``); ``url`` is presentation only.
    """

    kind: str
    ref: str
    origin: str
    url: str = ""
    title: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "origin": self.origin, "url": self.url, "title": self.title}


def normalize_reference(raw: str, *, repository: str = "") -> tuple[str, str] | None:
    """``(ref, url)`` for one link text, or ``None`` when it names nothing usable.

    ``repository`` (``owner/repo``) qualifies a bare ``#123``.
    """

    value = raw.strip().strip("<>()[],.;").strip()
    if not value or len(value) > 500:
        return None
    url_match = re.fullmatch(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d{1,9})", value)
    if url_match is not None:
        owner_repo, number = url_match.group(1), url_match.group(2)
        return f"github:{owner_repo.lower()}#{number}", f"https://github.com/{owner_repo}/issues/{number}"
    issue_match = re.fullmatch(r"(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d{1,9})", value)
    if issue_match is not None:
        owner_repo = issue_match.group(1) or repository
        number = issue_match.group(2)
        if owner_repo:
            return f"github:{owner_repo.lower()}#{number}", f"https://github.com/{owner_repo}/issues/{number}"
        return f"issue:#{number}", ""
    if re.match(r"(?i)^https?://", value):
        cleaned = value.split("#", 1)[0]
        return f"url:{cleaned}", cleaned
    if _SAFE_PATH.fullmatch(value) and ("/" in value or "." in value):
        return f"file:{value}", ""
    return None


def parse_intent_references(body: str, *, repository: str = "") -> tuple[IntentReference, ...]:
    """Links a PR description declares, by fixed patterns only.

    GitHub closing keywords (``Closes #12``) name issues; labeled lines
    (``Spec: docs/auth.md``, ``Incident: https://…``) name the other kinds.
    The text is data: nothing in it can change what is parsed or how.
    """

    text = (body or "")[:_MAX_SCAN]
    found: dict[str, IntentReference] = {}
    for match in _CLOSING.finditer(text):
        normalized = normalize_reference(match.group("ref"), repository=repository)
        if normalized is not None and normalized[0] not in found:
            found[normalized[0]] = IntentReference("issue", normalized[0], "closing_reference", normalized[1])
    for match in _LABELED.finditer(text):
        label = re.sub(r"[ \t]+", " ", match.group("label").lower())
        normalized = normalize_reference(match.group("ref"), repository=repository)
        if normalized is not None and normalized[0] not in found:
            found[normalized[0]] = IntentReference(
                _LABEL_KIND.get(label, "issue"), normalized[0], "body_link", normalized[1]
            )
    return tuple(found.values())[:MAX_INTENT_SOURCES]


def parse_cli_intent(spec: str, *, repository: str = "") -> IntentReference:
    """``KIND:REF`` from ``lc review --intent`` (``spec:docs/auth.md``, ``issue:#12``)."""

    kind, separator, raw = spec.partition(":")
    kind = kind.strip().lower()
    if not separator or kind not in INTENT_KINDS:
        raise ValueError(f"--intent must be KIND:REF with KIND one of {', '.join(INTENT_KINDS)}")
    normalized = normalize_reference(raw, repository=repository)
    if normalized is None:
        raise ValueError(f"--intent {spec!r} names no URL, issue reference or repository path")
    return IntentReference(kind, normalized[0], "cli", normalized[1])


# --------------------------------------------------------------------------- #
# captured content
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IntentContent:
    """Fetched text of one source at one moment (``source_revision``: e.g. its ``updated_at``)."""

    title: str
    body: str
    source_revision: str = ""
    fetched_at: str = ""
    url: str = ""


@dataclass(frozen=True, slots=True)
class IntentObservation:
    """What one capture saw for one reference; ``content`` ``None``: reference only."""

    reference: IntentReference
    content: IntentContent | None = None
    note: str = ""
    #: The provider reports the source deleted (or moved away): drift, never a re-bind.
    deleted: bool = False


def _instant(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _older(incoming: str, stored: str) -> bool:
    """Whether a provider ``source_revision`` (e.g. ``updated_at``) predates the stored one."""

    left, right = _instant(incoming), _instant(stored)
    return left is not None and right is not None and left < right


def _normalized_text(value: str) -> str:
    return "\n".join(
        line.rstrip() for line in (value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()


def intent_digest(title: str, body: str) -> str:
    """Content identity of one captured source: ``sha256:`` over normalized title and body."""

    payload = json.dumps(
        {"title": _normalized_text(title), "body": _normalized_text(body)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bounded_text(value: str, limit: int = MAX_INTENT_TEXT) -> tuple[str, bool]:
    text = _normalized_text(value)
    return (text, False) if len(text) <= limit else (text[:limit], True)


@dataclass(frozen=True, slots=True)
class IntentSnapshot:
    """One immutable captured intent (or description) row, read back."""

    evidence_id: str
    review_id: str
    revision_id: str
    ref: str
    kind: str
    origin: str
    role: str
    title: str
    text: str
    digest: str
    state: Literal["captured", "unavailable", "deleted"]
    truncated: bool = False
    source_revision: str = ""
    fetched_at: str = ""
    url: str = ""
    note: str = ""
    actor: str = ""
    created_at: str = ""

    @property
    def captured(self) -> bool:
        return self.state == "captured"

    def to_wire(self, *, snapshot_number: int | None = None, include_text: bool = False) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "evidence_id": self.evidence_id,
            "revision_id": self.revision_id,
            "snapshot_number": snapshot_number,
            "ref": self.ref,
            "kind": self.kind,
            "origin": self.origin,
            "role": self.role,
            "title": self.title,
            "digest": self.digest,
            "state": self.state,
            "truncated": self.truncated,
            "source_revision": self.source_revision,
            "fetched_at": self.fetched_at,
            "url": self.url,
            "note": self.note,
            "actor": self.actor,
            "created_at": self.created_at,
        }
        if include_text:
            wire["text"] = self.text
        return wire


def _evidence_row(
    *,
    review_id: str,
    revision_id: str,
    ref: str,
    kind: str,
    origin: str,
    role: str,
    content: IntentContent | None,
    url: str,
    note: str,
    actor: str,
    source: Literal["human", "external"],
    preserve: IntentSnapshot | None = None,
    deleted: bool = False,
) -> ReviewEvidence:
    title = (content.title if content is not None else "").strip()[:MAX_INTENT_TITLE]
    text, truncated = bounded_text(content.body if content is not None else "")
    digest = intent_digest(content.title, content.body) if content is not None else ""
    if preserve is not None and preserve.captured:
        # A re-bind copies a capture: it keeps that capture's digest (taken over
        # the full source text) so later identical fetches are not new drift.
        digest, truncated = preserve.digest, preserve.truncated
    detail = {
        "v": 1,
        "role": role,
        "kind": kind,
        "origin": origin,
        "text": text,
        "truncated": truncated,
        "state": "deleted" if deleted else "captured" if content is not None else "unavailable",
        "source_revision": (content.source_revision if content is not None else "")[:200],
        "fetched_at": (content.fetched_at if content is not None else "")[:64],
        "note": note[:500],
        # The digest lives here, not in ``content_hash``: that column names a
        # stored artifact blob everywhere else, and intent text is inline.
        "digest": digest,
    }
    return ReviewEvidence(
        id="",
        review_id=review_id,
        revision_id=revision_id,
        kind=INTENT_EVIDENCE_KIND,  # type: ignore[arg-type]
        title=title or ref,
        url=((content.url if content is not None and content.url else url) or "")[:1000],
        source=source,
        source_ref=f"{INTENT_SOURCE_PREFIX}{ref}"[:500],
        detail=json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        stable_key=role,
        attested_by=actor[:200],
        provider_revision=(content.source_revision if content is not None else "")[:200],
        observed_at=(content.fetched_at if content is not None else "")[:64],
    )


def snapshot_from_evidence(item: ReviewEvidence) -> IntentSnapshot | None:
    """Read one intent row back; anything else (or a malformed row) is ``None``."""

    if item.kind != INTENT_EVIDENCE_KIND or not item.source_ref.startswith(INTENT_SOURCE_PREFIX):
        return None
    try:
        detail = json.loads(item.detail or "{}")
    except ValueError:
        return None
    if not isinstance(detail, Mapping):
        return None
    role = str(detail.get("role") or item.stable_key or "")
    if role not in {"bound", "observed", "description"}:
        return None
    digest = str(detail.get("digest") or "")
    raw_state = str(detail.get("state") or "")
    state = "captured" if raw_state == "captured" and digest else "deleted" if raw_state == "deleted" else "unavailable"
    return IntentSnapshot(
        evidence_id=item.id,
        review_id=item.review_id,
        revision_id=item.revision_id,
        ref=item.source_ref.removeprefix(INTENT_SOURCE_PREFIX),
        kind=str(detail.get("kind") or "issue"),
        origin=str(detail.get("origin") or ""),
        role=role,
        title=item.title,
        text=str(detail.get("text") or ""),
        digest=digest,
        state=state,  # type: ignore[arg-type]
        truncated=bool(detail.get("truncated")),
        source_revision=str(detail.get("source_revision") or item.provider_revision or ""),
        fetched_at=str(detail.get("fetched_at") or item.observed_at or ""),
        url=item.url,
        note=str(detail.get("note") or ""),
        actor=item.attested_by,
        created_at=item.created_at,
    )


def intent_snapshots(evidence: Iterable[ReviewEvidence]) -> tuple[IntentSnapshot, ...]:
    """Every intent row, oldest first (by receipt, then id)."""

    rows = [snapshot for item in evidence if (snapshot := snapshot_from_evidence(item)) is not None]
    rows.sort(key=lambda row: (row.created_at, row.evidence_id))
    return tuple(rows)


# --------------------------------------------------------------------------- #
# bindings: what checks read, and drift
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IntentBinding:
    """One reference: the bound capture checks read and the newest observation."""

    bound: IntentSnapshot
    latest: IntentSnapshot
    bound_snapshot_number: int | None = None
    latest_snapshot_number: int | None = None

    @property
    def ref(self) -> str:
        return self.bound.ref

    @property
    def kind(self) -> str:
        return self.bound.kind

    @property
    def deleted(self) -> bool:
        """The provider reports the bound source deleted since it was captured."""

        return self.latest.evidence_id != self.bound.evidence_id and self.latest.state == "deleted"

    @property
    def changed(self) -> bool:
        """The source now reads differently from what this Review is bound to (or is gone)."""

        if not self.bound.captured or self.latest.evidence_id == self.bound.evidence_id:
            return False
        return self.deleted or (self.latest.captured and self.latest.digest != self.bound.digest)

    @property
    def changed_note(self) -> str:
        if not self.changed:
            return ""
        where = f"Snapshot {self.bound_snapshot_number}" if self.bound_snapshot_number is not None else "capture"
        if self.deleted:
            return (
                f"Intent changed since {where}: {self.bound.title or self.ref} was deleted on the provider. "
                f"Checks stay bound to the captured text ({self.bound.digest[:19]})."
            )
        return (
            f"Intent changed since {where}: {self.bound.title or self.ref} now reads differently "
            f"({self.latest.digest[:19]}). Checks stay bound to the captured text ({self.bound.digest[:19]}) "
            "until a reviewer re-binds it."
        )

    def to_wire(self, *, include_text: bool = False) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "origin": self.bound.origin,
            "bound": self.bound.to_wire(snapshot_number=self.bound_snapshot_number, include_text=include_text),
            "latest": (
                None
                if self.latest.evidence_id == self.bound.evidence_id
                else self.latest.to_wire(snapshot_number=self.latest_snapshot_number)
            ),
            "changed": self.changed,
            "deleted": self.deleted,
            "changed_note": self.changed_note,
        }


def intent_bindings(
    evidence: Iterable[ReviewEvidence],
    snapshot_numbers: Mapping[str, int] | None = None,
) -> tuple[IntentBinding, ...]:
    """Bindings per reference, in first-capture order.

    The bound capture is the newest ``bound`` row (the first capture, or a
    human re-bind); the latest is the newest row of any role for that ref.
    """

    numbers = snapshot_numbers or {}
    by_ref: dict[str, list[IntentSnapshot]] = {}
    for row in intent_snapshots(evidence):
        if row.role == "description":
            continue
        by_ref.setdefault(row.ref, []).append(row)
    out: list[IntentBinding] = []
    for rows in by_ref.values():
        bound_rows = [row for row in rows if row.role == "bound"]
        if not bound_rows:
            continue  # observations without a binding never bind themselves
        bound = bound_rows[-1]
        latest = rows[-1]
        out.append(
            IntentBinding(
                bound=bound,
                latest=latest,
                bound_snapshot_number=numbers.get(bound.revision_id),
                latest_snapshot_number=numbers.get(latest.revision_id),
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ChangeDescription:
    """The Change's own title/body as last recorded, with its digest."""

    title: str
    body: str
    digest: str
    evidence_id: str
    revision_id: str
    origin: str
    truncated: bool = False

    def to_wire(self, *, snapshot_number: int | None = None) -> dict[str, Any]:
        return {
            "title": self.title,
            "digest": self.digest,
            "evidence_id": self.evidence_id,
            "revision_id": self.revision_id,
            "snapshot_number": snapshot_number,
            "origin": self.origin,
            "truncated": self.truncated,
            "body_chars": len(self.body),
        }


def latest_description(evidence: Iterable[ReviewEvidence]) -> ChangeDescription | None:
    rows = [row for row in intent_snapshots(evidence) if row.role == "description" and row.captured]
    if not rows:
        return None
    row = rows[-1]
    return ChangeDescription(
        title=row.title,
        body=row.text,
        digest=row.digest,
        evidence_id=row.evidence_id,
        revision_id=row.revision_id,
        origin=row.origin,
        truncated=row.truncated,
    )


# --------------------------------------------------------------------------- #
# writers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IntentRecordResult:
    bound: tuple[str, ...] = ()
    observed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    description: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "bound": list(self.bound),
            "observed": list(self.observed),
            "unchanged": list(self.unchanged),
            "description": self.description,
        }


def record_intent(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    observations: Sequence[IntentObservation] = (),
    description: IntentContent | None = None,
    description_origin: str = "provider_linked",
    actor: str = "",
    source: Literal["human", "external"] = "external",
) -> IntentRecordResult:
    """Append what one capture saw; never rewrites or re-binds an earlier capture.

    Per reference: no binding yet -> the capture binds (a reference-only
    binding is replaced by the first readable capture, since nothing was bound
    to); a digest equal to the newest row -> nothing written; text older than
    the newest row (by provider ``source_revision``, an out-of-order delivery)
    -> nothing written; a provider deletion of a bound source -> one
    ``observed`` deleted row; otherwise an ``observed`` row (drift). The
    description is written when its digest differs from the newest recorded
    description.
    """

    existing = intent_snapshots(store.list_evidence(review_id))
    bound: list[str] = []
    observed: list[str] = []
    unchanged: list[str] = []
    for observation in list(observations)[:MAX_INTENT_SOURCES]:
        reference = observation.reference
        if reference.kind not in INTENT_KINDS or not reference.ref:
            continue
        rows = [row for row in existing if row.ref == reference.ref and row.role != "description"]
        bindings = [row for row in rows if row.role == "bound"]
        newest = rows[-1] if rows else None
        content = observation.content
        if observation.deleted:
            if not bindings or newest is None or newest.state == "deleted":
                unchanged.append(reference.ref)  # nothing bound, or already recorded
                continue
            store.add_evidence(
                _evidence_row(
                    review_id=review_id,
                    revision_id=revision_id,
                    ref=reference.ref,
                    kind=reference.kind,
                    origin=reference.origin,
                    role="observed",
                    content=None,
                    url=reference.url or newest.url,
                    note=observation.note or "deleted on the provider",
                    actor=actor,
                    source=source,
                    deleted=True,
                )
            )
            observed.append(reference.ref)
            continue
        digest = intent_digest(content.title, content.body) if content is not None else ""
        if bindings and newest is not None and (newest.digest == digest or (content is None and not newest.digest)):
            unchanged.append(reference.ref)
            continue
        if newest is not None and content is not None and _older(content.source_revision, newest.source_revision):
            unchanged.append(reference.ref)  # an out-of-order older delivery never reads as drift
            continue
        if bindings and content is None:
            unchanged.append(reference.ref)  # a failed refetch never displaces readable text
            continue
        role: IntentRole = "bound" if not bindings or not bindings[-1].captured else "observed"
        store.add_evidence(
            _evidence_row(
                review_id=review_id,
                revision_id=revision_id,
                ref=reference.ref,
                kind=reference.kind,
                origin=reference.origin,
                role=role,
                content=content,
                url=reference.url,
                note=observation.note,
                actor=actor,
                source=source,
            )
        )
        (bound if role == "bound" else observed).append(reference.ref)
    description_id = ""
    if description is not None and (description.title.strip() or description.body.strip()):
        digest = intent_digest(description.title, description.body)
        previous = [row for row in existing if row.role == "description"]
        if not previous or previous[-1].digest != digest:
            record = store.add_evidence(
                _evidence_row(
                    review_id=review_id,
                    revision_id=revision_id,
                    ref=DESCRIPTION_REF,
                    kind="issue",
                    origin=description_origin,
                    role="description",
                    content=description,
                    url=description.url,
                    note="",
                    actor=actor,
                    source=source,
                )
            )
            description_id = str(record.id)
    return IntentRecordResult(tuple(bound), tuple(observed), tuple(unchanged), description_id)


def rebind_intent(store: Any, *, review_id: str, revision_id: str, ref: str, actor: str) -> IntentSnapshot:
    """A human explicitly binds the newest captured text of one reference.

    Raises ``LookupError`` when the reference is unknown or nothing newer is
    captured. The previous binding stays in history; checks read the new one.
    """

    bindings = {item.ref: item for item in intent_bindings(store.list_evidence(review_id))}
    binding = bindings.get(ref)
    if binding is None:
        raise LookupError(f"no intent {ref!r} is linked to this Review")
    if binding.deleted:
        raise LookupError(f"intent {ref!r} was deleted on the provider; there is no newer text to bind")
    if not binding.changed:
        raise LookupError(f"intent {ref!r} has no newer captured text to bind")
    latest = binding.latest
    record = store.add_evidence(
        _evidence_row(
            review_id=review_id,
            revision_id=revision_id,
            ref=ref,
            kind=latest.kind,
            origin="human",
            role="bound",
            content=IntentContent(
                title=latest.title,
                body=latest.text,
                source_revision=latest.source_revision,
                fetched_at=latest.fetched_at,
                url=latest.url,
            ),
            url=latest.url,
            note=f"re-bound from observation {latest.evidence_id}",
            actor=actor,
            source="human",
            preserve=latest,
        )
    )
    snapshot = snapshot_from_evidence(record)
    if snapshot is None:  # pragma: no cover - the row was just written by this module
        raise LookupError("re-bound intent could not be read back")
    return snapshot


__all__ = [
    "DESCRIPTION_REF",
    "INTENT_EVIDENCE_KIND",
    "INTENT_KINDS",
    "INTENT_ORIGINS",
    "INTENT_SOURCE_PREFIX",
    "MAX_INTENT_SOURCES",
    "MAX_INTENT_TEXT",
    "ChangeDescription",
    "IntentBinding",
    "IntentContent",
    "IntentKind",
    "IntentObservation",
    "IntentOrigin",
    "IntentRecordResult",
    "IntentReference",
    "IntentRole",
    "IntentSnapshot",
    "bounded_text",
    "intent_bindings",
    "intent_digest",
    "intent_snapshots",
    "latest_description",
    "normalize_reference",
    "parse_cli_intent",
    "parse_intent_references",
    "rebind_intent",
    "record_intent",
    "snapshot_from_evidence",
]
