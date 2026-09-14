"""Terminal rendering for the `lc review` packet.

Why this module exists: the packet is deliberately a superset of what any one
review needs — four producers fill it and three of them can legitimately come
back empty. The renderer's whole job is to make an empty slice *disappear*
rather than print a heading over nothing, so a diff-only packet reads as a
clean diff summary and a fully-populated one reads as a review brief. Rich is
imported inside the function so importing this module stays free.

The shape of the analysis sections is fixed by the product plan
(`docs/planning/2026-09-07-agent-runtime-competitive-plan.md` §4.1) and each
choice there is load-bearing:

* **ATTENTION comes first, and it is capped.** A reviewer scans for about five
  seconds, so the risks are above the fold or they are not read at all. The
  default keeps the three highest-priority findings with five locations each and
  states the count it withheld; ``--all`` prints every one. An elision that does
  not say how much it hid would be its own lie about the blast radius.
* **START HERE names one file, not a ranking.** The claim the product makes is
  that one file is where to begin, so it says so; the rest of the order follows
  as one-liners that can be skimmed without scrolling. ``--all`` restores the
  reasons under every entry.
* **The FILES table lives behind ``--all``.** It is a ``git diff --stat``
  reprint, which is not the question anyone runs this command to answer.
* **REVIEW ORDER reasons are sentences, not a table.** The reasons are the point
  — a rank a reviewer cannot argue with is worse than no rank — and a
  fixed-width column shreds them.
* **ATTENTION groups by *finding*, not by site.** One changed contract that
  reaches nine files is one warning with nine locations, not nine warnings.
  Rendering it per-site turned a real run of this command into forty lines that
  buried the four that mattered.
* **EXECUTION EVIDENCE never upgrades a gap into a claim.** ``unknown``
  provenance prints ``unknown``, an unrecorded read list prints "not recorded",
  and no row is ever synthesised to fill a column. A *scored* attribution is a
  gap too, only a narrower one, so the hedge sits on the ``Generated with:``
  line itself: ``[possible match, confidence 0.45]``. A reader who stops after
  the first line has still been told the truth, which is not something a
  confidence number parked in ``--json`` can claim.

One thing is never conditional: the ``Human review  REQUIRED`` footer. The
packet is evidence, not a verdict — there is no AI opinion anywhere in it.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

from .models import FILE_CATEGORY_ORDER, ChangedFile, ImpactSite, ReviewPacket

_STATUS_CHAR = {
    "added": "A",
    "modified": "M",
    "deleted": "D",
    "renamed": "R",
    "copied": "C",
    "typechange": "T",
}

# Only these degradations are fixable by indexing; suggesting `lc code index`
# for an unreadable submodule pointer would be noise.
_INDEX_SIGNALS = frozenset({"centrality", "symbol_relations", "symbol_line_ranges"})
_INDEX_HINT = "run `lc code index` for caller and centrality signals"

# `--staged` drops `git add -N` placeholders to agree with `git diff --cached`.
# Dropping them silently would be its own lie -- a reviewer who ran `git status`
# and saw the paths needs to be told where they went, and why they are not part
# of the change under review. The signal carries its own count, so the prefix is
# what matches.
_ITA_SIGNAL_PREFIX = "intent_to_add_excluded"
_ITA_HINT = "`git add -N` paths hold no staged content; `git diff --cached` skips them too"
_HUMAN_REVIEW_FOOTER = "Human review  REQUIRED"

# Severity order for the ATTENTION section: a contract that can no longer be
# satisfied outranks a call site that merely needs re-reading.
_ATTENTION_ORDER: tuple[str, ...] = (
    "signature_change",
    "removed_symbol",
    "contract_literal",
    "decorator_contract",
    "untouched_caller",
)
# What the listed locations *are*, per finding kind. Naming the relationship is
# what makes the location actionable rather than a bare grep hit.
_ATTENTION_LABELS: dict[str, str] = {
    "signature_change": "untouched call sites:",
    "removed_symbol": "references remain:",
    "contract_literal": "old consumer still exists:",
    "decorator_contract": "affected sites:",
    "untouched_caller": "untouched call sites:",
}
# What the same locations are when the finding is qualified. The confident label
# is itself a claim -- "untouched call sites" asserts these *are* call sites of the
# changed definition -- so a finding carrying an uncertainty must not borrow it.
_UNCERTAIN_ATTENTION_LABELS: dict[str, str] = {
    "signature_change": "possible call sites:",
    "removed_symbol": "possible references:",
    "untouched_caller": "possible call sites:",
}
# Marker glyphs. A qualified finding must not wear the same warning sign as a
# proven one; `?` reads as a question at a glance, which is what it is.
_ATTENTION_MARK = "⚠"
_UNCERTAIN_MARK = "?"


def _attention_label(kind: str, uncertainty: str) -> str:
    if uncertainty:
        return _UNCERTAIN_ATTENTION_LABELS.get(kind, "possible affected sites:")
    return _ATTENTION_LABELS.get(kind, "affected sites:")


# A single finding may reach a lot of files. Five locations is enough to judge
# the blast radius on a screen a reviewer actually reads; the rest are one
# honest counted line away, and `--all` prints every one.
_MAX_SITES_PER_FINDING = 5
# The same reasoning applied to the findings themselves: three risks read, ten
# scroll past. Ordered by `_ATTENTION_ORDER`, so what survives the cap is the
# most severe, never the alphabetically luckiest.
_MAX_ATTENTION_GROUPS = 3
# The provenance record can name many never-opened files; the same reasoning.
_MAX_UNINSPECTED = 10
# Every elision in this module says how much it hid, and names the one flag that
# undoes it. One convention, so a reader learns it once.
_ALL_FLAG = "lc review --all"
# A `THEN` line is a one-liner or it is not doing its job. Reasons are appended
# only while they fit; the rest are counted. 100 leaves margin inside the Rich
# console's 110 columns for the two-space indent and a narrow terminal.
_COMPACT_WIDTH = 100
# The terminal is a triage surface, not a second file browser. One starting
# point plus seven follow-ups establishes a queue without charging the reviewer
# for forty paths before they have inspected the first one.
_DEFAULT_ORDER_ROWS = 8


def _more_line(remaining: int, *, flag: bool = False) -> str:
    """The one elision sentence this module uses everywhere."""

    return f"… and {remaining:,} more" + (f" ({_ALL_FLAG})" if flag else "")


# What an `untouched_caller` finding's `SymbolChange` reads as in a headline. A
# newly added definition and an edited one are different news for a reviewer, and
# the fallback is the neutral word rather than a guess at which one it was.
_CHANGE_VERB: dict[str, str] = {
    "added": "added",
    "modified": "changed",
    "deleted": "removed",
    "unknown": "changed",
}


def _range_label(packet: ReviewPacket) -> str:
    if packet.range_mode == "working_tree":
        return f"{packet.base_rev}..WORKDIR"
    if packet.range_mode == "staged":
        return f"{packet.base_rev}..INDEX"
    return f"{packet.base_rev}..{packet.head_rev}"


def _summary_line(packet: ReviewPacket) -> str:
    """``3 files · +12 -4`` — and what to say when nothing has lines.

    A submodule pointer bump has no lines to count. ``gitdiff`` records zeros
    for it on purpose: git's own two-line ``Subproject commit`` patch is prose
    about the change, not lines of it, and counting it as ``+1 -1`` would put
    text nobody can open into the header a reviewer reads first. Summing those
    zeros into ``1 file · +0 -0`` throws that honesty away in the other
    direction and reads as "nothing happened" over a real pointer move, so the
    pointers are named — next to the counts when the range also touches lines,
    and instead of them when it does not.
    """

    stats = packet.stats
    files = stats.get("files", len(packet.files))
    additions = stats.get("additions", 0)
    deletions = stats.get("deletions", 0)
    pointers = sum(1 for item in packet.files if item.submodule_pointer is not None)
    parts = [f"{files:,} {'file' if files == 1 else 'files'}"]
    if additions or deletions or not pointers:
        parts.append(f"+{additions:,} -{deletions:,}")
    if pointers:
        parts.append(f"{pointers} submodule pointer" + ("" if pointers == 1 else "s"))
    return " · ".join(parts)


def _file_label(item: ChangedFile) -> str:
    if item.old_path and item.status in ("renamed", "copied"):
        return f"{item.old_path} → {item.path}"
    return item.path


def _submodule_delta(item: ChangedFile) -> str:
    """``submodule 59b402a → a920202`` — what a gitlink entry actually changed.

    The ``+1 -1`` git prints for a pointer bump counts the two lines of git's own
    ``Subproject commit`` patch text, not lines of anything a reviewer can open.
    The two commit ids are the change, so they are what the row says.
    """

    old, new = item.submodule_pointer or ("", "")
    return f"submodule {old[:7] or '(none)'} → {new[:7] or '(none)'}"


def _file_delta(item: ChangedFile) -> str:
    if item.submodule_pointer is not None:
        return _submodule_delta(item)
    if item.is_binary:
        return "binary"
    return f"+{item.additions:,} -{item.deletions:,}"


def _grouped_files(packet: ReviewPacket) -> list[tuple[str, list[ChangedFile]]]:
    buckets: dict[str, list[ChangedFile]] = {}
    for item in packet.files:
        buckets.setdefault(item.category, []).append(item)
    ordered: list[tuple[str, list[ChangedFile]]] = []
    for category in FILE_CATEGORY_ORDER:
        rows = buckets.pop(category, [])
        if rows:
            ordered.append((category, sorted(rows, key=lambda f: f.path)))
    for category in sorted(buckets):
        ordered.append((category, sorted(buckets[category], key=lambda f: f.path)))
    return ordered


# --- REVIEW ORDER -----------------------------------------------------------


def _order_rows(packet: ReviewPacket) -> list[tuple[int, str, str, str, tuple[str, ...]]]:
    """Return ``(rank, path, facts, churn, reasons)`` for every ordered entry.

    *facts* restates the delta straight from the ``ChangedFile`` (status,
    category, churn). The ranker emits that same churn as one of its reasons, so
    it is dropped from *reasons* here — printing ``+12 -3`` twice on adjacent
    lines reads as a bug, and the fact line is the better home for it.

    *churn* is carried out separately because the compact ``THEN`` line has room
    for the delta but not for the status and category words beside it.
    """

    by_path = {item.path: item for item in packet.files}
    rows: list[tuple[int, str, str, str, tuple[str, ...]]] = []
    for entry in packet.order:
        item = by_path.get(entry.path)
        reasons = entry.reasons
        if item is None:
            rows.append((entry.rank, entry.path, entry.group, "", reasons))
            continue
        churn = f"+{item.additions} -{item.deletions}"
        if item.submodule_pointer is not None:
            delta = _submodule_delta(item)
        else:
            delta = "binary" if item.is_binary else churn
        facts = f"{item.status} · {entry.group} · {delta}"
        rows.append((entry.rank, entry.path, facts, delta, tuple(r for r in reasons if r != churn)))
    return rows


def _compact_order_line(rank: int, path: str, delta: str, reasons: Sequence[str]) -> str:
    """``2. path  +12 -3  · 6 known callers`` — one line, reasons truncated honestly.

    Reasons are kept whole and appended only while the line stays inside
    ``_COMPACT_WIDTH``; whatever does not fit is counted rather than dropped. A
    reason cut in half would be a claim the ranker never made.
    """

    head = f"{rank}. {path}  {delta}".rstrip()
    if not reasons:
        return head
    kept: list[str] = []
    width = len(head) + 2  # the two-space indent every caller prepends
    for index, reason in enumerate(reasons):
        left = len(reasons) - index
        # Reserve room for the elision unless this reason is the last one.
        tail = 0 if left == 1 else len(f" · {_more_line(left - 1)}")
        cost = len(f" · {reason}") + (1 if not kept else 0)
        if width + cost + tail > _COMPACT_WIDTH:
            break
        width += cost
        kept.append(reason)
    if not kept:
        return f"{head}  · {_more_line(len(reasons))}"
    line = f"{head}  · " + " · ".join(kept)
    remaining = len(reasons) - len(kept)
    return line if remaining == 0 else f"{line} · {_more_line(remaining)}"


def _order_lines(row: tuple[int, str, str, str, tuple[str, ...]], *, show_all: bool) -> list[str]:
    """The ``THEN`` rendering of one entry: a one-liner, or the full block."""

    rank, path, facts, delta, reasons = row
    if not show_all:
        return [f"  {_compact_order_line(rank, path, delta, reasons)}"]
    return [f"  {rank}. {path}", f"     {facts}", *(f"     - {reason}" for reason in reasons)]


# --- ATTENTION --------------------------------------------------------------


def _headline(kind: str, old: str, new: str | None) -> str:
    """One sentence naming the finding, built only from the detector's own fields."""

    if kind == "contract_literal":
        return f'contract literal changed: "{old}" → "{new}"' if new else f'contract literal changed: "{old}"'
    if kind == "signature_change":
        return f"{old} {new}" if new else f"{old} signature changed"
    if kind == "removed_symbol":
        return new or f"{old} no longer defined here"
    if kind == "decorator_contract":
        return f"{old} — {new}" if new else old
    if kind == "untouched_caller":
        # `new` carries the producer's own `SymbolChange`. Hardcoding "changed"
        # here announced a definition that did not exist before the diff as a
        # modification of something the reader is expected to already know.
        return f"{old} {_CHANGE_VERB.get(new or '', 'changed')}"
    return f"{old} → {new}" if new else old


