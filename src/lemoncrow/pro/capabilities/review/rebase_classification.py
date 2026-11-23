"""Authored change vs base-only movement between two Snapshots (Phase 9 item 34).

A rebase, a merge of the integration branch, or a lower stack layer moving
underneath a Change all produce a new Snapshot whose bytes differ from the one a
reviewer judged, even when the *authored* change is exactly the same. Reopening
every judgment on such a Snapshot teaches reviewers to ignore "changed since your
review"; preserving judgments on a guess teaches them that "reviewed" lies.

This module answers the question deterministically or not at all:

**A unit moved base-only** when the diff base moved between the two Snapshots and
the authored patch of its file is *patch-equivalent* on both sides: the same
hunks, line numbers ignored and whitespace removed (git ``patch-id`` semantics,
context lines included), same status, paths, modes and submodule pointers. Its
content fingerprint may still differ (the base changed lines inside the symbol,
or around the hunk), but nothing the author wrote did.

Everything else is **authored**, including every case that cannot be decided:
an uncaptured or capped hunk body, a binary file, a file past the size bound, a
range-mode change, a packet that cannot be read. Failing conservative is the
rule; a bound never classifies as base-only.

No writes. The capture path (``sources.local.record_revision``) persists the
resulting judgment carry; surfaces only label.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewUnit

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.store import ReviewStore

PATCH_ID_VERSION = "v1"
"""Bumping this invalidates every cached patch id; nothing is persisted."""

# lc-debt: fixed per-file / per-Snapshot patch-id ceilings; an over-bound file or
# Snapshot is classified "authored" (never base-only). Upgrade path: stream
# hunks from the packet artifact and hash incrementally, or persist ids per
# revision in review_derived so large Snapshots are classified too.
MAX_PATCH_ID_FILE_BYTES = 512 * 1024
"""Hunk text hashed for one file; a larger file is not deterministically classified."""
MAX_PATCH_ID_FILES = 3000
"""Files hashed per revision; a larger revision classifies every path as authored."""
MAX_PATCH_ID_TOTAL_BYTES = 32 * 1024 * 1024
"""Hunk text hashed per revision; past it the remaining paths are not classified."""

_CACHE_LIMIT = 256

MovementKind = Literal["none", "authored", "base_only", "rebase_only", "rebase_with_authored_changes"]
UnitMovement = Literal["unchanged", "authored", "base_only", "added"]
RelativeTo = Literal["your_review", "previous_snapshot"]


@dataclass(frozen=True)
class PatchIdSet:
    """Per-path authored patch ids of one Snapshot; ``None`` = not deterministically knowable."""

    by_path: Mapping[str, str | None]
    complete: bool = True
    """False when a revision bound cut hashing short (the remaining paths are ``None``)."""
    reason: str = ""


def _strip_whitespace(text: str) -> str:
    return "".join(text.split())


def file_patch_id(row: Mapping[str, Any], *, max_bytes: int = MAX_PATCH_ID_FILE_BYTES) -> str | None:
    """The authored patch id of one packet file row, or ``None`` when it cannot be decided.

    Line numbers (hunk headers) are excluded and whitespace inside every line is
    removed, as ``git patch-id`` does; context lines are *included*, so a base
    edit within the diff context makes the patch differ (conservative). The
    origin character is kept apart from the content so a context line whose
    text starts with ``+`` cannot collide with an added line.
    """

    if not isinstance(row, Mapping):
        return None
    if row.get("is_binary") or str(row.get("diff_omitted_reason") or ""):
        return None
    hunks = row.get("hunks")
    if not isinstance(hunks, (list, tuple)) or not hunks:
        return None
    digest = hashlib.sha256()
    pointer = row.get("submodule_pointer")
    header = "\x00".join(
        (
            PATCH_ID_VERSION,
            str(row.get("status") or ""),
            str(row.get("old_path") or ""),
            str(row.get("path") or ""),
            str(row.get("old_mode") or 0),
            str(row.get("new_mode") or 0),
            "" if not isinstance(pointer, (list, tuple)) else "..".join(str(item) for item in pointer),
        )
    )
    digest.update(header.encode("utf-8"))
    size = 0
    for hunk in hunks:
        if not isinstance(hunk, Mapping):
            return None
        patch = hunk.get("patch")
        changed = int(hunk.get("added") or 0) + int(hunk.get("removed") or 0)
        if not isinstance(patch, str) or (not patch and changed):
            # An uncaptured or capped body is geometry, not content.
            return None
        size += len(patch.encode("utf-8", errors="replace"))
        if size > max_bytes:
            return None
        digest.update(b"\x01")
        for line in patch.splitlines():
            if not line:
                continue
            origin, content = line[0], line[1:]
            if origin in {"\\", "<", ">"}:
                # "No newline at end of file" markers belong to both sides' bytes;
                # keep them, they are part of what the author changed.
                digest.update(f"{origin}\x00\n".encode())
                continue
            digest.update(f"{origin}\x00{_strip_whitespace(content)}\n".encode("utf-8", errors="replace"))
    return digest.hexdigest()


_patch_cache: OrderedDict[tuple[str, ...], PatchIdSet] = OrderedDict()
_patch_lock = threading.Lock()


def _revision_cache_key(revision: ReviewRevision) -> tuple[str, ...]:
    """(base, head) identity of one immutable capture; the packet digest makes it exact."""

    return (
        PATCH_ID_VERSION,
        revision.range_mode,
        revision.base_sha,
        revision.merge_base_sha,
        revision.head_sha or revision.source_fingerprint or revision.tree_fingerprint,
        revision.packet_sha256,
    )


def compute_patch_ids(
    packet: Mapping[str, Any] | None,
    *,
    max_files: int = MAX_PATCH_ID_FILES,
    max_total_bytes: int = MAX_PATCH_ID_TOTAL_BYTES,
    max_file_bytes: int = MAX_PATCH_ID_FILE_BYTES,
) -> PatchIdSet:
    """Patch ids for every file of *packet*, bounded. Pure; see :func:`authored_patch_ids` for the cache."""

    if not isinstance(packet, Mapping):
        return PatchIdSet(by_path={}, complete=False, reason="packet_unavailable")
    rows = [row for row in packet.get("files") or () if isinstance(row, Mapping) and str(row.get("path") or "")]
    if len(rows) > max_files:
        return PatchIdSet(
            by_path={str(row["path"]): None for row in rows}, complete=False, reason="file_count_over_bound"
        )
    out: dict[str, str | None] = {}
    total = 0
    complete = True
    for row in rows:
        path = str(row["path"])
        if not complete:
            out[path] = None
            continue
        raw_hunks = row.get("hunks")
        hunks: Sequence[Any] = raw_hunks if isinstance(raw_hunks, (list, tuple)) else ()
        weight: int = sum(len(str(hunk.get("patch") or "")) for hunk in hunks if isinstance(hunk, Mapping))
        if total + weight > max_total_bytes:
            complete = False
            out[path] = None
            continue
        total += weight
        out[path] = file_patch_id(row, max_bytes=max_file_bytes)
    return PatchIdSet(by_path=out, complete=complete, reason="" if complete else "total_bytes_over_bound")


def authored_patch_ids(revision: ReviewRevision, packet: Mapping[str, Any] | None) -> PatchIdSet:
    """Cached per (base, head) capture: a revision's patch ids never change."""

    if packet is None:
        return PatchIdSet(by_path={}, complete=False, reason="packet_unavailable")
    key = _revision_cache_key(revision)
    with _patch_lock:
        hit = _patch_cache.get(key)
        if hit is not None:
            _patch_cache.move_to_end(key)
            return hit
    computed = compute_patch_ids(packet)
    with _patch_lock:
        _patch_cache[key] = computed
        _patch_cache.move_to_end(key)
        while len(_patch_cache) > _CACHE_LIMIT:
            _patch_cache.popitem(last=False)
    return computed


