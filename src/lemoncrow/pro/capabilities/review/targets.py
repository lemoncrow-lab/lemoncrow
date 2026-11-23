"""Reader-facing review targets: one human judgment per reviewable unit.

Raw :class:`ReviewUnit` rows deliberately overlap: a file contains hunks and a
symbol may contain both a hunk and another symbol. They are the durable
identity/fingerprint substrate, not a progress denominator. This module is the
pure projection that turns those overlapping units into a non-overlapping set
of things a human can actually review.

For source, every changed line represented by a trustworthy patch is assigned
exactly once: a stable symbol wins, then a hunk, then the file fallback. The
one deliberate exception is a supported generated dependency lockfile whose
exact bytes produced semantic ``dependency`` units. Its raw lockfile targets
are replaced by those component targets, while the source remains navigable.
The chosen target always stores an existing ReviewUnit key, so dependency
review reuses the same judgment/reconciliation model rather than adding a
second approval state.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import hashlib
import re
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from lemoncrow.pro.capabilities.review.answer_category import (
    QUICK_CHECK,
    Answer,
    StructuralFacts,
    answer_counts,
    answer_payload,
    effective_answer,
    stamped_answer,
    structural_answer,
)
from lemoncrow.pro.capabilities.review.impact import function_valued_binding as _function_valued_binding
from lemoncrow.pro.capabilities.review.review_priority import (
    QueueReason,
    TargetPriority,
    assign_review_priority,
    priority_payload,
)
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    FrontierEntry,
    MarkState,
    ReviewEvidence,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.units import DEFINITION_HEADER as _DEFINITION_HEADER
from lemoncrow.pro.capabilities.review.units import change_blocks as _change_blocks
from lemoncrow.pro.capabilities.review.units import module_data_name, new_side_line_text, owned_old_count

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.routine import RoutineProof

TOP_LEVEL_CODE_LABEL = "Imports and top-level code"
"""What a person reads for a module-level (document_section) target; never part of a target id."""

TargetKind = Literal["symbol", "hunk", "file", "dependency", "document_section"]
TargetSide = Literal["old", "new"]
AttentionLevel = Literal["high", "normal", "mechanical"]
TargetOrder = Literal["recommended", "file"]

_CHURN_REASON = re.compile(r"^\+\d+\s+-\d+$")
# "4 known callers", whoever wrote it. ``ordering.rank_files`` phrases it for a
# file and ``units._symbol_reasons`` for a symbol, in the same words, and a
# symbol unit inherits its file's copy on top of its own -- so the spelling can
# never say which one is speaking. Only the unit being ranked can.
_CALLER_COUNT_REASON = re.compile(r"^(\d+)\s+known callers?$")
# Reasons that describe a row instead of pointing at it. Both spellings of the
# skim note are kept because the ranker's em dash has been written with hyphens
# in older packets, and a signal nobody can spell twice is one nobody filters.
_DESCRIPTIVE_REASONS = frozenset(
    {
        "generated/vendor — skim",
        "generated/vendor -- skim",
        "generated/vendor - skim",
        "symbol added",
        "symbol modified",
        "symbol deleted",
        "symbol unknown",
    }
)


@dataclass(frozen=True)
class TargetSpan:
    """One side-specific changed span owned by a target, inclusive."""

    side: TargetSide
    start_line: int
    end_line: int
    hunk_ordinal: int


@dataclass(frozen=True)
class TargetVerification:
    pass_count: int = 0
    fail_count: int = 0
    unknown_count: int = 0


@dataclass(frozen=True)
class TargetAnnotationCounts:
    open: int = 0
    orphaned: int = 0
    addressed_needs_rereview: int = 0


@dataclass(frozen=True)
class ReviewTarget:
    """One non-overlapping human judgment target for the current revision."""

    target_id: str
    unit_key: str
    kind: TargetKind
    path: str
    label: str
    symbol: str
    start_line: int
    end_line: int
    hunk_ordinals: tuple[int, ...]
    spans: tuple[TargetSpan, ...]
    state: MarkState
    changed_since_mark: bool
    reviewed_revision_id: str
    attention_rank: int
    attention_level: AttentionLevel
    reasons: tuple[str, ...]
    additions: int
    deletions: int
    fingerprint_method: str
    content_fingerprint: str = ""
    verification: TargetVerification = TargetVerification()
    annotation_counts: TargetAnnotationCounts = TargetAnnotationCounts()
    structural_level: AttentionLevel = "normal"
    """Generated/vendor classification before the queue rewrote ``attention_level``."""
    priority: TargetPriority = field(default_factory=TargetPriority)
    """Canonical queue placement; see :mod:`review_priority`."""
    structural: Answer = QUICK_CHECK
    """What the captured change alone says (the unit's stamp, or derived for an unstamped unit)."""
    answer: Answer = QUICK_CHECK
    """How a person is asked about this part: ``structural`` raised to a decision by the queue's own
    Review-first facts (:func:`~lemoncrow.pro.capabilities.review.answer_category.effective_answer`).
    Never changes with a mark."""


@dataclass(frozen=True)
class ReviewProgress:
    """Counts over the reviewer's targets. ``later`` is not a judgment: it is counted apart from
    ``reviewed`` and ``needs_changes`` and every pending sum must include it."""

    target_count: int = 0
    reviewed: int = 0
    changed_since_review: int = 0
    needs_changes: int = 0
    unreviewed: int = 0
    unknown: int = 0
    mechanical: int = 0
    later: int = 0
    decision: int = 0
    quick_check: int = 0
    accepted_by_rule: int = 0


@dataclass(frozen=True)
class ReviewOutlineItem:
    path: str
    target_count: int
    reviewed: int
    changed_since_review: int
    needs_changes: int
    unreviewed: int
    unknown: int
    mechanical: int
    attention_rank: int
    reasons: tuple[str, ...]
    section: str = "unreviewed"
    """``review_first`` | ``unreviewed`` | ``mechanical`` | ``done``, from the
    file's first outstanding queue entry."""
    position: int = 0
    next_target_id: str = ""
    next_reason: str = ""
    next_reason_code: str = ""
    review_first: int = 0
    understand: int = 0
    routine: int = 0
    later: int = 0


@dataclass(frozen=True)
class RevisionTargetDelta:
    """Human-facing effect of one revision transition on review targets."""

    preserved: tuple[ReviewTarget, ...] = ()
    reopened: tuple[ReviewTarget, ...] = ()
    added: tuple[ReviewTarget, ...] = ()
    removed: tuple[ReviewTarget, ...] = ()
    active: tuple[ReviewTarget, ...] = ()


# Variable-like declarations are valuable to the index but usually too
# fine-grained for a human review denominator. A React component can contain
# dozens of one-line state bindings and temporaries; those are legitimate search
# symbols, not independent "reviewed" judgments.
_VARIABLE_LIKE_KINDS = frozenset(
    {
        "variable",
        "field",
        "property",
        "lexical_declaration",
        "variable_declaration",
        "field_definition",
        "public_field_definition",
        "property_signature",
        "declaration",
        "var_declaration",
        "val_definition",
        "var_definition",
    }
)
_MIN_COHESIVE_VARIABLE_LINES = 6


def _packet_mapping(packet: Mapping[str, Any] | Any | None) -> Mapping[str, Any]:
    if packet is None:
        return {}
    if isinstance(packet, Mapping):
        return packet
    to_dict = getattr(packet, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return value if isinstance(value, Mapping) else {}
    return {}


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return default


def _compress(lines: Sequence[int], *, side: TargetSide, hunk_ordinal: int) -> tuple[TargetSpan, ...]:
    numbers = sorted(set(line for line in lines if line > 0))
    if not numbers:
        return ()
    spans: list[TargetSpan] = []
    start = previous = numbers[0]
    for line in numbers[1:]:
        if line == previous + 1:
            previous = line
            continue
        spans.append(TargetSpan(side=side, start_line=start, end_line=previous, hunk_ordinal=hunk_ordinal))
        start = previous = line
    spans.append(TargetSpan(side=side, start_line=start, end_line=previous, hunk_ordinal=hunk_ordinal))
    return tuple(spans)


def _fallback_spans(hunk: Mapping[str, Any], ordinal: int) -> tuple[TargetSpan, ...]:
    """Exact changed spans preserved independently of a captured patch body.

    ``old_start/new_start`` plus ``old_lines/new_lines`` describe the printed
    unified-diff hunk, including context. They are not changed-line geometry and
    must never become progress spans. If the producer did not preserve exact
    ``*_ranges`` either, the caller must promote the file to a file-level target
    rather than manufacture precision from the hunk header.
    """

    spans: list[TargetSpan] = []
    for side in ("old", "new"):
        for raw in hunk.get(f"{side}_ranges") or ():
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                continue
            low, high = _as_int(raw[0]), _as_int(raw[1])
            if low > 0 and high >= low:
                spans.append(TargetSpan(side=side, start_line=low, end_line=high, hunk_ordinal=ordinal))
    return tuple(spans)


def _span_size(spans: Sequence[TargetSpan], side: TargetSide) -> int:
    return sum(span.end_line - span.start_line + 1 for span in spans if span.side == side)


def _unit_label(unit: ReviewUnit) -> str:
    if unit.kind == "symbol" and unit.symbol:
        return f"{unit.path}::{unit.symbol}"
    if unit.kind == "dependency" and unit.symbol:
        return unit.symbol
    if unit.kind == "hunk":
        return f"{unit.path}#{unit.ordinal}"
    return unit.path


def _frontier_state(entry: FrontierEntry | None) -> tuple[MarkState, bool, str]:
    if entry is None:
        return "unreviewed", False, ""
    state = entry.state
    # Either judgment covers only the content it was made on (spec §7: content
    # changed since judgment is re-review). A changed needs-changes area -- the
    # author's fix -- reopens for the human; the durable mark itself is never
    # withdrawn, so reverting the content restores it.
    if entry.changed_since_mark and state in ("reviewed", "needs_changes"):
        state = "changed_since_review"
    return state, entry.changed_since_mark, entry.reviewed_revision_id


def reason_is_informative(reason: str) -> bool:
    """Whether one ranker reason is worth *showing* as context for a row.

    No reason string decides a tier: the canonical queue in
    :mod:`review_priority` promotes only on sourced records (spec §7). This
    predicate only filters what the outline prints beside a file.

    Some reasons are carried by every row of their kind and so say nothing: the
    churn line on every ranked file, the skim note on every generated/vendor
    file, and the change verb ``units._symbol_reasons`` stamps on every symbol
    unit. "``N`` known callers" describes topology, not a consequence or a
    review question (spec §2 defect 1), and ``units._symbol_reasons`` copies a
    file's count onto every symbol of that file, so it never qualifies.
    """

    text = reason.strip()
    if not text or _CHURN_REASON.match(text):
        return False
    if text in _DESCRIPTIVE_REASONS:
        return False
    return _CALLER_COUNT_REASON.match(text) is None


def _attention_level(unit: ReviewUnit) -> AttentionLevel:
    """Structural level only; the canonical queue decides ``high``."""

    if unit.attention_group in ("generated", "vendor"):
        return "mechanical"
    return "normal"


def _annotation_belongs_to_target(
    unit_key: str,
    path: str,
    spans: Sequence[TargetSpan],
    item: Annotation,
    *,
    path_target_keys: Collection[str] = (),
) -> bool:
    """Whether one root annotation belongs to a derived human target.

    *path_target_keys* is every target key of this file, when the caller knows
    them. It is what makes the answer exclusive: an anchor that names another
    target outright is that target's thread, so the changed-span fallback below
    must not claim it as well and count one judgment twice.
    """

    if item.parent_id or item.anchor.path != path:
        return False
    if item.anchor.unit_key == unit_key:
        return True
    if item.anchor.unit_key in path_target_keys:
        return False
    if item.anchor.start_line < 1:
        return False
    side = item.anchor.side
    if side not in ("old", "new"):
        return False
    anchor_start = item.anchor.start_line
    anchor_end = max(anchor_start, item.anchor.end_line)
    return any(span.side == side and span.start_line <= anchor_end and anchor_start <= span.end_line for span in spans)


def target_has_unresolved_request_change(
    target: ReviewTarget,
    annotations: Sequence[Annotation],
    *,
    path_target_keys: Collection[str] = (),
) -> bool:
    """Whether a request-change thread still needs individual review.

    *path_target_keys* is every target key of *target*'s file, and it must be
    the same set :func:`_annotation_counts` was given, or one request answers
    the same question twice with two different answers: ``GET /targets`` would
    print ``open: 0`` on a row that ``POST /marks/bulk`` then refuses because it
    reads a sibling's thread as this row's.
    """

    return any(
        item.kind == "request_change"
        and item.state in ("open", "orphaned")
        and item.author_response != "addressed"
        and _annotation_belongs_to_target(
            target.unit_key,
            target.path,
            target.spans,
            item,
            path_target_keys=path_target_keys,
        )
        for item in annotations
    )


def target_keys_by_path(targets: Sequence[ReviewTarget]) -> dict[str, frozenset[str]]:
    """Each file's full set of target keys, for the exclusive-ownership rule.

    :func:`derive_review_targets` builds this set inside itself while a path's
    targets are still being assembled. A caller holding the finished list has to
    rebuild it, and every caller must rebuild it the same way.
    """

    grouped: dict[str, set[str]] = {}
    for target in targets:
        grouped.setdefault(target.path, set()).add(target.unit_key)
    return {path: frozenset(keys) for path, keys in grouped.items()}


def paths_with_unattributed_request_change(
    targets: Sequence[ReviewTarget],
    annotations: Sequence[Annotation],
) -> frozenset[str]:
    """Files carrying an open request-change that no target of theirs owns.

    ``sources.local.annotate`` anchors a comment on the symbol unit containing
    it, or on the file unit when no symbol does, and never on the target key the
    client sent. A file-level objection, or one written on a context line no
    symbol covers, therefore belongs to no target at all once the file has split
    into symbol and hunk targets -- and :func:`target_has_unresolved_request_change`
    truthfully answers ``False`` for every one of them. The objection is against
    the file, so it is the file that has to refuse bulk completion; otherwise a
    sweep closes a file whose reviewer is still waiting for an answer.
    """

    by_path: dict[str, list[ReviewTarget]] = {}
    for target in targets:
        by_path.setdefault(target.path, []).append(target)
    keys_by_path = target_keys_by_path(targets)
    out: set[str] = set()
    for item in annotations:
        if (
            item.parent_id
            or item.kind != "request_change"
            or item.state not in ("open", "orphaned")
            or item.author_response == "addressed"
        ):
            continue
        owners = by_path.get(item.anchor.path)
        if not owners:
            continue
        keys = keys_by_path[item.anchor.path]
        if not any(
            _annotation_belongs_to_target(
                target.unit_key,
                target.path,
                target.spans,
                item,
                path_target_keys=keys,
            )
            for target in owners
        ):
            out.add(item.anchor.path)
    return frozenset(out)


def _verification_summary(evidence: Sequence[ReviewEvidence]) -> TargetVerification:
    """Summarize newest caller-approved file verification rows by check title.

    ``derive_review_targets`` is a pure projection and does not have revision
    history available to decide whether an artifact from an analysis-only prior
    row still describes the same reviewed code. Callers that supply evidence
    must therefore pass only evidence current for the reviewed code. The review
    API does this via ``_current_evidence``; other callers may omit evidence.
    """

    latest: dict[str, ReviewEvidence] = {}
    for item in sorted(evidence, key=lambda row: (row.created_at, row.id), reverse=True):
        if not item.path:
            continue
        status = item.verification_status.upper()
        if status not in {"PASS", "FAIL", "NOT_RUN", "UNKNOWN"}:
            continue
        name = item.title.strip() or "Verification"
        latest.setdefault(name, item)
    statuses = [item.verification_status.upper() for item in latest.values()]
    return TargetVerification(
        pass_count=statuses.count("PASS"),
        fail_count=statuses.count("FAIL"),
        unknown_count=sum(status in {"UNKNOWN", "NOT_RUN"} for status in statuses),
    )


def _annotation_counts(
    unit: ReviewUnit,
    spans: Sequence[TargetSpan],
    annotations: Sequence[Annotation],
    path_target_keys: Collection[str],
) -> TargetAnnotationCounts:
    """Count root threads that belong to this human target.

    Exact raw-unit identity is strongest, but R20 intentionally folds many
    micro-symbols into a larger ReviewTarget. A line comment anchored to one of
    those raw symbols must still appear on the target after reload, so changed
    spans are the fallback ownership proof. ReviewTarget spans are disjoint by
    construction, which prevents that fallback from double-counting a changed
    line across targets, and *path_target_keys* stops it claiming a thread that
    already belongs to a sibling target by name.
    """

    # Only human feedback is review work (spec §7; ``ReviewClosure`` counts the
    # same way). Author rationale, AI-review hints and LemonCrow notes explain
    # or suggest; counting them would turn preparation into fake open threads.
    roots = [
        item
        for item in annotations
        if item.source == "human"
        and _annotation_belongs_to_target(
            unit.unit_key,
            unit.path,
            spans,
            item,
            path_target_keys=path_target_keys,
        )
    ]
    return TargetAnnotationCounts(
        open=sum(item.state == "open" for item in roots),
        orphaned=sum(item.state == "orphaned" for item in roots),
        addressed_needs_rereview=sum(item.state == "open" and item.author_response == "addressed" for item in roots),
    )


def _target(
    unit: ReviewUnit,
    spans: Sequence[TargetSpan],
    entry: FrontierEntry | None,
    annotations: Sequence[Annotation],
    *,
    verification: TargetVerification | None = None,
    additions: int | None = None,
    deletions: int | None = None,
    path_target_keys: Collection[str] = (),
) -> ReviewTarget:
    state, changed, reviewed_revision_id = _frontier_state(entry)
    structural = stamped_answer(unit.answer_category, unit.category_rule) or structural_answer(
        # An unstamped unit (stored before the stamp existed) has no generated marking or Git change to
        # read, so it can reach accepted-by-rule only as a lockfile version move, never from a guess.
        StructuralFacts(
            path=unit.path,
            kind=unit.kind,
            fingerprint_known=unit.fingerprint_method != "unknown",
            reasons=unit.reasons,
        )
    )
    ordered_spans = tuple(
        sorted(spans, key=lambda span: (span.hunk_ordinal, span.side, span.start_line, span.end_line))
    )
    return ReviewTarget(
        target_id=f"target:{unit.unit_key}",
        unit_key=unit.unit_key,
        kind=unit.kind if unit.kind in ("symbol", "hunk", "file", "dependency", "document_section") else "file",
        path=unit.path,
        label=f"{unit.path}::{TOP_LEVEL_CODE_LABEL}" if unit.kind == "document_section" else _unit_label(unit),
        symbol="" if unit.kind == "document_section" else unit.symbol,
        start_line=unit.start_line,
        end_line=unit.end_line,
        hunk_ordinals=tuple(sorted({span.hunk_ordinal for span in ordered_spans if span.hunk_ordinal >= 0})),
        spans=ordered_spans,
        state=state,
        changed_since_mark=changed,
        reviewed_revision_id=reviewed_revision_id,
        attention_rank=unit.attention_rank,
        attention_level=_attention_level(unit),
        structural_level=_attention_level(unit),
        reasons=unit.reasons or ("no ranking signal fired -- review in normal order",),
        additions=_span_size(ordered_spans, "new") if additions is None else additions,
        deletions=_span_size(ordered_spans, "old") if deletions is None else deletions,
        fingerprint_method=unit.fingerprint_method,
        content_fingerprint=unit.content_fingerprint,
        verification=verification or TargetVerification(),
        annotation_counts=_annotation_counts(unit, ordered_spans, annotations, path_target_keys),
        structural=structural,
        answer=effective_answer(structural),
    )


def _trustworthy_symbols(units: Sequence[ReviewUnit]) -> tuple[ReviewUnit, ...]:
    counts: dict[tuple[str, str], int] = {}
    for unit in units:
        if unit.kind in ("symbol", "document_section") and unit.symbol:
            key = (unit.path, unit.symbol)
            counts[key] = counts.get(key, 0) + 1
    return tuple(
        unit
        for unit in units
        if unit.kind in ("symbol", "document_section")
        and unit.symbol
        and unit.start_line > 0
        and unit.end_line >= unit.start_line
        and unit.fingerprint_method != "unknown"
        and counts.get((unit.path, unit.symbol), 0) == 1
    )


def _symbol_metadata_by_path(data: Mapping[str, Any]) -> dict[str, dict[tuple[str, int], Mapping[str, Any]]]:
    """Packet symbol metadata indexed once for all changed files."""

    out: dict[str, dict[tuple[str, int], Mapping[str, Any]]] = {}
    for raw in data.get("symbols", ()):
        if not isinstance(raw, Mapping):
            continue
        path = str(raw.get("file_path") or "")
        start = _as_int(raw.get("start_line"), 0)
        if not path or start <= 0:
            continue
        bucket = out.setdefault(path, {})
        for key in (str(raw.get("qualified_name") or ""), str(raw.get("symbol_name") or "")):
            if key:
                bucket[(key, start)] = raw
    return out


def _strictly_contains(outer: ReviewUnit, inner: ReviewUnit) -> bool:
    return (
        outer.unit_key != inner.unit_key
        and outer.start_line <= inner.start_line
        and outer.end_line >= inner.end_line
        and (outer.start_line < inner.start_line or outer.end_line > inner.end_line)
    )


def _review_worthy_symbols(
    units: Sequence[ReviewUnit],
    metadata: Mapping[tuple[str, int], Mapping[str, Any]],
    source_lines: Mapping[int, str],
) -> tuple[ReviewUnit, ...]:
    """Keep semantic definitions; coalesce index-useful local bindings.

    Structural definitions and stable module declaration groups remain targets.
    Short data bindings fold into their group or hunk even when the index finds
    callers; substantial module values and callable bindings stay independent.
    """

    trustworthy = _trustworthy_symbols(units)

    structural: list[ReviewUnit] = []
    variables: list[ReviewUnit] = []
    for unit in trustworthy:
        if unit.kind == "document_section":
            structural.append(unit)
            continue
        grouped_data = unit.kind == "symbol" and any(
            group.kind == "document_section" and _strictly_contains(group, unit) for group in trustworthy
        )
        if grouped_data:
            variables.append(unit)
            continue
        meta = metadata.get((unit.symbol, unit.start_line))
        if meta is None:
            declaration = source_lines.get(unit.start_line, "")
            if module_data_name(declaration) is not None:
                variables.append(unit)
                continue
            semantic_parent = any(
                _strictly_contains(parent, unit) and metadata.get((parent.symbol, parent.start_line)) is not None
                for parent in trustworthy
            )
            visible_definition = bool(
                declaration and (_DEFINITION_HEADER.match(declaration) or _function_valued_binding(declaration))
            )
            if semantic_parent and declaration and not visible_definition:
                # Tree-sitter/index capture may emit useful leaf symbols that
                # are intentionally absent from the packet's semantic symbol
                # table: interface fields, destructured parameters and local
                # bindings are common examples. They belong to the enclosing
                # definition's human judgment rather than becoming one review
                # target per line. Keep a visible nested definition/callback,
                # and stay conservative when its declaration text is missing.
                continue
            structural.append(unit)
            continue
        kind = str(meta.get("kind") or "").lower()
        if kind in _VARIABLE_LIKE_KINDS or module_data_name(source_lines.get(unit.start_line, "")) is not None:
            variables.append(unit)
        else:
            structural.append(unit)

    kept_variables: list[ReviewUnit] = []
    for unit in variables:
        width = unit.end_line - unit.start_line + 1
        nested_in_structure = any(_strictly_contains(parent, unit) for parent in structural)
        declaration = source_lines.get(unit.start_line, "")
        callable_binding = _function_valued_binding(declaration) if declaration else False
        locally_scoped = bool(declaration and declaration[:1].isspace())

        if locally_scoped or nested_in_structure:
            if callable_binding:
                kept_variables.append(unit)
            elif not declaration and width >= _MIN_COHESIVE_VARIABLE_LINES:
                # An unseen declaration may be substantial; keep its own target.
                kept_variables.append(unit)
            continue

        # Usage raises attention, not the number of human judgments needed for
        # a run of short data declarations.
        if callable_binding or width >= _MIN_COHESIVE_VARIABLE_LINES:
            kept_variables.append(unit)

    # If one function-valued binding encloses another variable candidate, keep
    # the outer callback as the judgment boundary unless the inner declaration
    # is itself visibly callable. This removes promise-chain temporaries while
    # preserving explicit nested helper functions.
    final_variables: list[ReviewUnit] = []
    for unit in kept_variables:
        declaration = source_lines.get(unit.start_line, "")
        callable_binding = _function_valued_binding(declaration) if declaration else False
        enclosing = [other for other in kept_variables if _strictly_contains(other, unit)]
        if enclosing and not callable_binding:
            continue
        final_variables.append(unit)
    return tuple(structural + final_variables)


def _deepest_owner(line: int, symbols: Sequence[ReviewUnit]) -> ReviewUnit | None:
    candidates = [unit for unit in symbols if unit.start_line <= line <= unit.end_line]
    if not candidates:
        return None
    # The smallest definition window is the deepest containment owner.  Stable
    # name is the final tie-break only to make pathological equal windows fully
    # deterministic.
    return min(candidates, key=lambda unit: (unit.end_line - unit.start_line, -unit.start_line, unit.symbol))


def derive_review_targets(
    units: Sequence[ReviewUnit],
    frontier: Sequence[FrontierEntry],
    packet: Mapping[str, Any] | Any | None,
    *,
    annotations: Sequence[Annotation] = (),
    evidence: Sequence[ReviewEvidence] = (),
    routine_proofs: Mapping[str, RoutineProof] | None = None,
    question_reasons: Mapping[str, Sequence[QueueReason]] | None = None,
) -> tuple[ReviewTarget, ...]:
    """Derive the current revision's non-overlapping reader targets.

    No rows are persisted. The same units + packet + frontier always produce
    the same targets and order-independent progress denominator. Routine
    needs a ``routine_proofs`` entry; callers that pass none get no Routine.
    """

    data = _packet_mapping(packet)
    entries = {entry.unit_key: entry for entry in frontier}
    by_path: dict[str, list[ReviewUnit]] = {}
    for unit in units:
        by_path.setdefault(unit.path, []).append(unit)
    annotations_by_path: dict[str, list[Annotation]] = {}
    for annotation in annotations:
        annotations_by_path.setdefault(annotation.anchor.path, []).append(annotation)
    evidence_by_path: dict[str, list[ReviewEvidence]] = {}
    for item in evidence:
        if item.path:
            evidence_by_path.setdefault(item.path, []).append(item)
    symbol_metadata_by_path = _symbol_metadata_by_path(data)
    files = {
        str(item.get("path") or ""): item
        for item in data.get("files", ())
        if isinstance(item, Mapping) and str(item.get("path") or "")
    }

    dependency_units = tuple(unit for unit in units if unit.kind == "dependency")
    semantic_lock_paths = {
        unit.path
        for unit in dependency_units
        if unit.path.rsplit("/", 1)[-1] in {"uv.lock", "poetry.lock", "package-lock.json", "Cargo.lock"}
    }
    targets: list[ReviewTarget] = []
    for path in sorted(by_path):
        # Supported generated lockfiles with semantic component units do not
        # also become raw hunk/file judgment targets. Source remains navigable.
        if path in semantic_lock_paths:
            continue
        path_target_start = len(targets)
        path_annotations = annotations_by_path.get(path, ())
        path_units = by_path[path]
        file_unit = next((unit for unit in path_units if unit.kind == "file"), None)
        if file_unit is None:
            continue
        path_verification = _verification_summary(evidence_by_path.get(path, ()))
        entry = files.get(path)
        hunks = list(entry.get("hunks") or ()) if entry is not None else []
        file_fallback = (
            entry is None or bool(entry.get("is_binary")) or bool(entry.get("submodule_pointer")) or not hunks
        )
        if file_fallback:
            additions = _as_int(entry.get("additions"), 0) if entry is not None else 0
            deletions = _as_int(entry.get("deletions"), 0) if entry is not None else 0
            targets.append(
                _target(
                    file_unit,
                    (),
                    entries.get(file_unit.unit_key),
                    path_annotations,
                    verification=path_verification,
                    additions=additions,
                    deletions=deletions,
                )
            )
            continue

        # ``file_fallback`` above is true whenever the packet entry is absent.
        # Keep that runtime invariant explicit for static type checking below.
        assert entry is not None

        hunk_units = {unit.ordinal: unit for unit in path_units if unit.kind == "hunk"}
        source_lines = new_side_line_text(entry.get("hunks") or ())
        symbols = _review_worthy_symbols(
            path_units,
            symbol_metadata_by_path.get(path, {}),
            source_lines,
        )
        symbol_lines: dict[str, dict[tuple[TargetSide, int], list[int]]] = {}
        hunk_lines: dict[int, dict[TargetSide, list[int]]] = {}
        hard_fallback = False

        def assign_symbol(
            unit: ReviewUnit,
            side: TargetSide,
            ordinal: int,
            lines: Sequence[int],
            *,
            assignments: dict[str, dict[tuple[TargetSide, int], list[int]]] = symbol_lines,
        ) -> None:
            if not lines:
                return
            assignments.setdefault(unit.unit_key, {}).setdefault((side, ordinal), []).extend(lines)

        def assign_hunk(
            ordinal: int,
            side: TargetSide,
            lines: Sequence[int],
            *,
            assignments: dict[int, dict[TargetSide, list[int]]] = hunk_lines,
        ) -> None:
            if not lines:
                return
            assignments.setdefault(ordinal, {}).setdefault(side, []).extend(lines)

        for ordinal, raw_hunk in enumerate(hunks):
            if not isinstance(raw_hunk, Mapping):
                hard_fallback = True
                break
            hunk_unit = hunk_units.get(ordinal)
            if hunk_unit is None:
                hard_fallback = True
                break
            blocks = _change_blocks(raw_hunk)
            if blocks is None:
                fallback = _fallback_spans(raw_hunk, ordinal)
                if not fallback:
                    hard_fallback = True
                    break
                for span in fallback:
                    assign_hunk(
                        ordinal,
                        span.side,
                        range(span.start_line, span.end_line + 1),
                    )
                continue
            if not blocks:
                # A textual hunk with no parseable changed row is not something
                # we can honestly split into symbols. Exact producer ranges may
                # still support a hunk target; without them the whole file is the
                # only truthful judgment boundary.
                fallback = _fallback_spans(raw_hunk, ordinal)
                if not fallback:
                    hard_fallback = True
                    break
                for span in fallback:
                    assign_hunk(ordinal, span.side, range(span.start_line, span.end_line + 1))
                continue
            for block in blocks:
                owners: list[ReviewUnit | None] = []
                for line in block.new_lines:
                    owner = _deepest_owner(line, symbols)
                    owners.append(owner)
                    if owner is None:
                        assign_hunk(ordinal, "new", (line,))
                    else:
                        assign_symbol(owner, "new", ordinal, (line,))

                removal_owner: ReviewUnit | None = None
                concrete = [owner for owner in owners if owner is not None]
                if block.new_lines and len(concrete) == len(owners) and len({item.unit_key for item in concrete}) == 1:
                    # A replacement/addition block proves old and new content
                    # belong to one surviving definition only when every new
                    # changed line has that same trustworthy owner. A pure
                    # deletion has no surviving changed line to prove ownership;
                    # its join point may merely be the following symbol.
                    removal_owner = concrete[0]

                if removal_owner is None:
                    assign_hunk(ordinal, "old", block.old_lines)
                else:
                    # The owner keeps its own deleted body. A definition deleted
                    # beside it is not its own: its fingerprint cannot vouch for
                    # that text, so it goes to the hunk, whose patch fingerprint
                    # can. ``owned_old_count`` is the rule the capture hashes by.
                    owned = owned_old_count(
                        block,
                        source_lines,
                        removal_owner.start_line,
                        removal_owner.end_line,
                        owner_is_group=removal_owner.kind == "document_section",
                    )
                    assign_symbol(removal_owner, "old", ordinal, block.old_lines[:owned])
                    assign_hunk(ordinal, "old", block.old_lines[owned:])

        if hard_fallback:
            additions = _as_int(entry.get("additions"), 0)
            deletions = _as_int(entry.get("deletions"), 0)
            del targets[path_target_start:]
            targets.append(
                _target(
                    file_unit,
                    (),
                    entries.get(file_unit.unit_key),
                    path_annotations,
                    verification=path_verification,
                    additions=additions,
                    deletions=deletions,
                )
            )
            continue

        units_by_key = {unit.unit_key: unit for unit in path_units}
        # Every key this path is about to produce, known before the first target
        # is built so each one can tell an anchor of its own from a sibling's.
        path_target_keys = frozenset(symbol_lines) | frozenset(
            hunk_units[hunk_ordinal].unit_key for hunk_ordinal in hunk_lines if hunk_ordinal in hunk_units
        )
        for key, symbol_assignments in symbol_lines.items():
            symbol_unit = units_by_key[key]
            spans: list[TargetSpan] = []
            for (symbol_side, symbol_ordinal), lines in symbol_assignments.items():
                spans.extend(_compress(lines, side=symbol_side, hunk_ordinal=symbol_ordinal))
            targets.append(
                _target(
                    symbol_unit,
                    spans,
                    entries.get(key),
                    path_annotations,
                    verification=path_verification,
                    path_target_keys=path_target_keys,
                )
            )

        for hunk_ordinal, hunk_assignments in hunk_lines.items():
            hunk_unit = hunk_units.get(hunk_ordinal)
            if hunk_unit is None:
                hard_fallback = True
                break
            spans = []
            for hunk_side, lines in hunk_assignments.items():
                spans.extend(_compress(lines, side=hunk_side, hunk_ordinal=hunk_ordinal))
            targets.append(
                _target(
                    hunk_unit,
                    spans,
                    entries.get(hunk_unit.unit_key),
                    path_annotations,
                    verification=path_verification,
                    path_target_keys=path_target_keys,
                )
            )

        if hard_fallback or len(targets) == path_target_start:
            # Last-resort safety: no changed file disappears because a packet
            # projection was partial or unexpectedly empty. Everything appended
            # since ``path_target_start`` belongs to this path, so truncating is
            # cheaper than rescanning the growing global target list.
            del targets[path_target_start:]
            targets.append(
                _target(
                    file_unit,
                    (),
                    entries.get(file_unit.unit_key),
                    path_annotations,
                    verification=path_verification,
                    additions=_as_int(entry.get("additions"), 0),
                    deletions=_as_int(entry.get("deletions"), 0),
                )
            )

    for unit in dependency_units:
        targets.append(
            _target(
                unit,
                (),
                entries.get(unit.unit_key),
                annotations_by_path.get(unit.path, ()),
            )
        )

    keys_by_path = target_keys_by_path(targets)
    requested = frozenset(
        target.target_id
        for target in targets
        if target_has_unresolved_request_change(
            target,
            annotations_by_path.get(target.path, ()),
            path_target_keys=keys_by_path.get(target.path, frozenset()),
        )
    )
    return assign_review_priority(
        targets,
        request_change_target_ids=requested,
        unattributed_request_change_paths=paths_with_unattributed_request_change(targets, annotations),
        routine_proofs=routine_proofs,
        question_reasons=question_reasons,
    )


def order_review_targets(
    targets: Sequence[ReviewTarget], *, order: TargetOrder = "recommended"
) -> tuple[ReviewTarget, ...]:
    """``recommended`` is the canonical queue order; ``file`` is source order."""

    if order == "file":
        return tuple(sorted(targets, key=lambda target: (target.path, target.start_line, target.kind, target.unit_key)))
    return tuple(
        sorted(
            targets,
            key=lambda target: (target.priority.position, target.path, target.start_line, target.unit_key),
        )
    )


# Modules whose functions compute stored derivations (targets, progress,
# frontiers, Snapshot history numbers). Their Review-package imports are
# followed from the source, so a helper moved elsewhere stays covered.
_DERIVATION_ROOTS = ("targets", "revisions")
_REVIEW_PACKAGE = "lemoncrow.pro.capabilities.review"
# Compute a request may spend refreshing results that older code derived.
DERIVED_REFRESH_BUDGET_S = 1.0


def _derivation_sources() -> list[Path]:
    """The derivation modules plus every Review module they import, from their import statements."""

    package = Path(__file__).resolve().parent

    def source(module: str) -> Path | None:
        relative = Path(*module.split("."))
        for candidate in (package / relative.with_suffix(".py"), package / relative / "__init__.py"):
            if candidate.is_file():
                return candidate
        return None

    found: dict[str, Path] = {}
    todo = list(_DERIVATION_ROOTS)
    while todo:
        module = todo.pop()
        path = None if module in found else source(module)
        if path is None:
            continue
        found[module] = path
        try:
            tree = ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            continue
        parent = module.split(".")[: -1 if path.name != "__init__.py" else None]
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = parent[: len(parent) - (node.level - 1)] if node.level > 1 else parent
                    absolute = ".".join([_REVIEW_PACKAGE, *base, *([node.module] if node.module else [])])
                else:
                    absolute = node.module or ""
                names = [absolute, *(f"{absolute}.{alias.name}" for alias in node.names)]
            todo.extend(name[len(_REVIEW_PACKAGE) + 1 :] for name in names if name.startswith(f"{_REVIEW_PACKAGE}."))
    return sorted(found.values())


@functools.cache
def derivation_code_stamp() -> str:
    """Identity of the Review code that derives targets, frontiers, progress and history.

    Stored derivations (``ReviewStore.derived_results``) include it in their
    stamp. It hashes only the deriving modules and what they import: hashing
    the whole package made every edit to the API, capture or preview code
    re-derive every stored result -- minutes of CPU for a long Review.
    """

    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for source in _derivation_sources():
        digest.update(str(source.relative_to(package)).encode("utf-8"))
        try:
            digest.update(source.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def derivation_stamp(*inputs: object) -> str:
    """Stamp of a stored derivation: its exact inputs, then the code that derived it."""

    inputs_digest = hashlib.sha256(repr(inputs).encode("utf-8", "surrogatepass")).hexdigest()
    return f"{inputs_digest}:{derivation_code_stamp()}"


class DerivedRefresh:
    """Decides, per stored result, whether to serve it or derive it again.

    A result stamped with the current inputs and code is served. One with other
    inputs, or none at all, is always derived. One whose inputs match but whose
    code differs (or whose stamp predates the inputs/code split) is derived
    again while this request has budget left, newest first, and served as
    stored after that; later requests refresh the rest. Without this, one
    code change re-derived a whole long Review in a single request and
    starved every other Reader request of CPU.
    """

    def __init__(self, budget_s: float | None = None) -> None:
        # Any positive budget refreshes at least one row per request, so
        # repeated reads always converge.
        self.budget_s = DERIVED_REFRESH_BUDGET_S if budget_s is None else budget_s
        self.spent_s = 0.0

    def serve(self, stored_stamp: str | None, stamp: str) -> bool:
        if stored_stamp is None:
            return False
        if stored_stamp == stamp:
            return True
        if self.spent_s < self.budget_s:
            return False
        stored_inputs, separator, _code = stored_stamp.partition(":")
        return not separator or stored_inputs == stamp.partition(":")[0]

    @contextlib.contextmanager
    def deriving(self) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.spent_s += time.monotonic() - started


def review_progress(targets: Sequence[ReviewTarget]) -> ReviewProgress:
    return ReviewProgress(
        target_count=len(targets),
        reviewed=sum(target.state == "reviewed" for target in targets),
        changed_since_review=sum(target.state == "changed_since_review" for target in targets),
        needs_changes=sum(target.state == "needs_changes" for target in targets),
        unreviewed=sum(target.state == "unreviewed" for target in targets),
        unknown=sum(target.state == "unknown" for target in targets),
        mechanical=sum(target.attention_level == "mechanical" for target in targets),
        later=sum(target.state == "later" for target in targets),
        decision=sum(target.answer.category == "decision" for target in targets),
        quick_check=sum(target.answer.category == "quick_check" for target in targets),
        accepted_by_rule=sum(target.answer.category == "accepted_by_rule" for target in targets),
    )


def answers_payload(targets: Sequence[ReviewTarget]) -> dict[str, Any]:
    """How many parts are decisions, quick checks and accepted by rule, and how many each rule accepted.

    Listed, never judged: an accepted-by-rule part is counted here and in no judged tally.
    """

    return answer_counts([target.answer for target in targets])


_OUTLINE_SECTION = {"review_first": "review_first", "understand": "unreviewed", "routine": "mechanical"}


def build_review_outline(targets: Sequence[ReviewTarget]) -> tuple[ReviewOutlineItem, ...]:
    """One row per file, in canonical file order.

    A file's place is its earliest queue entry (spec §4.6). Outstanding tiers
    sort before done work, so first appearance in queue order *is* the file
    order; nothing here re-ranks. A mixed file shows its first unfinished
    item, not one permanent risk label.
    """

    by_path: dict[str, list[ReviewTarget]] = {}
    for target in sorted(targets, key=lambda item: item.priority.position):
        by_path.setdefault(target.path, []).append(target)
    rows: list[ReviewOutlineItem] = []
    for path, path_targets in by_path.items():
        ranks = [target.attention_rank for target in path_targets if target.attention_rank > 0]
        reasons: list[str] = []
        for target in path_targets:
            for reason in target.reasons:
                if reason_is_informative(reason) and reason not in reasons:
                    reasons.append(reason)
        outstanding = [target for target in path_targets if target.priority.outstanding]
        first = outstanding[0] if outstanding else None
        first_reason = first.priority.reasons[0] if first is not None and first.priority.reasons else None
        rows.append(
            ReviewOutlineItem(
                path=path,
                target_count=len(path_targets),
                reviewed=sum(target.state == "reviewed" for target in path_targets),
                changed_since_review=sum(target.state == "changed_since_review" for target in path_targets),
                needs_changes=sum(target.state == "needs_changes" for target in path_targets),
                unreviewed=sum(target.state == "unreviewed" for target in path_targets),
                unknown=sum(target.state == "unknown" for target in path_targets),
                later=sum(target.state == "later" for target in path_targets),
                mechanical=sum(target.attention_level == "mechanical" for target in path_targets),
                attention_rank=min(ranks) if ranks else 0,
                reasons=tuple(reasons),
                section=_OUTLINE_SECTION.get(first.priority.attention, "unreviewed") if first is not None else "done",
                position=path_targets[0].priority.position,
                next_target_id=first.target_id if first is not None else "",
                next_reason=first_reason.detail if first_reason is not None else "",
                next_reason_code=first_reason.code if first_reason is not None else "",
                review_first=sum(target.priority.attention == "review_first" for target in outstanding),
                understand=sum(target.priority.attention == "understand" for target in outstanding),
                routine=sum(target.priority.attention == "routine" for target in outstanding),
            )
        )
    return tuple(rows)


def revision_target_delta(
    previous: Sequence[ReviewTarget],
    current: Sequence[ReviewTarget],
    *,
    aliases: Sequence[tuple[str, str]] = (),
) -> RevisionTargetDelta:
    """Project raw reconciliation into the queues a reviewer actually sees."""

    alias_map = dict(aliases)
    previous_by_current_key = {alias_map.get(target.unit_key, target.unit_key): target for target in previous}
    current_keys = {target.unit_key for target in current}
    preserved: list[ReviewTarget] = []
    reopened: list[ReviewTarget] = []
    added: list[ReviewTarget] = []
    active: list[ReviewTarget] = []

    for target in current:
        before = previous_by_current_key.get(target.unit_key)
        if before is None:
            added.append(target)
        elif before.state == "reviewed" and target.state == "reviewed":
            preserved.append(target)
        elif before.state == "reviewed" and target.state != "reviewed":
            reopened.append(target)
        elif before.state == "needs_changes" and target.state == "changed_since_review":
            # The requested change arrived: that judgment reopens for re-review.
            reopened.append(target)

        has_unresolved_thread = (
            target.annotation_counts.open > 0
            or target.annotation_counts.orphaned > 0
            or target.annotation_counts.addressed_needs_rereview > 0
        )
        has_unresolved_check = target.verification.fail_count > 0 or target.verification.unknown_count > 0
        if target.state != "reviewed" or has_unresolved_thread or has_unresolved_check:
            active.append(target)

    removed = tuple(
        target for target in previous if alias_map.get(target.unit_key, target.unit_key) not in current_keys
    )
    return RevisionTargetDelta(
        preserved=tuple(preserved),
        reopened=tuple(reopened),
        added=tuple(added),
        removed=removed,
        active=tuple(active),
    )


def _delta_target_payload(target: ReviewTarget) -> dict[str, Any]:
    return {
        "target_id": target.target_id,
        "unit_key": target.unit_key,
        "kind": target.kind,
        "path": target.path,
        "label": target.label,
        "symbol": target.symbol,
        "start_line": target.start_line,
        "state": target.state,
        "annotation_counts": {
            "open": target.annotation_counts.open,
            "orphaned": target.annotation_counts.orphaned,
            "addressed_needs_rereview": target.annotation_counts.addressed_needs_rereview,
        },
    }


def revision_delta_payload(delta: RevisionTargetDelta) -> dict[str, Any]:
    return {
        "preserved": [_delta_target_payload(target) for target in delta.preserved],
        "reopened": [_delta_target_payload(target) for target in delta.reopened],
        "added": [_delta_target_payload(target) for target in delta.added],
        "removed": [_delta_target_payload(target) for target in delta.removed],
        "active": [_delta_target_payload(target) for target in delta.active],
    }


def label_symbol_moves(
    payloads: Sequence[dict[str, Any]], provenance: Mapping[str, tuple[str, str, str]]
) -> list[dict[str, Any]]:
    """Add ``moved_from`` / ``renamed_from`` to the target payloads whose mark followed a symbol.

    *provenance* is ``unit_key -> (kind, previous path, previous qualified name)`` from one store
    read, so the Reader gets it with the targets it already fetches. ``symbol`` is present only when
    the qualified name differs from the target's own (a pure move keeps its name).

    ``renamed_from`` is a hint, **not proof the change is routine**: the same-body test also matches
    a body whose only change is a use of the old name elsewhere than the definition (an attribute
    access, a string). Whoever batches renamed parts (P11-2) has to compare the full content.
    """

    if not provenance:
        return list(payloads)
    out: list[dict[str, Any]] = []
    for payload in payloads:
        found = provenance.get(str(payload.get("unit_key") or ""))
        if found is None:
            out.append(payload)
            continue
        kind, path, symbol = found
        origin: dict[str, str] = {"path": path}
        if symbol and symbol != str(payload.get("symbol") or ""):
            origin["symbol"] = symbol
        out.append({**payload, "moved_from" if kind == "moved" else "renamed_from": origin})
    return out


def target_payload(target: ReviewTarget) -> dict[str, Any]:
    return {
        "target_id": target.target_id,
        "unit_key": target.unit_key,
        "kind": target.kind,
        "path": target.path,
        "label": target.label,
        "symbol": target.symbol,
        "start_line": target.start_line,
        "end_line": target.end_line,
        "hunk_ordinals": list(target.hunk_ordinals),
        "spans": [
            {
                "side": span.side,
                "start_line": span.start_line,
                "end_line": span.end_line,
                "hunk_ordinal": span.hunk_ordinal,
            }
            for span in target.spans
        ],
        "state": target.state,
        "changed_since_mark": target.changed_since_mark,
        "reviewed_revision_id": target.reviewed_revision_id,
        "attention_rank": target.attention_rank,
        "attention_level": target.attention_level,
        "reasons": list(target.reasons),
        "additions": target.additions,
        "deletions": target.deletions,
        "fingerprint_method": target.fingerprint_method,
        "content_fingerprint": target.content_fingerprint,
        "verification": {
            "pass": target.verification.pass_count,
            "fail": target.verification.fail_count,
            "unknown": target.verification.unknown_count,
        },
        "annotation_counts": {
            "open": target.annotation_counts.open,
            "orphaned": target.annotation_counts.orphaned,
            "addressed_needs_rereview": target.annotation_counts.addressed_needs_rereview,
        },
        "priority": priority_payload(target.priority),
        "answer": answer_payload(target.answer),
    }


def priorities_payload(targets: Sequence[ReviewTarget]) -> dict[str, dict[str, Any]]:
    """Every target's current queue placement, keyed by target id.

    A judgment moves positions of targets it did not touch; a reply carrying
    only the judged target would leave the client's queue stale.
    """

    return {target.target_id: priority_payload(target.priority) for target in targets}


def progress_payload(progress: ReviewProgress, *, answers: bool = True) -> dict[str, int]:
    """Counts per state. ``answers=False`` leaves out the decision / quick check / accepted-by-rule counts.

    Those need the queue's own facts (policy, wide reach, safeguards), which only the Reader API derives; a
    surface that cannot derive them (the CLI) prints none rather than a different answer.
    """

    counts = {
        "target_count": progress.target_count,
        "reviewed": progress.reviewed,
        "changed_since_review": progress.changed_since_review,
        "needs_changes": progress.needs_changes,
        "unreviewed": progress.unreviewed,
        "unknown": progress.unknown,
        "mechanical": progress.mechanical,
        "later": progress.later,
    }
    if answers:
        counts["decision"] = progress.decision
        counts["quick_check"] = progress.quick_check
        counts["accepted_by_rule"] = progress.accepted_by_rule
    return counts


def outline_payload(item: ReviewOutlineItem) -> dict[str, Any]:
    return {
        "path": item.path,
        "target_count": item.target_count,
        "reviewed": item.reviewed,
        "changed_since_review": item.changed_since_review,
        "needs_changes": item.needs_changes,
        "unreviewed": item.unreviewed,
        "unknown": item.unknown,
        "later": item.later,
        "mechanical": item.mechanical,
        "attention_rank": item.attention_rank,
        "reasons": list(item.reasons),
        "section": item.section,
        "position": item.position,
        "next_target_id": item.next_target_id,
        "next_reason": item.next_reason,
        "next_reason_code": item.next_reason_code,
        "review_first": item.review_first,
        "understand": item.understand,
        "routine": item.routine,
    }


__all__ = [
    "AttentionLevel",
    "ReviewOutlineItem",
    "ReviewProgress",
    "ReviewTarget",
    "RevisionTargetDelta",
    "TargetAnnotationCounts",
    "TargetOrder",
    "TargetSpan",
    "TargetVerification",
    "answers_payload",
    "build_review_outline",
    "derive_review_targets",
    "label_symbol_moves",
    "order_review_targets",
    "outline_payload",
    "paths_with_unattributed_request_change",
    "priorities_payload",
    "progress_payload",
    "reason_is_informative",
    "review_progress",
    "revision_delta_payload",
    "revision_target_delta",
    "target_keys_by_path",
    "target_payload",
]