def _site_sort_key(site: ImpactSite) -> tuple[str, int, str]:
    """Sort ``rel/p.py:L9`` before ``rel/p.py:L82`` — lexicographic would not."""

    head, _, tail = site.path.rpartition(":L")
    if head and tail.isdigit():
        return (head, int(tail), site.snippet)
    return (site.path, 0, site.snippet)


def _attention_groups(packet: ReviewPacket) -> list[tuple[str, str, str, str, list[ImpactSite]]]:
    """Group out-of-patch sites into ``(kind, headline, source, uncertainty, sites)``.

    *source* is the changed file the finding came out of, so the warning is
    traceable in both directions: from the diff to what it reaches, and from a
    surprising location back to the edit that caused it. Empty when nothing in
    the change reaches outside the patch, which is the common and desirable case
    — the section then disappears entirely.

    *uncertainty* is part of the grouping key, not a property read off the first
    site: a qualified finding and a confident one are two different claims about
    the same code and merging them would let the confident wording swallow the
    doubt. It is the detector's own sentence, empty for a plain fact.
    """

    buckets: dict[tuple[str, str, str, str], list[ImpactSite]] = {}
    for site in packet.impact:
        if site.in_patch:
            continue
        key = (site.kind, _headline(site.kind, site.old, site.new), site.source_path, site.uncertainty)
        buckets.setdefault(key, []).append(site)

    def _kind_rank(kind: str) -> int:
        return _ATTENTION_ORDER.index(kind) if kind in _ATTENTION_ORDER else len(_ATTENTION_ORDER)

    return [
        (kind, headline, source, doubt, sorted(buckets[(kind, headline, source, doubt)], key=_site_sort_key))
        # Confident findings first within a kind: an empty uncertainty sorts before
        # any sentence, so a reviewer meets the facts before the maybes.
        for kind, headline, source, doubt in sorted(
            buckets, key=lambda key: (_kind_rank(key[0]), key[3] != "", key[1], key[2], key[3])
        )
    ]


