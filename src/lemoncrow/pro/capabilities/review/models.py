"""The `lc review` packet contract — pure data, zero I/O.

Why this module exists: four independent producers (raw diff, semantic change
impact, review ordering, agent provenance) each fill a disjoint slice of the
*same* packet, and none of them can be written until all of them agree on the
shape. Pinning the contract in one stdlib-only module is what lets those
producers land separately, keeps `--json` a mechanical projection of the
dataclasses, and makes an unfilled slice an empty tuple rather than a crash.

Every collection field defaults to empty and every unknown scalar defaults to
``None``/sentinel, so a packet built from the diff alone is already valid.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Bump on any field removal or type change. Additive changes keep the number
# where it is; consumers must ignore unknown keys.
#
# 2: ``DiffHunk.patch`` -- appended, so every existing key keeps its position
#    and its meaning. The number moved anyway because a *persisted* packet is
#    now read back long after it was written (``review_revisions.packet_path``
#    stores this contract's version alongside the artifact), and a reader that
#    cannot tell "this capture predates hunk bodies" from "this capture had no
#    hunk bodies" would report an empty ``patch`` as content rather than as age.
SCHEMA_VERSION = 2

FileStatus = Literal["added", "modified", "deleted", "renamed", "copied", "typechange"]
FILE_STATUS_VALUES: tuple[str, ...] = (
    "added",
    "modified",
    "deleted",
    "renamed",
    "copied",
    "typechange",
)

FileCategory = Literal["production", "test", "generated", "vendor", "docs", "config"]
FILE_CATEGORY_VALUES: tuple[str, ...] = (
    "production",
    "test",
    "generated",
    "vendor",
    "docs",
    "config",
)
# Review-attention order, most-to-least. Used as the ordering tie-break so the
# ranking is a total order and therefore byte-reproducible.
FILE_CATEGORY_ORDER: tuple[str, ...] = (
    "production",
    "config",
    "test",
    "docs",
    "generated",
    "vendor",
)

SymbolChange = Literal["added", "modified", "deleted", "unknown"]
SYMBOL_CHANGE_VALUES: tuple[str, ...] = ("added", "modified", "deleted", "unknown")

SymbolSource = Literal["index", "tree_sitter", "unknown"]
SYMBOL_SOURCE_VALUES: tuple[str, ...] = ("index", "tree_sitter", "unknown")

ImpactKind = Literal[
    "contract_literal",
    "removed_symbol",
    "signature_change",
    "decorator_contract",
    "untouched_caller",
]
IMPACT_KIND_VALUES: tuple[str, ...] = (
    "contract_literal",
    "removed_symbol",
    "signature_change",
    "decorator_contract",
    "untouched_caller",
)

EvidenceStatus = Literal["PASS", "FAIL", "NOT_RUN", "UNKNOWN"]
EVIDENCE_STATUS_VALUES: tuple[str, ...] = ("PASS", "FAIL", "NOT_RUN", "UNKNOWN")

ProvenanceStatus = Literal["matched", "ambiguous", "unknown"]
PROVENANCE_STATUS_VALUES: tuple[str, ...] = ("matched", "ambiguous", "unknown")

ProvenanceCertainty = Literal["exact", "probable", "possible", "none"]
"""How much the correlator actually knows, as a word every surface must print.

``exact``     an anchor, not a guess: an explicit ``--session-id``, or a run
              ledger whose recorded HEAD *is* the reviewed head sha.
``probable``  the heuristic cleared the match floor with a clear runner-up gap.
``possible``  scored, real edit overlap, but under the match floor.
``none``      nothing was attributed; every other field is at its default.

