"""Review units and their fingerprints — what a mark is actually attached to.

Why this module exists: a review mark has to survive the thing it is attached
to being *moved*, and has to die when that thing is *rewritten*. Those are two
different questions and they need two different keys, so every unit carries
both.

``unit_key`` answers **"is this the same thing?"**. It is composed from kind,
path, symbol name and an ordinal — and from nothing else. No line number, no
content hash. Agents rewrite files constantly; ten lines added above a function
move every definition below them, and a key that noticed would evaporate every
mark in the file for an edit nobody made to any of them.

``content_fingerprint`` answers **"did it change?"**. It is a sha256 over the
normalized content the reviewer actually saw. A mark is valid only for the
fingerprint it was made against, which is what makes "reviewed" impossible to
preserve silently across changed content.

Conflating the two is the single most common way to break a review frontier,
so they are computed by separate functions over separate inputs and neither
one can see the other's input.

Three deliberate properties, each pinned by a test:

* **Whitespace-only churn changes nothing.** :func:`normalize_body` strips
  trailing whitespace and line-ending noise before hashing, because a reviewer
  who is asked to re-read a function because an editor trimmed a space stops
  believing the tool.
* **A decorator-only edit changes the file fingerprint, not the symbol's.** The
  symbol window starts at the ``def`` keyword (see
  :func:`~lemoncrow.pro.capabilities.review.impact.symbol_windows`), so the
  prelude above it is charged to the file. Documented limitation, asserted so
  it cannot regress into a surprise.
* **``unknown`` is a first-class fingerprint method, never an empty string.** A
  unit whose content could not be hashed still carries a real, deterministic
  value plus ``fingerprint_method="unknown"``, so it can never be mistaken for a
  match and can never read as reviewed.

``hashlib.sha256`` always — never ``blake3``, which this repo imports through an
optional-runtime shim that degrades to ``None``. A fingerprint whose recipe
depended on which wheels happened to be installed would silently reopen every
unit on some machines.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from lemoncrow.pro.capabilities.review.impact import SymbolWindow, symbol_windows
from lemoncrow.pro.capabilities.review.models import (
    ChangedFile,
    ChangedSymbol,
    DiffHunk,
    FileCategory,
    ReviewOrderEntry,
    ReviewPacket,
)
from lemoncrow.pro.capabilities.review.session_models import FingerprintMethod, ReviewUnit

# Bumping this reopens every unit in every stored review, by design: it is the
# escape hatch for a recipe that turned out to be wrong. Nothing else in this
# module may change without it.
_FP_VERSION = "v1"

_UNKNOWN: FingerprintMethod = "unknown"


def normalize_body(text: str) -> str:
    """Strip trailing whitespace and line-ending noise before hashing.

    Neither is a change a human reviews, and both are produced constantly by
    editors, formatters and platform line endings. A fingerprint that noticed
    them would reopen units for edits nobody made.
    """

    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _digest(kind: str, *parts: str) -> str:
    """sha256 over ``\\x00``-joined parts, prefixed by the recipe version.

    The NUL separator is what stops ``("ab", "c")`` and ``("a", "bc")`` from
    hashing alike; the version prefix is what lets a future recipe be told
    apart from this one rather than silently colliding with it.
    """

    payload = "\x00".join((_FP_VERSION, kind, *parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def unit_key(kind: str, path: str, symbol: str, ordinal: int) -> str:
    """Stable, position-independent identity for one reviewable thing.

    Deliberately **not** versioned with ``_FP_VERSION``: identity must survive a
    fingerprint recipe change, or every mark in every stored review would be
    orphaned by a hashing fix.

    * ``file``   -- ``symbol=""``, ``ordinal=0``.
    * ``symbol`` -- ``symbol`` is the qualified name where one is known, and
      ``ordinal`` is the 0-based index among same-``(path, symbol)`` definitions
      in file order, so overloads and same-named siblings stay distinct.
    * ``hunk``   -- ``symbol=""``, ``ordinal`` is the 0-based hunk index within
      the file. This one *is* position-derived and therefore the weakest of the
      three: re-hunking a file cannot preserve it, which is why hunk marks are
      advisory and file/symbol marks are authoritative.
    """

    payload = "\x00".join((kind, path, symbol, str(ordinal)))
    return f"{kind[:3]}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def file_fingerprint(path: str, text: str) -> str:
    """Fingerprint of a whole new-side file.

    Path is *included*: two files with identical content are two different
    things to review, and the file unit is the one place where a move genuinely
    is a change worth re-reading.
    """

    return _digest("file", path, normalize_body(text))


def unknown_file_fingerprint(path: str, status: str) -> str:
    """The placeholder for a file whose new-side content cannot be hashed.

    Deleted files, binaries and unreadable blobs. Real and deterministic rather
    than ``""``, so it can be stored, compared and carried — and paired with
    ``fingerprint_method="unknown"`` so it can never read as reviewed.
    """

    return _digest("file", path, status)


def symbol_fingerprint(kind: str, qualified_name: str, body: str) -> str:
    """Fingerprint of one definition's body.

    **Path is excluded on purpose**: moving a file must not reopen every symbol
    in it. The file unit already carries the move.
    """

    return _digest("symbol", kind, qualified_name, normalize_body(body))


def deleted_symbol_fingerprint(path: str, qualified_name: str) -> str:
    """The placeholder for a base-side definition that no longer exists.

    A deleted symbol has no body anywhere: nothing in this package computes
    base-side text, and its window collapses to a single line. It gets a
    synthetic fingerprint and ``fingerprint_method="unknown"`` — it can be
    marked reviewed, it is never "changed since review", and it simply
    disappears from the next revision.
    """

    return _digest("deleted", path, qualified_name)


def hunk_fingerprint(path: str, patch_body: str) -> str:
    """Fingerprint of one hunk's captured body."""

    return _digest("hunk", path, normalize_body(patch_body))