def _site_lines(sites: Sequence[ImpactSite], *, show_all: bool = False) -> list[tuple[str, str]]:
    """``(location, context)`` pairs, truncated with an honest counted tail."""

    kept = sites if show_all else sites[:_MAX_SITES_PER_FINDING]
    rows = [(site.path, site.snippet) for site in kept]
    remaining = len(sites) - len(rows)
    if remaining > 0:
        rows.append((_more_line(remaining), ""))
    return rows


def _visible_attention(
    packet: ReviewPacket, *, show_all: bool
) -> tuple[list[tuple[str, str, str, str, list[ImpactSite]]], int]:
    """Return the findings to print and how many were withheld.

    The withheld count is returned rather than swallowed: the section is capped
    to keep the risks on one screen, and a cap the reader is not told about is
    indistinguishable from a detector that found nothing.
    """

    groups = _attention_groups(packet)
    if show_all:
        return groups, 0
    return groups[:_MAX_ATTENTION_GROUPS], max(0, len(groups) - _MAX_ATTENTION_GROUPS)


# --- EXECUTION EVIDENCE -----------------------------------------------------


def _provenance_lines(packet: ReviewPacket, *, show_all: bool = False) -> list[str]:
    record = packet.provenance
    if record.status == "unknown":
        # Plan A4: an honest "unknown" beats a confident wrong attribution. The
        # reason still prints, so the gap is auditable rather than mysterious.
        lines = ["Generated with: unknown"]
        if record.match_reason:
            lines.append(f"  ({record.match_reason})")
        return lines

    # R1: only the three anchors -- an explicit `--session-id`, a run ledger
    # that recorded editing a reviewed file at edit time, or one whose recorded
    # HEAD is the reviewed head -- know the author. Everything else is a scored
    # guess, and the hedge rides on the line that carries the claim: a reader
    # who sees only `Generated with: claude` has been told a fact, and leaving
    # the number in `--json` does not undo that.
    hedge = (
        "" if record.certainty == "exact" else f"  [{record.certainty} match, confidence {record.match_confidence:.2f}]"
    )
    # Host, model and session are three facets of one attribution, so they ride
    # one line and the hedge closes it. Three stacked labels cost three of the
    # ~25 lines this view gets and told the reader nothing the separators do not.
    who = " · ".join(part for part in (record.host or "unknown", record.model, record.session_id) if part)
    lines = [f"Generated with: {who}{hedge}"]
    if record.certainty != "exact":
        lines.append("  correlated by score, not recorded: host, model and session are unconfirmed")
    if record.match_reason:
        # The reason must always name the evidence that fired, so a wrong match
        # stays auditable rather than merely hedged.
        lines.append(f"Matched on: {record.match_reason}")
    if record.files_inspected:
        lines.append(f"Agent inspected: {len(record.files_inspected):,} files")
    else:
        # R2: several hosts never record reads at all. "0 files" would read as a
        # finding; this is a gap in the recording, not in the agent's work.
        lines.append("Agent inspected: not recorded for this host")
    if record.uninspected_impacted:
        lines.append("Agent did not inspect:")
        kept = record.uninspected_impacted if show_all else record.uninspected_impacted[:_MAX_UNINSPECTED]
        lines.extend(f"  {path}" for path in kept)
        remaining = len(record.uninspected_impacted) - len(kept)
        if remaining > 0:
            lines.append(f"  {_more_line(remaining)}")
    return lines


