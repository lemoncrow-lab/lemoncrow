"""Read-only stack/dependency awareness (Phase 9 item 24).

LemonCrow shows where a Change sits in a stack -- its layer, the lower and
upper related Changes and their state -- and routes feedback on code a lower
layer introduced to the Change that owns it. It is **not** a stack-management
product: nothing here (or anywhere this module is called from) creates,
rebases, retargets or pushes a branch. Every projection carries
``mutation: "read_only"`` so consumers can assert it.

Two detectors feed one projection:

* **Hosted** (provider facts, ``lemoncrow_server.review.providers.stack``): a
  pull request whose base branch is another open pull request's head branch
  in the same repository is the upper layer of that pull request.
* **Local** (:func:`local_stack`): branch ancestry among captured Reviews. An
  open Review whose head commit is a strict, not-yet-integrated ancestor of
  this Review's head is a lower layer. When this Review's diff base is at (or
  above) that commit the lower layer's work is already excluded (``exact``);
  when the diff base is below it the lower work is inside this diff
  (``included``) and is attributed to the lower Change, file by file, instead
  of being presented as this layer's authored work.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.store import ReviewStore

STACK_CONTRACT_VERSION = 1

# lc-debt: local stack detection scans at most this many open Reviews and walks
# at most MAX_STACK_DEPTH layers with pygit2 ancestry checks (O(sessions x
# depth) graph walks). Upgrade path: persist detected edges as change
# relationships at capture time and read them instead of re-deriving.
MAX_LOCAL_CANDIDATES = 50
MAX_STACK_DEPTH = 8
MAX_HEADS_PER_REVIEW = 32

StackSource = Literal["provider", "local_ancestry"]
LowerRelation = Literal["exact", "included", "moved"]
BaseKind = Literal["trunk", "lower_change", "includes_lower_changes"]
ReadinessState = Literal["landed", "closed", "changes_requested", "approved", "in_review", "unknown"]


@dataclass(frozen=True)
class RelatedReadiness:
    """A related Change's review/landing state from durable facts (never a model call)."""

    state: ReadinessState
    label: str
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"state": self.state, "label": self.label, "detail": self.detail}


@dataclass(frozen=True)
class StackMember:
    position: Literal["lower", "upper"]
    distance: int
    """1 = directly below/above this Change."""
    review_id: str = ""
    title: str = ""
    provider: str = "git"
    external_id: str = ""
    url: str = ""
    head_ref: str = ""
    head_sha: str = ""
    status: str = ""
    readiness: RelatedReadiness = field(default_factory=lambda: RelatedReadiness("unknown", "Unknown"))
    relation: LowerRelation | Literal[""] = ""
    """Lower layers only: is this Change's diff base exactly at its head (``exact``)?"""
    owned_paths: tuple[str, ...] = ()
    """``included`` lower layers: paths of this diff that only the lower layer changed."""

    @property
    def change_ref(self) -> str:
        return f"r/{self.review_id}" if self.review_id else ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "distance": self.distance,
            "change_ref": self.change_ref,
            "review_id": self.review_id,
            "title": self.title,
            "provider": self.provider,
            "external_id": self.external_id,
            "url": self.url,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "status": self.status,
            "readiness": self.readiness.to_wire(),
            "relation": self.relation,
            "owned_paths": list(self.owned_paths[:200]),
        }


@dataclass(frozen=True)
class StackProjection:
    source: StackSource
    base_kind: BaseKind
    base_ref: str = ""
    base_sha: str = ""
    head_ref: str = ""
    lower: tuple[StackMember, ...] = ()
    upper: tuple[StackMember, ...] = ()
    note: str = ""
    complete: bool = True
    """False when a bound (candidate Reviews, stack depth) may have hidden a layer."""

    @property
    def layer(self) -> int:
        return len(self.lower) + 1

    @property
    def depth(self) -> int:
        return self.layer + max((member.distance for member in self.upper), default=0)

    @property
    def stacked(self) -> bool:
        return bool(self.lower or self.upper)

    def to_wire(self) -> dict[str, Any]:
        nearest = self.lower[0] if self.lower else None
        return {
            "version": STACK_CONTRACT_VERSION,
            "source": self.source,
            "mutation": "read_only",
            "layer": self.layer,
            "depth": self.depth,
            "base": {
                "kind": self.base_kind,
                "ref": self.base_ref,
                "sha": self.base_sha,
                "change_ref": nearest.change_ref if nearest is not None and self.base_kind != "trunk" else "",
            },
            "head_ref": self.head_ref,
            "lower": [member.to_wire() for member in self.lower],
            "upper": [member.to_wire() for member in self.upper],
            "note": self.note,
            "complete": self.complete,
        }


