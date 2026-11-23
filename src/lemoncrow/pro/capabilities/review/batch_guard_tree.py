"""The Quick check batch guard read from the whole file on both sides with a real parser.

The hunk reading in :mod:`batch_guard` sees a window of a file and guesses what lies outside it (a comment, a
template, a string, a header above the first row). This module reads the whole file on each side with tree-sitter,
so blocks, comments, strings and templates are the grammar's, not a guess. Its holds are added to the hunk
reading's: the guard never holds less for having it.

What it reads. A *statement* is a statement node of the grammar (an ``if``, a ``for``, an expression statement, a
declaration, an ``else`` clause, a ``case``). Its *key* is its own leaf tokens (the tokens of statements nested in
it belong to those), comments dropped, a trailing ``;`` dropped and a ``,`` before a closing bracket dropped, so
formatting does not change it. Its *chain* is the statements and functions around it, each as ``(kind, the row it
starts on, its depth)``: never the header text, and the row is the same row on both sides when the line is
unchanged or is one a row-by-row edit replaced in place.

What it holds. (1) A statement the part removed and one it added, with equal keys and different chains: it moved
across a block (``loop_bound`` if either chain holds a loop, ``block_moved`` otherwise). (2) The same for two that
are equal once names, numbers and strings are abstracted: it moved and was edited on the way. (3) A statement the
change leaves alone whose chain differs between the sides when a block that starts or ends on one of the part's own
rows accounts for the difference: a header or brace added or removed around it.

What it refuses (``TreeClosed``, the part is held as "could not be checked"): the text of either side is not
available, a file is longer than ``MAX_FILE_CHARS``, a grammar is missing, or a parse error overlaps one of the
part's rows or sits inside a block of a chain it reads.
"""

from __future__ import annotations

import bisect
import functools
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lemoncrow.pro.capabilities.review.change_risk import _rows

# Each side is read whole. Past this the file is not read at all and the part is held. 2,000,000 characters is about
# 50,000 lines and about a second of parsing a side: a hand-written file is far below it, a bundle or a generated
# file far above. (This repository's largest source file, review/api.py, is 1.0 million.)
MAX_FILE_CHARS = 2_000_000

_LOOPS = frozenset({"for_statement", "for_in_statement", "while_statement", "do_statement"})
_KINDS = {
    "for_statement": "loop", "for_in_statement": "loop", "while_statement": "loop", "do_statement": "loop",
    "if_statement": "if", "elif_clause": "elif", "else_clause": "else", "try_statement": "try",
    "except_clause": "except", "except_group_clause": "except", "catch_clause": "catch", "finally_clause": "finally",
    "with_statement": "with", "switch_statement": "switch", "switch_case": "case", "switch_default": "case",
    "case_clause": "case", "match_statement": "match",
    "function_definition": "function", "function_declaration": "function", "function_expression": "function",
    "function": "function", "generator_function": "function", "generator_function_declaration": "function",
    "method_definition": "function", "arrow_function": "arrow",
    "class_definition": "class", "class_declaration": "class", "class": "class", "abstract_class_declaration": "class",
}  # fmt: skip
_FUNCTION_LIKE = frozenset(
    {"arrow_function", "function_expression", "function", "generator_function", "lambda", "class"}
)
_EXTRA_STATEMENTS = {
    "python": frozenset(
        {
            "function_definition", "class_definition", "decorated_definition", "decorator", "elif_clause",
            "else_clause", "except_clause", "except_group_clause", "finally_clause", "case_clause",
        }
    ),
    "script": frozenset(
        {
            "else_clause", "catch_clause", "finally_clause", "switch_case", "switch_default", "method_definition",
            "public_field_definition", "field_definition", "class_static_block", "decorator",
        }
    ),
}  # fmt: skip
_TRANSPARENT = frozenset({"export_statement", "decorated_definition"})  # they wrap a declaration: not a block
_IMPORTS = frozenset({"import_statement", "import_from_statement", "future_import_statement", "export_statement"})
_DECLARATIONS = frozenset(
    {"type_alias_declaration", "interface_declaration", "enum_declaration", "ambient_declaration"}
)
_STRING_PIECES = frozenset(
    {
        "string_fragment",
        "string_content",
        "template_chars",
        "escape_sequence",
        "regex_pattern",
        "regex_flags",
        "jsx_text",
    }
)
_BARE_BLOCK_PARENTS = frozenset({"program", "statement_block", "switch_case", "switch_default", "labeled_statement"})
_INTERPOLATIONS = frozenset({"interpolation", "template_substitution"})
_ABSTRACTED = frozenset({"ID", "STR", "NUM"})
_PARAMETERS = frozenset({"required_parameter", "optional_parameter"})