def _evidence_lines(packet: ReviewPacket) -> list[str]:
    """Fixed-width ``name status detail`` rows, exactly as the plan renders them."""

    return [f"{record.name:<16} {record.status:<8}{record.detail}".rstrip() for record in packet.evidence]


# --- footer -----------------------------------------------------------------


def _footer_line(packet: ReviewPacket) -> str:
    """State the index status and every signal that fell back, or nothing."""

    parts: list[str] = []
    if packet.files and "impact_disabled" not in packet.degraded:
        # With `--no-impact` the index was never consulted, so "absent" would
        # report a missing index rather than a skipped question.
        parts.append(f"index: {packet.index_status}")
    if packet.degraded:
        parts.append(f"degraded: {', '.join(packet.degraded)}")
    if not parts:
        return ""
    line = " · ".join(parts)
    if _INDEX_SIGNALS & set(packet.degraded):
        line = f"{line} — {_INDEX_HINT}"
    if any(name.startswith(_ITA_SIGNAL_PREFIX) for name in packet.degraded):
        line = f"{line} — {_ITA_HINT}"
    return line


# --- renderers --------------------------------------------------------------


def _change_story(packet: ReviewPacket, *, limit: int = 4) -> str:
    """Compact deterministic orientation for a large change.

    The final commit title is a poor summary of a multi-commit agent change.
    Reuse the same Intent chapter projection as the browser, with synthetic
    unreviewed rows because grouping itself depends only on path/category,
    commit scope and attention rank.
    """

    if len(packet.files) < 8:
        return ""
    try:
        from .chapters import build_chapter_lenses, change_story_labels

        rank_by_path = {entry.path: entry.rank for entry in packet.order}
        rows = [
            {
                "path": item.path,
                "category": item.category,
                "attention_rank": rank_by_path.get(item.path, 0),
                "group": "unreviewed",
            }
            for item in packet.files
        ]
        chapters = build_chapter_lenses(
            rows,
            packet.to_dict(),
            repo_root=Path(packet.repo_root),
            base_sha=packet.base_sha,
            head_sha=packet.head_sha,
        ).get("intent", [])
    except Exception:
        return ""
    if len(chapters) <= 1:
        return ""
    labels, remaining = change_story_labels(chapters, limit=limit)
    if not labels:
        return ""
    return " · ".join(labels) + (f" · +{remaining} more" if remaining else "")