def unknown_hunk_fingerprint(hunk: DiffHunk, path: str) -> str:
    """The fallback when the hunk body was not captured or was over the cap.

    Composed from the header and both range tuples — the only hunk data with
    reviewable precision left. It is geometry, not content, so it is paired with
    ``fingerprint_method="unknown"``.
    """

    return _digest("hunk", path, hunk.header, str(hunk.new_ranges), str(hunk.old_ranges))


def tree_fingerprint(units: Sequence[ReviewUnit]) -> str:
    """The ReviewRevision content identity: one hash over whole-file units only.

    A ReviewRevision represents the code bytes the human is reviewing, not the
    current parser/index interpretation of those bytes. Symbol names, hunk
    boundaries, caller counts and other analysis projections may legitimately
    improve between LemonCrow builds; none of them may manufacture a new code
    revision or make valid evidence stale.

    Every changed path is guaranteed to have one file unit, whose fingerprint is
    content-derived (or an explicit unknown placeholder), so file units are the
    stable code-identity substrate. Sorted for derivation-order independence.
    """

    lines = sorted(f"{unit.unit_key}:{unit.content_fingerprint}" for unit in units if unit.kind == "file")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def qualified_window_names(windows: Sequence[SymbolWindow]) -> tuple[str, ...]:
    """Each window's dotted name, read off definition containment.

    ``symbol_windows`` names a definition by its own identifier, so two methods
    called ``run`` in one file are two windows both called ``run``. That is not
    a name a reviewer can act on: sent to ``svc.py::run`` they arrive at one of
    two methods and have no way to tell which. Nesting is the disambiguator the
    code itself already carries -- ``Reader.run`` and ``Writer.run`` -- and it
    is recoverable from the windows without a second parse, because a window
    that starts after another and ends no later than it is inside it.

    Preferred over the bare name, and *not* over a qualified name the code index
    supplied: the index resolves imports and aliases, and this does not. It is
    the fallback for the very common case of a repository with no index at all.

    Returns one name per window, in the order the windows were given, so a
    caller can zip it against them. Never raises: overlapping or degenerate
    windows simply qualify less.
    """

    order = sorted(range(len(windows)), key=lambda index: (windows[index].start_line, -windows[index].end_line))
    names: list[str] = [""] * len(windows)
    # (end_line, qualified name) of every definition still open at this point in
    # the file, outermost first.
    open_windows: list[tuple[int, str]] = []
    for index in order:
        window = windows[index]
        while open_windows and open_windows[-1][0] < window.end_line:
            open_windows.pop()
        prefix = open_windows[-1][1] if open_windows else ""
        qualified = f"{prefix}.{window.name}" if prefix and window.name else window.name
        names[index] = qualified
        open_windows.append((window.end_line, qualified))
    return tuple(names)