# --------------------------------------------------------------------------- #
# related Change readiness                                                    #
# --------------------------------------------------------------------------- #


def related_readiness(
    store: ReviewStore, session: ReviewSession | None, *, provider_state: str = ""
) -> RelatedReadiness:
    """Landing/review state of a related Change from durable rows only.

    Precedence: landed > closed > changes requested > approved > in review. An
    approval or changes request counts only on that Change's current Snapshot.
    """

    if session is None:
        if provider_state == "merged":
            return RelatedReadiness("landed", "Merged", "The provider reports this related change as merged.")
        if provider_state == "closed":
            return RelatedReadiness("closed", "Closed", "The provider reports this related change as closed.")
        return RelatedReadiness("unknown", "Not in LemonCrow", "No LemonCrow Review is linked to this change yet.")
    from lemoncrow.pro.capabilities.review.landing import landing_relationship

    landing = landing_relationship(store, session.id)
    landing_status = str(getattr(landing, "status", "") or "")
    if provider_state == "merged" or landing_status == "merged":
        return RelatedReadiness("landed", "Merged", "Landed; its code is now part of the base.")
    if provider_state == "closed" or session.status == "archived":
        return RelatedReadiness("closed", "Closed", "Closed or discarded without landing.")
    current = store.latest_revision(session.id)
    from lemoncrow.pro.capabilities.review.record_retraction import standing_outcomes

    outcomes = [
        item for item in standing_outcomes(store, session.id) if current is not None and item.revision_id == current.id
    ]
    if any(item.outcome == "changes_requested" for item in outcomes):
        return RelatedReadiness(
            "changes_requested", "Changes requested", "A reviewer requested changes on its current revision."
        )
    if any(item.outcome == "lgtm" for item in outcomes):
        detail = "Approved on its current revision"
        if landing_status in {"armed", "delegated", "queued"}:
            detail += f"; landing is {landing_status}"
        return RelatedReadiness("approved", "Approved", detail + ".")
    if session.status == "finished":
        return RelatedReadiness("in_review", "Review finished", "Review is finished; landing is not confirmed.")
    return RelatedReadiness("in_review", "In review", "Still in review.")


# --------------------------------------------------------------------------- #
# local detector: branch ancestry among captured Reviews                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Candidate:
    session: ReviewSession
    revision: ReviewRevision
    head_ref: str
    heads: tuple[str, ...]
    """Commits that are this Review's work: current head first, then checkpoints."""


def _open_repo(repo_root: str) -> Any | None:
    path = Path(repo_root)
    if not repo_root or not path.is_dir():
        return None
    try:
        import pygit2

        return pygit2.Repository(str(path))
    except Exception:
        return None


def _resolve(repo: Any, ref: str) -> str:
    if not ref:
        return ""
    try:
        import pygit2

        reference = repo.references.get(ref)
        if reference is None:
            return ""
        return str(reference.resolve().peel(pygit2.Commit).id)
    except Exception:
        return ""


def _commit_id(repo: Any, sha: str) -> Any | None:
    if not sha:
        return None
    try:
        import pygit2

        return repo.revparse_single(sha).peel(pygit2.Commit).id
    except Exception:
        return None


def _head_commit(repo: Any, revision: ReviewRevision) -> str:
    """The commit a Review's latest Snapshot is about: its head, else its branch tip."""

    if revision.range_mode == "commit_range" and revision.head_sha:
        return revision.head_sha
    return _resolve(repo, revision.branch_ref)


def _is_ancestor(repo: Any, ancestor: Any, descendant: Any) -> bool:
    if ancestor is None or descendant is None:
        return False
    if ancestor == descendant:
        return True
    try:
        return bool(repo.descendant_of(descendant, ancestor))
    except Exception:
        return False


