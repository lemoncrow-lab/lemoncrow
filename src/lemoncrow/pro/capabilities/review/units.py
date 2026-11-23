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

import bisect
import hashlib
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

from lemoncrow.pro.capabilities.review.answer_category import (
    StructuralFacts,
    git_change_needs_person,
    structural_answer,
)
from lemoncrow.pro.capabilities.review.gitdiff import MAX_BLOB_BYTES
from lemoncrow.pro.capabilities.review.impact import SymbolWindow, function_valued_binding, symbol_windows
from lemoncrow.pro.capabilities.review.models import (
    ChangedFile,
    ChangedSymbol,
    DiffHunk,
    FileCategory,
    ReviewOrderEntry,
    ReviewPacket,
)
from lemoncrow.pro.capabilities.review.session_models import FingerprintMethod, ReviewUnit
from lemoncrow.pro.capabilities.review.special_changes import GITLINK_MODE, SYMLINK_MODE, special_git_change

# Bumping this reopens every unit in every stored review, by design: it is the
# escape hatch for a recipe that turned out to be wrong. Nothing else in this
# module may change without it.
_FP_VERSION = "v1"

_UNKNOWN: FingerprintMethod = "unknown"
_MODULE_DATA_LINE = re.compile(
    r"^(?:(?:export|declare|const|let|var|final|readonly)\s+)*" r"([A-Za-z_$][\w$]*)(?:\s*:\s*[^=]+)?\s*=(?!=)"
)
# Identity, not display: it is part of every module-data unit_key, so renaming it would reopen stored marks.
# The words a person reads are in targets.py (TOP_LEVEL_CODE_LABEL).
_MODULE_DATA_LABEL = "Module declarations"

# Files whose parts are statements that run top to bottom (SPEC section 7.1, OWNER-16): reordering them can change what
# the file does while every part keeps its bytes, so a reorder is a change the content fingerprint cannot see.
_ORDERED_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh", ".mk", ".mak")
_ORDERED_NAMES = frozenset({"makefile", "gnumakefile"})
_ORDERED_PREFIXES = ("makefile.", "gnumakefile.")
# ``Makefile.*`` is a Make file unless the tail says it is a document or data file about one.
_NOT_MAKE_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc", ".json", ".yml", ".yaml", ".html", ".pdf", ".pyc")
_SHELL_SHEBANG = re.compile(r"#![ \t]*(?:\S*/)?(?:env[ \t]+(?:-\S+[ \t]+)*)?(?:sh|bash|zsh|ksh|mksh|dash|ash)(?:\s|$)")


def orders_parts(path: str, text: str = "") -> bool:
    """Whether the order of a file's parts is part of what it means: shell scripts and Make files.

    By name (``*.sh``, ``*.bash``, ``*.zsh``, ``*.ksh``, ``*.mk``, ``*.mak``, ``Makefile``, ``GNUmakefile`` and
    ``Makefile.*`` unless it ends in a document or data suffix), or by content: a file whose first line is a shell ``#!`` line is a shell script whatever it is
    called, which is how an extensionless ``bin/run`` is recognised. *text* is the new-side content, read only for its
    first line; a caller with no content (``revisions``, which sees only units) tells such a file by the anchor its
    parts carry (:func:`split_anchor`), because the part was derived from the content.
    """

    name = path.rsplit("/", 1)[-1].lower()
    if name in _ORDERED_NAMES or name.endswith(_ORDERED_SUFFIXES):
        return True
    if name.startswith(_ORDERED_PREFIXES) and not name.endswith(_NOT_MAKE_SUFFIXES):
        return True
    return text.startswith("#!") and _SHELL_SHEBANG.match(text[:512].split("\n", 1)[0]) is not None


# What an ordered file's part adds to its content fingerprint: ``<fingerprint>@<anchor>`` (see :class:`_PartAnchors`).
_ANCHOR_SEP = "@"
ANCHOR_MOVED = "!moved"
"""The anchor a reopened mark is given when its part changed place among the file's other parts: it matches no real one."""
UNANCHORED = "unanchored:"
"""The prefix of the anchor of a part whose place in its file cannot be checked (:class:`_PartAnchors`)."""


def is_unanchored(anchor: str) -> bool:
    return anchor.startswith(UNANCHORED)


def split_anchor(fingerprint: str) -> tuple[str, str]:
    """``(fingerprint without its anchor, the anchor)``; the anchor is ``""`` for a part that has none."""

    core, sep, anchor = fingerprint.partition(_ANCHOR_SEP)
    return (core, anchor) if sep else (fingerprint, "")


def with_anchor(fingerprint: str, anchor: str) -> str:
    return f"{split_anchor(fingerprint)[0]}{_ANCHOR_SEP}{anchor}"


def place_reason(judged: str, now: str) -> str | None:
    """The reason code when two fingerprints of the same content (anchor aside) do not hold the same place, else ``None``.

    The anchor of an ordered file's part (``units._PartAnchors``) is part of its fingerprint, so a part that crossed
    lines of the base file it had no business crossing no longer matches the mark. Only the anchor differs here, which
    is what makes the reopening about the part's place and not "the part changed".

    * ``part_order_changed``: both places are known and differ (or the mark was stamped moved,
      ``units.ANCHOR_MOVED``). It says the part's position among the file's unchanged base lines changed, not that the
      part is known to have moved: a line just above it that the branch edited or deleted gives the same answer.
    * ``part_place_unverifiable``: a place cannot be checked, so nothing says it is the one the reviewer saw. The part's
      place is unreadable now or then (``units.UNANCHORED``: the file is too large or unreadable, and it changed since),
      or the verdict was recorded before places were kept and the part has one now. It is never carried: it reopens
      once and a new mark records the place.

    A unit with no anchor (a part of a file this build does not order) has no place to compare and says nothing.
    """

    if split_anchor(judged)[0] != split_anchor(now)[0]:
        return None
    return anchor_reason(judged, now)


def anchor_reason(judged: str, now: str) -> str | None:
    """:func:`place_reason` for the anchors alone, whatever the content: a part that was edited and moved is still moved."""

    judged_anchor, now_anchor = split_anchor(judged)[1], split_anchor(now)[1]
    if judged_anchor == ANCHOR_MOVED:
        return "part_order_changed"
    if not now_anchor:
        return None
    if not judged_anchor:
        return "part_place_unverifiable"
    if judged_anchor == now_anchor:
        return None
    if is_unanchored(judged_anchor) or is_unanchored(now_anchor):
        return "part_place_unverifiable"
    return "part_order_changed"


