"""P3-2: reviewer-reported changed-line exposure, never independent proof of reading.

The server derives viewer, revision, and content fingerprint from the authenticated
Review; client-submitted elapsed times remain labelled as *self reported*. Every
accepted record is immutable, scoped and later bound to a signed record v2.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent, ReviewRevision, ReviewSession

SEEN_KIND = "review.coverage_seen"
SKIPPED_KIND = "review.coverage_skipped"
COVERAGE_KINDS = (SEEN_KIND, SKIPPED_KIND)


class SeenCoverageRefused(ValueError):
    pass


def _bounded_string(value: object, *, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SeenCoverageRefused(f"{name} must be a nonempty string of at most {maximum} characters")
    return value


def _integer(value: object, *, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SeenCoverageRefused(f"{name} must be an integer from {low} to {high}")
    return value


@dataclass(frozen=True, slots=True)
class CoverageBlock:
    """One changed span of one part, exactly as the current revision defines it."""

    key: str
    unit_key: str
    path: str
    side: str
    hunk: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CoverageIndex:
    """Every block a reviewer may report on, and the parts with no measurable block."""

    blocks: Mapping[str, CoverageBlock]
    unmeasured: frozenset[str]

    @property
    def skippable(self) -> frozenset[str]:
        return frozenset(self.blocks) | self.unmeasured


def _key_prefix(target: Mapping[str, Any]) -> str:
    fingerprint = target.get("content_fingerprint") or "fingerprint-unavailable"
    return f"{target['unit_key']}:{target['path']}:{fingerprint}"


def coverage_index(target_rows: Sequence[Mapping[str, Any]]) -> CoverageIndex:
    """Build the server's own block set from ``_targets_payload`` rows.

    The key format and the span validity rule mirror ``seenModel.ts``
    (``blocksFromSpan``): a part whose spans are all invalid is *unmeasured*, and
    unmeasured parts can be disclosed as skipped but never reported as seen.
    """

    blocks: dict[str, CoverageBlock] = {}
    unmeasured: set[str] = set()
    for target in target_rows:
        measured = 0
        for span in target.get("spans") or ():
            start, end, side = span.get("start_line"), span.get("end_line"), span.get("side")
            hunk = span.get("hunk_ordinal")
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or isinstance(hunk, bool)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not isinstance(hunk, int)
                or side not in {"old", "new"}
                or start <= 0
                or end < start
                or end - start + 1 > 100_000
            ):
                continue
            key = f"{_key_prefix(target)}:{side}:{hunk}:{start}:{end}"
            blocks[key] = CoverageBlock(key, target["unit_key"], target["path"], side, hunk, start, end)
            measured += 1
        if not measured:
            unmeasured.add(f"{_key_prefix(target)}:unmeasured")
    return CoverageIndex(blocks, frozenset(unmeasured))


def validate_seen_payload(
    body: object, *, units: Mapping[str, Any], revision_id: str, index: CoverageIndex
) -> dict[str, Any]:
    if not isinstance(body, Mapping) or body.get("revision_id") != revision_id:
        raise SeenCoverageRefused("the coverage report must name the current revision")
    action = body.get("action")
    if action not in {"seen", "skipped"}:
        raise SeenCoverageRefused("coverage action must be seen or skipped")
    if action == "skipped":
        keys = body.get("block_keys")
        if (
            not isinstance(keys, list)
            or len(keys) > 500
            or not all(isinstance(key, str) and 0 < len(key) <= 512 for key in keys)
        ):
            raise SeenCoverageRefused("skipped block keys must be a bounded list")
        if not set(keys).issubset(index.skippable):
            raise SeenCoverageRefused("skipped span does not belong to the current revision")
        return {
            "action": action,
            "block_keys": sorted(set(keys)),
            "basis": "reviewer_reported_not_independent_evidence",
        }
    key = _bounded_string(body.get("unit_key"), name="unit_key")
    unit = units.get(key)
    if unit is None:
        raise SeenCoverageRefused("no such part in the current revision")
    if str(unit.fingerprint_method) == "unknown" or not unit.content_fingerprint:
        raise SeenCoverageRefused("cannot record exposure of a part without a content fingerprint")
    block_key = _bounded_string(body.get("block_key"), name="block_key")
    if block_key in index.unmeasured or block_key.endswith(":unmeasured"):
        raise SeenCoverageRefused("a part with no measurable changed lines cannot be reported as seen")
    block = index.blocks.get(block_key)
    if block is None:
        raise SeenCoverageRefused("seen span does not belong to the current revision")
    side = body.get("side")
    if side not in {"old", "new"}:
        raise SeenCoverageRefused("side must be old or new")
    start = _integer(body.get("start"), name="start", low=1, high=100_000_000)
    end = _integer(body.get("end"), name="end", low=start, high=100_000_000)
    hunk = _integer(body.get("hunk"), name="hunk", low=-1, high=100_000)
    elapsed = _integer(body.get("elapsed_ms"), name="elapsed_ms", low=0, high=60_000)
    # The block key names the real span; the body must say the same thing, so a
    # key borrowed from another part or a shrunk span is refused rather than signed.
    if (
        block.unit_key != key
        or block.side != side
        or block.hunk != hunk
        or block.start != start
        or block.end != end
        or block.path != unit.path
    ):
        raise SeenCoverageRefused("seen span does not match the changed block it names")
    source = body.get("source")
    if source not in {"dwell", "focus_answer"}:
        raise SeenCoverageRefused("source must be dwell or focus_answer")
    # Time is computed from the real block. A client-side test override never
    # relaxes the production minimum. elapsed_ms is still browser-reported. A focus
    # answer is held to it too: the card shows the part whole, so it reports the full time.
    required_ms = min(3000, max(600, 40 * (block.end - block.start + 1)))
    if elapsed < required_ms:
        raise SeenCoverageRefused(f"dwell exposure requires at least {required_ms} ms")
    return {
        "action": "seen",
        "unit_key": key,
        "block_key": block.key,
        "path": unit.path,
        "side": side,
        "start": start,
        "end": end,
        "hunk": hunk,
        "elapsed_ms": elapsed,
        "source": source,
        "content_fingerprint": unit.content_fingerprint,
        "basis": "reviewer_reported_not_independent_evidence",
    }


def reviewer_coverage_events(
    store: Any, session: ReviewSession, revision: ReviewRevision, *, as_of: str = "", reviewer_id: str = ""
) -> tuple[ReviewActivityEvent, ...]:
    """Every coverage row of one reviewer on one revision, oldest first, none dropped.

    Revision and actor are filtered in the store query (no row cap). ``as_of``
    bounds the rows to those written by that instant, so a record signed earlier
    still recomputes after later reports. ``reviewer_id`` defaults to the session's
    acting reviewer; a record passes its decision's actor instead.
    """

    from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

    cutoff = parse_timestamp(as_of) if as_of else None
    return tuple(
        event
        for event in store.list_coverage_events(
            session.id, revision_id=revision.id, actor_id=reviewer_id or session.reviewer_id, kinds=COVERAGE_KINDS
        )
        if cutoff is None or (parsed := parse_timestamp(event.created_at)) is None or parsed <= cutoff
    )


def already_reported(events: Sequence[ReviewActivityEvent], detail: Mapping[str, Any]) -> bool:
    """Whether an identical report is already stored, so a repeat appends nothing."""

    if detail["action"] == "seen":
        for event in events:
            if event.kind != SEEN_KIND:
                continue
            try:
                stored = json.loads(event.detail_json)
            except (TypeError, ValueError):
                continue
            if isinstance(stored, dict) and stored.get("block_key") == detail["block_key"]:
                return True
        return False
    wanted = set(detail["block_keys"])
    have: set[str] = set()
    for event in events:
        if event.kind != SKIPPED_KIND:
            continue
        try:
            stored = json.loads(event.detail_json)
        except (TypeError, ValueError):
            continue
        keys = stored.get("block_keys") if isinstance(stored, dict) else None
        if isinstance(keys, list):
            have.update(key for key in keys if isinstance(key, str))
    return wanted <= have


def coverage_section(events: Sequence[ReviewActivityEvent], *, revision_id: str) -> dict[str, Any]:
    seen: dict[str, dict[str, Any]] = {}
    skipped: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda event: (event.created_at, event.id)):
        if event.revision_id != revision_id or event.kind not in COVERAGE_KINDS:
            continue
        try:
            detail = json.loads(event.detail_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict):
            continue
        if event.kind == SEEN_KIND:
            key = detail.get("block_key")
            if not isinstance(key, str) or not key:
                continue
            seen[key] = {
                "block_key": key,
                "unit_key": detail.get("unit_key", ""),
                "path": detail.get("path", ""),
                "side": detail.get("side", ""),
                "start": detail.get("start", 0),
                "end": detail.get("end", 0),
                "hunk": detail.get("hunk", 0),
                "elapsed_ms": detail.get("elapsed_ms", 0),
                "source": detail.get("source", ""),
                "content_fingerprint": detail.get("content_fingerprint", ""),
                "reported_at": event.created_at,
                "event_id": event.id,
            }
        else:
            keys = detail.get("block_keys")
            if not isinstance(keys, list):
                continue
            for key in keys:
                if isinstance(key, str) and key:
                    skipped[key] = {"block_key": key, "event_id": event.id, "reported_at": event.created_at}
    return {
        "basis": "reviewer_reported_not_independent_evidence",
        "revision_id": revision_id,
        "seen": [seen[key] for key in sorted(seen)],
        "skipped": [skipped[key] for key in sorted(skipped)],
        "meaning": "Seen means the browser reported sufficient on-screen exposure, not approval or proof of comprehension.",
    }