def _integration_ids(repo: Any) -> tuple[Any, ...]:
    """Commits that are trunk, not a layer: remote integration tips plus local trunk branches.

    Local ``main``/``master`` count here (unlike Review base selection) so a
    Review of trunk commits is never mistaken for a lower layer of every
    feature branch. The cost of that choice is conservative: work parked on a
    local trunk branch is "not stacked" and keeps its feedback here.
    """

    from lemoncrow.pro.capabilities.review.gitdiff import _INTEGRATION_BRANCHES, _integration_tips

    tips: list[Any] = []
    try:
        tips.extend(_integration_tips(repo))
    except Exception:
        pass
    for branch in _INTEGRATION_BRANCHES:
        oid = _commit_id(repo, _resolve(repo, f"refs/heads/{branch}"))
        if oid is not None and oid not in tips:
            tips.append(oid)
    return tuple(tips)


def _candidates(store: ReviewStore, session: ReviewSession, repo: Any) -> tuple[list[_Candidate], bool]:
    """Open Reviews of the same repository (bounded) and whether the bound cut the list."""

    out: list[_Candidate] = []
    sessions = [
        item
        for item in store.list_sessions(status="open", limit=MAX_LOCAL_CANDIDATES + 2)
        if item.id != session.id and item.repo_root == session.repo_root
    ]
    truncated = len(sessions) > MAX_LOCAL_CANDIDATES
    for other in sessions[:MAX_LOCAL_CANDIDATES]:
        revision = store.latest_revision(other.id)
        if revision is None:
            continue
        head = _head_commit(repo, revision)
        if not head:
            continue
        # Every commit that was this Review's head: a layer based on an older
        # Snapshot (or an amended checkpoint) of it is still above it ("moved").
        heads = [head]
        try:
            history = sorted(store.list_revisions(other.id), key=lambda item: -item.revision_number)
            earlier = [item.commit_sha or item.head_sha for item in history[:MAX_HEADS_PER_REVIEW]]
            earlier += [item.commit_sha for item in store.list_commits(other.id)][:MAX_HEADS_PER_REVIEW]
        except Exception:
            earlier = []
        for sha in earlier:
            if sha and sha not in heads and len(heads) < MAX_HEADS_PER_REVIEW:
                heads.append(sha)
        out.append(_Candidate(other, revision, revision.branch_ref, tuple(heads)))
    return out, truncated


def _lowest_anchor(
    repo: Any, candidate: _Candidate, head: Any, integrated: Sequence[Any], *, exclude_ref: str
) -> Any | None:
    """The most-descended of *candidate*'s commits strictly below *head* and not integrated."""

    if exclude_ref and candidate.head_ref == exclude_ref:
        return None
    best: Any | None = None
    for sha in candidate.heads:
        oid = _commit_id(repo, sha)
        if oid is None or oid == head or not _is_ancestor(repo, oid, head):
            continue
        if any(_is_ancestor(repo, oid, tip) for tip in integrated):
            continue
        if best is None or _is_ancestor(repo, best, oid):
            best = oid
    return best


def _changed_paths(repo: Any, left: Any, right: Any) -> frozenset[str] | None:
    try:
        diff = repo.diff(repo.get(left), repo.get(right))
        paths: set[str] = set()
        for patch in diff:
            delta = patch.delta
            paths.add(str(delta.new_file.path))
            paths.add(str(delta.old_file.path))
        return frozenset(paths)
    except Exception:
        return None


_local_cache: OrderedDict[tuple[str, ...], StackProjection | None] = OrderedDict()
_local_lock = threading.Lock()
_LOCAL_CACHE_LIMIT = 64


def local_stack(store: ReviewStore, session: ReviewSession) -> StackProjection | None:
    """Branch-ancestry stack for a local Review; ``None`` when it is not stacked or not local.

    Read-only git: refs are resolved and ancestry is checked, nothing is written.
    """

    repo = _open_repo(session.repo_root)
    if repo is None:
        return None
    revision = store.latest_revision(session.id)
    if revision is None:
        return None
    head_sha = _head_commit(repo, revision)
    head = _commit_id(repo, head_sha)
    if head is None:
        return None
    candidates, truncated = _candidates(store, session, repo)
    if not candidates:
        return None
    integrated = _integration_ids(repo)
    key = (
        session.id,
        revision.id,
        str(head),
        str(truncated),
        str(MAX_STACK_DEPTH),
        *sorted(f"{item.session.id}:{item.revision.id}:{','.join(item.heads)}" for item in candidates),
        *sorted(str(tip) for tip in integrated),
    )
    with _local_lock:
        if key in _local_cache:
            _local_cache.move_to_end(key)
            return _local_cache[key]
    try:
        projection = _local_projection(
            store, session, revision, repo, head, candidates, integrated, truncated=truncated
        )
    except Exception:  # read-only context; a failed walk is "not stacked", never an error page
        logging.getLogger(__name__).warning("local stack detection failed for %s", session.id, exc_info=True)
        projection = None
    with _local_lock:
        _local_cache[key] = projection
        while len(_local_cache) > _LOCAL_CACHE_LIMIT:
            _local_cache.popitem(last=False)
    return projection