def module_data_name(line: str) -> str | None:
    """Name a top-level data binding without treating a callback as data."""

    match = _MODULE_DATA_LINE.match(line)
    if match is None:
        return None
    value = line.split("=", 1)[1].strip()
    if re.match(r"(?:lambda\b|(?:async\s+)?function\b|(?:useCallback|forwardRef|memo)\s*\()", value):
        return None
    # Arrow functions, including async, generic and return-typed ones.
    if re.match(r"(?:async\s+)?(?:<[^>]*>\s*)?(?:\([^)]*\)(?:\s*:\s*[^=]+?)?|[A-Za-z_$][\w$]*)\s*=>", value):
        return None
    return match.group(1)


def _module_data_groups(windows: Sequence[SymbolWindow], lines: Sequence[str]) -> tuple[tuple[SymbolWindow, ...], ...]:
    """Runs of small top-level bindings separated only by nearby comments or blanks."""

    groups: list[tuple[SymbolWindow, ...]] = []
    pending: list[SymbolWindow] = []

    def flush() -> None:
        if len(pending) > 1:
            groups.append(tuple(pending))
        pending.clear()

    for window in windows:
        declaration = lines[window.start_line - 1] if 0 < window.start_line <= len(lines) else ""
        candidate = window.end_line - window.start_line < 5 and module_data_name(declaration) is not None
        if not candidate:
            flush()
            continue
        if pending:
            gap = lines[pending[-1].end_line : window.start_line - 1]
            nearby = len(gap) <= 4 and all(
                not row.strip() or row.lstrip().startswith(("#", "//", "/*", "*")) for row in gap
            )
            if not nearby:
                flush()
        pending.append(window)
    flush()
    return tuple(groups)


def _int_or(value: object, default: int = 0) -> int:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return default


def patch_rows(patch: str) -> list[str]:
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


_OLD_NEIGHBOURS = 2


@dataclass(frozen=True)
class ChangeBlock:
    old_lines: tuple[int, ...]
    new_lines: tuple[int, ...]
    new_anchor: int
    old_text: tuple[str, ...] = ()
    """The deleted rows' own source text, parallel to *old_lines*.

    The base blob is not an input here, and the deleted text is the only
    evidence in the packet about what the old side of a block actually was.
    """
    old_before: tuple[str, ...] = ()
    """Up to :data:`_OLD_NEIGHBOURS` base rows just above the block, as the patch shows them.

    Where the block sat, said in text the patch already carries: unlike a base line number it does not move when
    the branch is rebased onto upstream lines elsewhere in the file. Only the rows above: what follows a block is
    whatever the next revision deleted beside it, so it cannot place the text the verdict covered.
    """
    old_raw: tuple[str, ...] = ()
    """*old_text* exactly as the patch prints it (see :func:`_row_text`); equal to it except on a marker row."""
    old_before_raw: tuple[str, ...] = ()
    """*old_before* exactly as the patch prints it."""


def _row_text(raw: str, following: str) -> tuple[str, str]:
    """A patch row as ``(source text, the text as the patch prints it)``, both without the origin prefix.

    Two tools print a row that ends the file without a newline differently. libgit2's rebuilt patch text puts the
    "no newline at end of file" marker's origin character (``<``, ``>`` or ``=``) on the end of the row before the
    marker line, because that row carries no newline of its own; a standard unified diff (GitHub's ``patch``)
    prints the row as it is. A row followed by a ``\\`` marker line that ends in one of those characters is
    therefore ambiguous here, so the source text drops it and the printed text keeps it: the base file says
    which is right (:meth:`_OwnedOldText._count_earlier`).
    """

    shown = raw[1:]
    if following.startswith("\\") and shown[-1:] in ("<", ">", "="):
        return shown[:-1], shown
    return shown, shown


def change_blocks(hunk: Mapping[str, Any]) -> tuple[ChangeBlock, ...] | None:
    """Parse exact +/- geometry from a captured hunk body.

    ``None`` means the body was unavailable or unrecognised.  In that case the
    caller must use one hunk fallback target; it must not manufacture symbol
    precision from the context-inclusive hunk header.
    """

    patch = str(hunk.get("patch") or "")
    if not patch:
        return None
    old_line = _int_or(hunk.get("old_start"), 0)
    new_line = _int_or(hunk.get("new_start"), 0)
    blocks: list[tuple[tuple[int, ...], tuple[int, ...], int, tuple[str, ...], tuple[str, ...], int]] = []
    old_changed: list[int] = []
    old_text: list[str] = []
    old_text_raw: list[str] = []
    new_changed: list[int] = []
    block_anchor = max(1, new_line)
    old_rows: list[str] = []  # every old-side row of the hunk (context and deleted), in order
    old_rows_raw: list[str] = []  # the same rows as the patch prints them
    block_start = 0

    def flush() -> None:
        nonlocal old_changed, old_text, old_text_raw, new_changed, block_anchor
        if old_changed or new_changed:
            blocks.append(
                (
                    tuple(old_changed),
                    tuple(new_changed),
                    max(1, block_anchor),
                    tuple(old_text),
                    tuple(old_text_raw),
                    block_start,
                )
            )
        old_changed = []
        old_text = []
        old_text_raw = []
        new_changed = []
        block_anchor = max(1, new_line)

    patch_lines = patch_rows(patch)
    for index, raw in enumerate(patch_lines):
        if raw.startswith("\\"):
            continue
        if not raw:
            # An empty patch row still has an origin prefix in a valid unified
            # diff.  Seeing a truly empty row means this body cannot be trusted.
            return None
        prefix = raw[0]
        text, shown = _row_text(raw, patch_lines[index + 1] if index + 1 < len(patch_lines) else "")
        if prefix == " ":
            flush()
            old_rows.append(text)
            old_rows_raw.append(shown)
            old_line += 1
            new_line += 1
            block_anchor = max(1, new_line)
        elif prefix == "-":
            if not old_changed and not new_changed:
                block_anchor = max(1, new_line)
                block_start = len(old_rows)
            old_changed.append(max(1, old_line))
            old_text.append(text)
            old_text_raw.append(shown)
            old_rows.append(text)
            old_rows_raw.append(shown)
            old_line += 1
        elif prefix == "+":
            if not old_changed and not new_changed:
                block_anchor = max(1, new_line)
                block_start = len(old_rows)
            new_changed.append(max(1, new_line))
            new_line += 1
        else:
            return None
    flush()
    return tuple(
        ChangeBlock(
            old,
            new,
            anchor,
            text,
            tuple(old_rows[max(0, start - _OLD_NEIGHBOURS) : start]),
            raw,
            tuple(old_rows_raw[max(0, start - _OLD_NEIGHBOURS) : start]),
        )
        for old, new, anchor, text, raw, start in blocks
    )