@dataclass(frozen=True)
class MovementClassification:
    """What moved between two Snapshots, per unit, and why."""

    from_revision_id: str
    to_revision_id: str
    kind: MovementKind
    base_moved: bool
    head_moved: bool
    deterministic: bool
    """False when some changed path could not be decided and fell back to authored."""
    base_only_paths: tuple[str, ...] = ()
    authored_paths: tuple[str, ...] = ()
    undecided_paths: tuple[str, ...] = ()
    """Changed paths classified authored only because equivalence was not knowable."""
    removed_unit_count: int = 0
    unit_movement: Mapping[str, UnitMovement] = field(default_factory=dict)
    reason: str = ""

    @property
    def base_only_unit_keys(self) -> frozenset[str]:
        return frozenset(key for key, value in self.unit_movement.items() if value == "base_only")

    @property
    def authored_unit_keys(self) -> frozenset[str]:
        return frozenset(key for key, value in self.unit_movement.items() if value in {"authored", "added"})

    def to_wire(self) -> dict[str, Any]:
        counts = {"unchanged": 0, "authored": 0, "base_only": 0, "added": 0}
        for value in self.unit_movement.values():
            counts[value] += 1
        return {
            "kind": self.kind,
            "base_moved": self.base_moved,
            "head_moved": self.head_moved,
            "deterministic": self.deterministic,
            "base_only_paths": list(self.base_only_paths),
            "authored_paths": list(self.authored_paths),
            "undecided_paths": list(self.undecided_paths),
            "unit_counts": {**counts, "removed": self.removed_unit_count},
            "reason": self.reason,
        }