def _local_projection(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    repo: Any,
    head: Any,
    candidates: Sequence[_Candidate],
    integrated: Sequence[Any],
    *,
    truncated: bool = False,
) -> StackProjection | None:
    own_ref = revision.branch_ref
    base = _commit_id(repo, revision.merge_base_sha or revision.base_sha)

    # Lower chain: nearest lower layer first, then its own lower layer, ...
    lower: list[StackMember] = []
    seen = {session.id}
    cursor = head
    cursor_ref = own_ref
    for distance in range(1, MAX_STACK_DEPTH + 2):
        best: tuple[_Candidate, Any] | None = None
        for candidate in candidates:
            if candidate.session.id in seen:
                continue
            anchor = _lowest_anchor(repo, candidate, cursor, integrated, exclude_ref=cursor_ref)
            if anchor is not None and (best is None or _is_ancestor(repo, best[1], anchor)):
                best = (candidate, anchor)
        if best is None:
            break
        if distance > MAX_STACK_DEPTH:
            truncated = True  # one more layer exists below the bound: say so, never drop it silently
            break
        candidate, anchor = best
        seen.add(candidate.session.id)
        relation: LowerRelation | Literal[""] = ""
        owned: tuple[str, ...] = ()
        if distance == 1:
            if base is not None and _is_ancestor(repo, anchor, base):
                # Exact only while this diff's base *is* the lower Change's current head.
                current = _commit_id(repo, candidate.heads[0])
                relation = "exact" if anchor == base and current == anchor else "moved"
            else:
                relation = "included"
                lower_paths = _changed_paths(repo, base, anchor) if base is not None else None
                own_paths = _changed_paths(repo, anchor, head)
                if lower_paths is not None and own_paths is not None:
                    owned = tuple(sorted(lower_paths - own_paths))
        lower.append(
            StackMember(
                position="lower",
                distance=distance,
                review_id=candidate.session.id,
                title=candidate.session.title,
                provider="git",
                external_id=candidate.head_ref or str(anchor),
                head_ref=candidate.head_ref,
                head_sha=str(anchor),
                status=candidate.session.status,
                readiness=related_readiness(store, candidate.session),
                relation=relation,
                owned_paths=owned,
            )
        )
        cursor = anchor
        cursor_ref = candidate.head_ref

    # Upper layers: Reviews whose own lower layer is this Change.
    upper: list[StackMember] = []
    own_heads = [head]
    for sha in (checkpoint.commit_sha for checkpoint in store.list_commits(session.id, include_superseded=False)):
        oid = _commit_id(repo, sha)
        if oid is not None and oid not in own_heads:
            own_heads.append(oid)
    me = _Candidate(session, revision, own_ref, tuple(str(item) for item in own_heads))
    for candidate in candidates:
        their_head = _commit_id(repo, candidate.heads[0])
        if their_head is None:
            continue
        if _lowest_anchor(repo, me, their_head, integrated, exclude_ref=candidate.head_ref) is None:
            continue
        # Only *direct* upper layers: no other candidate sits between us.
        between = any(
            other.session.id != candidate.session.id
            and _lowest_anchor(repo, other, their_head, integrated, exclude_ref=candidate.head_ref) is not None
            and _lowest_anchor(repo, me, _commit_id(repo, other.heads[0]), integrated, exclude_ref=other.head_ref)
            is not None
            for other in candidates
        )
        if between:
            continue
        upper.append(
            StackMember(
                position="upper",
                distance=1,
                review_id=candidate.session.id,
                title=candidate.session.title,
                provider="git",
                external_id=candidate.head_ref or str(their_head),
                head_ref=candidate.head_ref,
                head_sha=str(their_head),
                status=candidate.session.status,
                readiness=related_readiness(store, candidate.session),
            )
        )
    if not lower and not upper and not truncated:
        return None
    nearest = lower[0] if lower else None
    if nearest is None:
        base_kind: BaseKind = "trunk"
        note = ""
    elif nearest.relation == "included":
        base_kind = "includes_lower_changes"
        # Exclusion is not attempted on a captured diff: a file both layers changed
        # cannot be split deterministically, so the limitation is stated instead.
        note = (
            f"This diff still contains {nearest.change_ref}'s commits (its base is below that layer). "
            "Files only that layer changed are attributed to it, not to this Change; "
            "files both layers changed are not split and show that layer's lines too. "
            f"Review against {nearest.head_ref or nearest.head_sha[:12]} to exclude that layer."
        )
    else:
        base_kind = "lower_change"
        note = (
            ""
            if nearest.relation == "exact"
            else f"{nearest.change_ref} has moved on since this layer was based on it."
        )
    return StackProjection(
        source="local_ancestry",
        base_kind=base_kind,
        base_ref=nearest.head_ref if nearest is not None else "",
        base_sha=revision.merge_base_sha or revision.base_sha,
        head_ref=own_ref,
        lower=tuple(lower),
        upper=tuple(sorted(upper, key=lambda item: item.review_id)),
        note=" ".join(
            part
            for part in (note, "Some layers may be missing: the stack is larger than shown." if truncated else "")
            if part
        ),
        complete=not truncated,
    )