def _render_plain(packet: ReviewPacket, *, show_all: bool = False) -> str:
    out: list[str] = []
    story = _change_story(packet)
    out.append(f"CHANGE STORY  {story}" if story else packet.title or _range_label(packet))
    out.append(f"{_range_label(packet)}   {_summary_line(packet)}")

    if not packet.files:
        out.append("")
        out.append("no changes in this range")
        out.append("")
        out.append(_HUMAN_REVIEW_FOOTER)
        footer = _footer_line(packet)
        if footer:
            out.append(footer)
        return "\n".join(out)

    attention, withheld = _visible_attention(packet, show_all=show_all)
    if attention:
        out.append("")
        out.append("ATTENTION")
        for kind, headline, source, uncertainty, sites in attention:
            out.append("")
            mark = _UNCERTAIN_MARK if uncertainty else _ATTENTION_MARK
            out.append(f"  {mark} {headline}" + (f"   ({source})" if source else ""))
            if uncertainty:
                out.append(f"    {uncertainty}")
            out.append(f"    {_attention_label(kind, uncertainty)}")
            for location, context in _site_lines(sites, show_all=show_all):
                out.append(f"      {location}   {context}".rstrip())
        if withheld:
            out.append("")
            out.append(f"  {_more_line(withheld, flag=True)}")

    rows = _order_rows(packet)
    visible_rows = rows if show_all else rows[:_DEFAULT_ORDER_ROWS]
    if visible_rows:
        head, rest = visible_rows[0], visible_rows[1:]
        out.append("")
        out.append("START HERE")
        out.append(f"  {head[0]}. {head[1]}")
        out.append(f"     {head[2]}")
        for reason in head[4]:
            out.append(f"     - {reason}")
        if rest:
            out.append("")
            out.append("THEN")
            for row in rest:
                if show_all:
                    out.append("")
                out.extend(_order_lines(row, show_all=show_all))
            hidden = len(rows) - len(visible_rows)
            if hidden > 0:
                out.append(f"  {_more_line(hidden, flag=True)}")

    # `not rows` is the `--limit 0` case: with no ranking to print, the table is
    # the only thing left that names what changed, and a packet that names
    # nothing is worse than a stat reprint.
    if show_all or not rows:
        out.append("")
        out.append("FILES")
        for category, files in _grouped_files(packet):
            out.append(f"  {category}")
            for item in files:
                out.append(f"    {_STATUS_CHAR.get(item.status, '?')}  {_file_label(item)}  {_file_delta(item)}")

    out.append("")
    out.append("EXECUTION EVIDENCE")
    out.append("")
    for line in _provenance_lines(packet, show_all=show_all):
        out.append(f"  {line}")
    evidence = _evidence_lines(packet)
    if evidence:
        out.append("")
        for line in evidence:
            out.append(f"  {line}")

    out.append("")
    out.append(_HUMAN_REVIEW_FOOTER)
    footer = _footer_line(packet)
    if footer:
        out.append(footer)
    return "\n".join(out)