# One guard call is one Snapshot: all the files its parts sit in together may not cost more than this to parse. Past it
# the parts that still need a file are held. 8,000,000 characters is about four seconds of parsing.
MAX_SNAPSHOT_CHARS = 8_000_000
MAX_SNAPSHOT_SECONDS = 8.0


class Budget:
    """What one guard call may still spend on reading files whole (characters parsed and wall-clock time)."""

    def __init__(self) -> None:
        self.chars = MAX_SNAPSHOT_CHARS
        self.deadline = time.monotonic() + MAX_SNAPSHOT_SECONDS

    def spend(self, chars: int) -> None:
        if chars > self.chars:
            raise TreeClosed("the files of this change are too large to read whole together")
        if time.monotonic() > self.deadline:
            raise TreeClosed("reading the files of this change whole took too long")
        self.chars -= chars


class TreeClosed(Exception):
    """The whole-file reading cannot decide; the reason is the card's."""


@functools.lru_cache(maxsize=8)
def _parser(language: str) -> Any:
    from tree_sitter_language_pack import get_parser

    return get_parser(language)


def grammar_for(path: str) -> tuple[str, str]:
    """``(statement family, grammar)`` for a path, or raise ``TreeClosed``."""

    lowered = path.lower()
    if lowered.endswith((".py", ".pyi")):
        return "python", "python"
    if lowered.endswith(".tsx"):
        return "script", "tsx"
    if lowered.endswith((".ts", ".mts", ".cts")):
        return "script", "typescript"
    if lowered.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "script", "javascript"
    raise TreeClosed("this file type has no grammar here")


@dataclass(frozen=True)
class _Elem:
    kind: str
    start: int
    end: int
    depth: int
    slot: int | str = 0  # for a decorated member or parameter: its name (so a shifted list changes nothing)


@dataclass
class _Unit:
    key: tuple[str, ...]
    shape: tuple[str, ...]
    lines: frozenset[int]
    first: int
    chain: tuple[_Elem, ...]
    imported: bool
    declaration: bool
    decorator: bool = False


@dataclass
class _Builder:
    chain: tuple[_Elem, ...]
    node_type: str
    tokens: list[tuple[str, str]] = field(default_factory=list)  # (text, shape)
    lines: set[int] = field(default_factory=set)
    first: int = 0


@dataclass
class _Side:
    units: list[_Unit]
    errors: list[tuple[int, int]]


def _is_statement(node: Any, parent_type: str, family: str) -> bool:
    node_type = node.type
    if family == "python" and node.is_named and parent_type in ("block", "module"):
        return True  # a bare expression is the node itself here, not an expression_statement
    return node_type.endswith(("_statement", "_declaration")) or node_type in _EXTRA_STATEMENTS[family]


def _shape_of(node: Any, text: str) -> str:
    kind = node.type
    if not node.is_named:
        return text
    if kind in _STRING_PIECES:
        return "STR"
    if "identifier" in kind:
        return "ID"
    if kind in ("number", "integer", "float", "decimal_integer_literal"):
        return "NUM"
    return text


def _unquote(content: str) -> str:
    return content.replace("\\'", "'").replace('\\"', '"')


def _string_token(node: Any, source: bytes, family: str, parent_type: str = "") -> str:
    """A string or template without code in it as one token that does not depend on its quote style.

    Not for what quotes do not decide: a Python raw string keeps its backslashes, and a tagged template
    (``tag`a```, ``String.raw`a```) hands its text to the tag, so those keep their text as written.
    """

    raw = source[node.start_byte : node.end_byte].decode("utf-8", "replace")
    if node.type == "template_string":
        if parent_type == "call_expression" or len(raw) < 2 or not (raw.startswith("`") and raw.endswith("`")):
            return raw
        return '"' + _unquote(raw[1:-1]) + '"'
    if family == "python":
        prefix = raw[: len(raw) - len(raw.lstrip("rRbBuUfF"))]
        rest = raw[len(prefix) :]
        quote = rest[:3] if rest[:3] in ('"""', "'''") else rest[:1]
        if not quote or len(rest) < 2 * len(quote) or not rest.endswith(quote):
            return raw
        content = rest[len(quote) : len(rest) - len(quote)]
        return prefix.lower() + '"' + (content if "r" in prefix.lower() else _unquote(content)) + '"'
    if len(raw) < 2:
        return raw
    return '"' + _unquote(raw[1:-1]) + '"'