def _touches_hunk(start: int, end: int, hunk: DiffHunk) -> bool:
    """True when ``[start, end]`` intersects a line the hunk actually changed.

    The same rule ``impact._overlaps`` applies, restated here rather than
    imported because it is private and this module may not reach into it. Empty
    ``new_ranges`` means the producer could not read the patch body — *not* that
    nothing changed — so the span widens back to the printed hunk rather than
    dropping the symbol.
    """

    if not hunk.new_ranges:
        return start <= hunk.new_start + hunk.new_lines - 1 and end >= hunk.new_start
    return any(start <= high and end >= low for low, high in hunk.new_ranges)


def _symbol_reasons(base: tuple[str, ...], symbol: ChangedSymbol | None) -> tuple[str, ...]:
    """The file's reasons plus this symbol's own, in a fixed, duplicate-free order.

    ``ordering.rank_files`` already phrases some of its reasons from the same
    caller counts, so the two lists overlap; printing "4 known callers" twice on
    one row reads as a bug in the ranking rather than as an overlap in the
    evidence.
    """

    out: list[str] = list(base)
    if symbol is not None:
        candidates = [f"symbol {symbol.change}"]
        if symbol.caller_count >= 0:
            candidates.append(f"{symbol.caller_count} known callers")
        if symbol.centrality_rank is not None:
            candidates.append(f"centrality rank {symbol.centrality_rank}")
        out.extend(candidate for candidate in candidates if candidate not in out)
    return tuple(out)


def _name_for(symbol: ChangedSymbol | None, fallback: str) -> str:
    """The name a symbol unit is keyed on, keeping the most specific scope.

    The index normally wins because it resolves imports and aliases that the
    containment walk cannot see. Some language indexes, however, report a local
    assignment with only its bare name (for example a shell variable assigned
    both at module scope and inside a function). In that case blindly preferring
    the index collapses two definitions onto one ``unit_key``. When the
    containment name is a strict qualification of the indexed name, keep the
    containment name instead; it contains strictly more identity information
    without contradicting the index.

    Where there is no index, *fallback* is the name read off definition
    containment (:func:`qualified_window_names`) rather than the bare
    identifier, so ``Reader.run`` and ``Writer.run`` remain distinct.

    Known limitation: units derived with an index and units derived without one
    can key the same definition differently. Reopening a file for one look is a
    far smaller harm than sending a reviewer to the wrong definition.
    """

    indexed = symbol.qualified_name if symbol is not None else None
    if indexed:
        if fallback and fallback != indexed:
            if fallback.endswith(f".{indexed}"):
                return fallback
            if indexed.endswith(f".{fallback}"):
                return indexed
            # If both sources agree on the leaf definition but disagree on its
            # owning scope, the containment walk is tied to the actual parsed
            # body we are fingerprinting. Prefer it over a stale/misattributed
            # index qualification rather than collapsing two sibling methods
            # onto the same unit key.
            if "." in fallback and "." in indexed and fallback.rsplit(".", 1)[-1] == indexed.rsplit(".", 1)[-1]:
                return fallback
        return indexed
    if fallback:
        return fallback
    return symbol.symbol_name if symbol is not None else ""