# --------------------------------------------------------------------------- #
# projection entry point (local + provider)                                   #
# --------------------------------------------------------------------------- #

ProviderStackHook = Callable[[ReviewSession], "StackProjection | None"]


def stack_for(store: ReviewStore, session: ReviewSession) -> StackProjection | None:
    """Provider truth on a server store (its ``provider_stack`` hook), else local git ancestry.

    A store with the hook is authoritative: a server-backed Review's
    ``repo_root`` is an opaque identity, never a checkout to walk.
    """

    hook = getattr(store, "provider_stack", None)
    if callable(hook):
        try:
            provided = hook(session)
        except Exception:
            logging.getLogger(__name__).warning("provider stack unavailable for %s", session.id, exc_info=True)
            return None
        return provided if isinstance(provided, StackProjection) else None
    return local_stack(store, session)


# --------------------------------------------------------------------------- #
# ownership routing: feedback on lower-layer code goes to the lower Change    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OwningChange:
    review_id: str
    title: str
    reason: str

    @property
    def change_ref(self) -> str:
        return f"r/{self.review_id}"

    def to_wire(self) -> dict[str, Any]:
        return {"change_ref": self.change_ref, "review_id": self.review_id, "title": self.title, "reason": self.reason}


def _file_row(packet: Mapping[str, Any] | None, path: str) -> Mapping[str, Any] | None:
    if not isinstance(packet, Mapping):
        return None
    for row in packet.get("files") or ():
        if isinstance(row, Mapping) and str(row.get("path") or "") == path:
            return row
    return None


