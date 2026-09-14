"""Reader-facing review targets: one human judgment per changed span.

Raw :class:`ReviewUnit` rows deliberately overlap: a file contains hunks and a
symbol may contain both a hunk and another symbol.  They are the durable
identity/fingerprint substrate, not a progress denominator.  This module is the
pure projection that turns those overlapping units into a non-overlapping set
of things a human can actually review.

The invariant is intentionally stronger than "pick the nicest unit": every
changed line represented by a trustworthy patch is assigned exactly once.  A
stable, fingerprintable symbol wins for new-side text it owns; anything we
cannot attribute safely falls back to its hunk.  Files whose textual geometry
is unavailable fall back to the file unit.  The chosen target still stores the
underlying unit key, so no second persistence model is introduced.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    FrontierEntry,
    MarkState,
    ReviewEvidence,
    ReviewUnit,
)

TargetKind = Literal["symbol", "hunk", "file"]
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
# A deleted row that opens a definition of its own. Deliberately an allowlist of
# declaration keywords with a name after them: a keyword this misses costs one
# extra line on a symbol's old span, while a false positive would split an
# ordinary rewrite into two review targets.
_DEFINITION_HEADER = re.compile(
    r"^\s*(?:(?:export|default|public|private|protected|internal|static|final"
    r"|abstract|override|open|pub|declare|inline)\s+)*"
    r"(?:async\s+)?"
    r"(?:def|class|function|func|fn|struct|trait|impl|enum|interface"
    r"|namespace|module|record|object|type|sub|proc)\s+(?:\([^)]*\)\s*)?[A-Za-z_$]"
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
    verification: TargetVerification = TargetVerification()
    annotation_counts: TargetAnnotationCounts = TargetAnnotationCounts()


@dataclass(frozen=True)
class ReviewProgress:
    target_count: int = 0
    reviewed: int = 0
    changed_since_review: int = 0
    needs_changes: int = 0
    unreviewed: int = 0
    unknown: int = 0
    mechanical: int = 0


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


@dataclass(frozen=True)
class RevisionTargetDelta:
    """Human-facing effect of one revision transition on review targets."""

    preserved: tuple[ReviewTarget, ...] = ()
    reopened: tuple[ReviewTarget, ...] = ()
    added: tuple[ReviewTarget, ...] = ()
    removed: tuple[ReviewTarget, ...] = ()
    active: tuple[ReviewTarget, ...] = ()


@dataclass(frozen=True)
class _ChangeBlock:
    old_lines: tuple[int, ...]
    new_lines: tuple[int, ...]
    new_anchor: int
    old_text: tuple[str, ...] = ()
    """The deleted rows' own source text, parallel to *old_lines*.

    The base blob is not an input here, and the deleted text is the only
    evidence in the packet about what the old side of a block actually was.
    """


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


def _patch_rows(patch: str) -> list[str]:
    """The rows of a patch body, split only where a unified diff ends one.

    A diff row ends at ``\\n`` and nowhere else. ``str.splitlines`` also breaks on
    form feeds, vertical tabs, the file/group/record separators and the Unicode
    line separators, so a changed source line containing one of those would
    become two rows -- and a tail that happened to start with ``' '``, ``'+'`` or
    ``'-'`` would be read as another diff row, shifting every line number after
    it. The final empty element is the body's own trailing newline, not a row.
    """

    rows = patch.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    return rows


def _new_side_line_text(entry: Mapping[str, Any]) -> dict[int, str]:
    """Best-effort new-side source lines available inside captured patch hunks."""

    out: dict[int, str] = {}
    for raw_hunk in entry.get("hunks") or ():
        if not isinstance(raw_hunk, Mapping):
            continue
        patch = str(raw_hunk.get("patch") or "")
        if not patch:
            continue
        old_line = _as_int(raw_hunk.get("old_start"), 0)
        new_line = _as_int(raw_hunk.get("new_start"), 0)
        for raw in _patch_rows(patch):
            if raw.startswith("\\"):
                continue
            prefix = raw[:1]
            if prefix == " ":
                if new_line > 0:
                    out[new_line] = raw[1:]
                old_line += 1
                new_line += 1
            elif prefix == "+":
                if new_line > 0:
                    out[new_line] = raw[1:]
                new_line += 1
            elif prefix == "-":
                old_line += 1
            elif not raw:
                # An empty row is an empty context line whose single trailing
                # space a producer stripped. It is still one line on both sides:
                # skipping it without moving the cursors would file every later
                # row of this hunk one line too high.
                if new_line > 0:
                    out[new_line] = ""
                old_line += 1
                new_line += 1
            else:
                # A row with some other origin character leaves both cursors
                # where they are, so every later row of this hunk would be filed
                # under the wrong line number. Stopping keeps the text read so
                # far honest.
                break
    return out


def _change_blocks(hunk: Mapping[str, Any]) -> tuple[_ChangeBlock, ...] | None:
    """Parse exact +/- geometry from a captured hunk body.

    ``None`` means the body was unavailable or unrecognised.  In that case the
    caller must use one hunk fallback target; it must not manufacture symbol
    precision from the context-inclusive hunk header.
    """

    patch = str(hunk.get("patch") or "")
    if not patch:
        return None
    old_line = _as_int(hunk.get("old_start"), 0)
    new_line = _as_int(hunk.get("new_start"), 0)
    blocks: list[_ChangeBlock] = []
    old_changed: list[int] = []
    old_text: list[str] = []
    new_changed: list[int] = []
    block_anchor = max(1, new_line)

    def flush() -> None:
        nonlocal old_changed, old_text, new_changed, block_anchor
        if old_changed or new_changed:
            blocks.append(_ChangeBlock(tuple(old_changed), tuple(new_changed), max(1, block_anchor), tuple(old_text)))
        old_changed = []
        old_text = []
        new_changed = []
        block_anchor = max(1, new_line)

    for raw in _patch_rows(patch):
        if raw.startswith("\\"):
            continue
        if not raw:
            # An empty patch row still has an origin prefix in a valid unified
            # diff.  Seeing a truly empty row means this body cannot be trusted.
            return None
        prefix = raw[0]
        if prefix == " ":
            flush()
            old_line += 1
            new_line += 1
            block_anchor = max(1, new_line)
        elif prefix == "-":
            if not old_changed and not new_changed:
                block_anchor = max(1, new_line)
            old_changed.append(max(1, old_line))
            old_text.append(raw[1:])
            old_line += 1
        elif prefix == "+":
            if not old_changed and not new_changed:
                block_anchor = max(1, new_line)
            new_changed.append(max(1, new_line))
            new_line += 1
        else:
            return None
    flush()
    return tuple(blocks)


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
    if unit.kind == "hunk":
        return f"{unit.path}#{unit.ordinal}"
    return unit.path


def _frontier_state(entry: FrontierEntry | None) -> tuple[MarkState, bool, str]:
    if entry is None:
        return "unreviewed", False, ""
    state = entry.state
    if entry.changed_since_mark and state == "reviewed":
        state = "changed_since_review"
    return state, entry.changed_since_mark, entry.reviewed_revision_id


def reason_is_promoting(reason: str, *, kind: str = "file") -> bool:
    """Whether one ranking reason is a finding rather than a description.

    Attention is only worth anything if it discriminates *within the list it is
    ranking*. Some reasons are carried by every row of their kind and so argue
    for nothing: the churn line on every ranked file, the skim note on every
    generated/vendor file, and the change verb ``units._symbol_reasons`` stamps
    on every symbol unit. Promoting on those puts the entire review in "needs
    attention" and leaves bulk completion with nothing it is allowed to sweep.

    "``N`` known callers" is the one reason whose answer depends on *kind*, and
    it cannot be decided from the string. ``ordering.rank_files`` writes it for
    a file, where it does separate this file from the others; then
    ``units._symbol_reasons`` copies the file's whole reason list onto every
    symbol unit of that file and adds the symbol's own count in the same words.
    Inside one file, therefore, every symbol and hunk target carries it and it
    separates nothing -- which is exactly how bulk completion came to have
    nothing to sweep on any repo whose call index resolves. A count of zero
    says the index found nothing pointing at the row and is never a finding.

    ``api.group_for`` and :func:`_attention_level` decide the same question for
    the two panes of the same reader, so they ask it here rather than keeping a
    copy each.
    """

    text = reason.strip()
    if not text or _CHURN_REASON.match(text):
        return False
    if text in _DESCRIPTIVE_REASONS:
        return False
    callers = _CALLER_COUNT_REASON.match(text)
    if callers is not None:
        return kind == "file" and _as_int(callers.group(1)) > 0
    return True


def _attention_level(unit: ReviewUnit, entry: FrontierEntry | None) -> AttentionLevel:
    state, changed, _ = _frontier_state(entry)
    if state in ("changed_since_review", "needs_changes") or changed:
        return "high"
    if unit.attention_group in ("generated", "vendor"):
        return "mechanical"
    if any(reason_is_promoting(reason, kind=unit.kind) for reason in unit.reasons):
        return "high"
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
        if item.parent_id or item.kind != "request_change" or item.state not in ("open", "orphaned"):
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

    roots = [
        item
        for item in annotations
        if _annotation_belongs_to_target(
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
    ordered_spans = tuple(
        sorted(spans, key=lambda span: (span.hunk_ordinal, span.side, span.start_line, span.end_line))
    )
    return ReviewTarget(
        target_id=f"target:{unit.unit_key}",
        unit_key=unit.unit_key,
        kind=unit.kind if unit.kind in ("symbol", "hunk", "file") else "file",
        path=unit.path,
        label=_unit_label(unit),
        symbol=unit.symbol,
        start_line=unit.start_line,
        end_line=unit.end_line,
        hunk_ordinals=tuple(sorted({span.hunk_ordinal for span in ordered_spans if span.hunk_ordinal >= 0})),
        spans=ordered_spans,
        state=state,
        changed_since_mark=changed,
        reviewed_revision_id=reviewed_revision_id,
        attention_rank=unit.attention_rank,
        attention_level=_attention_level(unit, entry),
        reasons=unit.reasons or ("no ranking signal fired -- review in normal order",),
        additions=_span_size(ordered_spans, "new") if additions is None else additions,
        deletions=_span_size(ordered_spans, "old") if deletions is None else deletions,
        fingerprint_method=unit.fingerprint_method,
        verification=verification or TargetVerification(),
        annotation_counts=_annotation_counts(unit, ordered_spans, annotations, path_target_keys),
    )


def _trustworthy_symbols(units: Sequence[ReviewUnit]) -> tuple[ReviewUnit, ...]:
    counts: dict[tuple[str, str], int] = {}
    for unit in units:
        if unit.kind == "symbol" and unit.symbol:
            key = (unit.path, unit.symbol)
            counts[key] = counts.get(key, 0) + 1
    return tuple(
        unit
        for unit in units
        if unit.kind == "symbol"
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


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip())


def _prelude_row(line: str) -> bool:
    """Whether *line* is blank, a comment or a decorator/annotation.

    These belong to the definition *below* them, which is the same rule
    ``impact._prelude_start`` uses to fence one definition off from the next.
    """

    text = line.strip()
    return not text or text.startswith(("#", "//", "/*", "*", "@"))


def _foreign_definition_start(block: _ChangeBlock, owner_declaration: str) -> int | None:
    """Where in *block*'s deleted rows a definition the owner does not own begins.

    A block whose added lines all belong to one symbol usually also owns the
    lines it deleted -- a body rewritten in place, which is the ordinary case and
    must stay one review target. But the same shape covers a second, dangerous
    one: the symbol's body was rewritten *and a neighbour was deleted beside it*
    in one block. There the owner's ``symbol_body_sha256`` hashes only its own
    surviving body, so the neighbour's deleted lines would ride under a mark
    that cannot notice them -- delete another method next revision and the
    fingerprint, and the reader, do not move.

    Only the deleted text tells the two apart, and only its structure: a row
    that opens a definition at or above the owner's own nesting level is not the
    owner's body. Deeper than the owner is a nested helper inside it, which the
    owner's fingerprint does cover. The definition's prelude -- the blank lines,
    comments and decorators immediately above it -- goes with it.

    ``None`` means every deleted row is the owner's own, so it keeps them all.
    """

    if len(block.old_text) != len(block.old_lines):
        return None
    # An owner whose declaration line is outside the captured patch cannot vouch
    # for its own nesting level, so any definition in the deleted text counts.
    owner_indent = _indent_width(owner_declaration) if owner_declaration.strip() else -1
    for index, text in enumerate(block.old_text):
        if not text.strip():
            continue
        if not (_DEFINITION_HEADER.match(text) or _function_valued_binding(text)):
            continue
        if _indent_width(text) > owner_indent:
            continue
        start = index
        while start > 0 and _prelude_row(block.old_text[start - 1]):
            start -= 1
        return start
    return None


def _function_valued_binding(line: str) -> bool:
    """Whether a variable declaration visibly defines callable behavior."""

    if "=" not in line:
        return False
    rhs = line.split("=", 1)[1].strip()
    if re.match(r"(?:async\s+)?function\b", rhs):
        return True
    if re.match(r"(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", rhs):
        return True
    return re.match(r"(?:useCallback|forwardRef|memo)\s*\(", rhs) is not None


def _review_worthy_symbols(
    units: Sequence[ReviewUnit],
    metadata: Mapping[tuple[str, int], Mapping[str, Any]],
    source_lines: Mapping[int, str],
) -> tuple[ReviewUnit, ...]:
    """Keep semantic definitions; coalesce index-useful local bindings.

    Structural definitions always remain targets. At module scope, a cohesive
    multi-line value may also be a sensible review target. Inside a function or
    class, however, a long computed value is still implementation detail: only
    function-valued bindings (callbacks/components) or variables with real
    external usage evidence become independent judgments.
    """

    trustworthy = _trustworthy_symbols(units)
    if not metadata:
        return trustworthy

    structural: list[ReviewUnit] = []
    variables: list[ReviewUnit] = []
    kinds: dict[str, str] = {}
    for unit in trustworthy:
        meta = metadata.get((unit.symbol, unit.start_line))
        if meta is None:
            structural.append(unit)
            continue
        kind = str(meta.get("kind") or "").lower()
        kinds[unit.unit_key] = kind
        if kind in _VARIABLE_LIKE_KINDS:
            variables.append(unit)
        else:
            structural.append(unit)

    kept_variables: list[ReviewUnit] = []
    for unit in variables:
        meta = metadata.get((unit.symbol, unit.start_line)) or {}
        width = unit.end_line - unit.start_line + 1
        caller_count = _as_int(meta.get("caller_count"), -1)
        externally_significant = caller_count > 0 or meta.get("centrality_rank") is not None
        nested_in_structure = any(_strictly_contains(parent, unit) for parent in structural)
        declaration = source_lines.get(unit.start_line, "")
        callable_binding = _function_valued_binding(declaration) if declaration else False
        locally_scoped = bool(declaration and declaration[:1].isspace())

        if locally_scoped:
            if callable_binding or externally_significant:
                kept_variables.append(unit)
            continue

        if nested_in_structure:
            if callable_binding or externally_significant:
                kept_variables.append(unit)
            elif not declaration and width >= _MIN_COHESIVE_VARIABLE_LINES:
                # The declaration line was outside the captured patch. Preserve
                # the conservative target rather than guessing its initializer.
                kept_variables.append(unit)
            continue

        if callable_binding or externally_significant or width >= _MIN_COHESIVE_VARIABLE_LINES:
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
) -> tuple[ReviewTarget, ...]:
    """Derive the current revision's non-overlapping reader targets.

    No rows are persisted. The same units + packet + frontier always produce
    the same targets and order-independent progress denominator.
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

    targets: list[ReviewTarget] = []
    for path in sorted(by_path):
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
        source_lines = _new_side_line_text(entry)
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
                    # can.
                    boundary = _foreign_definition_start(block, source_lines.get(removal_owner.start_line, ""))
                    if boundary is None:
                        assign_symbol(removal_owner, "old", ordinal, block.old_lines)
                    else:
                        assign_symbol(removal_owner, "old", ordinal, block.old_lines[:boundary])
                        assign_hunk(ordinal, "old", block.old_lines[boundary:])

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

    return order_review_targets(targets, order="recommended")