``match_confidence`` is the number behind this word. The word exists because a
renderer that prints only the number leaves the reader to know where the floor
is, and a host printed without either reads as fact.
"""
PROVENANCE_CERTAINTY_VALUES: tuple[str, ...] = ("exact", "probable", "possible", "none")

IndexStatus = Literal["fresh", "stale", "absent"]
INDEX_STATUS_VALUES: tuple[str, ...] = ("fresh", "stale", "absent")

RangeMode = Literal["commit_range", "working_tree", "staged"]
RANGE_MODE_VALUES: tuple[str, ...] = ("commit_range", "working_tree", "staged")


@dataclass(frozen=True)
class DiffHunk:
    """One ``@@`` block of a textual patch."""

    old_start: int
    """1-based; 0 for a pure addition."""
    old_lines: int
    new_start: int
    """1-based; 0 for a pure deletion."""
    new_lines: int
    header: str
    """The raw ``@@ -a,b +c,d @@ ctx`` line, newline-stripped."""
    added: int = 0
    removed: int = 0
    new_ranges: tuple[tuple[int, int], ...] = ()
    """Inclusive new-side spans this hunk actually *changed*, ascending and disjoint.

    ``new_start .. new_start + new_lines`` is not that span and must never be
    used as one: under ``-U3`` it also covers the three *unchanged* context
    lines git prints on either side of every edit. A two-line comment added
    above a function therefore made the hunk's span reach three lines into that
    function's body, and every consumer that intersected symbol ranges with it
    reported a byte-identical definition as changed.

    These ranges hold only the lines the patch added, plus the join point of
    each deletion -- a removed line leaves no new-side line of its own, so it is
    recorded as the two new-side lines it now sits between.

    Appended after ``removed`` on purpose: the ``--json`` payload pins keys in
    declaration order, so an appended field keeps ``SCHEMA_VERSION`` at 1
    (additive-only) where an inserted one would reorder an existing consumer's
    keys.

    ``()`` means the producer could not read the patch body -- *not* "nothing
    changed" -- so a consumer must fall back to the full span rather than to
    silence.
    """
    old_ranges: tuple[tuple[int, int], ...] = ()
    """The same on the base side: removed lines plus each addition's join point.

    Appended after ``new_ranges``, for the same additive-only reason.
    """
    patch: str = ""
    """The hunk's own body -- the ``+``/``-``/context lines, without the header.

    ``""`` unless the producer was asked for it (``collect_diff(...,
    with_patch_text=True)``) or the body exceeded ``MAX_HUNK_PATCH_BYTES``, in
    which case ``"hunk_patch_truncated"`` is named in ``degraded``. Empty is
    therefore "not captured", never "the hunk is empty".

    The header is deliberately *not* included: it is already carried by
    ``header``, and it holds line numbers, so a fingerprint taken over a body
    that included it would change every time anything above the hunk moved --
    which is exactly the position-dependence review units exist to avoid.

    Appended last, for the same additive-only reason as ``old_ranges``.
    """


@dataclass(frozen=True)
class ChangedFile:
    """One delta in the reviewed range."""

    path: str
    """Repo-relative NEW path; the old path when ``status == "deleted"``."""
    old_path: str | None
    """Set only for renamed/copied/deleted."""
    status: FileStatus
    similarity: int = 0
    """0..100; 0 unless renamed/copied."""
    is_binary: bool = False
    additions: int = 0
    deletions: int = 0
    language: str | None = None
    category: FileCategory = "production"
    hunks: tuple[DiffHunk, ...] = ()
    """Empty when ``is_binary`` or when the delta carries no textual patch."""
    submodule_pointer: tuple[str, str] | None = None
    """``(old_commit, new_commit)`` when this entry is a submodule gitlink.

    ``None`` for every ordinary file, so ``is not None`` is the test for "this
    row is a pointer, not source". A side that does not exist -- an added or
    removed submodule -- is ``""`` rather than the null oid libgit2 reports,
    because forty zeros is not a commit anyone can look up.

    Appended after ``hunks`` on purpose: the ``--json`` payload pins keys in
    declaration order, so an appended field is additive (SS5.4)."""


@dataclass(frozen=True)
class ChangedSymbol:
    """A definition the diff touched. Populated by PR-2 (`review/impact.py`)."""

    symbol_name: str
    qualified_name: str | None
    kind: str | None
    """``"function"`` | ``"class"`` | ``"method"`` | a tree-sitter node kind."""
    file_path: str
    start_line: int
    end_line: int
    """Last line of the definition's body. Equals ``start_line`` for a deleted one."""
    change: SymbolChange
    caller_count: int = -1
    """-1 when unknown (no index)."""
    usage_count: int = -1
    centrality_rank: int | None = None
    """1-based rank in ``call_graph_centrality``; None when unknown."""
    centrality_percentile: float | None = None
    source: SymbolSource = "unknown"


@dataclass(frozen=True)
class ImpactSite:
    """A place the change reaches. Populated by PR-2 (`review/impact.py`)."""

    kind: ImpactKind
    path: str
    """``"rel/path.py:L123"`` — verbatim from the edit_impact site dicts."""
    old: str
    new: str | None
    snippet: str
    """Already redacted and truncated to 80 chars by edit_impact."""
    in_patch: bool = False
    inspected_by_agent: bool | None = None
    """None when ``provenance.status != "matched"`` (PR-4 fills it)."""
    source_path: str = ""
    """Repo-relative path of the *changed* file this site is a consequence of.

    Appended after ``inspected_by_agent`` on purpose: the ``--json`` payload
    pins keys in declaration order, and an appended field keeps
    ``SCHEMA_VERSION`` at 1 (SS5.4 additive-only) where an inserted one would
    reorder an existing consumer's keys.

    ``""`` when it could not be attributed to exactly one changed file. An
    out-of-patch site by definition lives in a file the diff does not contain,
    so without this there is no link back to the change that caused it -- and
    the review ordering, which ranks changed files, could never credit the file
    whose edit reached outside the patch.
    """
    uncertainty: str = ""
    """Why this site is a *candidate* rather than a fact; ``""`` when it is a fact.

    A detector that cannot fully qualify a match has three options and only one of
    them is honest. Dropping it hides a real defect; printing it plainly asserts
    something unproven; carrying the reason lets every consumer -- terminal, HTML,
    ``--json``, the ranking -- present it as the qualified claim it is. The
    sentence is the detector's own, so it names the specific doubt ("3 definitions
    named refresh() in this repo") rather than a bare confidence score.

    Appended last for the same additive-only reason as ``source_path``.
    """