def _name_of(node: Any, source: bytes, *fields: str) -> str:
    """The text of the first named field the node has (a parameter's pattern, a member's name), or ""."""

    for field_name in fields:
        child = node.child_by_field_name(field_name)
        if child is not None:
            return source[child.start_byte : child.end_byte].decode("utf-8", "replace")
    return ""


def _delimiter(kind: str, parent_type: str, piece: str) -> str | None:
    """A string's opening or closing quote as its lowercased prefix and one canonical quote, or None."""

    if kind in ("string_start", "string_end"):
        return piece.rstrip("\"'").lower() + '"'
    if kind == "`" and parent_type == "template_string":
        return '"'
    return None


def _parse_side(text: str, grammar: str, family: str) -> _Side:
    source = text.encode("utf-8", "surrogateescape")
    tree = _parser(grammar).parse(source)
    root = tree.root_node
    starts = [0] + [index + 1 for index, byte in enumerate(source) if byte == 10]
    builders: list[_Builder] = []
    errors: list[tuple[int, int]] = []
    stack: list[tuple[Any, tuple[_Elem, ...], _Builder | None, str, int]] = [(root, (), None, "", 0)]
    while stack:
        node, chain, owner, parent_type, slot = stack.pop()
        kind = node.type
        if kind == "comment":
            continue
        start_line = bisect.bisect_right(starts, node.start_byte)  # not node.start_point: it crashes the binding
        end_line = bisect.bisect_right(starts, max(node.start_byte, node.end_byte - 1))
        if kind == "ERROR" or node.is_missing:
            errors.append((start_line, max(start_line, end_line)))
        below = chain
        if node is not root and _is_statement(node, parent_type, family):
            builder = _Builder(chain, kind, first=start_line)
            builders.append(builder)
            owner = builder
            if kind not in _TRANSPARENT:
                below = (*chain, _Elem(_KINDS.get(kind, kind), start_line, end_line, len(chain)))
        elif kind in _FUNCTION_LIKE:
            below = (*chain, _Elem(_KINDS.get(kind, kind), start_line, end_line, len(chain)))
        elif kind == "statement_block" and parent_type in _BARE_BLOCK_PARENTS:
            below = (*chain, _Elem("block", start_line, end_line, len(chain)))  # a block of its own scope
        if node.type in ("string", "template_string") and not any(c.type in _INTERPOLATIONS for c in node.children):
            piece = _string_token(node, source, family, parent_type)
            if owner is not None:
                owner.tokens.append((piece, "STR"))
                owner.lines.update(range(start_line, max(start_line, end_line) + 1))
                if not owner.first:
                    owner.first = start_line
            continue
        if node.child_count == 0:
            piece = source[node.start_byte : node.end_byte].decode("utf-8", "replace")
            quote = _delimiter(kind, parent_type, piece)
            if piece.strip() and owner is not None:
                owner.tokens.append((quote, "Q") if quote is not None else (piece, _shape_of(node, piece)))
                owner.lines.update(range(start_line, max(start_line, end_line) + 1))
                if not owner.first:
                    owner.first = start_line
        else:
            children = node.children
            chains = [below] * len(children)
            if kind == "decorated_definition":
                # a decorator belongs to the definition it decorates: moving it to another one is a move
                definition = next((c for c in reversed(children) if c.type.endswith("_definition")), None)
                if definition is not None:
                    first = bisect.bisect_right(starts, definition.start_byte)
                    last = bisect.bisect_right(starts, max(definition.start_byte, definition.end_byte - 1))
                    elem = _Elem("decorated", first, last, len(below))
                    chains = [(*below, elem) if c.type == "decorator" else below for c in children]
            elif kind == "class_body":
                # a member's decorators are siblings before it: each belongs to the next member
                waiting: list[int] = []
                for index, child in enumerate(children):
                    if child.type == "decorator":
                        waiting.append(index)
                    elif child.is_named and waiting:
                        first = bisect.bisect_right(starts, child.start_byte)
                        last = bisect.bisect_right(starts, max(child.start_byte, child.end_byte - 1))
                        elem = _Elem("decorated", first, last, len(below), _name_of(child, source, "name") or index)
                        for position in waiting:
                            chains[position] = (*below, elem)
                        waiting = []
            elif kind in _PARAMETERS:
                # a parameter's decorators are inside it: the decorated parameter is the chain element
                elem = _Elem(
                    "decorated", start_line, end_line, len(below), _name_of(node, source, "pattern", "name") or slot
                )
                chains = [(*below, elem) if c.type == "decorator" else below for c in children]
            for index in range(len(children) - 1, -1, -1):
                stack.append((children[index], chains[index], owner, kind, index))
    return _Side([_finish(builder) for builder in builders if builder.tokens], errors)