# A deleted row that opens a definition of its own. Deliberately an allowlist of
# declaration keywords with a name after them: a keyword this misses costs one
# extra line on a symbol's old span, while a false positive would split an
# ordinary rewrite into two review targets.
DEFINITION_HEADER = re.compile(
    r"^\s*(?:(?:export|default|public|private|protected|internal|static|final"
    r"|abstract|override|open|pub|declare|inline)\s+)*"
    r"(?:async\s+)?"
    r"(?:def|class|function|func|fn|struct|trait|impl|enum|interface"
    r"|namespace|module|record|object|type|sub|proc)\s+(?:\([^)]*\)\s*)?[A-Za-z_$]"
)


def new_side_line_text(hunks: Sequence[Any]) -> dict[int, str]:
    """Best-effort new-side source lines available inside captured patch hunks."""

    out: dict[int, str] = {}
    for raw_hunk in hunks:
        if not isinstance(raw_hunk, Mapping):
            continue
        patch = str(raw_hunk.get("patch") or "")
        if not patch:
            continue
        old_line = _int_or(raw_hunk.get("old_start"), 0)
        new_line = _int_or(raw_hunk.get("new_start"), 0)
        for raw in patch_rows(patch):
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


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip())


def _prelude_row(line: str) -> bool:
    """Whether *line* is blank, a comment or a decorator/annotation.

    These belong to the definition *below* them, which is the same rule
    ``impact._prelude_start`` uses to fence one definition off from the next.
    """

    text = line.strip()
    return not text or text.startswith(("#", "//", "/*", "*", "@"))


def _definition_shape(line: str) -> str | None:
    """Normalize only the declared name of a visible callable/definition."""

    binding = re.match(
        r"^(\s*(?:(?:export|default|public|private|protected|static|declare)\s+)*"
        r"(?:const|let|var)\s+)([A-Za-z_$][\w$]*)(\s*=\s*.+)$",
        line,
    )
    if binding and function_valued_binding(line):
        return f"{binding.group(1)}<name>{binding.group(3)}"

    definition = re.match(
        r"^(\s*(?:(?:export|default|public|private|protected|internal|static|final|abstract|override|open|pub|declare|inline)\s+)*"
        r"(?:async\s+)?(?:def|class|function|func|fn|struct|trait|impl|enum|interface|namespace|module|record|object|type|sub|proc)\s+"
        r"(?:\([^)]*\)\s*)?)([A-Za-z_$][\w$]*)(.*)$",
        line,
    )
    if definition:
        return f"{definition.group(1)}<name>{definition.group(3)}"
    return None


def _same_definition_replacement(block: ChangeBlock, owner_declaration: str) -> bool:
    """Whether one deleted declaration is plainly the surviving declaration renamed."""

    if len(block.old_lines) != 1 or len(block.new_lines) != 1 or len(block.old_text) != 1:
        return False
    old_shape = _definition_shape(block.old_text[0])
    new_shape = _definition_shape(owner_declaration)
    return bool(old_shape and new_shape and old_shape == new_shape)


def _foreign_definition_start(block: ChangeBlock, owner_declaration: str) -> int | None:
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
    if _same_definition_replacement(block, owner_declaration):
        return None
    # An owner whose declaration line is outside the captured patch cannot vouch
    # for its own nesting level, so any definition in the deleted text counts.
    owner_indent = _indent_width(owner_declaration) if owner_declaration.strip() else -1
    for index, text in enumerate(block.old_text):
        if not text.strip():
            continue
        if not (DEFINITION_HEADER.match(text) or function_valued_binding(text)):
            continue
        if _indent_width(text) > owner_indent:
            continue
        start = index
        while start > 0 and _prelude_row(block.old_text[start - 1]):
            start -= 1
        return start
    return None


def owned_old_count(
    block: ChangeBlock,
    source_lines: Mapping[int, str],
    owner_start: int,
    owner_end: int,
    *,
    owner_is_group: bool,
) -> int:
    """How many of *block*'s leading deleted rows the owner of its added lines owns.

    The one ownership rule, read by both ends. ``targets.derive_review_targets``
    shows the owner ``block.old_lines[:count]`` (the rest goes to the hunk) and
    :class:`_OwnedOldText` hashes exactly those rows into the owner's
    fingerprint, so a definition's verdict covers the deleted text its part shows
    and no other: two reviewed definitions swapped in place stay reviewed, while
    a foreign definition deleted beside one is never carried under its mark.

    *source_lines* is the new-side text the captured patch carries, which is
    what the Reader sees; a declaration line outside the patch reads as empty.
    A module-data group that does not still declare every deleted member owns
    nothing, because its current content cannot vouch for a member that is gone.
    """

    if not block.old_lines:
        return 0
    if owner_is_group:
        current_names = {
            name
            for line in range(owner_start, owner_end + 1)
            if (name := module_data_name(source_lines.get(line, ""))) is not None
        }
        deleted_names = {name for text in block.old_text if (name := module_data_name(text)) is not None}
        if not deleted_names.issubset(current_names):
            return 0
    boundary = _foreign_definition_start(block, source_lines.get(owner_start, ""))
    return len(block.old_lines) if boundary is None else boundary


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
    * ``dependency`` -- callers pass the package ecosystem in ``path`` and the
      normalized component identity in ``symbol``. The persisted ReviewUnit's
      own ``path`` remains its source anchor (manifest/lockfile), so package
      identity survives source movement and version changes.
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


def symbol_fingerprint(kind: str, qualified_name: str, body: str, owned_old: str = "") -> str:
    """Fingerprint of one definition's body, and of the deleted text it owns.

    **Path is excluded on purpose**: moving a file must not reopen every symbol
    in it. The file unit already carries the move.

    *owned_old* is the base-side text of every change block that has an added
    line inside this definition. Git aligns a deletion with the added lines
    beside it, and the Reader gives that deletion to the definition that holds
    the added lines (``targets.derive_review_targets``), so a mark that did not
    hash it would stay valid when a later revision deletes more of it: the
    body, the only thing hashed, did not move. Empty -- no deleted text beside
    the definition -- keeps the original recipe, so nothing already stored
    reopens.

    With deleted text the value is ``<digest of body and deleted text>~<the
    body-only digest>~<digest of the deleted text alone>``. The first part is
    what a mark compares (and what a prefix-comparing consumer sees first); the
    second is what a definition that *moves* is recognised by
    (:func:`bare_fingerprint`), because the deleted text belongs to the place the
    definition sat, not to the definition; the third lets a reader that has only
    two fingerprints say whether the deleted text is the same without the base
    file (:func:`owned_text_digest`).
    """

    bare = _digest("symbol", kind, qualified_name, normalize_body(body))
    if owned_old:
        full = _digest("symbol+old", kind, qualified_name, normalize_body(body), owned_old)
        return f"{full}~{bare}~{_digest('owned', owned_old)}"
    return bare