def _known(unit: ReviewUnit) -> bool:
    return unit.fingerprint_method != "unknown"


def classify_movement(
    previous: ReviewRevision,
    previous_units: Sequence[ReviewUnit],
    previous_packet: Mapping[str, Any] | None,
    current: ReviewRevision,
    current_units: Sequence[ReviewUnit],
    current_packet: Mapping[str, Any] | None,
) -> MovementClassification:
    """Classify every current unit relative to *previous*. Pure apart from the patch-id cache.

    Without base movement every content change is authored by definition. With
    it, a unit whose fingerprint changed is base-only only when its file's
    authored patch id is known and equal on both sides; anything undecidable is
    authored.
    """

    base_moved = (previous.base_sha, previous.merge_base_sha) != (current.base_sha, current.merge_base_sha)
    previous_head = previous.head_sha or previous.source_fingerprint or previous.tree_fingerprint
    current_head = current.head_sha or current.source_fingerprint or current.tree_fingerprint
    head_moved = previous_head != current_head
    same_mode = previous.range_mode == current.range_mode
    reason = ""
    base_only_paths: set[str] = set()
    undecided: set[str] = set()
    before_ids: PatchIdSet | None = None
    after_ids: PatchIdSet | None = None
    if base_moved:
        if not same_mode:
            reason = "range_mode_changed"
        else:
            before_ids = authored_patch_ids(previous, previous_packet)
            after_ids = authored_patch_ids(current, current_packet)
            if not before_ids.complete or not after_ids.complete:
                reason = before_ids.reason or after_ids.reason

    before = {unit.unit_key: unit for unit in previous_units}
    movement: dict[str, UnitMovement] = {}
    authored_paths: set[str] = set()
    for unit in current_units:
        old = before.get(unit.unit_key)
        if old is None:
            movement[unit.unit_key] = "added"
            authored_paths.add(unit.path)
            continue
        if _known(old) and _known(unit) and old.content_fingerprint == unit.content_fingerprint:
            movement[unit.unit_key] = "unchanged"
            continue
        decided = False
        if base_moved and before_ids is not None and after_ids is not None and _known(old) and _known(unit):
            left = before_ids.by_path.get(unit.path)
            right = after_ids.by_path.get(unit.path)
            if left is not None and right is not None:
                decided = True
                if left == right:
                    movement[unit.unit_key] = "base_only"
                    base_only_paths.add(unit.path)
                    continue
        if base_moved and not decided:
            undecided.add(unit.path)
        movement[unit.unit_key] = "authored"
        authored_paths.add(unit.path)

    current_keys = {unit.unit_key for unit in current_units}
    removed = sum(1 for key in before if key not in current_keys)
    # A path is base-only only when *every* moved unit in it is; one authored
    # unit makes the file authored for the summary (units keep their own label).
    base_only_paths -= authored_paths
    authored_count = sum(1 for value in movement.values() if value in {"authored", "added"}) + removed
    kind: MovementKind
    if not base_moved:
        kind = "authored" if authored_count or head_moved else "none"
    elif authored_count:
        kind = "rebase_with_authored_changes"
    else:
        kind = "rebase_only" if head_moved else "base_only"
    return MovementClassification(
        from_revision_id=previous.id,
        to_revision_id=current.id,
        kind=kind,
        base_moved=base_moved,
        head_moved=head_moved,
        deterministic=not undecided,
        base_only_paths=tuple(sorted(base_only_paths)),
        authored_paths=tuple(sorted(authored_paths)),
        undecided_paths=tuple(sorted(undecided)),
        removed_unit_count=removed,
        unit_movement=movement,
        reason=reason or ("patch_equivalence_unknown" if undecided else ""),
    )


_movement_cache: OrderedDict[tuple[str, ...], MovementClassification] = OrderedDict()
_movement_lock = threading.Lock()