def _recommended_key(target: ReviewTarget) -> tuple[int, int, int, int, str, int, str]:
    state_order = {
        "changed_since_review": 0,
        "needs_changes": 1,
        "unknown": 2,
        "unreviewed": 3,
        "reviewed": 5,
    }
    level_order = {"high": 0, "normal": 1, "mechanical": 2}
    rank = target.attention_rank
    return (
        state_order.get(target.state, 4),
        level_order[target.attention_level],
        1 if rank == 0 else 0,
        rank,
        target.path,
        target.start_line,
        target.unit_key,
    )


def order_review_targets(
    targets: Sequence[ReviewTarget], *, order: TargetOrder = "recommended"
) -> tuple[ReviewTarget, ...]:
    if order == "file":
        return tuple(sorted(targets, key=lambda target: (target.path, target.start_line, target.kind, target.unit_key)))
    return tuple(sorted(targets, key=_recommended_key))


def review_progress(targets: Sequence[ReviewTarget]) -> ReviewProgress:
    return ReviewProgress(
        target_count=len(targets),
        reviewed=sum(target.state == "reviewed" for target in targets),
        changed_since_review=sum(target.state == "changed_since_review" for target in targets),
        needs_changes=sum(target.state == "needs_changes" for target in targets),
        unreviewed=sum(target.state == "unreviewed" for target in targets),
        unknown=sum(target.state == "unknown" for target in targets),
        mechanical=sum(target.attention_level == "mechanical" for target in targets),
    )