def bare_fingerprint(fingerprint: str) -> str:
    """The body-only part of a unit fingerprint: the whole value when the unit owns no deleted text."""

    parts = split_anchor(fingerprint)[0].split("~")
    return parts[1] if len(parts) > 1 else parts[0]


def owned_text_digest(fingerprint: str) -> str:
    """The digest of the deleted text (and where it sat) a fingerprint covers; ``""`` when it covers none.

    A composite fingerprint from before this part existed has none to compare, so it is never equal to a current one.
    """

    parts = split_anchor(fingerprint)[0].split("~")
    return parts[2] if len(parts) > 2 else ""


def owns_deleted_text(fingerprint: str) -> bool:
    """Whether *fingerprint* covers deleted base text beside its definition, not only the definition's body."""

    return "~" in split_anchor(fingerprint)[0]


def symbol_body_digest(kind: str, qualified_name: str, body: str) -> str:
    """The body of one definition with its own name masked out, or ``""`` when it has none.

    Not a fingerprint and never stored: :func:`symbol_fingerprint` includes the
    qualified name, so a renamed definition can never match its old self by
    fingerprint. This digest is what recognises "same body, different name" so
    the rename can be *reported* (a renamed part reopens, it is never carried).
    A false match here only labels a reopened part as renamed; it cannot keep
    anything reviewed.

    **A "renamed" label is not proof that the change is routine.** Every whole-word
    use of the leaf name is masked, not just the definition's own name token, so
    ``return x.foo`` -> ``return x.bar`` and ``'foo'`` -> ``'bar'`` in a body read as
    the same shape. A consumer that wants to treat a rename as mechanical (the
    P11-2 Quick check batch) must compare the full old and new content itself and
    must not rely on this digest. ``lc-debt: masks every whole-word use of the
    leaf, attribute accesses and string literals included; mask only the
    definition token and recursive self-calls (needs a per-language parse)``.
    """

    words = re.findall(r"\w+", qualified_name)
    if not words or not normalize_body(body):
        return ""
    masked = re.sub(rf"\b{re.escape(words[-1])}\b", "\x00", normalize_body(body))
    return _digest("symbol-shape", kind, masked)


def symbol_header(lines: Sequence[str], start_line: int) -> str | None:
    """The decorator, annotation or attribute lines above a definition's window, normalised.

    Python decorators, Rust attributes, TypeScript decorators and the like sit *outside* the window that
    :func:`symbol_fingerprint` hashes, so two definitions with the same fingerprint can still differ in
    ``@login_required`` or ``#[cfg(test)]``. A move carries a judgment only when this header is also the same.

    Scanning goes upward from *start_line* (1-based) through blank lines and comment lines (``#``, ``//``, ``/* */``),
    which every supported language allows between a decorator and its definition, collecting the lines that start
    with ``@`` or ``#[`` or are a bare ``[Attr]`` line. A multi-line decorator is read as one run: a line that closes
    more brackets than it opens starts it, and the run is kept only if the line where it opens is itself a decorator
    or attribute head (``@``, ``#[``, or ``[`` when the run closes with ``]``, so ``#[derive(`` ... ``)]`` is read whole).
    Brackets inside string literals and trailing comments are not counted; a ``#`` is a comment only after whitespace.

    Fail-closed: whatever cannot be positively resolved returns ``None``, which the caller reads as "do not carry".
    That covers an unterminated quote (except the closing line of a docstring directly above, which ends the scan), a bracket after a trailing ``//`` or ``/*``, a run that never opens, and a run
    that opens on something other than a decorator while no blank line separates it from the definition or from the
    decorators already collected (it may be a decorator's argument line the bracket count got wrong). A run that opens
    on another statement after a blank line is that statement's, and ends the scan. Inside a run only whole-line
    comments are skipped, and ``//`` is never one there or where it closes a bracket (Python's floor division). Where
    the scan would end on a line that could still be the tail of a decorator (the line above it starts a decorator or
    ends in ``\\``, ``(``, ``[`` or, below a closing bracket, ``,``), it returns ``None`` instead of ``""``.
    Comments and blank lines are not part of the header: adding one never reopens a part.

    Returns ``""`` when nothing decorates the definition.
    """

    def code_of(line: str) -> str | None:
        """*line* without string literals and trailing comments, or ``None`` when that cannot be told."""

        out: list[str] = []
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if c in "\"'`":
                if line.startswith(c * 3, i):
                    end = line.find(c * 3, i + 3)
                    if end < 0:
                        return None
                    i = end + 3
                    continue
                j = i + 1
                while j < n and line[j] != c:
                    j += 2 if line[j] == "\\" else 1
                if j >= n:
                    return None
                i = j + 1
                continue
            if c == "#" and (i == 0 or line[i - 1].isspace()) and not (i == 0 and line.startswith(("#[", "#!"))):
                break  # a comment needs whitespace or the line start before it (``this.#x``, ``r#".."#`` are not one)
            if line.startswith(("//", "/*"), i):
                return None if any(ch in line[i:] for ch in "()[]") else "".join(out)
            out.append(c)
            i += 1
        return "".join(out)

    def balance(line: str) -> int | None:  # closers minus openers: positive while a run is still open above
        text = line if line.startswith("//") else code_of(line)
        if text is None:
            return None
        return text.count(")") + text.count("]") - text.count("(") - text.count("[")

    def reads_on_above(at: int, closes: bool = True) -> bool:
        """True when the next non-blank line at or above *at* is a decorator, or leaves a statement open.

        The counter reads a line as balanced and head-less that may be the tail of a decorator (a regex with an
        unbalanced bracket, a backslash continuation, ``this.#x)``): the scan then ends where a decorator may continue.
        A trailing comma above counts only when the line below *closes* a bracket, as a tail does: a dict's last
        entry above its ``}`` is not one.
        """
        while 0 <= at < len(lines):
            text = " ".join(lines[at].split())
            at -= 1
            if text:
                return text.startswith(("@", "#[")) or text.endswith(("\\", "(", "[") + ((",",) if closes else ()))
        return False

    collected: list[str] = []
    run_lines: list[str] = []
    run_open = False
    run_attached = False  # no blank line between the run and what is below it
    depth = 0  # brackets that lines of the run closed and a higher line has yet to open
    attached = True  # no blank line since the definition or the last decorator collected
    in_block = False  # inside a ``/* ... */`` comment, scanning upward to its opener
    index = start_line - 2
    while 0 <= index < len(lines):
        raw = " ".join(lines[index].split())
        index -= 1
        if in_block:
            in_block = "/*" not in raw
            continue
        if not raw:
            attached = False
            continue
        if raw.endswith("*/") and (raw.startswith("/*") or "/*" not in raw):
            in_block = not raw.startswith("/*")  # a one-line comment, or the closer of a longer one
            continue
        if not run_open and raw.startswith(("//", "/*")):
            if raw.startswith("/*") or (balance(raw) or 0) <= 0:
                continue  # (a ``//`` line that closes a bracket is Python's floor division, so it is code)
        if raw.startswith("#") and not raw.startswith("#["):
            continue
        delta = balance(raw)
        if delta is None:
            if not run_open and raw.endswith(('"""', "'''")):
                break  # the closing quotes of a docstring: the previous statement, which no decorator ends with
            return None
        if not run_open:
            if raw.startswith(("@", "#[")) or (raw.startswith("[") and raw.endswith("]")):
                collected.append(raw)
                attached = True
            elif delta > 0:
                run_open, run_attached, depth = True, attached, delta
                run_lines = [raw]  # a closing line: a multi-line run starts here
            else:
                is_stub = raw.startswith(("def ", "async def ", "class "))  # a one-line stub under its own decorator
                if not is_stub and reads_on_above(index, (code_of(raw) or "").rstrip().endswith((")", "]"))):
                    return None  # the tail of a decorator the counter read as balanced
                break  # neither a decorator nor the tail of a multi-line one
            continue
        run_lines.append(raw)
        depth += delta
        if depth <= 0:
            # A ``[Attr(`` head (C#) is a decorator only when the run closes with ``]``: the first line scanned.
            is_head = raw.startswith(("@", "#[")) or (raw.startswith("[") and run_lines[0].endswith("]"))
            if not is_head:
                # The previous statement's call, or a decorator argument line miscounted: only a blank line says which.
                if not raw.startswith(("def ", "async def ", "class ")) and (run_attached or reads_on_above(index)):
                    return None  # (a run that opens on a definition is that definition's own signature)
                return "\n".join(reversed(collected))
            collected.extend(run_lines)
            run_open, attached = False, True
    if run_open or in_block:
        return None
    return "\n".join(reversed(collected))


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