def _hunk_rows(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    hunks = row.get("hunks")
    return [hunk for hunk in hunks if isinstance(hunk, Mapping)] if isinstance(hunks, (list, tuple)) else []


def new_to_old_line(row: Mapping[str, Any] | None, line: int) -> int | None:
    """Map a new-side line of one file diff to its base-side line; ``None`` for a changed line.

    A file absent from the diff is unchanged (identity). Undecidable geometry
    (a hunk without its body) is ``None`` -- ownership stays with this Change.
    """

    if row is None:
        return line
    offset = 0
    for hunk in _hunk_rows(row):
        new_start = int(hunk.get("new_start") or 0)
        new_lines = int(hunk.get("new_lines") or 0)
        old_start = int(hunk.get("old_start") or 0)
        old_lines = int(hunk.get("old_lines") or 0)
        if line < new_start:
            break
        if line < new_start + new_lines:
            patch = hunk.get("patch")
            if not isinstance(patch, str) or not patch:
                return None
            new_no, old_no = new_start, old_start
            for text in patch.splitlines():
                origin = text[:1]
                if origin == "+":
                    if new_no == line:
                        return None
                    new_no += 1
                elif origin == "-":
                    old_no += 1
                elif origin in {"\\", "<", ">"}:
                    continue
                else:
                    if new_no == line:
                        return old_no
                    new_no += 1
                    old_no += 1
            return None
        offset = (old_start + old_lines) - (new_start + new_lines)
    return line + offset


def _added_new_lines(row: Mapping[str, Any]) -> frozenset[int] | None:
    """New-side line numbers the diff *added* (``+`` lines); ``None`` when a hunk body is missing.

    ``new_ranges`` is deliberately not used: it also holds deletion join points,
    which are pre-existing lines and must not move ownership.
    """

    added: set[int] = set()
    for hunk in _hunk_rows(row):
        patch = hunk.get("patch")
        if int(hunk.get("new_lines") or 0) and (not isinstance(patch, str) or not patch):
            return None
        new_no = int(hunk.get("new_start") or 0)
        for text in str(patch or "").splitlines():
            origin = text[:1]
            if origin == "+":
                added.add(new_no)
                new_no += 1
            elif origin in {"-", "\\", "<", ">"}:
                continue
            else:
                new_no += 1
    return frozenset(added)


class OwnershipResolver:
    """Which Change owns the code at an anchor of one Snapshot: this one, or a lower layer.

    Built once per request; the lower layer's Snapshot and packet are read at
    most once. Conservative: ownership moves only on exact evidence --

    * an ``included`` lower layer that alone changed the anchored file in this
      diff (:attr:`StackMember.owned_paths`), or
    * a lower layer whose current clean commit capture *is* this Snapshot's
      diff base, and whose own diff added the anchored base-side line.

    Anything else (moved lower layer, dirty capture, missing hunk body, a line
    this Change added) stays with this Change.
    """

    def __init__(
        self,
        store: ReviewStore,
        stack: StackProjection | None,
        revision: ReviewRevision,
        packet: Mapping[str, Any] | None,
    ) -> None:
        self._store = store
        self._stack = stack
        self._revision = revision
        self._packet = packet
        self._lower_packet: Mapping[str, Any] | None = None
        self._lower_loaded = False

    @property
    def active(self) -> bool:
        return self._stack is not None and bool(self._stack.lower) and bool(self._stack.lower[0].review_id)

    def _lower_geometry(self) -> Mapping[str, Any] | None:
        if self._lower_loaded:
            return self._lower_packet
        self._lower_loaded = True
        assert self._stack is not None
        nearest = self._stack.lower[0]
        diff_base = self._revision.merge_base_sha or self._revision.base_sha
        lower_revision = self._store.latest_revision(nearest.review_id)
        if (
            lower_revision is None
            or not diff_base
            or lower_revision.range_mode != "commit_range"
            or lower_revision.dirty
            or lower_revision.head_sha != diff_base
        ):
            # The lower layer moved (or is a draft): its line numbers do not describe our base.
            return None
        from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

        self._lower_packet = read_packet_json(self._store, lower_revision)
        return self._lower_packet

    def resolve(self, *, path: str, side: str, start_line: int) -> OwningChange | None:
        if not self.active or not path:
            return None
        assert self._stack is not None
        nearest = self._stack.lower[0]
        if nearest.relation == "included":
            if path in nearest.owned_paths:
                return OwningChange(
                    nearest.review_id, nearest.title, f"only {nearest.change_ref} changed {path} in this diff"
                )
            return None
        if start_line <= 0:
            return None
        base_line: int | None
        if side == "old":
            base_line = start_line
        elif side == "new":
            base_line = new_to_old_line(_file_row(self._packet, path), start_line)
        else:
            return None
        if base_line is None:
            return None
        row = _file_row(self._lower_geometry(), path)
        if row is None:
            return None
        added = _added_new_lines(row)
        if added is not None and base_line in added:
            return OwningChange(
                nearest.review_id,
                nearest.title,
                f"{path}:{base_line} was introduced by lower layer {nearest.change_ref}",
            )
        return None


def ownership_resolver(store: ReviewStore, session: ReviewSession, revision: ReviewRevision) -> OwnershipResolver:
    """Resolver for *revision* of *session*; inert when the Change is not above another layer."""

    try:
        stack = stack_for(store, session)
    except Exception:
        logging.getLogger(__name__).warning("stack unavailable for %s", session.id, exc_info=True)
        stack = None
    packet: Mapping[str, Any] | None = None
    if stack is not None and stack.lower:
        from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

        packet = read_packet_json(store, revision)
    return OwnershipResolver(store, stack, revision, packet)


__all__ = [
    "MAX_LOCAL_CANDIDATES",
    "MAX_STACK_DEPTH",
    "STACK_CONTRACT_VERSION",
    "OwnershipResolver",
    "OwningChange",
    "RelatedReadiness",
    "StackMember",
    "StackProjection",
    "local_stack",
    "new_to_old_line",
    "ownership_resolver",
    "related_readiness",
    "stack_for",
]