def derive_units(packet: ReviewPacket, new_blobs: Mapping[str, str]) -> tuple[ReviewUnit, ...]:
    """Every reviewable unit of *packet*, flat, in packet-file order.

    One ``file`` unit per changed file, one ``hunk`` unit per hunk, and one
    ``symbol`` unit per definition the diff actually touched in a file the
    impact pass had something to say about. Attention rank, group and reasons
    are joined by path from ``packet.order``; a file the ``--limit`` truncation
    left unranked gets rank ``0`` and sorts last rather than going missing.

    ``revision_id`` is left blank: units are derived before the revision they
    belong to exists, and ``ReviewStore.add_revision`` stamps it inside the same
    transaction that writes the revision row.

    Never raises. A file whose new-side text is unavailable still produces a file
    unit, with ``fingerprint_method="unknown"``.
    """

    order_by_path: dict[str, ReviewOrderEntry] = {entry.path: entry for entry in packet.order}
    symbols_by_path: dict[str, list[ChangedSymbol]] = {}
    for symbol in packet.symbols:
        symbols_by_path.setdefault(symbol.file_path, []).append(symbol)

    units: list[ReviewUnit] = []
    for item in packet.files:
        entry = order_by_path.get(item.path)
        rank = entry.rank if entry is not None else 0
        group: FileCategory = entry.group if entry is not None else item.category
        reasons = entry.reasons if entry is not None else ()

        units.append(_file_unit(item, new_blobs, rank=rank, group=group, reasons=reasons))
        units.extend(_hunk_units(item, rank=rank, group=group, reasons=reasons))
        units.extend(
            _symbol_units(
                item,
                new_blobs.get(item.path, ""),
                symbols_by_path.get(item.path, []),
                rank=rank,
                group=group,
                reasons=reasons,
            )
        )
    return tuple(units)


def _file_unit(
    item: ChangedFile,
    new_blobs: Mapping[str, str],
    *,
    rank: int,
    group: FileCategory,
    reasons: tuple[str, ...],
) -> ReviewUnit:
    """The whole-file unit — the one every change is guaranteed to have."""

    method: FingerprintMethod
    if item.is_binary or item.status == "deleted" or item.path not in new_blobs:
        fingerprint = unknown_file_fingerprint(item.path, item.status)
        method = _UNKNOWN
    else:
        fingerprint = file_fingerprint(item.path, new_blobs[item.path])
        method = "blob_sha256"
    return ReviewUnit(
        revision_id="",
        unit_key=unit_key("file", item.path, "", 0),
        kind="file",
        path=item.path,
        content_fingerprint=fingerprint,
        fingerprint_method=method,
        attention_rank=rank,
        attention_group=group,
        reasons=reasons,
    )


def _hunk_units(
    item: ChangedFile,
    *,
    rank: int,
    group: FileCategory,
    reasons: tuple[str, ...],
) -> list[ReviewUnit]:
    """One unit per hunk, keyed on its ordinal within the file."""

    out: list[ReviewUnit] = []
    for ordinal, hunk in enumerate(item.hunks):
        method: FingerprintMethod
        if hunk.patch:
            fingerprint = hunk_fingerprint(item.path, hunk.patch)
            method = "hunk_patch_sha256"
        else:
            fingerprint = unknown_hunk_fingerprint(hunk, item.path)
            method = _UNKNOWN
        out.append(
            ReviewUnit(
                revision_id="",
                unit_key=unit_key("hunk", item.path, "", ordinal),
                kind="hunk",
                path=item.path,
                content_fingerprint=fingerprint,
                fingerprint_method=method,
                ordinal=ordinal,
                start_line=hunk.new_start,
                end_line=max(hunk.new_start, hunk.new_start + hunk.new_lines - 1),
                attention_rank=rank,
                attention_group=group,
                reasons=reasons,
            )
        )
    return out


