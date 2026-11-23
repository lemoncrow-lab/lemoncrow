"""Grounded change risk: a removed-safeguard detector and the one level rule.

Both are pure and deterministic. The detector reads only the captured diff
(hunk ``patch`` text); the level rule reads only facts the engine measured.
No model, no score, no percentage.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

RiskLevel = Literal["high", "medium", "low", "not_measured"]

RISK_LABELS: dict[str, str] = {"high": "High", "medium": "Medium", "low": "Low", "not_measured": "Not measured"}
RISK_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2, "not_measured": 3}


def risk_level(
    *,
    removed_safeguard: bool,
    sensitive: bool,
    outside_callers: bool,
    tested_not_run: bool,
    callers_measured: bool,
    checks_ran: bool | None,
) -> RiskLevel:
    """The level for one part (or, with the inputs OR-ed, for the whole change).

    ``checks_ran`` is ``True`` when a check result is recorded on this
    revision, ``False`` when none is, and ``None`` when the check records
    could not be read. First matching row wins:

    ==================================================  ============
    Measured facts                                      Level
    ==================================================  ============
    a removed or narrowed safeguard                     High
    sensitive area (whether or not a check ran)         High
    callers outside the change and no check ran         High
    callers outside the change (a check ran / unknown)  Medium
    tests exercise it but nothing ran them              Medium
    callers or checks not measured                      Not measured
    callers and checks measured, none of the above      Low
    ==================================================  ============

    Each High/Medium row is itself a measured fact, so it stands even when
    another input is unknown. Low needs both key inputs (callers and checks)
    measured: with either unknown and nothing found the answer is Not
    measured, never Low.
    """

    if removed_safeguard or sensitive or (outside_callers and checks_ran is False):
        return "high"
    if outside_callers or tested_not_run:
        return "medium"
    if not callers_measured or checks_ran is None:
        return "not_measured"
    return "low"


# --------------------------------------------------------------------------- #
# Removed safeguards                                                          #
# --------------------------------------------------------------------------- #

_PYTHON_SUFFIXES = (".py", ".pyi")
_SCRIPT_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
READ_LANGUAGES = "Python, JavaScript and TypeScript"
_SNIPPET = 80


def safeguard_language(path: str) -> str:
    """``python`` | ``script`` | ``""`` (a language the detector does not read)."""

    lowered = path.lower()
    if lowered.endswith(_PYTHON_SUFFIXES):
        return "python"
    if lowered.endswith(_SCRIPT_SUFFIXES):
        return "script"
    return ""


@dataclass(frozen=True)
class Safeguard:
    """One line that refuses, checks or leaves early; ``inline`` is an added conditional expression on a value."""

    kind: Literal["raise", "assert", "guard", "inline"]
    statement: str
    #: ``raise``: the exception name (``""`` for a bare re-raise); ``guard``: ``rejects`` or ``returns``.
    head: str = ""
    #: ``guard`` / ``assert``: the normalized condition; ``inline``: the value it tests.
    condition: str = ""
    connector: str = ""
    clauses: tuple[str, ...] = ()
    #: ``raise`` only: it is the body of a guard on the line before.
    guarded: bool = False
    #: ``guard`` only: its body is the next code line, not on the same line.
    body_next: bool = False


def _scan(text: str, start: int = 0) -> Iterable[tuple[int, str, int]]:
    """``(index, char, depth)`` outside string literals, depth counting ``([{``."""

    depth = 0
    quote = ""
    i = start
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        yield i, char, depth
        i += 1


def _top_level(text: str, target: str) -> int:
    """Index of the first *target* char at bracket depth 0 outside strings, ``-1`` when none."""

    for index, char, depth in _scan(text):
        if char == target and depth == 0:
            return index
    return -1


def _closing(text: str, open_index: int) -> int:
    """Index of the bracket closing the one at *open_index*, ``-1`` when unbalanced."""

    for index, char, depth in _scan(text, open_index):
        if char in ")]}" and depth == 0:
            return index
    return -1


def _unwrap(text: str) -> str:
    text = " ".join(text.split())
    while text.startswith("(") and _closing(text, 0) == len(text) - 1:
        text = " ".join(text[1:-1].split())
    return text


def _split(condition: str, language: str) -> tuple[str, tuple[str, ...]]:
    """``(connector, clauses)`` at the top level; ``mixed`` keeps the whole condition as one clause."""

    ors, ands = (" or ", " and ") if language == "python" else ("||", "&&")
    text = _unwrap(condition)
    cuts: list[tuple[int, int, str]] = []
    for index, _char, depth in _scan(text):
        if depth != 0:
            continue
        for op, name in ((ors, "or"), (ands, "and")):
            if text.startswith(op, index):
                cuts.append((index, len(op), name))
    names = {name for _i, _n, name in cuts}
    if not names:
        return "", (text,)
    if len(names) > 1:
        return "mixed", (text,)
    parts: list[str] = []
    last = 0
    for index, size, _name in cuts:
        parts.append(_unwrap(text[last:index]))
        last = index + size
    parts.append(_unwrap(text[last:]))
    return next(iter(names)), tuple(part for part in parts if part)


_PY_RAISE = re.compile(r"raise\b\s*([A-Za-z_][\w.]*)?")
_JS_THROW = re.compile(r"throw\b\s*(?:new\s+)?([A-Za-z_$][\w$.]*)?")
_JS_ASSERT = re.compile(r"(?:assert(?:\.ok)?|invariant)\s*\(")


def _action(body: str, language: str) -> str:
    """``rejects`` / ``returns`` when a guard body refuses or leaves early; ``""`` otherwise."""

    body = body.strip().lstrip("{").strip()
    if re.match(r"raise\b" if language == "python" else r"throw\b", body):
        return "rejects"
    if re.match(r"return\b", body):
        return "returns"
    return ""


def _comment(text: str, language: str) -> bool:
    if language == "python":
        return text.startswith("#")
    return text.startswith(("//", "/*", "*"))


def parse_safeguard(line: str, following: str, language: str) -> Safeguard | None:
    """The safeguard *line* states; *following* is the next code line (a guard's body)."""

    text = line.strip()
    if not text or _comment(text, language):
        return None
    if language == "python":
        match = re.match(r"(?:el)?if\b\s*", text)
        if match:
            rest = text[match.end() :]
            colon = _top_level(rest, ":")
            if colon < 0:
                return None
            inline = rest[colon + 1 :].strip()
            action = _action(inline or following, language)
            if not action:
                return None
            connector, clauses = _split(rest[:colon], language)
            return Safeguard("guard", text, action, _unwrap(rest[:colon]), connector, clauses, body_next=not inline)
        raised = _PY_RAISE.match(text)
        if raised:
            return Safeguard("raise", text, raised.group(1) or "")
        if re.match(r"assert\b", text):
            body = text[len("assert") :]
            comma = _top_level(body, ",")
            condition = body[:comma] if comma >= 0 else body
            connector, clauses = _split(condition, language)
            return Safeguard("assert", text, "", _unwrap(condition), connector, clauses)
        return None
    stripped = re.sub(r"^\}?\s*(?:else\s+)?", "", text)
    match = re.match(r"if\s*\(", stripped)
    if match:
        close = _closing(stripped, match.end() - 1)
        if close < 0:
            return None
        inline = stripped[close + 1 :].strip().lstrip("{").strip()
        action = _action(inline or following, language)
        if not action:
            return None
        condition = stripped[match.end() : close]
        connector, clauses = _split(condition, language)
        return Safeguard("guard", text, action, _unwrap(condition), connector, clauses, body_next=not inline)
    thrown = _JS_THROW.match(text)
    if thrown:
        return Safeguard("raise", text, thrown.group(1) or "")
    asserted = _JS_ASSERT.match(text)
    if asserted:
        close = _closing(text, asserted.end() - 1)
        inner = text[asserted.end() : close] if close > 0 else ""
        comma = _top_level(inner, ",")
        condition = inner[:comma] if comma >= 0 else inner
        if not condition.strip():
            return None
        connector, clauses = _split(condition, language)
        return Safeguard("assert", text, "", _unwrap(condition), connector, clauses)
    return None


@dataclass(frozen=True)
class _Row:
    tag: str
    old: int
    new: int
    text: str


def _rows(hunk: Mapping[str, Any]) -> list[_Row]:
    """Patch body lines with their old/new numbers; a removed line carries the new line it now sits before."""

    old = int(hunk.get("old_start") or 0)
    new = int(hunk.get("new_start") or 0)
    # A pure addition/deletion names the line *before* the change on the empty side.
    if int(hunk.get("old_lines") or 0) == 0:
        old += 1
    if int(hunk.get("new_lines") or 0) == 0:
        new += 1
    rows: list[_Row] = []
    # Split on newlines only: a form feed, U+2028 or a lone carriage return inside a line is that line's text.
    lines = str(hunk.get("patch") or "").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        raw = line[:-1] if line.endswith("\r") else line
        if raw.startswith("\\"):
            continue
        tag, text = (raw[:1] or " "), raw[1:]
        if tag == "-":
            rows.append(_Row("-", old, new, text))
            old += 1
        elif tag == "+":
            rows.append(_Row("+", old, new, text))
            new += 1
        else:
            rows.append(_Row(" ", old, new, text))
            old += 1
            new += 1
    return rows


_GUARD_START = re.compile(r"^(?:\}?\s*else\s+)?(?:el)?if\b")


def _next_code(view: Sequence[_Row], index: int, language: str) -> tuple[int, str]:
    """``(index, text)`` of the first code line after *index* in *view*; ``(-1, "")`` when none."""

    for offset in range(index + 1, len(view)):
        text = view[offset].text.strip()
        if text and not _comment(text, language):
            return offset, text
    return -1, ""


def _side_safeguards(rows: Sequence[_Row], side: str, language: str) -> list[tuple[_Row, Safeguard]]:
    """Safeguards on the changed lines of one side; context lines only supply a guard's body."""

    view = [row for row in rows if row.tag in (" ", side)]
    out: list[tuple[_Row, Safeguard]] = []
    guard_body = -1
    for index, row in enumerate(view):
        if row.tag != side:
            continue
        if side == "+":
            out.extend((row, item) for item in _inline_checks(row.text, language))
        body_at, following = _next_code(view, index, language) if _GUARD_START.match(row.text.strip()) else (-1, "")
        found = parse_safeguard(row.text, following, language)
        if found is None:
            continue
        if found.kind == "raise" and guard_body == index:
            found = Safeguard("raise", found.statement, found.head, guarded=True)
        if found.kind == "guard" and found.body_next:
            # Its body sits on the next code line; a raise there is judged with this guard.
            guard_body = body_at
        out.append((row, found))
    return out


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _SNIPPET else text[: _SNIPPET - 1].rstrip() + "…"


def _short_statement(statement: str) -> str:
    """``raise ValueError(...)``: arguments elided, so the fact names what was removed, not its message."""

    text = " ".join(statement.split()).rstrip(";")
    open_index = text.find("(")
    if open_index > 0 and _closing(text, open_index) == len(text) - 1:
        return text[:open_index] + "(...)"
    return _snippet(text)


_COMPARISON_WORDS = {">": "over", "<": "under", ">=": "of at least", "<=": "of at most", "==": "of", "===": "of"}


def _plain(clause: str) -> str:
    """``a percent over 100`` for a simple comparison; the clause in backticks otherwise."""

    match = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*(>=|<=|===|==|>|<)\s*(-?\d+(?:\.\d+)?)", clause)
    if match:
        return f"a {match.group(1)} {_COMPARISON_WORDS[match.group(2)]} {match.group(3)}"
    match = re.fullmatch(r"(?:not\s+|!)([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\s+is\s+None", clause)
    if match:
        return f"a missing {match.group(1) or match.group(2)}"
    return f"`{_snippet(clause)}`"


def _verb(action: str) -> str:
    return "rejects" if action == "rejects" else "returns early for"


def _compare(old: Safeguard, new: Safeguard) -> str:
    """``same`` | ``narrowed`` | ``edited`` for two safeguards of one kind and action.

    A guard fires on its condition, so dropping an ``or`` clause narrows it; an
    assert requires its condition, so dropping an ``and`` clause narrows it.
    Anything else that changed is an edit the detector does not judge.
    """

    if old.condition == new.condition:
        return "same"
    weakening = "or" if old.kind == "guard" else "and"
    if old.connector not in ("", weakening) or new.connector not in ("", weakening):
        return "edited"
    before, after = set(old.clauses), set(new.clauses)
    if after >= before:
        return "same"
    if after and after < before:
        return "narrowed"
    return "edited"


_ABSENT = re.compile(
    r"(?:not\s+|!)(?P<a>[A-Za-z_$][\w$.]*)|(?P<b>[A-Za-z_$][\w$.]*)\s*(?:is\s+None|={2,3}\s*(?:null|undefined))"
)


def _absent_subject(guard: Safeguard) -> str:
    """The value a one-clause "nothing here" guard tests (``not rows``, ``x is None``, ``!x``); ``""`` otherwise."""

    match = _ABSENT.fullmatch(guard.condition) if not guard.connector else None
    return (match.group("a") or match.group("b")) if match else ""


_PY_INLINE = re.compile(r"\bif\s+(?:not\s+)?([A-Za-z_][\w.]*)(?:\s+is(?:\s+not)?\s+None)?\s+else\b")
_JS_INLINE = re.compile(r"([A-Za-z_$][\w$.]*)\s*(?:\?\?|\?\.|\?\s)")


def _inline_checks(text: str, language: str) -> list[Safeguard]:
    """The values an added line tests in a conditional expression (``a if rows else b``, ``rows?.x``, ``rows ?? y``)."""

    if _comment(text.strip(), language):
        return []
    pattern = _PY_INLINE if language == "python" else _JS_INLINE
    return [Safeguard("inline", text.strip(), condition=name) for name in dict.fromkeys(pattern.findall(text))]


def _changes(removed: Sequence[Safeguard], added: Sequence[Safeguard]) -> list[tuple[Safeguard, str]]:
    """``(removed safeguard, phrase)`` for each one the added side neither keeps nor merely edits."""

    out: list[tuple[Safeguard, str]] = []
    for gone in removed:
        if gone.kind == "raise":
            if gone.guarded:
                continue  # judged with its guard
            if any(item.kind == "raise" for item in added):
                continue  # still raises (perhaps another type or message): an edit
            verb = "throws" if gone.statement.lstrip().startswith("throw") else "raises"
            # ``raise ValueError`` names a type; ``throw reason`` a value, shown as code.
            what = (gone.head if gone.head[:1].isupper() else f"`{gone.head}`") if gone.head else "the error it caught"
            out.append((gone, f"no longer {verb} {what} (removed `{_short_statement(gone.statement)}`)"))
            continue
        subject = _absent_subject(gone) if gone.kind == "guard" and gone.head == "returns" else ""
        if subject and any(item.kind == "inline" and item.condition == subject for item in added):
            continue  # the early return became a conditional expression on the same value
        peers = [item for item in added if item.kind == gone.kind and (gone.kind == "assert" or item.head == gone.head)]
        verdicts = [(_compare(gone, item), item) for item in peers]
        if any(verdict == "same" for verdict, _item in verdicts):
            continue
        narrowed = next((item for verdict, item in verdicts if verdict == "narrowed"), None)
        if narrowed is not None:
            dropped = [clause for clause in gone.clauses if clause not in set(narrowed.clauses)]
            span = f"from `{_snippet(gone.condition)}` to `{_snippet(narrowed.condition)}`"
            if gone.kind == "guard":
                phrase = " or ".join(_plain(clause) for clause in dropped)
                text = f"no longer {_verb(gone.head)} {phrase} (check narrowed {span})"
            else:
                phrase = " and ".join(f"`{_snippet(clause)}`" for clause in dropped)
                text = f"no longer checks that {phrase} (assert narrowed {span})"
            out.append((gone, text))
            continue
        if peers:
            continue  # rewritten, not removed: the detector stays quiet
        if gone.kind == "guard":
            phrase = (
                " or ".join(_plain(clause) for clause in gone.clauses)
                if gone.connector in ("", "or")
                else f"`{_snippet(gone.condition)}`"
            )
            text = f"no longer {_verb(gone.head)} {phrase} (removed `{_snippet(gone.statement)}`)"
        else:
            text = f"no longer checks that `{_snippet(gone.condition)}` (removed `{_snippet(gone.statement)}`)"
        out.append((gone, text))
    return out


def weakened_safeguards(
    hunks: Sequence[Mapping[str, Any]], language: str, name: str, *, lines: tuple[int, int] | None = None
) -> list[str]:
    """Fact texts for the safeguards *hunks* remove or narrow, inside new-side *lines* when given.

    A removed ``raise``/``throw``, ``assert``, or ``if`` whose body raises or
    returns counts only when the added side has no equivalent: the same guard
    with the same or a wider condition, an assert at least as strict, or any
    raise. A guard that loses an ``or`` clause (an assert that loses an
    ``and`` clause) is narrowed; every other rewrite is left alone.
    """

    return [f"{name} {phrase}" for phrase in _within(_file_safeguards(hunks, language), lines)]


_Sides = tuple[list[tuple[int, Safeguard]], list[tuple[int, Safeguard]]]


def _file_safeguards(hunks: Sequence[Mapping[str, Any]], language: str, deleted: Collection[int] = ()) -> _Sides:
    """``(removed, added)`` safeguards of one file, each with its new-side line.

    *deleted* holds the old-side definition lines of deleted symbols: the
    removed run that starts there is that symbol's body, never a neighbour's.
    """

    removed: list[tuple[int, Safeguard]] = []
    added: list[tuple[int, Safeguard]] = []
    for hunk in hunks:
        rows = _rows(hunk)
        gone: set[int] = set()
        inside = False
        for row in rows:
            inside = row.tag == "-" and (inside or row.old in deleted)
            if inside:
                gone.add(row.old)
        removed.extend((row.new, item) for row, item in _side_safeguards(rows, "-", language) if row.old not in gone)
        added.extend((row.new, item) for row, item in _side_safeguards(rows, "+", language))
    return removed, added


def _within(sides: _Sides, lines: tuple[int, int] | None) -> list[str]:
    """Phrases for the part spanning new-side *lines* (the whole file when ``None``).

    An added line belongs to the part it sits in. A removed line has no new
    line of its own; it sits just before its join point, so it belongs to the
    part whose span holds the line before that point: a removal at a part's
    last line joins on the next line, and one right before a part's first line
    is not that part's.
    """

    removed, added = sides
    if lines is None:
        kept = [item for _line, item in removed]
        new = [item for _line, item in added]
    else:
        start, end = lines
        kept = [item for line, item in removed if start < line <= end + 1]
        new = [item for line, item in added if start <= line <= end]
    return [text for _item, text in _changes(kept, new)] if kept else []


def removed_safeguards(packet: Mapping[str, Any] | None, targets: Sequence[Any]) -> dict[str, list[str]]:
    """Per changed symbol target, the safeguards its diff removes or narrows (see :func:`weakened_safeguards`).

    Each entry is a phrase without the part's name (``no longer rejects a
    percent over 100 (check narrowed from ...)``); callers put the name in.

    Added and deleted symbols are skipped: a new part had nothing to weaken,
    and a deleted one is reported as deleted, not as weakened. Test files are
    skipped too: an assert there checks the code under test, it does not guard
    it.
    """

    from .differential import is_test_path

    files = {
        str(item.get("path") or ""): item
        for item in (packet or {}).get("files") or ()
        if isinstance(item, Mapping) and str(item.get("status") or "") == "modified"
    }
    change: dict[tuple[str, str], str] = {}
    deleted: dict[str, set[int]] = {}
    for symbol in (packet or {}).get("symbols") or ():
        if isinstance(symbol, Mapping):
            if symbol.get("change") == "deleted":
                deleted.setdefault(str(symbol.get("file_path") or ""), set()).add(int(symbol.get("start_line") or 0))
            for key in (symbol.get("qualified_name"), symbol.get("symbol_name")):
                if key:
                    change.setdefault((str(symbol.get("file_path") or ""), str(key)), str(symbol.get("change") or ""))
    parsed: dict[str, _Sides] = {}
    out: dict[str, list[str]] = {}
    for target in targets:
        if getattr(target, "kind", "") != "symbol" or not target.symbol:
            continue
        entry = files.get(str(target.path))
        language = safeguard_language(str(target.path))
        if entry is None or not language or is_test_path(str(target.path)):
            continue
        how = change.get((str(target.path), str(target.symbol))) or change.get(
            (str(target.path), str(target.symbol).rsplit(".", 1)[-1])
        )
        if how in ("added", "deleted"):
            continue
        path = str(target.path)
        if path not in parsed:
            hunks = [hunk for hunk in entry.get("hunks") or () if isinstance(hunk, Mapping) and hunk.get("patch")]
            parsed[path] = _file_safeguards(hunks, language, deleted.get(path, set()))
        found = _within(parsed[path], (target.start_line, target.end_line))
        if found:
            out[str(target.target_id)] = found
    return out


def unread_safeguard_files(packet: Mapping[str, Any] | None, targets: Sequence[Any]) -> dict[str, list[str]]:
    """Changed files with symbol parts the detector could not read, keyed by the plain reason."""

    from .differential import is_test_path

    files = {
        str(item.get("path") or ""): item for item in (packet or {}).get("files") or () if isinstance(item, Mapping)
    }
    out: dict[str, list[str]] = {}
    for path in dict.fromkeys(
        str(target.path) for target in targets if getattr(target, "kind", "") == "symbol" and target.symbol
    ):
        entry = files.get(path)
        if entry is None or str(entry.get("status") or "") != "modified" or is_test_path(path):
            continue
        if not safeguard_language(path):
            reason = f"only {READ_LANGUAGES} are read"
        elif not any(isinstance(hunk, Mapping) and hunk.get("patch") for hunk in entry.get("hunks") or ()):
            reason = "diff text unavailable"
        else:
            continue
        out.setdefault(reason, []).append(path)
    return out


__all__ = [
    "READ_LANGUAGES",
    "RISK_LABELS",
    "RISK_ORDER",
    "RiskLevel",
    "Safeguard",
    "parse_safeguard",
    "removed_safeguards",
    "risk_level",
    "safeguard_language",
    "unread_safeguard_files",
    "weakened_safeguards",
]