class _OwnedOldText:
    """The deleted text a definition's part shows, found by the added lines it replaces.

    Built once per file from the hunks' captured patch bodies (one linear pass
    over text :func:`_hunk_units` already hashes) and only for a file that has a
    touched definition. A block with no deleted text, or no added line, is not
    kept: a pure deletion belongs to the hunk, never to a definition.

    What a window owns is decided by the same two rules the Reader applies
    (``targets.derive_review_targets``): every added line of the block lies in
    the window, and :func:`owned_old_count` says how many of the block's deleted
    rows are the window's own (a foreign definition deleted beside it is the
    hunk's). Each owned block is hashed with its base path, the base rows just
    above it and its *occurrence*: how many times that same run of rows (the
    rows above and the owned text) already occurs earlier in the base file. Text,
    not line numbers, so a rebase that only shifts the block keeps it; and the
    occurrence is what tells two identical runs of the same file apart, so the
    same text deleted from a second copy is different text to the verdict.

    The occurrence reads the base file, once per file that has owned deleted
    text (a split of at most ``MAX_BLOB_BYTES``) and then one scan of the rows
    above each owned block. When the base file is not at hand, is over that
    bound, or does not hold the rows the patch says it deleted, the occurrence is
    ``unanchored:<digest>`` of the base path, the run's rows (as the patch prints
    them) and the base line the run starts on (:meth:`_unanchored`): the same on
    every refresh of a revision, so an unchanged part keeps its verdict, and
    different as soon as the owned text, the rows above it or its base line
    differ, so a changed part reopens. It never equals an anchored occurrence, so
    a base file that becomes readable (or stops being) reopens the part once.

    A rebase can reopen a part: an upstream edit to either row above the block, or
    rows added above a block that sat at base line 1 or 2 (its rows above grow),
    change what is hashed, and so does an upstream copy of the same run added or
    removed above it when the base file is readable. With no readable base the
    last is not seen: an upstream edit that adds an identical copy of the run above
    it and leaves the run on the same base line keeps the part's verdict where the
    anchored path would reopen it (the base line is the only place the digest has).

    ``lc-debt: every window that contains a block's added lines hashes the
    block's owned rows, including enclosing windows the targets pass would not
    own it for, so an enclosing definition can reopen when a nested one's
    neighbour changes (fail-safe only); mirror _review_worthy_symbols at capture
    to tighten.``
    """

    def __init__(self, raw_hunks: Sequence[Mapping[str, Any]], old_path: str, old_text: str | None) -> None:
        self._old_path = old_path
        self._old_text = old_text
        self._base_rows: list[str] | None = None
        self._anchors: dict[tuple[int, int], tuple[str, list[str]]] = {}
        self._source = new_side_line_text(raw_hunks)
        found: list[ChangeBlock] = []
        for raw in raw_hunks:
            for block in change_blocks(raw) or ():
                if block.new_lines and block.old_text:
                    found.append(block)
        found.sort(key=lambda block: block.new_lines[0])
        self._blocks = found
        self._firsts = [block.new_lines[0] for block in found]

    def within(self, start: int, end: int, *, group: bool = False) -> str:
        """The owned deleted text of every block whose added lines all lie in ``[start, end]``; ``""`` when none.

        A window that does not contain the whole block is never shown its text and must not hash it: a definition
        that merely sits beside a replaced neighbour keeps its fingerprint when the neighbour's deletion changes. A
        window that contains the block may or may not be the Reader's owner (a nested definition or a filtered one
        can be), and hashing for every window that could be only ever reopens a part.
        """

        # Blocks are sorted by first line (and are disjoint on the new side), so one bisect finds the first block that
        # can start inside the window and the scan stops at the first one that starts past its end.
        index = bisect.bisect_left(self._firsts, start)
        out: list[str] = []
        while index < len(self._blocks) and self._firsts[index] <= end:
            block = self._blocks[index]
            index += 1
            if block.new_lines[-1] > end:
                continue
            count = owned_old_count(block, self._source, start, end, owner_is_group=group)
            if count:
                occurrence, run = self._anchor(block, count)
                above = len(block.old_before)
                out.append(
                    "\n".join(run[:above]) + f"\x01{self._old_path}\x01{occurrence}\x01" + "\n".join(run[above:])
                )
        return "\x00".join(out)

    def _anchor(self, block: ChangeBlock, count: int) -> tuple[str, list[str]]:
        """``(occurrence, rows)`` of the block's run: the rows above, then the *count* owned ones.

        *occurrence* is how many earlier copies of the run the base file holds, and *rows* are the run's text as the
        base file has it. Each patch row is checked against the base row at the position the diff names: it must be
        that row, or that row plus the one marker character a libgit2-built patch appends to a row that ends the
        file with no newline (:func:`_row_text`). The hashed text is the base row's, so a tool that prints the end
        of file differently from the other cannot change a fingerprint, and a row the base file does not hold
        cannot be hashed as if it were anchored.
        """

        key = (block.new_lines[0], count)
        found = self._anchors.get(key)
        if found is None:
            found = self._anchors[key] = self._find_anchor(block, count)
        return found

    def _find_anchor(self, block: ChangeBlock, count: int) -> tuple[str, list[str]]:
        run = [*block.old_before, *block.old_text[:count]]
        shown = [*block.old_before_raw, *block.old_raw[:count]]
        if self._old_text is None or len(self._old_text) > MAX_BLOB_BYTES:
            return self._unanchored(block, run, shown)
        if self._base_rows is None:
            self._base_rows = self._old_text.split("\n")
        rows = self._base_rows
        here = block.old_lines[0] - 1 - len(block.old_before)
        base = rows[here : here + len(run)] if here >= 0 else []
        if len(base) != len(run) or any(row not in (a, b) for row, a, b in zip(base, run, shown, strict=True)):
            return self._unanchored(block, run, shown)
        head, size = base[0], len(base)
        return str(sum(1 for i in range(here) if rows[i] == head and rows[i : i + size] == base)), base

    def _unanchored(self, block: ChangeBlock, run: list[str], shown: list[str]) -> tuple[str, list[str]]:
        """The occurrence of a run whose base file cannot place it: ``unanchored:<digest>`` of what the patch alone says.

        The digest covers the base path, the base line the run starts on (the patch's own geometry, so two identical
        runs of one file are two places), where the owned rows begin in the run, the run's rows, and the rows as the
        patch prints them (*shown*). The base file is what says whether a trailing ``<``, ``>`` or ``=`` belongs to a
        row (:func:`_row_text`), and here there is none, so a row read both ways is two different texts: a patch from
        another tool costs one extra reopening and never a carry. The digest is the same on every refresh of a revision
        and on any revision that leaves the run alone (another file, or lines elsewhere in this one, change nothing
        here), so such a part keeps its verdict. It differs when the owned text, its rows above or its base line
        differ, so the part reopens. A rebase that only shifts the base line reopens it too.

        This is not a guard against every upstream change: an upstream edit that adds an identical copy of the run
        above it and leaves the run on the same base line (what it adds and removes above cancels out) changes
        nothing the digest reads, so the part keeps its verdict, where the anchored path counts the extra copy and
        reopens it. ``_OwnedOldText`` lists that edit among those that may reopen a part, not those that must.
        """

        digest = _digest(
            "owned-unanchored",
            self._old_path,
            str(block.old_lines[0]),
            str(len(block.old_before)),
            *run,
            "\x02",
            *shown,
        )
        return f"{UNANCHORED}{digest[:32]}", run