def _render_rich(packet: ReviewPacket, *, show_all: bool = False) -> str:
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=110, legacy_windows=False, highlight=False)

    story = _change_story(packet)
    if story:
        console.print(f"[dim]CHANGE STORY[/]  [bold bright_white]{escape(story)}[/]")
    else:
        console.print(f"[bold bright_white]{escape(packet.title or _range_label(packet))}[/]")
    console.print(f"[dim]{escape(_range_label(packet))}[/]  {escape(_summary_line(packet))}")

    if not packet.files:
        console.print()
        console.print("[dim]no changes in this range[/]")
        console.print()
        console.print(f"[bold]{_HUMAN_REVIEW_FOOTER}[/]")
        footer = _footer_line(packet)
        if footer:
            console.print(f"[dim]{escape(footer)}[/]")
        return buffer.getvalue()

    attention, withheld = _visible_attention(packet, show_all=show_all)
    if attention:
        console.print()
        console.print("[bold bright_white]ATTENTION[/]")
        for kind, headline, source, uncertainty, sites in attention:
            console.print()
            origin = f"   [dim]({escape(source)})[/]" if source else ""
            mark = f"[dim]{_UNCERTAIN_MARK}[/]" if uncertainty else f"[yellow]{_ATTENTION_MARK}[/]"
            console.print(f"  {mark} [bold]{escape(headline)}[/]{origin}")
            if uncertainty:
                console.print(f"    [dim]{escape(uncertainty)}[/]")
            console.print(f"    [dim]{escape(_attention_label(kind, uncertainty))}[/]")
            for location, context in _site_lines(sites, show_all=show_all):
                suffix = f"   [dim]{escape(context)}[/]" if context else ""
                console.print(f"      {escape(location)}{suffix}")
        if withheld:
            console.print()
            console.print(f"  [dim]{escape(_more_line(withheld, flag=True))}[/]")

    rows = _order_rows(packet)
    visible_rows = rows if show_all else rows[:_DEFAULT_ORDER_ROWS]
    if visible_rows:
        head, rest = visible_rows[0], visible_rows[1:]
        console.print()
        console.print("[bold bright_white]START HERE[/]")
        console.print(f"  [dim]{head[0]}.[/] [bold]{escape(head[1])}[/]")
        console.print(f"     [dim]{escape(head[2])}[/]")
        for reason in head[4]:
            console.print(f"     [dim]-[/] {escape(reason)}")
        if rest:
            console.print()
            console.print("[bold bright_white]THEN[/]")
            for row in rest:
                if show_all:
                    console.print()
                for line in _order_lines(row, show_all=show_all):
                    console.print(f"[dim]{escape(line)}[/]")
            hidden = len(rows) - len(visible_rows)
            if hidden > 0:
                console.print(f"[dim]  {escape(_more_line(hidden, flag=True))}[/]")

    if show_all or not rows:
        console.print()
        console.print("[bold bright_white]FILES[/]")
        file_table = Table(box=box.SIMPLE, show_header=True, header_style="dim", padding=(0, 1))
        file_table.add_column("Group")
        file_table.add_column("", width=1)
        file_table.add_column("Path", overflow="fold")
        file_table.add_column("Added", justify="right")
        file_table.add_column("Removed", justify="right")
        for category, files in _grouped_files(packet):
            for index, item in enumerate(files):
                file_table.add_row(
                    escape(category) if index == 0 else "",
                    _STATUS_CHAR.get(item.status, "?"),
                    escape(_file_label(item)),
                    (
                        _submodule_delta(item)
                        if item.submodule_pointer is not None
                        else ("binary" if item.is_binary else f"{item.additions:,}")
                    ),
                    "" if item.is_binary or item.submodule_pointer is not None else f"{item.deletions:,}",
                )
        console.print(file_table)

    console.print()
    console.print("[bold bright_white]EXECUTION EVIDENCE[/]")
    console.print()
    for line in _provenance_lines(packet, show_all=show_all):
        console.print(f"  {escape(line)}")
    evidence = _evidence_lines(packet)
    if evidence:
        console.print()
        for line in evidence:
            console.print(f"  {escape(line)}")

    console.print()
    console.print(f"[bold]{_HUMAN_REVIEW_FOOTER}[/]")
    footer = _footer_line(packet)
    if footer:
        console.print(f"[dim]{escape(footer)}[/]")
    return buffer.getvalue()


def render_review(packet: ReviewPacket, *, no_color: bool = False, show_all: bool = False) -> str:
    """Render *packet* as terminal text.

    ``no_color`` takes the Rich-free path entirely rather than stripping ANSI
    afterwards, so piping into a file or a hook script never yields box-drawing
    characters either.

    ``show_all`` lifts every cap and adds back the ``git diff --stat`` table: the
    default view is a triage screen, and a reviewer who has read it must be able
    to see the whole packet without leaving the terminal. It is a *display*
    switch only -- ``--json`` and the HTML report were never capped, because a
    machine consumer that silently receives three of nine findings is the one
    reader that cannot notice.
    """

    if no_color:
        return _render_plain(packet, show_all=show_all)
    try:
        return _render_rich(packet, show_all=show_all)
    except Exception:
        # A renderer must never be the reason a review fails to print.
        return _render_plain(packet, show_all=show_all)


__all__ = ["render_review"]