def _finish(builder: _Builder) -> _Unit:
    tokens = list(builder.tokens)
    kept = [
        item
        for index, item in enumerate(tokens)
        if not (item[0] == "," and index + 1 < len(tokens) and tokens[index + 1][0] in (")", "]", "}"))
    ]
    while kept and kept[-1][0] == ";":
        kept.pop()
    return _Unit(
        tuple(text for text, _ in kept),
        tuple(shape for _, shape in kept),
        frozenset(builder.lines),
        min(builder.lines),
        builder.chain,
        builder.node_type in _IMPORTS,
        builder.node_type in _DECLARATIONS,
        builder.node_type == "decorator",
    )


class _Canon:
    """Which row of the file each changed row stands for, so a block's start row means the same on both sides.

    An unchanged line is ``("u", its old line)`` on both sides. In a hunk, a run of removed rows followed by as
    many added rows is a row-by-row edit: the pair stands for one row. Any other changed row is its own.
    """

    def __init__(self, hunks: Sequence[Mapping[str, Any]]) -> None:
        self.old: dict[int, tuple[Any, ...]] = {}
        self.new: dict[int, tuple[Any, ...]] = {}
        self._ends: list[tuple[int, int]] = []  # (new line after a hunk, old line after it)
        for hunk in hunks:
            old = int(hunk.get("old_start") or 0) + (1 if int(hunk.get("old_lines") or 0) == 0 else 0)
            new = int(hunk.get("new_start") or 0) + (1 if int(hunk.get("new_lines") or 0) == 0 else 0)
            if hunk.get("patch"):
                rows = _rows(hunk)
                self._read_rows(rows)
                last = rows[-1] if rows else None
                end_old = (last.old + (1 if last.tag != "+" else 0)) if last else old
                end_new = (last.new + (1 if last.tag != "-" else 0)) if last else new
            else:  # the hunk's text was not kept: every row in it is its own
                end_old = old + int(hunk.get("old_lines") or 0)
                end_new = new + int(hunk.get("new_lines") or 0)
                self.old.update({n: ("o", n) for n in range(old, end_old)})
                self.new.update({n: ("n", n) for n in range(new, end_new)})
            self._ends.append((end_new, end_old))
        # The shift of a line is that of the last hunk (in list order) whose end is at or before it. Sorted by end, it
        # is the highest index among the first ``k`` ends: found by bisection, not by walking every hunk for a line.
        by_end = sorted(range(len(self._ends)), key=lambda index: self._ends[index][0])
        self._end_new = [self._ends[index][0] for index in by_end]
        self._last: list[int] = []
        latest = -1
        for index in by_end:
            latest = max(latest, index)
            self._last.append(latest)

    def _read_rows(self, rows: Sequence[Any]) -> None:
        index = 0
        while index < len(rows):
            row = rows[index]
            if row.tag == " ":
                self.old[row.old] = self.new[row.new] = ("u", row.old)
                index += 1
                continue
            removed: list[Any] = []
            added: list[Any] = []
            while index < len(rows) and rows[index].tag == "-":
                removed.append(rows[index])
                index += 1
            while index < len(rows) and rows[index].tag == "+":
                added.append(rows[index])
                index += 1
            paired = len(removed) == len(added)
            for number, item in enumerate(removed):
                self.old[item.old] = ("p", removed[number].old) if paired else ("o", item.old)
            for number, item in enumerate(added):
                self.new[item.new] = ("p", removed[number].old) if paired else ("n", item.new)

    def of(self, side: str, line: int) -> tuple[Any, ...]:
        table = self.old if side == "old" else self.new
        found = table.get(line)
        if found is not None:
            return found
        if side == "old":
            return ("u", line)
        count = bisect.bisect_right(self._end_new, line)
        if not count:
            return ("u", line)
        end_new, end_old = self._ends[self._last[count - 1]]
        return ("u", line - (end_new - end_old))