class _Unanchorable(Exception):
    """The base file and the patch do not agree well enough to place a part against the base."""


class _PartAnchors:
    """Where each part of an order-sensitive file sits among the lines of the base file (SPEC section 7.1, OWNER-16).

    The order of a shell or Make file is what it does, and a mark that only covers a part's bytes cannot see a part
    move across lines the branch never touched: ``main() {...}`` above ``echo start`` and the same ``main() {...}``
    below ``main`` are the same bytes. So each part carries an **anchor**, and the anchor is part of its fingerprint
    (``<fingerprint>@<anchor>``, :func:`split_anchor`): it names the nearest *surviving base line above* the part, that
    is the nearest line above it in the new file that the branch did not add (a context row, or a line outside every
    hunk, which the patch maps to its base row). The anchor is the digest of the base path, the two base rows ending
    at that line and the **occurrence** of that run among earlier identical runs in the base file, the same recipe
    :class:`_OwnedOldText` uses, so it is text and not a line number: lines the branch adds or removes elsewhere, and a
    rebase that only shifts the base, leave it as it was, and two identical runs of a repeating file stay two places.

    A definition is anchored at the line it starts on; a hunk at the first line of its patch. A part with nothing
    above it has the anchor ``top``, and every part of a file the branch added has ``new`` (there is no base line to
    cross; the order of such parts among themselves is read from the units, ``revisions._order_moved``).

    What changes an anchor is the part crossing a base line, or the base line just above it being deleted or edited
    by the branch (the nearest survivor is then a different line): a reopening, never a carry. Nothing about the
    branch's own added lines does. When the base file is not at hand (not given, over ``MAX_BLOB_BYTES``), a patch
    body is missing or malformed, or a row the geometry names is not the row the base holds, the place cannot be
    checked and the anchor is ``unanchored:<digest>`` of the file's path, its new text and its hunks. That is equal
    on two revisions exactly when the file did not change between them, so an untouched file keeps its verdicts, and
    it differs as soon as the file does: the part then reopens as ``part_place_unverifiable``, never as a move
    (:func:`place_reason`).
    """

    def __init__(self, item: ChangedFile, new_text: str, old_text: str | None) -> None:
        self._item = item
        self._base_path = item.old_path or item.path
        self._fresh = item.status == "added"
        self._new_text = new_text
        self._old_text = old_text
        self._state: str = "new"  # "new" until prepared, then "ready" or "failed"
        self._unanchored_digest: str | None = None
        self._new_rows: list[str] = []
        self._rows: list[str] = []
        self._runs: dict[tuple[str, ...], list[int]] = {}
        self._context: dict[int, int] = {}
        self._add_low: list[int] = []
        self._add_high: list[int] = []
        self._span_low: list[int] = []
        self._span_high: list[int] = []
        self._after: list[int] = []
        self._shift: list[int] = []

    def at_line(self, new_line: int) -> str:
        """The anchor of a part that starts on *new_line* of the new file."""

        if self._fresh:
            return "new"
        try:
            self._prepare()
            return self._anchor(new_line)
        except _Unanchorable:
            return self._unanchored()

    def _unanchored(self) -> str:
        if self._unanchored_digest is None:
            geometry = (f"{h.old_start},{h.new_start},{h.patch or ''}" for h in self._item.hunks)
            self._unanchored_digest = _digest("order-unanchored", self._base_path, self._new_text, *geometry)[:32]
        return f"{UNANCHORED}{self._unanchored_digest}"

    def at_hunk(self, hunk: DiffHunk) -> str:
        """The anchor of a hunk: the place just above its first line."""

        return self.at_line(hunk.new_start if hunk.new_lines > 0 else hunk.new_start + 1)

    def _prepare(self) -> None:
        if self._state == "ready":
            return
        if self._state == "failed":
            raise _Unanchorable
        self._state = "failed"
        old = self._old_text
        if old is None or len(old) > MAX_BLOB_BYTES or not self._item.hunks:
            raise _Unanchorable
        added: list[int] = []
        shift = 0
        last_high = 0
        last_after = 0
        for hunk in self._item.hunks:
            if not hunk.patch:
                raise _Unanchorable
            old_n, new_n = hunk.old_start, hunk.new_start
            for raw in patch_rows(hunk.patch):
                if raw.startswith("\\"):
                    continue
                prefix = raw[:1]
                if prefix == " " or not raw:
                    self._context[new_n] = old_n
                    old_n += 1
                    new_n += 1
                elif prefix == "+":
                    added.append(new_n)
                    new_n += 1
                elif prefix == "-":
                    old_n += 1
                else:
                    raise _Unanchorable
            new_count, old_count = new_n - hunk.new_start, old_n - hunk.old_start
            after = hunk.new_start + new_count if new_count else hunk.new_start + 1
            if new_count:
                if hunk.new_start <= last_high or hunk.new_start < last_after - 1:
                    raise _Unanchorable
                last_high = new_n - 1
                self._span_low.append(hunk.new_start)
                self._span_high.append(last_high)
            if after < last_after:
                raise _Unanchorable
            last_after = after
            shift += old_count - new_count
            self._after.append(after)
            self._shift.append(shift)
        for number in added:
            if self._add_high and number == self._add_high[-1] + 1:
                self._add_high[-1] = number
            else:
                self._add_low.append(number)
                self._add_high.append(number)
        self._rows = old.split("\n")
        self._new_rows = self._new_text.splitlines()
        for index in range(len(self._rows) - 1):
            self._runs.setdefault((self._rows[index], self._rows[index + 1]), []).append(index)
        self._state = "ready"

    def _anchor(self, new_line: int) -> str:
        line = new_line - 1
        slot = bisect.bisect_right(self._add_low, line) - 1
        if slot >= 0 and line <= self._add_high[slot]:
            line = self._add_low[slot] - 1  # inside a run of added lines: the line above the run is the survivor
        if line < 1:
            return "top"
        base = self._base_line(line)
        if not 1 <= base <= len(self._rows):
            raise _Unanchorable
        if line <= len(self._new_rows) and self._new_rows[line - 1].rstrip("\r") != self._rows[base - 1].rstrip("\r"):
            raise _Unanchorable  # the patch's geometry and the two files disagree: place nothing
        first = max(0, base - 2)
        run = tuple(self._rows[first:base])
        occurrence = bisect.bisect_left(self._runs.get(run, ()), first) if len(run) == 2 else 0
        return _digest("order-anchor", self._base_path, str(occurrence), *run)[:32]

    def _base_line(self, line: int) -> int:
        """The base line number of surviving new-file line *line* (a context row, or a line outside every hunk)."""

        slot = bisect.bisect_right(self._span_low, line) - 1
        if slot >= 0 and line <= self._span_high[slot]:
            base = self._context.get(line)
            if base is None:
                raise _Unanchorable
            return base
        before = bisect.bisect_right(self._after, line) - 1
        return line + (self._shift[before] if before >= 0 else 0)