@dataclass(frozen=True)
class ReviewOrderEntry:
    """One row of the reading order. Populated by PR-3 (`review/ordering.py`)."""

    path: str
    rank: int
    """1-based."""
    score: float
    """Rounded to 4dp so the ordering is reproducible byte-for-byte."""
    reasons: tuple[str, ...] = ()
    """Deterministic and sorted, e.g. ``("6 known callers", "public contract changed")``."""
    group: FileCategory = "production"


@dataclass(frozen=True)
class EvidenceRecord:
    """A verification signal that actually ran. Populated by PR-4."""

    name: str
    """``"Focused tests"`` | ``"Full suite"`` | ``"Typecheck"`` | ``"Lint"`` | ``"Migration check"``."""
    status: EvidenceStatus
    detail: str = ""
    source: str = "none"
    """``"session:<id>"`` | ``"run_ledger"`` | ``"none"``."""


@dataclass(frozen=True)
class ProvenanceRecord:
    """Who generated the change, and what they looked at. Populated by PR-4.

    ``status == "unknown"`` is a first-class answer: no session record stores a
    commit sha, so correlation is a heuristic and a wrong match is worse than
    an honest gap.
    """

    status: ProvenanceStatus = "unknown"
    host: str | None = None
    model: str | None = None
    session_id: str | None = None
    task: str | None = None
    """``Trace.task`` — already redacted, <= 200 chars."""
    files_inspected: tuple[str, ...] = ()
    files_changed: tuple[str, ...] = ()
    commands_run: tuple[str, ...] = ()
    subagents: tuple[tuple[str, int], ...] = ()
    """Sorted ``(agent_type, count)``; a tuple, not a dict, for frozen-hashability."""
    uninspected_impacted: tuple[str, ...] = ()
    match_confidence: float = 0.0
    certainty: ProvenanceCertainty = "none"
    """The hedge every renderer must carry with ``host``/``model``.

    Anything but ``"exact"`` means the attribution is a scored guess, and the
    surfaces print it that way rather than leaving the number in ``--json`` for
    a reader who was never told a floor exists.
    """
    match_reason: str = ""
    """Always states which evidence fired, so a wrong match stays auditable."""


def unknown_provenance() -> ProvenanceRecord:
    """Return the honest-gap provenance record used before PR-4 correlation runs."""

    return ProvenanceRecord(status="unknown", match_reason="provenance not collected")


@dataclass(frozen=True)
class ReviewPacket:
    """The whole reviewable answer for one change."""

    schema_version: int
    generated_at: str
    """ISO-8601 UTC, timezone-aware."""
    repo_root: str
    range_mode: RangeMode
    base_rev: str
    """The user-facing spec, e.g. ``"HEAD~1"`` or ``"main"``."""
    head_rev: str
    """``"HEAD"`` | ``"WORKDIR"`` | ``"INDEX"`` | an explicit rev."""
    base_sha: str
    """Resolved 40-char sha; ``""`` only when the base is the empty tree."""
    head_sha: str
    """``""`` for WORKDIR/INDEX."""
    merge_base_sha: str = ""
    dirty: bool = False
    title: str = ""
    files: tuple[ChangedFile, ...] = ()
    symbols: tuple[ChangedSymbol, ...] = ()
    impact: tuple[ImpactSite, ...] = ()
    order: tuple[ReviewOrderEntry, ...] = ()
    provenance: ProvenanceRecord = field(default_factory=unknown_provenance)
    evidence: tuple[EvidenceRecord, ...] = ()
    index_status: IndexStatus = "absent"
    degraded: tuple[str, ...] = ()
    """Sorted names of the signals that fell back."""
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the exact ``--json`` payload: asdict semantics, declaration order."""

        return asdict(self)


def empty_stats() -> dict[str, int]:
    """Return the zeroed ``stats`` block with every key the renderers expect."""

    return {
        "files": 0,
        "additions": 0,
        "deletions": 0,
        "hunks": 0,
        "symbols": 0,
        "impact_sites": 0,
    }


__all__ = [
    "EVIDENCE_STATUS_VALUES",
    "FILE_CATEGORY_ORDER",
    "FILE_CATEGORY_VALUES",
    "FILE_STATUS_VALUES",
    "IMPACT_KIND_VALUES",
    "INDEX_STATUS_VALUES",
    "PROVENANCE_CERTAINTY_VALUES",
    "PROVENANCE_STATUS_VALUES",
    "RANGE_MODE_VALUES",
    "SCHEMA_VERSION",
    "SYMBOL_CHANGE_VALUES",
    "SYMBOL_SOURCE_VALUES",
    "ChangedFile",
    "ChangedSymbol",
    "DiffHunk",
    "EvidenceRecord",
    "EvidenceStatus",
    "FileCategory",
    "FileStatus",
    "ImpactKind",
    "ImpactSite",
    "IndexStatus",
    "ProvenanceCertainty",
    "ProvenanceRecord",
    "ProvenanceStatus",
    "RangeMode",
    "ReviewOrderEntry",
    "ReviewPacket",
    "SymbolChange",
    "SymbolSource",
    "empty_stats",
    "unknown_provenance",
]