@dataclass
class TreeFile:
    """Both sides of a file, and what ``verdict`` reads of it for every part, built once so a part reads only its own.

    ``old_at`` and ``new_at``: a line -> the units (by position) that have it. ``shifted``: the unchanged statement
    pairs whose chains differ, with whether a loop is among the difference, and ``old_marks`` / ``new_marks`` the lines
    (a block's first and last row) that would make a part's rows explain that pair. ``decorators``: the order of the
    decorators on each decorated definition, per side.
    """

    old: _Side
    new: _Side
    canon: _Canon
    untouched: list[tuple[_Unit, _Unit]]
    old_at: dict[int, list[int]] = field(init=False, repr=False)
    new_at: dict[int, list[int]] = field(init=False, repr=False)
    shifted: list[bool] = field(init=False, repr=False)
    old_marks: dict[int, list[int]] = field(init=False, repr=False)
    new_marks: dict[int, list[int]] = field(init=False, repr=False)
    old_errors: list[tuple[int, int]] = field(init=False, repr=False)
    new_errors: list[tuple[int, int]] = field(init=False, repr=False)
    decorators: tuple[dict[Any, list[tuple[str, ...]]], dict[Any, list[tuple[str, ...]]]] = field(
        init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.old_at, self.new_at = _lines_at(self.old.units), _lines_at(self.new.units)
        self.old_errors, self.new_errors = _merged(self.old.errors), _merged(self.new.errors)
        self.shifted, self.old_marks, self.new_marks = [], {}, {}
        for before, after in self.untouched:
            ident_before, ident_after = _ident(self.canon, "old", before), _ident(self.canon, "new", after)
            if ident_before == ident_after:
                continue
            only_before, only_after, loop = _difference(ident_before, ident_after, before.chain, after.chain)
            for elem in only_before:
                self.old_marks.setdefault(elem.start, []).append(len(self.shifted))
                self.old_marks.setdefault(elem.end, []).append(len(self.shifted))
            for elem in only_after:
                self.new_marks.setdefault(elem.start, []).append(len(self.shifted))
                self.new_marks.setdefault(elem.end, []).append(len(self.shifted))
            self.shifted.append(loop)
        self.decorators = (
            _decorator_order(self.canon, "old", self.old.units),
            _decorator_order(self.canon, "new", self.new.units),
        )


def _merged(errors: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """The parse-error ranges as disjoint sorted ranges covering the same lines, so a range is tested by bisection."""

    merged: list[tuple[int, int]] = []
    for first, stop in sorted(errors):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((first, stop))
    return merged


def _error_between(merged: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    """Whether a range of ``_merged`` ranges overlaps lines ``start`` to ``end``: the one reaching back from ``end`` does."""

    index = bisect.bisect_right(merged, (end, float("inf"))) - 1
    return index >= 0 and merged[index][1] >= start


def _lines_at(units: Sequence[_Unit]) -> dict[int, list[int]]:
    at: dict[int, list[int]] = {}
    for position, unit in enumerate(units):
        for line in unit.lines:
            at.setdefault(line, []).append(position)
    return at


def _units_with(units: Sequence[_Unit], at: dict[int, list[int]], own: set[int]) -> list[_Unit]:
    """The units (in order) that have one of the lines ``own``: those whose lines are not disjoint from it."""

    found: set[int] = set()
    for line in own:
        found.update(at.get(line, ()))
    return [units[position] for position in sorted(found)]


def _side_text(read: Callable[[str, str], str | None], path: str, side: str, absent: bool) -> str:
    if absent:
        return ""
    try:
        text = read(path, side)
    except Exception:
        text = None
    if text is None:
        raise TreeClosed("the text of the file on one side was not available")
    if len(text) > MAX_FILE_CHARS:
        raise TreeClosed(f"the file is longer than {MAX_FILE_CHARS:,} characters, so it is not read as a whole")
    return text


def _check_against_diff(old_text: str, new_text: str, hunks: Sequence[Mapping[str, Any]]) -> None:
    """The texts must be the two sides the hunks were cut from: every context and removed row is the old text at
    its line, every context and added row the new text at its line."""

    old_lines = [line.rstrip("\r") for line in old_text.split("\n")]
    new_lines = [line.rstrip("\r") for line in new_text.split("\n")]
    mismatch = TreeClosed("the file text does not match the diff")
    for hunk in hunks:
        if not hunk.get("patch"):
            old_end = int(hunk.get("old_start") or 0) + int(hunk.get("old_lines") or 0) - 1
            new_end = int(hunk.get("new_start") or 0) + int(hunk.get("new_lines") or 0) - 1
            if (int(hunk.get("old_lines") or 0) and old_end > len(old_lines)) or (
                int(hunk.get("new_lines") or 0) and new_end > len(new_lines)
            ):
                raise mismatch
            continue
        for row in _rows(hunk):
            if row.tag in (" ", "-") and (
                row.old < 1 or row.old > len(old_lines) or old_lines[row.old - 1] != row.text.rstrip("\r")
            ):
                raise mismatch
            if row.tag in (" ", "+") and (
                row.new < 1 or row.new > len(new_lines) or new_lines[row.new - 1] != row.text.rstrip("\r")
            ):
                raise mismatch


def read_file(
    entry: Mapping[str, Any], path: str, read: Callable[[str, str], str | None], budget: Budget | None = None
) -> TreeFile:
    status = str(entry.get("status") or "")
    family, grammar = grammar_for(path)
    old_path = str(entry.get("old_path") or path)
    old_text = _side_text(read, old_path, "old", status == "added")
    new_text = _side_text(read, path, "new", status == "deleted")
    if budget is not None:
        budget.spend(len(old_text) + len(new_text))
    _check_against_diff(old_text, new_text, [h for h in entry.get("hunks") or () if isinstance(h, Mapping)])
    try:
        old, new = _parse_side(old_text, grammar, family), _parse_side(new_text, grammar, family)
    except Exception as exc:  # a missing grammar, a parser that refuses
        raise TreeClosed("the file could not be parsed") from exc
    hunks = [hunk for hunk in entry.get("hunks") or () if isinstance(hunk, Mapping)]
    canon = _Canon(hunks)
    return TreeFile(old, new, canon, _untouched(old, new, canon))


def _untouched(old: _Side, new: _Side, canon: _Canon) -> list[tuple[_Unit, _Unit]]:
    """Statements no changed line touches, old with their new twin (the same place, the same key)."""

    def clean(side: str, unit: _Unit) -> bool:
        return all(canon.of(side, line)[0] == "u" for line in unit.lines)

    twins: dict[tuple[Any, ...], list[_Unit]] = {}
    for unit in new.units:
        if clean("new", unit):
            twins.setdefault((canon.of("new", unit.first), unit.key), []).append(unit)
    pairs = []
    for unit in old.units:
        if clean("old", unit):
            candidates = twins.get((canon.of("old", unit.first), unit.key))
            if candidates:
                pairs.append((unit, candidates.pop(0)))
    return pairs


def _ident(canon: _Canon, side: str, unit: _Unit) -> tuple[tuple[Any, ...], ...]:
    return tuple((e.kind, canon.of(side, e.start), e.depth, e.slot) for e in unit.chain)


def _difference(
    before: Sequence[Any], after: Sequence[Any], elems_before: Sequence[_Elem], elems_after: Sequence[_Elem]
) -> tuple[list[_Elem], list[_Elem], bool]:
    """The chain elements only one side has, and whether one of them is a loop."""

    only_before = [e for i, e in enumerate(elems_before) if before[i] not in after]
    only_after = [e for i, e in enumerate(elems_after) if after[i] not in before]
    loop = any(e.kind == "loop" for e in (*only_before, *only_after))
    return only_before, only_after, loop


def verdict(tf: TreeFile, own_old: set[int], own_new: set[int]) -> str:
    """``loop_bound`` | ``block_moved`` | ``""`` for one part's rows; ``TreeClosed`` when the parse cannot be trusted."""

    gone = _units_with(tf.old.units, tf.old_at, own_old)
    came = _units_with(tf.new.units, tf.new_at, own_new)
    for errors, own, units in ((tf.old_errors, own_old, gone), (tf.new_errors, own_new, came)):
        if not errors:
            continue
        if any(_error_between(errors, line, line) for line in own):
            raise TreeClosed("the file does not parse where the change is")
        for unit in units:
            for elem in unit.chain:
                if _error_between(errors, elem.start, elem.end):
                    raise TreeClosed("the file does not parse inside a block around the change")
    canon = tf.canon
    found = ""

    def note(loop: bool) -> None:
        nonlocal found
        found = "loop_bound" if (loop or found == "loop_bound") else "block_moved"

    gone_ids = [(u, _ident(canon, "old", u)) for u in gone]
    came_ids = [(u, _ident(canon, "new", u)) for u in came]
    stayed = Counter((u.key, ident) for u, ident in gone_ids) & Counter((u.key, ident) for u, ident in came_ids)
    left_gone: list[tuple[_Unit, Any]] = []
    left_came: list[tuple[_Unit, Any]] = []
    budget = Counter(stayed)
    for u, ident in gone_ids:
        if budget[(u.key, ident)] > 0:
            budget[(u.key, ident)] -= 1
        else:
            left_gone.append((u, ident))
    budget = Counter(stayed)
    for u, ident in came_ids:
        if budget[(u.key, ident)] > 0:
            budget[(u.key, ident)] -= 1
        else:
            left_came.append((u, ident))
    by_key: dict[tuple[str, ...], list[tuple[_Unit, Any]]] = {}
    by_shape: dict[tuple[str, ...], list[tuple[_Unit, Any]]] = {}
    for u, ident in left_came:
        by_key.setdefault(u.key, []).append((u, ident))
        if _ABSTRACTED & set(u.shape) and not u.imported and not u.declaration:
            by_shape.setdefault(u.shape, []).append((u, ident))
    for u, ident in left_gone:
        for twin, twin_ident in by_key.get(u.key, ()):
            note(_difference(ident, twin_ident, u.chain, twin.chain)[2])
        if _ABSTRACTED & set(u.shape) and not u.imported and not u.declaration:
            for twin, twin_ident in by_shape.get(u.shape, ()):
                if twin_ident != ident:
                    note(_difference(ident, twin_ident, u.chain, twin.chain)[2])
    explained: set[int] = set()  # the unchanged statements whose chain differs and that a row of this part accounts for
    for line in own_old:
        explained.update(tf.old_marks.get(line, ()))
    for line in own_new:
        explained.update(tf.new_marks.get(line, ()))
    for index in sorted(explained):
        note(tf.shifted[index])
    if not found and _decorators_reordered(tf, gone, came):
        return "block_changed"
    return found


def _decorator_order(canon: _Canon, side: str, units: Sequence[_Unit]) -> dict[Any, list[tuple[str, ...]]]:
    found: dict[Any, list[tuple[str, ...]]] = {}
    for u in units:
        if u.decorator and u.chain:
            found.setdefault(_ident(canon, side, u)[-1], []).append(u.key)
    return found


def _decorators_reordered(tf: TreeFile, gone: Sequence[_Unit], came: Sequence[_Unit]) -> bool:
    """Decorators wrap each other in order: the same ones in another order on one definition change what runs."""

    canon = tf.canon
    targets = {_ident(canon, "old", u)[-1] for u in gone if u.decorator} | {
        _ident(canon, "new", u)[-1] for u in came if u.decorator
    }
    if not targets:
        return False
    before, after = tf.decorators
    return any(
        before.get(t, []) != after.get(t, []) and sorted(before.get(t, [])) == sorted(after.get(t, [])) for t in targets
    )


def own_lines(spans: Sequence[Any]) -> tuple[set[int], set[int]]:
    """The old-side and new-side lines a part owns."""

    old: set[int] = set()
    new: set[int] = set()
    for span in spans:
        (old if span.side == "old" else new).update(range(int(span.start_line), int(span.end_line) + 1))
    return old, new


__all__ = [
    "MAX_FILE_CHARS",
    "MAX_SNAPSHOT_CHARS",
    "MAX_SNAPSHOT_SECONDS",
    "Budget",
    "TreeClosed",
    "TreeFile",
    "grammar_for",
    "own_lines",
    "read_file",
    "verdict",
]