def _symbol_units(
    item: ChangedFile,
    new_text: str,
    symbols: Sequence[ChangedSymbol],
    *,
    rank: int,
    group: FileCategory,
    reasons: tuple[str, ...],
) -> list[ReviewUnit]:
    """One unit per definition the hunks touched, plus one per deleted definition.

    Gated on the file appearing in ``packet.symbols`` at all: without that, the
    impact pass either skipped the file (binary, non-code category, no index) or
    found nothing in it, and manufacturing symbol units from a second, unfiltered
    parse would claim knowledge the packet does not have.
    """

    if not symbols:
        return []

    # One ``ChangedSymbol`` is claimed by at most one window. Two methods named
    # ``run`` in one file would otherwise both fall through to the same by-name
    # entry and be handed the *same* qualified name -- one of them wrong, and
    # wrong in the way that sends a reviewer to the other method.
    unclaimed: dict[str, list[ChangedSymbol]] = {}
    for symbol in symbols:
        unclaimed.setdefault(symbol.symbol_name, []).append(symbol)

    def _claim(name: str, start_line: int) -> ChangedSymbol | None:
        """The impact pass's record of this definition, consumed so it cannot repeat."""

        pool = unclaimed.get(name)
        if not pool:
            return None
        for index, candidate in enumerate(pool):
            if candidate.start_line == start_line:
                return pool.pop(index)
        return pool.pop(0)

    out: list[ReviewUnit] = []
    ordinals: dict[str, int] = {}
    matched: set[str] = set()

    if not item.is_binary and new_text and item.status != "deleted":
        windows = symbol_windows(new_text, item.path)
        qualified = qualified_window_names(windows)
        for window, containment_name in zip(windows, qualified, strict=True):
            # Ordinals are identity, so count *every* same-named definition in
            # file order before filtering to the hunk-touched subset. Otherwise
            # editing the second duplicate while the first is untouched mints
            # ordinal 0 and silently migrates the first definition's unit key.
            identity_name = containment_name or window.name
            ordinal = ordinals.get(identity_name, 0)
            ordinals[identity_name] = ordinal + 1
            if not any(_touches_hunk(window.start_line, window.end_line, hunk) for hunk in item.hunks):
                continue
            found = _claim(window.name, window.start_line)
            name = _name_for(found, identity_name)
            matched.add(name)
            method: FingerprintMethod = "symbol_body_sha256" if normalize_body(window.body) else _UNKNOWN
            out.append(
                ReviewUnit(
                    revision_id="",
                    unit_key=unit_key("symbol", item.path, name, ordinal),
                    kind="symbol",
                    path=item.path,
                    content_fingerprint=symbol_fingerprint(window.kind, name, window.body),
                    fingerprint_method=method,
                    symbol=name,
                    ordinal=ordinal,
                    start_line=window.start_line,
                    end_line=window.end_line,
                    attention_rank=rank,
                    attention_group=group,
                    reasons=_symbol_reasons(reasons, found),
                )
            )

    for symbol in symbols:
        if symbol.change != "deleted":
            continue
        name = _name_for(symbol, symbol.symbol_name)
        if name in matched:
            continue
        ordinal = ordinals.get(name, 0)
        ordinals[name] = ordinal + 1
        out.append(
            ReviewUnit(
                revision_id="",
                unit_key=unit_key("symbol", item.path, name, ordinal),
                kind="symbol",
                path=item.path,
                content_fingerprint=deleted_symbol_fingerprint(item.path, name),
                fingerprint_method=_UNKNOWN,
                symbol=name,
                ordinal=ordinal,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                attention_rank=rank,
                attention_group=group,
                reasons=_symbol_reasons(reasons, symbol),
            )
        )
    return out


__all__ = [
    "deleted_symbol_fingerprint",
    "derive_units",
    "file_fingerprint",
    "hunk_fingerprint",
    "normalize_body",
    "qualified_window_names",
    "symbol_fingerprint",
    "tree_fingerprint",
    "unit_key",
    "unknown_file_fingerprint",
    "unknown_hunk_fingerprint",
]
