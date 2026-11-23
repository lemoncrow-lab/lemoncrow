"""Cross-repository Review Inbox views, selection and pagination (Phase 9 item 1, P9-INBOX).

Views are *membership* over lightweight per-Review facts, never a second
state machine:

* ``to_review`` -- open, and the viewer holds a reviewer-side ReviewAttention
  reason (requested, work returned, thread returned);
* ``assigned_to_my_teams`` -- open, with an active review request to one of the
  viewer's teams (an actual request; a reviewer *suggestion* never counts);
* ``authored_by_me`` -- the viewer authored the Change;
* ``participated`` -- the viewer took part in someone else's Review (a role, a
  request, a mark, an outcome or a comment); authorship alone is its own view;
* ``waiting_on_me`` -- open, and the viewer holds any attention reason;
* ``ready`` -- workflow state ``ready_to_merge`` (readiness record only);
* ``blocked`` -- open, waiting on someone or something other than the viewer
  (reviewer, author/agent, checks, a decision).

Access is applied by the caller *before* rows reach this module: a row exists
only for a Review in a repository the viewer can read, so view counts and
saved filters can never reveal or widen access.

Pagination is keyset over most-recently-updated first, with repository and Review id
as stable tie-breakers. A cursor carries the ``as_of`` instant used to score
attention on the first page so later pages use the same clock; it is bounded
in size and rejected when malformed. ``limit`` has an explicit range and
out-of-range values are errors; non-final pages return ``next_cursor``.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from lemoncrow.pro.capabilities.review.freshness import format_timestamp, parse_timestamp

InboxView = Literal[
    "all",
    "to_review",
    "assigned_to_my_teams",
    "authored_by_me",
    "participated",
    "waiting_on_me",
    "ready",
    "blocked",
]
INBOX_VIEWS: Final[tuple[str, ...]] = (
    "to_review",
    "assigned_to_my_teams",
    "authored_by_me",
    "participated",
    "waiting_on_me",
    "ready",
    "blocked",
)
VIEW_LABELS: Final[Mapping[str, str]] = {
    "all": "All",
    "to_review": "To review",
    "assigned_to_my_teams": "Assigned to my teams",
    "authored_by_me": "Authored by me",
    "participated": "Participated",
    "waiting_on_me": "Waiting on me",
    "ready": "Ready",
    "blocked": "Blocked / waiting",
}
INBOX_STATUSES: Final[tuple[str, ...]] = ("all", "open", "finished", "archived")
_BLOCKED_STATES: Final[frozenset[str]] = frozenset(
    {"waiting_on_reviewer", "waiting_on_author", "waiting_on_checks", "needs_decision"}
)

INBOX_DEFAULT_LIMIT: Final[int] = 30
INBOX_MAX_LIMIT: Final[int] = 100
#: What Home reads in one request (``read=home``), instead of five pages that each score every Review again (P9-6).
INBOX_HOME_LIMIT: Final[int] = 500
INBOX_READS: Final[tuple[str, ...]] = ("page", "home")
INBOX_MAX_QUERY: Final[int] = 256
INBOX_MAX_CURSOR: Final[int] = 1024
CURSOR_VERSION: Final[int] = 2  # v1 used attention ranking and cannot page updated-first rows


class InboxQueryError(ValueError):
    """A malformed Inbox query parameter; ``field`` names it."""

    def __init__(self, field: str, message: str, *, limit: int | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.limit = limit


@dataclass(frozen=True, slots=True)
class ViewFacts:
    """What view membership reads for one Review and one viewer."""

    open: bool
    reviewer_turn_for_me: bool = False
    team_request: bool = False
    authored: bool = False
    participated: bool = False
    attention_for_me: bool = False
    workflow_state: str = "needs_review"


def inbox_views(facts: ViewFacts) -> tuple[str, ...]:
    """Every view this Review belongs to for this viewer, in :data:`INBOX_VIEWS` order."""

    member = {
        "to_review": facts.open and facts.reviewer_turn_for_me,
        "assigned_to_my_teams": facts.open and facts.team_request,
        "authored_by_me": facts.authored,
        "participated": facts.participated,
        "waiting_on_me": facts.open and facts.attention_for_me,
        "ready": facts.workflow_state == "ready_to_merge",
        "blocked": facts.open and not facts.attention_for_me and facts.workflow_state in _BLOCKED_STATES,
    }
    return tuple(view for view in INBOX_VIEWS if member[view])


@dataclass(frozen=True)
class InboxRow:
    """One Review as the Inbox selects, counts, orders and pages it."""

    repo_id: str
    review_id: str
    status: str
    views: tuple[str, ...]
    workflow_state: str
    priority: str
    sort_key: tuple[Any, ...]
    haystack: str = ""
    team_keys: frozenset[str] = frozenset()
    wire: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboxSelection:
    """The scope one Inbox read applies (a saved filter is one of these, never more)."""

    view: str = "all"
    status: str = "all"
    repos: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    workflow_states: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()
    query: str = ""


def parse_view(raw: str) -> str:
    value = (raw or "all").strip() or "all"
    if value != "all" and value not in INBOX_VIEWS:
        raise InboxQueryError("view", "unknown Review Inbox view")
    return value


def parse_status(raw: str) -> str:
    value = (raw or "all").strip() or "all"
    if value not in INBOX_STATUSES:
        raise InboxQueryError("status", "status must be one of all, open, finished, archived")
    return value


def parse_query(raw: str) -> str:
    value = (raw or "").strip().lower()
    if len(value) > INBOX_MAX_QUERY:
        raise InboxQueryError("q", f"Review Inbox search exceeds {INBOX_MAX_QUERY} characters", limit=INBOX_MAX_QUERY)
    return value


def parse_read(raw: str | None) -> str:
    """``page`` (the default) or ``home``: how much one request may return."""

    value = (raw or "page").strip() or "page"
    if value not in INBOX_READS:
        raise InboxQueryError("read", "read must be page or home")
    return value


def parse_limit(raw: str | None, *, default: int = INBOX_DEFAULT_LIMIT, read: str = "page") -> int:
    """``1..INBOX_MAX_LIMIT`` (``1..INBOX_HOME_LIMIT`` for a Home read); anything else is an error, never a silent clamp."""

    maximum = INBOX_HOME_LIMIT if read == "home" else INBOX_MAX_LIMIT
    if raw is None or str(raw).strip() == "":
        return INBOX_HOME_LIMIT if read == "home" else default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise InboxQueryError("limit", "limit must be an integer") from exc
    if value < 1 or value > maximum:
        raise InboxQueryError("limit", f"limit must be between 1 and {maximum}", limit=maximum)
    return value


def updated_sort_key(updated_at: str, *, repo_id: str, review_id: str) -> tuple[int, str, str, str]:
    """Newest Review update first, exact to microseconds, with deterministic ties."""

    stamp = parse_timestamp(updated_at)
    if stamp is None:
        rank = 2**63 - 1  # Missing/invalid dates sort last.
    else:
        delta = stamp - datetime(1970, 1, 1, tzinfo=UTC)
        rank = -((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds)
    return (rank, "", repo_id, review_id)


@dataclass(frozen=True, slots=True)
class InboxCursor:
    as_of: str
    after: tuple[Any, ...]


def encode_cursor(as_of: datetime, after: Sequence[Any]) -> str:
    payload = json.dumps(
        {"v": CURSOR_VERSION, "as_of": format_timestamp(as_of), "after": list(after)},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> InboxCursor | None:
    """``None`` for an empty cursor; :class:`InboxQueryError` for anything malformed or oversized."""

    if not raw:
        return None
    if len(raw) > INBOX_MAX_CURSOR:
        raise InboxQueryError("cursor", "Review Inbox cursor is invalid", limit=INBOX_MAX_CURSOR)
    try:
        padding = "=" * ((4 - (len(raw) % 4)) % 4)
        value = json.loads(base64.urlsafe_b64decode((raw + padding).encode("ascii")).decode("utf-8"))
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise InboxQueryError("cursor", "Review Inbox cursor is invalid") from exc
    if not isinstance(value, dict) or value.get("v") != CURSOR_VERSION:
        raise InboxQueryError("cursor", "Review Inbox cursor is invalid")
    after = value.get("after")
    as_of = value.get("as_of")
    if (
        not isinstance(after, list)
        or len(after) != 4
        or not isinstance(after[0], int)
        or isinstance(after[0], bool)
        or not all(isinstance(item, str) for item in after[1:])
        or not isinstance(as_of, str)
        or parse_timestamp(as_of) is None
    ):
        raise InboxQueryError("cursor", "Review Inbox cursor is invalid")
    return InboxCursor(as_of=as_of, after=tuple(after))


def _matches(row: InboxRow, selection: InboxSelection) -> bool:
    if selection.status != "all" and row.status != selection.status:
        return False
    if selection.repos and row.repo_id not in selection.repos:
        return False
    if selection.teams and not (row.team_keys & frozenset(selection.teams)):
        return False
    if selection.workflow_states and row.workflow_state not in selection.workflow_states:
        return False
    if selection.priorities and row.priority not in selection.priorities:
        return False
    return not selection.query or all(term in row.haystack for term in selection.query.split())


def narrow(base: InboxSelection, extra: InboxSelection) -> InboxSelection:
    """Intersect a saved filter with the request's own scope; neither can widen the other."""

    def both(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
        if not left:
            return right
        if not right:
            return left
        kept = tuple(item for item in left if item in right)
        # An empty intersection must still exclude everything, never mean "no filter".
        return kept or ("\0none",)

    def one(left: str, right: str, neutral: str) -> str:
        if left == neutral:
            return right
        if right in {neutral, left}:
            return left
        return "\0none"

    query = " ".join(part for part in (base.query, extra.query) if part)
    return InboxSelection(
        view=one(base.view, extra.view, "all"),
        status=one(base.status, extra.status, "all"),
        repos=both(base.repos, extra.repos),
        teams=both(base.teams, extra.teams),
        workflow_states=both(base.workflow_states, extra.workflow_states),
        priorities=both(base.priorities, extra.priorities),
        query=query,
    )


@dataclass(frozen=True)
class InboxPage:
    """One page; ``total`` is every row of the chosen view (all pages), ``counts`` every view's."""

    rows: tuple[InboxRow, ...]
    counts: dict[str, int]
    next_cursor: str
    total: int


def select_page(
    rows: Iterable[InboxRow],
    selection: InboxSelection,
    *,
    limit: int,
    cursor: InboxCursor | None,
    as_of: datetime,
) -> InboxPage:
    """Filter, count, order and page. Counts span every view of the filtered rows."""

    scoped = [row for row in rows if _matches(row, selection)]
    counts = {view: sum(view in row.views for row in scoped) for view in INBOX_VIEWS}
    counts["all"] = len(scoped)
    if selection.view == "all":
        chosen = scoped
    else:
        chosen = [row for row in scoped if selection.view in row.views]
    chosen.sort(key=lambda row: row.sort_key)
    total = len(chosen)
    if cursor is not None:
        chosen = [row for row in chosen if row.sort_key > cursor.after]
    page = chosen[:limit]
    next_cursor = encode_cursor(as_of, page[-1].sort_key) if len(chosen) > limit and page else ""
    return InboxPage(rows=tuple(page), counts=counts, next_cursor=next_cursor, total=total)


def inbox_as_of(cursor: InboxCursor | None, now: datetime) -> datetime:
    """The scoring instant: the first page's, carried by the cursor, else ``now``."""

    # Second precision, the precision a cursor carries, so page one and page two score alike.
    if cursor is None:
        return now.replace(microsecond=0)
    parsed = parse_timestamp(cursor.as_of)
    return now.replace(microsecond=0) if parsed is None else parsed


__all__ = [
    "CURSOR_VERSION",
    "INBOX_DEFAULT_LIMIT",
    "INBOX_HOME_LIMIT",
    "INBOX_MAX_CURSOR",
    "INBOX_MAX_LIMIT",
    "INBOX_MAX_QUERY",
    "INBOX_READS",
    "INBOX_STATUSES",
    "INBOX_VIEWS",
    "VIEW_LABELS",
    "InboxCursor",
    "InboxPage",
    "InboxQueryError",
    "InboxRow",
    "InboxSelection",
    "InboxView",
    "ViewFacts",
    "decode_cursor",
    "encode_cursor",
    "inbox_as_of",
    "inbox_views",
    "narrow",
    "parse_limit",
    "parse_query",
    "parse_read",
    "parse_status",
    "parse_view",
    "select_page",
    "updated_sort_key",
]