def raw_hunk_mappings(hunks: Sequence[DiffHunk]) -> list[dict[str, Any]]:
    """The ``patch``, ``old_start`` and ``new_start`` of each hunk, the shape the packet stores them in."""

    return [{"patch": hunk.patch, "old_start": hunk.old_start, "new_start": hunk.new_start} for hunk in hunks]


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


def derive_units(
    packet: ReviewPacket,
    new_blobs: Mapping[str, str],
    *,
    previous_packet: ReviewPacket | None = None,
    previous_units: Sequence[ReviewUnit] = (),
    previous_blobs: Mapping[str, str] | None = None,
    old_blobs: Mapping[str, str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[ReviewUnit, ...]:
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

    previous_files = {item.path: item for item in previous_packet.files} if previous_packet is not None else {}
    previous_symbols: dict[str, list[ChangedSymbol]] = {}
    if previous_packet is not None:
        for symbol in previous_packet.symbols:
            previous_symbols.setdefault(symbol.file_path, []).append(symbol)
    prior_files: dict[str, ReviewUnit] = {}
    prior_symbols: dict[str, list[ReviewUnit]] = {}
    for unit in previous_units:
        if unit.kind == "file":
            prior_files[unit.path] = unit
        elif unit.kind in {"symbol", "document_section"}:
            prior_symbols.setdefault(unit.path, []).append(unit)

    units: list[ReviewUnit] = []
    total = len(packet.files)
    last_report = time.monotonic()
    if progress is not None:
        progress(0, total)
    for completed, item in enumerate(packet.files):
        # Count fully derived files, including reused projections, at most four
        # times per second so worker feedback cannot flood the event loop.
        if progress is not None and time.monotonic() - last_report >= 0.25:
            progress(completed, total)
            last_report = time.monotonic()
        entry = order_by_path.get(item.path)
        rank = entry.rank if entry is not None else 0
        group: FileCategory = entry.group if entry is not None else item.category
        reasons = entry.reasons if entry is not None else ()

        file_unit = _file_unit(item, new_blobs, rank=rank, group=group, reasons=reasons)
        units.append(file_unit)
        new_text = new_blobs.get(item.path, "")
        old_text = None if old_blobs is None else old_blobs.get(item.path)
        # An ordered file's parts are anchored to the base file, so a move across lines it never touched reopens them.
        anchors = (
            _PartAnchors(item, new_text, old_text)
            if not item.is_binary and item.status != "deleted" and orders_parts(item.path, new_text)
            else None
        )
        units.extend(_hunk_units(item, rank=rank, group=group, reasons=reasons, anchors=anchors))
        previous_file = prior_files.get(item.path)
        # Exact frozen bytes, hunks and symbol inputs prove the same projection.
        # File fingerprints normalize whitespace, so they alone cannot prove
        # identical geometry. Ranking is joined anew.
        # Unknown fingerprints never authorize reuse.
        if (
            previous_file is not None
            and previous_file.fingerprint_method == file_unit.fingerprint_method == "blob_sha256"
            and previous_file.content_fingerprint == file_unit.content_fingerprint
            and previous_file.reasons == reasons
            and previous_blobs is not None
            and item.path in previous_blobs
            and previous_blobs[item.path] == new_blobs.get(item.path)
            and previous_files.get(item.path) == item
            and previous_symbols.get(item.path, []) == symbols_by_path.get(item.path, [])
            # A part recorded before anchors existed is derived again, so its file's parts all carry one.
            and (
                anchors is None
                or all(
                    split_anchor(prior.content_fingerprint)[1]
                    for prior in prior_symbols.get(item.path, [])
                    if prior.fingerprint_method != _UNKNOWN
                )
            )
        ):
            units.extend(
                replace(unit, revision_id="", attention_rank=rank, attention_group=group)
                for unit in prior_symbols.get(item.path, [])
            )
            continue
        units.extend(
            _symbol_units(
                item,
                new_blobs.get(item.path, ""),
                symbols_by_path.get(item.path, []),
                old_text=old_text,
                anchors=anchors,
                rank=rank,
                group=group,
                reasons=reasons,
            )
        )
    if progress is not None:
        progress(total, total)
    return tuple(units)


def _special_change_entry(item: ChangedFile) -> Mapping[str, Any] | None:
    """The packet-entry shape ``special_changes`` reads, built only for a file that can be special."""

    modes = {item.old_mode, item.new_mode}
    contentless = not item.hunks and not item.is_binary and item.status not in ("added", "deleted")
    if item.submodule_pointer is None and not (modes & {SYMLINK_MODE, GITLINK_MODE}) and not contentless:
        return None
    return {
        "path": item.path,
        "status": item.status,
        "old_path": item.old_path,
        "similarity": item.similarity,
        "is_binary": item.is_binary,
        "diff_omitted_reason": item.diff_omitted_reason,
        "old_mode": item.old_mode,
        "new_mode": item.new_mode,
        "submodule_pointer": item.submodule_pointer,
        "hunks": [{"patch": hunk.patch} for hunk in item.hunks],
    }


def stamp_answers(
    units: Sequence[ReviewUnit],
    packet: ReviewPacket,
    *,
    marked_generated: Collection[str] = (),
) -> tuple[ReviewUnit, ...]:
    """Stamp every unit with its structural answer category (and, if accepted by rule, the rule).

    Pure over the captured change: the same units, packet and ``marked_generated`` give the same
    stamps. *marked_generated* is the set of paths the trusted base marks ``linguist-generated``
    (``review_roles.marked_generated_paths``); a caller that cannot resolve it passes nothing, and
    a generated file then stays a quick check rather than being accepted without evidence.
    """

    marked = frozenset(marked_generated)
    special: dict[str, tuple[str, bool]] = {}
    for item in packet.files:
        entry = _special_change_entry(item)
        change = special_git_change(entry) if entry is not None else None
        special[item.path] = (str(change.get("kind") or "") if change else "", git_change_needs_person(change))
    out: list[ReviewUnit] = []
    for unit in units:
        git_kind, needs_person = special.get(unit.path, ("", False))
        answer = structural_answer(
            StructuralFacts(
                path=unit.path,
                kind=unit.kind,
                fingerprint_known=unit.fingerprint_method != "unknown",
                marked_generated=unit.path in marked,
                git_change=git_kind,
                git_change_needs_person=needs_person,
                reasons=unit.reasons,
            )
        )
        out.append(replace(unit, answer_category=answer.category, category_rule=answer.rule))
    return tuple(out)


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
    anchors: _PartAnchors | None = None,
) -> list[ReviewUnit]:
    """One unit per hunk, keyed on its ordinal within the file."""

    out: list[ReviewUnit] = []
    for ordinal, hunk in enumerate(item.hunks):
        method: FingerprintMethod
        if hunk.patch:
            fingerprint = hunk_fingerprint(item.path, hunk.patch)
            if anchors is not None:
                fingerprint = with_anchor(fingerprint, anchors.at_hunk(hunk))
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
    old_text: str | None = None,
    anchors: _PartAnchors | None = None,
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
        source_lines = new_text.splitlines()
        owned_old: _OwnedOldText | None = None

        def deleted_beside(start: int, end: int, *, group: bool = False) -> str:
            nonlocal owned_old
            if owned_old is None:
                owned_old = _OwnedOldText(raw_hunk_mappings(item.hunks), item.old_path or item.path, old_text)
            return owned_old.within(start, end, group=group)

        def placed(fingerprint: str, start_line: int, *, known: bool = True) -> str:
            """*fingerprint* with where the part sits in the base file, in an order-sensitive file."""

            return (
                with_anchor(fingerprint, anchors.at_line(start_line)) if anchors is not None and known else fingerprint
            )

        section_ordinals: dict[str, int] = {}
        for members in _module_data_groups(windows, source_lines):
            first, last = members[0], members[-1]
            name = f"{_MODULE_DATA_LABEL} · {first.name}"
            section_ordinal = section_ordinals.get(name, 0)
            section_ordinals[name] = section_ordinal + 1
            if not any(_touches_hunk(first.start_line, last.end_line, hunk) for hunk in item.hunks):
                continue
            end_line = last.end_line
            while end_line > last.start_line and not source_lines[end_line - 1].strip():
                end_line -= 1
            body = "\n".join(source_lines[first.start_line - 1 : end_line])
            out.append(
                ReviewUnit(
                    revision_id="",
                    unit_key=unit_key("document_section", item.path, name, section_ordinal),
                    kind="document_section",
                    path=item.path,
                    content_fingerprint=placed(
                        symbol_fingerprint(
                            "module_data_group", name, body, deleted_beside(first.start_line, end_line, group=True)
                        ),
                        first.start_line,
                    ),
                    fingerprint_method="symbol_body_sha256",
                    symbol=name,
                    start_line=first.start_line,
                    end_line=end_line,
                    attention_rank=rank,
                    attention_group=group,
                    reasons=reasons,
                )
            )
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
                    content_fingerprint=placed(
                        symbol_fingerprint(
                            window.kind, name, window.body, deleted_beside(window.start_line, window.end_line)
                        ),
                        window.start_line,
                        known=method != _UNKNOWN,
                    ),
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
    "ANCHOR_MOVED",
    "DEFINITION_HEADER",
    "UNANCHORED",
    "ChangeBlock",
    "anchor_reason",
    "bare_fingerprint",
    "change_blocks",
    "deleted_symbol_fingerprint",
    "derive_units",
    "file_fingerprint",
    "hunk_fingerprint",
    "is_unanchored",
    "new_side_line_text",
    "normalize_body",
    "orders_parts",
    "owned_old_count",
    "owned_text_digest",
    "owns_deleted_text",
    "patch_rows",
    "place_reason",
    "qualified_window_names",
    "raw_hunk_mappings",
    "split_anchor",
    "stamp_answers",
    "symbol_body_digest",
    "symbol_fingerprint",
    "symbol_header",
    "tree_fingerprint",
    "unit_key",
    "unknown_file_fingerprint",
    "unknown_hunk_fingerprint",
    "with_anchor",
]