def build_review_outline(targets: Sequence[ReviewTarget]) -> tuple[ReviewOutlineItem, ...]:
    by_path: dict[str, list[ReviewTarget]] = {}
    for target in targets:
        by_path.setdefault(target.path, []).append(target)
    rows: list[ReviewOutlineItem] = []
    for path, path_targets in by_path.items():
        ranks = [target.attention_rank for target in path_targets if target.attention_rank > 0]
        reasons: list[str] = []
        for target in path_targets:
            for reason in target.reasons:
                # The outline is an attention surface, not a dump of ranking
                # descriptors. Churn, generic symbol-change labels and copied
                # caller counts must not manufacture a "Needs attention" file.
                if reason_is_promoting(reason, kind=target.kind) and reason not in reasons:
                    reasons.append(reason)
        rows.append(
            ReviewOutlineItem(
                path=path,
                target_count=len(path_targets),
                reviewed=sum(target.state == "reviewed" for target in path_targets),
                changed_since_review=sum(target.state == "changed_since_review" for target in path_targets),
                needs_changes=sum(target.state == "needs_changes" for target in path_targets),
                unreviewed=sum(target.state == "unreviewed" for target in path_targets),
                unknown=sum(target.state == "unknown" for target in path_targets),
                mechanical=sum(target.attention_level == "mechanical" for target in path_targets),
                attention_rank=min(ranks) if ranks else 0,
                reasons=tuple(reasons),
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                1 if row.attention_rank == 0 else 0,
                row.attention_rank,
                row.path,
            ),
        )
    )


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
    }


def progress_payload(progress: ReviewProgress) -> dict[str, int]:
    return {
        "target_count": progress.target_count,
        "reviewed": progress.reviewed,
        "changed_since_review": progress.changed_since_review,
        "needs_changes": progress.needs_changes,
        "unreviewed": progress.unreviewed,
        "unknown": progress.unknown,
        "mechanical": progress.mechanical,
    }


def outline_payload(item: ReviewOutlineItem) -> dict[str, Any]:
    return {
        "path": item.path,
        "target_count": item.target_count,
        "reviewed": item.reviewed,
        "changed_since_review": item.changed_since_review,
        "needs_changes": item.needs_changes,
        "unreviewed": item.unreviewed,
        "unknown": item.unknown,
        "mechanical": item.mechanical,
        "attention_rank": item.attention_rank,
        "reasons": list(item.reasons),
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
    "build_review_outline",
    "derive_review_targets",
    "order_review_targets",
    "outline_payload",
    "paths_with_unattributed_request_change",
    "progress_payload",
    "reason_is_promoting",
    "review_progress",
    "revision_delta_payload",
    "revision_target_delta",
    "target_keys_by_path",
    "target_payload",
]