def movement_between(
    store: ReviewStore, previous: ReviewRevision, current: ReviewRevision
) -> MovementClassification | None:
    """Classify two stored Snapshots of one Review; ``None`` when they are the same Snapshot."""

    if previous.id == current.id:
        return None
    key = (*_revision_cache_key(previous), previous.id, *_revision_cache_key(current), current.id)
    with _movement_lock:
        hit = _movement_cache.get(key)
        if hit is not None:
            _movement_cache.move_to_end(key)
            return hit
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    classification = classify_movement(
        previous,
        store.list_units(previous.id),
        read_packet_json(store, previous),
        current,
        store.list_units(current.id),
        read_packet_json(store, current),
    )
    with _movement_lock:
        _movement_cache[key] = classification
        _movement_cache.move_to_end(key)
        while len(_movement_cache) > _CACHE_LIMIT:
            _movement_cache.popitem(last=False)
    return classification


def no_authored_change_since(store: ReviewStore, from_revision_id: str, current: ReviewRevision) -> bool:
    """True only when nothing judged on *from_revision_id* changed in content by *current*.

    Deterministic base-only/rebase-only movement, or no movement at all.
    Authored edits, an undecidable path, a removed target or an unreadable
    Snapshot are all ``False`` -- the conservative side.
    """

    if not from_revision_id:
        return False
    if from_revision_id == current.id:
        return True
    try:
        previous = store.get_revision(from_revision_id)
        if previous is None or previous.review_id != current.review_id:
            return False
        movement = movement_between(store, previous, current)
    except Exception:
        return False
    return movement is not None and movement.deterministic and movement.kind in {"none", "base_only", "rebase_only"}


@dataclass(frozen=True)
class ReviewerMovement:
    """Movement of the displayed Snapshot relative to what one reviewer last judged."""

    classification: MovementClassification
    relative_to: RelativeTo
    from_revision: ReviewRevision


def movement_for_reviewer(
    store: ReviewStore, review_id: str, revision: ReviewRevision, *, reviewer_id: str
) -> ReviewerMovement | None:
    """Movement since the reviewer's durable frontier, else since the previous Snapshot.

    Never raises: a classification that cannot be computed is simply absent,
    which every surface reads as "no base-only label" (the conservative side).
    """

    from lemoncrow.pro.capabilities.review.sources.local import baseline_revision

    try:
        baseline = baseline_revision(store, review_id, reviewer_id=reviewer_id)
        relative: RelativeTo = "your_review"
        if baseline is None or baseline.id == revision.id or baseline.revision_number > revision.revision_number:
            relative = "previous_snapshot"
            older = [
                item for item in store.list_revisions(review_id) if item.revision_number < revision.revision_number
            ]
            baseline = max(older, key=lambda item: item.revision_number) if older else None
        if baseline is None:
            return None
        classification = movement_between(store, baseline, revision)
    except Exception:  # labels are advisory; an unreadable history is "no label"
        return None
    if classification is None:
        return None
    return ReviewerMovement(classification=classification, relative_to=relative, from_revision=baseline)


def label_target_payloads(
    payloads: Sequence[dict[str, Any]], movement: ReviewerMovement | None
) -> list[dict[str, Any]]:
    """Add ``movement`` to each target payload: ``base_only`` only for deterministic base-only moves."""

    if movement is None:
        return list(payloads)
    labels = movement.classification.unit_movement
    out: list[dict[str, Any]] = []
    for payload in payloads:
        label = labels.get(str(payload.get("unit_key") or ""))
        out.append({**payload, "movement": label} if label is not None else payload)
    return out


def movement_summary(
    movement: ReviewerMovement | None, target_payloads: Sequence[Mapping[str, Any]], *, display_number: int
) -> dict[str, Any] | None:
    """The "changed since" summary line's facts; ``None`` when nothing moved."""

    if movement is None:
        return None
    classification = movement.classification
    base_only_targets = sum(1 for item in target_payloads if item.get("movement") == "base_only")
    authored_targets = sum(1 for item in target_payloads if item.get("movement") in {"authored", "added"})
    return {
        **classification.to_wire(),
        "relative_to": movement.relative_to,
        "from_revision_number": display_number,
        "base_only_targets": base_only_targets,
        "authored_targets": authored_targets,
    }


__all__ = [
    "MAX_PATCH_ID_FILES",
    "MAX_PATCH_ID_FILE_BYTES",
    "MAX_PATCH_ID_TOTAL_BYTES",
    "PATCH_ID_VERSION",
    "MovementClassification",
    "MovementKind",
    "PatchIdSet",
    "ReviewerMovement",
    "UnitMovement",
    "authored_patch_ids",
    "classify_movement",
    "compute_patch_ids",
    "file_patch_id",
    "label_target_payloads",
    "movement_between",
    "movement_for_reviewer",
    "movement_summary",
    "no_authored_change_since",
]
