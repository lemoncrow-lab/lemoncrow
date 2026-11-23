"""The Quick check batch guard: a part whose change touches arithmetic or a loop bound stays out of every batch.

A pure, deterministic function of the captured diff (hunk ``patch`` text, read the way
:mod:`change_risk` reads it). No model, no clock, no I/O.

The decision is the code change alone. Whether a test "ran" is not an input: the only result rows the server
holds on a path are file-level verification rows, which the author, an agent, CI or a lint step can post, and
none of them is the reviewer's own fresh Run test (``check_runs.publish_verdict`` writes no path). Until the
owner decides what may release the guard (SPEC 7.1 P4-2 D), nothing does. Fail closed.

When the classifier cannot tell (no diff text, a file type it does not read, text it cannot tokenize) the part
is guarded too, and the reason says it could not be checked.

Reading: each side of a hunk (context plus its own lines) is tokenized into logical lines (joined by bracket
depth, template ``${...}`` read as code). A change is judged by the logical line it sits in: a loop line, a
slice or ``range``-style bound, or an arithmetic or bitwise operator on the line. Only a rename that is
consistent across the hunk and changes a binding site (a definition, assignment target, loop variable,
parameter) is exempt; renaming an operand elsewhere is a change of what is computed.

A statement that ended up in another block (a different loop, branch, handler or function body, or into or out of
a brace block) is a change too: see "Moves across a block boundary" below. Where the blocks cannot be read, the
part is guarded.
"""

from __future__ import annotations

import itertools
import keyword
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from lemoncrow.pro.capabilities.review.change_risk import _rows, safeguard_language

LOOP_BOUND = "loop_bound"
ARITHMETIC = "arithmetic"
COMPARISON = "comparison"
UNVERIFIABLE = "unverifiable"
BLOCK_MOVED = "block_moved"
BLOCK_CHANGED = "block_changed"

_NON_CODE_SUFFIXES = (
    ".md", ".mdx", ".rst", ".txt", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".css", ".scss", ".csv",
)  # fmt: skip

_ARITH_OPS = frozenset(
    {
        "+", "-", "*", "/", "%", "**", "//", "<<", ">>", "++", "--", "&", "|", "^", "~",
        "+=", "-=", "*=", "/=", "%=", "**=", "//=", "<<=", ">>=", "&=", "|=", "^=",
    }
)  # fmt: skip
_COMPARE_OPS = frozenset({"<", "<=", ">", ">=", "==", "!=", "===", "!=="})
_BOUND_IDS = frozenset({"range", "len", "length", "size", "slice", "enumerate"})
_MATH_IDS = frozenset({"min", "max", "round", "floor", "ceil", "sum", "abs", "pow", "sqrt", "trunc", "divmod", "Math"})
_LOOP_WORDS = frozenset({"for", "while"})
_DEFINERS = frozenset({"def", "class", "function", "let", "const", "var", "as"})

_TOKEN = r"""
    (?P<comment>{comment})
  | (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<num>0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<id>[^\W\d][\w$]*)
  | (?P<op>===|!==|&&|\|\||\?\?|\*\*=?|//=?|<<=?|>>=?|<=|>=|==|!=|\+\+|--|[-+*/%&|^]=|=>|->|[-+*/%<>=!&|^~])
  | (?P<punct>[()\[\]{{}}.,:;?@\\{hash}])
  | (?P<ws>\s+)
"""
_PATTERNS = {
    "python": re.compile(_TOKEN.format(comment="\\#.*", hash=""), re.VERBOSE),
    "script": re.compile(_TOKEN.format(comment="//.*|/\\*.*?\\*/|/\\*.*|\\*/", hash="\\#"), re.VERBOSE),
}


# lc-debt: difflib's longest-match search is quadratic when tokens repeat (a file of look-alike functions), so each
# search is charged the inner-loop steps it can take (one per occurrence, on the other side, of every token in its
# range: an upper bound) and a comparison past this many steps is refused, which holds the part (fail closed: the
# part is reviewed on its own). About half a second of work (measured ~100 to 150 ns a step). Upgrade path: a linear
# diff over statement keys, proven equal to difflib's blocks on randomized inputs, or a per-hunk window proven sufficient.
_MATCH_BUDGET = 4_000_000


# lc-debt: the budget above is per comparison, but every part of a hunk compares and reads its own token stream (a part
# cuts its own moved lines out of it, and an edited string reads as one), so a hunk of N parts can cost N budgets, and
# a review of M such hunks M times that. Two further bounds, both on the steps charged: every comparison a part makes
# (token, row or statement) and every read of rows (_read: tokens, lines, blocks, statements, at _ROW_STEPS a row and
# _CHAR_STEPS a character, measured at about 100 ns a step like the comparisons) is charged to the hunk it reads:
# - one hunk: past _HUNK_BUDGET the parts not yet read are refused. 16 comparisons' worth, about 6 s (Python) to 8 s
#   (TypeScript) at the cap; a hunk of 100 three-line handlers with a part each stays under it, and about 110 do not;
# - one call: past _CALL_BUDGET no further comparison or read is started and every part not yet read is refused, so a
#   review's work is at most this plus one comparison and one part's reads (about 13 s Python, 15 s TypeScript at the
#   cap, measured on a quiet machine). Parts already read keep their verdicts. The call's total also takes what no
#   comparison or row counts: each part started is charged _PART_STEPS (its scope, view, memo lookups and payload, a
#   fixed ~230 to 390 us whatever its size, so thousands of small parts cannot add up to minutes), its view
#   _VIEW_STEPS a row and span, and the whole-file reading _TREE_CHAR_STEPS a parsed character and _TREE_PART_STEPS a
#   verdict. A part refused (about 35 us) is not charged: it is refused before the charge, so refusing costs nothing
#   that could pass the cap; the 8 s deadline of the whole-file reading stays as an inner backstop (it fails closed).
# Both refuse the same way a comparison too large does (fail closed: the part is reviewed on its own), and which parts
# get refused depends on the order they are read in. Not counted: tokenizing a row the first time (shared by every
# part) and a few dict lookups per part inside a read. Upgrade path: one comparison and one read per hunk on a stream
# that does not depend on the part, each part's own rows marked afterwards (see classify_hunk).
_HUNK_BUDGET = 16 * _MATCH_BUDGET
_ROW_STEPS = 25
_CHAR_STEPS = 1
_VIEW_STEPS = 5  # building a part's view of a hunk: a row, and each of the part's spans it is tested against
_PART_STEPS = 3500  # what a part costs before it reads a row: its scope, view, memo lookups, payload (about 350 us)
_TREE_CHAR_STEPS = 5  # a character parsed for the whole-file reading (8,000,000 of them is about four seconds)
_TREE_PART_STEPS = 3000  # a part's verdict against the parsed files (about 300 us; its own lines, not the file's units)
_CALL_BUDGET = 2 * _HUNK_BUDGET


class _TooLarge(Exception):
    pass


class _BudgetMatcher(SequenceMatcher[Any]):
    """``SequenceMatcher`` that gives up, deterministically, once its longest-match searches have cost too much."""

    def __init__(self, old: Sequence[Any], new: Sequence[Any]) -> None:
        super().__init__(None, old, new, autojunk=False)
        # autojunk=False: difflib's b2j lists every position of every token (no popular token is dropped), so the
        # inner loop walks, for each token of ``old`` in range, as many entries as that token has in ``new``.
        occurrences = Counter(new)
        self._reach = [0, *itertools.accumulate(occurrences.get(token, 0) for token in old)]
        self._length, self._width = len(old), len(new)
        self._spent = 0

    def find_longest_match(self, alo: int = 0, ahi: int | None = None, blo: int = 0, bhi: int | None = None) -> Any:
        ahi = self._length if ahi is None else ahi
        bhi = self._width if bhi is None else bhi
        # The inner loop visits every position of a[i] in b (the ones before blo too), then both ends are extended.
        cost = (ahi - alo) + (bhi - blo) + self._reach[ahi] - self._reach[alo]
        if self._spent + cost > _MATCH_BUDGET:  # refused before it runs, so the steps charged never pass the budget
            raise _TooLarge
        self._spent += cost
        return super().find_longest_match(alo, ahi, blo, bhi)


_Opcodes = tuple[tuple[str, int, int, int, int], ...]


class _Run:
    """Everything one ``batch_guards`` call memoizes. It is dropped with the call, so no source outlives its request.

    A hunk is read by every part that touches it, and each part reads the same rows with only its own few flagged, so
    rows, lines, clones and comparisons are built once per call and shared across its parts.
    """

    # lc-debt: each table but ``hunks`` is cleared whole when it reaches its bound, which only costs a rebuild (same
    # verdicts); the bounds count entries, not bytes, so one call's memory is its own input's size times a small
    # constant. ``hunks`` is not bounded: clearing it would bring back the scan of every hunk of a file for each of its
    # parts, and it holds only references into the packet the call already has (one list per file). Upgrade path: none
    # needed; a part's own spans are grouped by hunk on each read (its spans, not the file's), which is not charged.
    OPCODES, TOKENS, BRACKETS, LINES, CLONES, UNITS, CLASSES = 256, 4096, 4096, 16384, 16384, 256, 256

    def __init__(self) -> None:
        self.opcodes: dict[tuple[tuple[Any, ...], tuple[Any, ...]], _Opcodes | None] = {}
        self.tokens: dict[tuple[str, str], tuple[_Tok, ...]] = {}
        self.brackets: dict[tuple[str, str], tuple[int, int]] = {}
        self.lines: dict[tuple[tuple[tuple[str, bool], ...], str], tuple[list[_Tok], int]] = {}
        self.clones: dict[tuple[Any, ...], tuple[list[_Tok], list[_Tok]]] = {}
        self.units: dict[tuple[tuple[tuple[str, bool], ...], str], _Units] = {}
        self.classes: dict[tuple[Any, ...], str] = {}
        self.altered: dict[tuple[Any, ...], str] = {}
        # A file's hunk mappings by id(entry), the entry held beside them (so the id stays its own for the call). One
        # slot per file of the packet, which the call holds anyway: no bound, and it goes with the call.
        self.hunks: dict[int, tuple[Mapping[str, Any], list[Mapping[str, Any]], bool]] = {}
        self.rows = 0  # rows and characters charged by every read that ran (a cache hit costs none; tests count them)
        self.chars = 0
        self.too_large: set[tuple[tuple[str, ...], tuple[str, ...], str]] = set()  # hunks to refuse without reading
        self.matchers = 0  # comparisons started (the test counts them)
        self.spent = 0  # steps charged by every comparison that ran (a cache hit costs none)
        self.hunk_spent: dict[tuple[tuple[str, ...], tuple[str, ...], str], int] = {}  # the same, per hunk read
        self.charge_to: tuple[tuple[str, ...], tuple[str, ...], str] | None = None  # the hunk being read


_RUN: ContextVar[_Run | None] = ContextVar("batch_guard_run", default=None)


@contextmanager
def _scope() -> Iterator[_Run]:
    """Memoize inside the block. Nested scopes share the outer one; each thread or task has its own."""

    run = _RUN.get()
    if run is not None:
        yield run
        return
    run = _Run()
    token = _RUN.set(run)
    try:
        yield run
    finally:
        _RUN.reset(token)


def _run() -> _Run:
    """The active scope's memo, or a throwaway one when called outside any (nothing is then kept)."""

    return _RUN.get() or _Run()


def _remember(table: dict[Any, Any], bound: int, key: Any, value: Any) -> Any:
    if len(table) >= bound:
        table.clear()
    table[key] = value
    return value


def _charge(steps: int) -> None:
    """Add work done to the call's total and to the hunk being read; a hunk past its budget refuses the parts after it."""

    run = _run()
    run.spent += steps
    hunk = run.charge_to
    if hunk is not None:
        run.hunk_spent[hunk] = run.hunk_spent.get(hunk, 0) + steps
        if run.hunk_spent[hunk] > _HUNK_BUDGET:
            run.too_large.add(hunk)  # the part being read keeps its verdict; the parts after it are refused


def _read(*sides: Sequence[tuple[str, bool]]) -> None:
    """Charge reading these rows (tokens, lines, blocks, statements): ``_ROW_STEPS`` a row and ``_CHAR_STEPS`` a character."""

    run = _run()
    rows = sum(len(side) for side in sides)
    chars = sum(len(text) for side in sides for text, _ in side)
    run.rows += rows
    run.chars += chars
    _charge(rows * _ROW_STEPS + chars * _CHAR_STEPS)


def _opcodes_of(old: tuple[Any, ...], new: tuple[Any, ...]) -> _Opcodes | None:
    run = _run()
    key = (old, new)
    if key in run.opcodes:
        return run.opcodes[key]
    if run.spent > _CALL_BUDGET:
        return None  # not remembered: a comparison already made is still answered
    run.matchers += 1
    matcher = _BudgetMatcher(old, new)
    try:
        found: _Opcodes | None = tuple(matcher.get_opcodes())
    except _TooLarge:
        found = None
    _charge(matcher._spent)
    return _remember(run.opcodes, _Run.OPCODES, key, found)  # type: ignore[no-any-return]


def _opcodes(old: Sequence[Any], new: Sequence[Any]) -> _Opcodes:
    """``SequenceMatcher(autojunk=False).get_opcodes()``, memoized on the two sequences.

    Every part that touches one hunk reads the same two sequences (only which rows are the part's own differs), so
    a hunk is matched once, not once per part. Raises ``_Unresolved`` when the comparison is too large to finish: the
    caller holds the part.
    """

    found = _opcodes_of(tuple(old), tuple(new))
    if found is None:
        raise _TooBig
    return found


class _Unreadable(Exception):
    pass


class _Unresolved(Exception):
    """A line whose enclosing block cannot be told: it may have entered or left a loop or another block."""

    def __init__(self, why: str = "a moved line's block is not in the diff") -> None:
        super().__init__(why)


class _TooBig(_Unresolved):
    """The changed text is too large to compare within the budget."""

    def __init__(self) -> None:
        super().__init__("the changed text is too large to compare")


@dataclass(frozen=True)
class _Tok:
    kind: str  # num | id | op | punct | str | slice | indent | nl
    text: str
    mine: bool = False
    line: int = 0
    binding: bool = False
    in_loop: bool = False
    chain: tuple[tuple[str, ...], ...] = ()
    unresolved: bool = False

    @property
    def key(self) -> str:
        return "STR" if self.kind == "str" else self.text


def _with(tok: _Tok, **changes: Any) -> _Tok:
    """``dataclasses.replace`` for a token without its per-call field checks: a hunk makes tens of thousands of them."""

    clone = object.__new__(_Tok)
    clone.__dict__.update(tok.__dict__, **changes)
    return clone


def _template(text: str, start: int, language: str, out: list[_Tok]) -> int:
    """Read a backtick template from *start*; its literal text is one string token, each ``${...}`` is code."""

    index = start + 1
    out.append(_Tok("str", "`"))
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == "`":
            return index + 1
        elif text.startswith("${", index):
            depth, inner = 1, index + 2
            while inner < len(text) and depth:
                depth += (text[inner] == "{") - (text[inner] == "}")
                inner += 1
            if depth:
                raise _Unreadable(text)
            out.extend(_tokenize_text(text[index + 2 : inner - 1], language))
            index = inner
        else:
            index += 1
    raise _Unreadable(text)


def _tokenize_text(text: str, language: str) -> list[_Tok]:
    return list(_tokenized(text, language))


def _tokenized(text: str, language: str) -> tuple[_Tok, ...]:
    """One row's tokens, memoized: every part of a hunk reads the same context rows. Tokens are immutable."""

    run = _run()
    found = run.tokens.get((text, language))
    if found is None:
        found = _remember(run.tokens, _Run.TOKENS, (text, language), _tokenize_row(text, language))
    return found


def _tokenize_row(text: str, language: str) -> tuple[_Tok, ...]:
    pattern = _PATTERNS[language]
    out: list[_Tok] = []
    index = 0
    while index < len(text):
        if language == "script" and text[index] == "`":
            index = _template(text, index, language, out)
            continue
        match = pattern.match(text, index)
        if match is None:
            raise _Unreadable(text)
        index = match.end()
        kind = match.lastgroup or "ws"
        if kind in ("ws", "comment"):
            continue
        out.append(_Tok(kind, match.group()))
    if language == "script" and out and out[0].text == "*" and text.lstrip().startswith("*"):
        return ()  # the body of a block comment
    return tuple(out)


def _brackets(text: str, language: str) -> tuple[int, int]:
    """One row's bracket walk as ``(net, lowest)``: opens minus closes, and the lowest the running count reaches."""

    run = _run()
    found = run.brackets.get((text, language))
    if found is None:
        found = _remember(run.brackets, _Run.BRACKETS, (text, language), _bracket_walk(text, language))
    return found


def _bracket_walk(text: str, language: str) -> tuple[int, int]:
    try:
        tokens = _tokenized(text, language)
    except _Unreadable:
        return 0, 0
    running = lowest = 0
    for tok in tokens:
        if tok.kind == "punct":
            if tok.text in "([{":
                running += 1
            elif tok.text in ")]}":
                running -= 1
                lowest = min(lowest, running)
    return running, lowest


def _logical_line(rows: tuple[tuple[str, bool], ...], language: str) -> tuple[list[_Tok], int]:
    """One logical line from its rows (brackets balanced at the end), with how many closers had no opener.

    A pure function of the rows, and every part of a hunk reads the same rows with at most its own few flagged, so
    the line is built once and shared (its tokens are immutable and no caller changes the list).
    """

    run = _run()
    key = (rows, language)
    found = run.lines.get(key)
    if found is not None:
        return found
    pending: list[_Tok] = []
    stack: list[str] = []
    closed = 0
    for text, mine in rows:
        indent = len(text.expandtabs(4)) - len(text.expandtabs(4).lstrip())
        try:
            tokens = _tokenize_text(text, language)
        except _Unreadable:
            if mine:
                raise
            tokens = []
        if language == "python" and tokens and not pending:
            pending.append(_Tok("indent", f"I{indent}", mine=mine))
        for tok in tokens:
            kind = tok.kind
            if tok.text in "([{" and tok.kind == "punct":
                stack.append(tok.text)
            elif tok.text in ")]}" and tok.kind == "punct":
                if stack:
                    stack.pop()
                else:
                    closed += 1
            elif tok.text == ":" and tok.kind == "punct" and stack and stack[-1] == "[":
                kind = "slice"
            pending.append(tok if kind == tok.kind and not mine else _with(tok, kind=kind, mine=mine))
    return _remember(run.lines, _Run.LINES, key, (_mark_bindings(pending), closed))  # type: ignore[no-any-return]


def _side(rows: Sequence[tuple[str, bool]], language: str, cut: list[str] | None = None) -> list[list[_Tok]]:
    """Logical lines: rows joined while brackets are open. A context row that cannot be read is left out.

    ``cut`` collects a note whenever a bracket is closed that the hunk never opened, or left open at its end: the
    statement around it is cut by the edge of the hunk.
    """

    lines: list[list[_Tok]] = []
    group: list[tuple[str, bool]] = []
    started = False  # a row of this logical line has tokens
    depth = 0
    for text, mine in rows:
        try:
            tokens = _tokenized(text, language)
        except _Unreadable:
            if mine:
                raise
            tokens = ()
        group.append((text, mine))
        started = started or bool(tokens)
        net, lowest = _brackets(text, language)
        depth = net + max(depth, -lowest)  # the count never goes below zero: a closer with no opener is ignored
        if depth == 0:
            if started:
                line, closed = _logical_line(tuple(group), language)
                lines.append(line)
                if cut is not None:
                    cut.extend(["closed"] * closed)
            group, started = [], False
    if started:
        line, closed = _logical_line(tuple(group), language)
        lines.append(line)
        if cut is not None:
            cut.extend(["closed"] * closed)
            cut.append("open")
    return _annotate_blocks(lines) if language == "python" else lines


def _block_key(line: Sequence[_Tok]) -> tuple[str, ...]:
    return tuple(tok.key for tok in line if tok.kind != "indent")


def _cloned(line: list[_Tok], changes: tuple[tuple[str, Any], ...]) -> list[_Tok]:
    """``line`` with every token changed the same way, shared between parts that read the same line the same way.

    Keyed on the line object, which the entry keeps alive so its id cannot be reused.
    """

    run = _run()
    key = (id(line), changes)
    found = run.clones.get(key)
    if found is None:
        found = _remember(run.clones, _Run.CLONES, key, (line, [_with(tok, **dict(changes)) for tok in line]))
    return found[1]  # type: ignore[no-any-return]


def _annotated(line: list[_Tok], in_loop: bool, chain: tuple[Any, ...], unresolved: bool) -> list[_Tok]:
    return _cloned(line, (("in_loop", in_loop), ("chain", chain), ("unresolved", unresolved)))


def _parents(indents: Sequence[int], colon: Sequence[bool]) -> list[int]:
    """Per line, the nearest earlier line that ends in ``:`` and is indented less than it (``-1``: none).

    One pass with a stack of colon lines whose indentation strictly increases: a colon line followed by another that
    is indented no deeper can never be the nearest one for any later line, so it is dropped.
    """

    parent: list[int] = []
    stack: list[int] = []
    for index, current in enumerate(indents):
        at = len(stack) - 1
        while at >= 0 and indents[stack[at]] >= current:
            at -= 1
        parent.append(stack[at] if at >= 0 else -1)
        if colon[index]:
            while stack and indents[stack[-1]] >= current:
                stack.pop()
            stack.append(index)
    return parent


def _annotate_blocks(lines: list[list[_Tok]]) -> list[list[_Tok]]:
    """Give each Python line the headers of the blocks around it, as far as the diff shows them."""

    def indent_of(line: Sequence[_Tok]) -> int:
        return int(line[0].text[1:]) if line and line[0].kind == "indent" else 0

    indents = [indent_of(line) for line in lines]
    parent = _parents(indents, [bool(line) and line[-1].text == ":" for line in lines])
    keys = [_block_key(line) for line in lines]
    loop = [bool((head := [t for t in key if t != "async"]) and head[0] in _LOOP_WORDS) for key in keys]
    chains: list[tuple[tuple[str, ...], ...]] = []
    in_loops: list[bool] = []
    outermost: list[int] = []  # the indentation of the outermost header found: what is left unexplained
    out: list[list[_Tok]] = []
    for index, line in enumerate(lines):
        up = parent[index]
        chains.append((keys[up], *chains[up]) if up >= 0 else ())
        in_loops.append(up >= 0 and (loop[up] or in_loops[up]))
        outermost.append(outermost[up] if up >= 0 else indents[index])
        unresolved = outermost[index] > 0
        out.append(_annotated(line, in_loops[index], chains[index], unresolved))
    return out


def _mark_bindings(line: list[_Tok]) -> list[_Tok]:
    indent = [tok for tok in line[:1] if tok.kind == "indent"]
    line = line[len(indent) :]
    first = line[0].text
    out: list[_Tok] = list(indent)
    depth = 0
    in_for_vars = False
    for index, tok in enumerate(line):
        prev = line[index - 1].text if index else ""
        nxt = line[index + 1].text if index + 1 < len(line) else ""
        if tok.text in ("(", "[", "{"):
            depth += 1
        elif tok.text in (")", "]", "}"):
            depth -= 1
        if tok.kind == "id" and tok.text == "for":
            in_for_vars = True
        elif tok.kind == "id" and tok.text in ("in", "of"):
            in_for_vars = False
        binding = False
        if tok.kind == "id" and tok.text not in _DEFINERS and tok.text not in _LOOP_WORDS:
            binding = (
                prev in _DEFINERS
                or in_for_vars
                or (nxt == "=" and (index == 0 or prev in _DEFINERS))
                or (first in ("def", "function") and depth >= 1 and nxt in (",", ")", "=", ":"))
            )
        out.append(_with(tok, binding=binding))
    return out


def _flatten(lines: Sequence[Sequence[_Tok]]) -> list[_Tok]:
    out: list[_Tok] = []
    for number, line in enumerate(lines):
        out.extend(_cloned(line, (("line", number),)))  # type: ignore[arg-type]
        out.append(_Tok("nl", "\n", line=number))
    return out


def _cancel_moves(old: list[list[_Tok]], new: list[list[_Tok]]) -> tuple[list[list[_Tok]], list[list[_Tok]]]:
    """Drop changed lines that only moved: the same logical line removed in one place and added in another."""

    def key(line: Sequence[_Tok]) -> tuple[str, ...]:
        return tuple(tok.key for tok in line)

    gone = Counter(key(line) for line in old if any(tok.mine for tok in line))
    came = Counter(key(line) for line in new if any(tok.mine for tok in line))
    shared = gone & came

    def keep(lines: list[list[_Tok]]) -> list[list[_Tok]]:
        left = Counter(shared)
        out: list[list[_Tok]] = []
        for line in lines:
            k = key(line)
            if any(tok.mine for tok in line) and left[k] > 0:
                left[k] -= 1
                continue
            out.append(line)
        return out

    return keep(old), keep(new)


def _cancel_reindents(old: list[list[_Tok]], new: list[list[_Tok]]) -> tuple[list[list[_Tok]], list[list[_Tok]]]:
    """Python: a changed line re-indented under the same block headers is a reformat, not a change.

    Only when the headers around it are all in the diff on both sides; otherwise the line stays a change.
    """

    def key(line: Sequence[_Tok]) -> tuple[Any, ...] | None:
        if not line or line[0].kind != "indent" or not any(tok.mine for tok in line) or line[0].unresolved:
            return None
        return (_block_key(line), line[0].chain)

    gone = Counter(k for k in map(key, old) if k)
    came = Counter(k for k in map(key, new) if k)
    shared = gone & came

    def keep(lines: list[list[_Tok]]) -> list[list[_Tok]]:
        left = Counter(shared)
        out: list[list[_Tok]] = []
        for line in lines:
            k = key(line)
            if k and left[k] > 0:
                left[k] -= 1
                continue
            out.append(line)
        return out

    return keep(old), keep(new)


def _significant(tokens: Sequence[_Tok]) -> list[_Tok]:
    return [tok for tok in tokens if tok.kind in ("num", "id", "op")]


# --- Moves across a block boundary --------------------------------------------------------------------------
#
# A statement that is removed in one place and added in another used to cancel as "only moved". That is right
# inside one block and wrong across blocks: the same text then runs under a different loop, branch or handler.
# Every statement gets the chain of blocks around it (kind and identity of each), per side, and a moved
# statement whose chain differs is a change. So is a statement the part did not touch whose chain differs
# between the two sides, because the part inserted or removed a header or a brace around it. Python reads the chain from indentation and the block keyword
# lines, JavaScript and TypeScript from braces and the keyword that opens each. Where the chain cannot be
# resolved the part is guarded. This pass only adds findings: the cancelling above is untouched, so it can never
# guard less than before.

_LOOP_HEADS = frozenset({"for", "while", "do"})
_SCRIPT_KINDS = {
    "for": "loop", "while": "loop", "do": "loop", "if": "if", "else": "else", "try": "try", "catch": "catch",
    "finally": "finally", "switch": "switch", "function": "function", "class": "class", "interface": "interface",
    "enum": "enum", "type": "type",
}  # fmt: skip
_CONTROL_HEADS = frozenset({"if", "for", "while", "else"})
_OPERAND = ("id", "num", "str", "op")


@dataclass(frozen=True)
class _Unit:
    """One statement (a Python logical line, a script row): its tokens, the blocks around it, whether it is the part's."""

    key: tuple[str, ...]
    chain: tuple[Any, ...]  # each block: (kind, header is the part's, place among its siblings); never header text
    mine: bool
    weight: int = 0  # how many names, numbers, strings and operators it holds
    shape: tuple[str, ...] = ()  # its keywords, operators and punctuation, with names, numbers and strings blanked
    imported: bool = False  # an import or export statement
    cut: bool = False  # the hunk starts or ends inside this statement: only part of it is in view


@dataclass(frozen=True)
class _Units:
    units: list[_Unit]
    reliable: bool
    shape: tuple[int, int] = (0, 0)  # script: closing braces with no opener in the hunk, braces still open at its end
    structural: bool = False  # the part itself opens or closes a block (a brace, a header line)
    read: bool = True  # every row was read as code, comment or string; False: a string or comment cut by the hunk


def _is_loop_chain_element(element: Any) -> bool:
    return bool(element) and element[0] == "loop"


_PY_COMPOUND = frozenset(
    {"if", "elif", "else", "for", "while", "try", "except", "finally", "with", "def", "class", "match", "case"}
)
_IMPORT_WORDS = frozenset({"import", "from", "export"})
_TRIPLE = ('"""', "'''")


def _python_rows(rows: Sequence[tuple[str, bool]]) -> tuple[list[tuple[str, bool]], bool]:
    """Rows with backslash continuations joined and the inside of triple-quoted strings left out.

    ``False``: a row cannot be read, or the hunk may begin inside a triple-quoted string (a bare quote first), or the
    hunk ends inside one, so which rows are text and which are code is a guess.
    """

    joined: list[tuple[str, bool]] = []
    read = True
    open_tail = False
    in_string = False
    seen_quotes = False
    opened_here = False  # the string being read was opened by a row of this hunk
    for text, mine in rows:
        quotes = sum(text.count(q) for q in _TRIPLE)
        if in_string or quotes:
            if quotes % 2 and not in_string and not seen_quotes:
                # The first quote in the hunk: it opens a string only if it follows a header or has text after it;
                # a bare quote may as well close a docstring the hunk starts in.
                after_header = bool(joined) and joined[-1][0].rstrip().endswith(":")
                read = read and (after_header or text.strip() not in _TRIPLE)
            seen_quotes = True
            read = read and not (mine and in_string and not opened_here)
            if quotes % 2:
                opened_here = not in_string
                in_string = not in_string
            if in_string or quotes % 2:
                open_tail = False
                continue  # a row inside a string, or the one that opens or closes it: not a statement
        elif not mine:
            try:
                _tokenize_text(text, "python")
            except _Unreadable:
                read = False  # a context row the reader drops may be a header
        if open_tail and joined:
            before, was_mine = joined.pop()
            joined.append((before.rstrip()[:-1] + " " + text.strip(), was_mine or mine))
        else:
            joined.append((text, mine))
        open_tail = text.rstrip().endswith("\\")
    return joined, read and not in_string


def _python_header(line: Sequence[_Tok]) -> tuple[str, int | None] | None:
    """``(kind, split)`` for a line that opens a block. ``split`` is where an inline body starts, ``None`` for a bare header."""

    keys = [(i, t) for i, t in enumerate(line) if t.kind != "indent"]
    head = [t.key for _, t in keys if t.key != "async"]
    if not head:
        return None
    kind = "loop" if head[0] in _LOOP_HEADS else head[0]
    if line[-1].text == ":":
        return kind, None
    if head[0] not in _PY_COMPOUND:
        return None
    depth = 0
    for i, tok in keys:
        if tok.text in ("(", "[", "{"):
            depth += 1
        elif tok.text in (")", "]", "}"):
            depth -= 1
        elif tok.text == ":" and tok.kind == "punct" and depth == 0:
            following = line[i + 1].text if i + 1 < len(line) else ""
            if following != "=":  # `:=` is not a header colon
                return kind, i + 1
    return None


def _python_units(rows: Sequence[tuple[str, bool]]) -> _Units:
    joined, read = _python_rows(rows)
    reliable = read
    cut: list[str] = []
    lines = _side(joined, "python", cut)

    def indent_of(line: Sequence[_Tok]) -> int:
        return int(line[0].text[1:]) if line and line[0].kind == "indent" else 0

    indents = [indent_of(line) for line in lines]
    headers = [_python_header(line) for line in lines]
    units: list[_Unit] = []
    structural = False

    # Each header's identity (kind, whether it is the part's, place among the like-minded siblings before it) and,
    # per line, the headers around it: one forward pass, not a scan back over every earlier line per line.
    idents: list[tuple[Any, ...] | None] = []
    seen: dict[int, list[int]] = {}  # indentation -> headers so far in this region, [not the part's, the part's]
    for index, line in enumerate(lines):
        for level in [level for level in seen if level > indents[index]]:
            del seen[level]  # a less indented line ends the region those siblings share
        header = headers[index]
        if header is None:
            idents.append(None)
            continue
        mine = any(t.mine for t in line[: header[1]])
        counts = seen.setdefault(indents[index], [0, 0])
        idents.append((header[0], mine, counts[mine]))
        counts[mine] += 1

    def ident(back: int) -> tuple[Any, ...]:
        found = idents[back]
        assert found is not None
        return found

    parent = _parents(indents, [bool(line) and line[-1].text == ":" for line in lines])
    around: list[tuple[Any, ...]] = []
    outermost: list[int] = []
    for index in range(len(lines)):
        up = parent[index]
        around.append((ident(up), *around[up]) if up >= 0 else ())
        outermost.append(outermost[up] if up >= 0 else indents[index])

    for index, line in enumerate(lines):
        # indentation still unexplained: the headers above are outside the diff
        chain: list[Any] = [*around[index], ("open", outermost[index])]
        header = headers[index]
        parts = [(line, tuple(chain))]
        if header is not None:
            structural = structural or any(t.mine for t in line)
            if header[1] is not None:
                parts = [(line[: header[1]], tuple(chain)), (line[header[1] :], (ident(index), *chain))]
        for part, part_chain in parts:
            edge = bool(cut) and index in (0, len(lines) - 1)  # the hunk cuts the first or the last statement
            unit = _unit_of(part, part_chain, "python", edge)
            if unit is not None:
                units.append(unit)
    return _Units(units, reliable, structural=structural, read=read)


def _continues(previous: _Tok | None, first: _Tok) -> bool:
    if previous is None:
        return False
    if previous.kind == "op" and previous.text not in ("++", "--"):
        return True
    if previous.text in (",", ".", "?", ":", "=>", "(", "["):
        return True
    return first.text == "." or (first.kind == "op" and first.text not in ("++", "--", "!", "~"))


_REGEX_AFTER = frozenset(
    {
        "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "&&", "||", "??", "=>", "+", "-", "*", "%", "<",
        ">", "<=", ">=", "==", "===", "!=", "!==", "^", "~", "return", "typeof", "case", "in", "of", "void", "delete",
        "throw", "new", "await", "yield", "else", "do",
    }
)  # fmt: skip
_COMMENT_BODY = re.compile(r"\s*\*(?:/|\s|$)")
_STRING = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'""")


def _script_lex(rows: Sequence[tuple[str, bool]]) -> tuple[list[list[_Tok]], bool]:
    """Tokens per row, with the state carried from row to row: block comments, template literals (and the code in
    their ``${...}``), strings and regex literals. Braces count only in code. ``False``: a row ends or starts in
    something the lexer cannot resolve (a comment or template cut by the hunk, an unterminated string)."""

    out: list[list[_Tok]] = []
    reliable = True
    comment = False
    templates: list[int] = []  # one per open template: -1 in its text, n >= 0 in a ${ } that holds n open braces
    previous = ""
    for text, mine in rows:
        tokens: list[_Tok] = []
        i, n = 0, len(text)
        if not comment and not templates and not mine and _COMMENT_BODY.match(text):
            comment = True  # a context row of a doc comment whose opener is above the hunk
        while i < n:
            if comment:
                end = text.find("*/", i)
                comment, i = (False, end + 2) if end >= 0 else (True, n)
                continue
            char = text[i]
            if templates and templates[-1] == -1:  # inside the literal text of a template
                if char == "\\":
                    i += 2
                elif char == "`":
                    templates.pop()
                    i += 1
                elif text.startswith("${", i):
                    templates.append(0)
                    i += 2
                else:
                    i += 1
                continue
            if char.isspace():
                i += 1
            elif text.startswith("//", i):
                break
            elif text.startswith("/*", i):
                comment, i = True, i + 2
            elif text.startswith("*/", i):
                reliable, i = False, i + 2  # closes a comment opened above the hunk
            elif char == "`":
                tokens.append(_Tok("str", "`"))
                templates.append(-1)
                previous, i = "STR", i + 1
            elif templates and char in "{}":
                if char == "}" and templates[-1] == 0:
                    templates.pop()  # back to the template's text
                else:
                    templates[-1] += 1 if char == "{" else -1
                    tokens.append(_Tok("punct", char))
                    previous = char
                i += 1
            elif char in "\"'":
                match = _STRING.match(text, i)
                if match is None:
                    reliable, i = False, n
                else:
                    tokens.append(_Tok("str", match.group()))
                    previous, i = "STR", match.end()
            elif char == "/" and previous in _REGEX_AFTER and (closing := _regex_end(text, i)) is not None:
                tokens.append(_Tok("str", text[i:closing]))
                previous, i = "STR", closing
            else:
                match = _PATTERNS["script"].match(text, i)
                if match is None:
                    reliable, i = False, n
                    continue
                i = match.end()
                if match.lastgroup not in ("ws", "comment"):
                    tokens.append(_Tok(match.lastgroup or "ws", match.group()))
                    previous = match.group()
        out.append(tokens)
    return out, reliable and not templates


def _regex_end(text: str, start: int) -> int | None:
    """Where a regex literal that opens at ``start`` ends (flags included), or None if it does not close in the row."""

    i, in_class = start + 1, False
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 1
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            i += 1
            while i < len(text) and text[i].isalpha():
                i += 1
            return i
        i += 1
    return None


def _normalized(tokens: Sequence[_Tok]) -> list[_Tok]:
    """A statement without what formatting adds or drops: a trailing ``;`` and a ``,`` before a closing bracket."""

    kept = [
        tok
        for index, tok in enumerate(tokens)
        if not (
            tok.kind == "punct"
            and tok.text == ","
            and index + 1 < len(tokens)
            and tokens[index + 1].kind == "punct"
            and tokens[index + 1].text in ")]}"
        )
    ]
    while kept and kept[-1].kind == "punct" and kept[-1].text == ";":
        kept.pop()
    return kept


def _unit_of(tokens: Sequence[_Tok], chain: tuple[Any, ...], language: str, cut: bool = False) -> _Unit | None:
    body = _normalized([tok for tok in tokens if tok.kind != "indent"])
    weight = sum(tok.kind in _OPERAND for tok in body)
    if not weight:
        return None
    words = _WORDS[language]
    shape = tuple(
        (
            tok.text
            if tok.kind in ("op", "punct") or tok.text in words
            else {"num": "NUM", "str": "STR"}.get(tok.kind, "ID")
        )
        for tok in body
    )
    return _Unit(
        tuple(tok.key for tok in body),
        chain,
        any(tok.mine for tok in tokens),
        weight,
        shape,
        body[0].text in _IMPORT_WORDS,
        cut,
    )


def _name_list(statement: Sequence[str]) -> bool:
    """Does a ``{`` after these tokens open a list of names (``import {``, ``export {``) rather than a block?"""

    return bool(statement) and (
        statement[0] == "import"
        or (
            statement[0] == "export"
            and len(statement) <= 2
            and "default" not in statement[1:]
            and "function" not in statement
            and "class" not in statement
            and "const" not in statement
        )
    )


def _script_units(rows: Sequence[tuple[str, bool]]) -> _Units:
    """Statements with the brace blocks around each, read left to right; ``reliable`` is False where the braces do not parse.

    A statement is gathered across rows up to its ``;`` or the next brace, so prettier's wrapping and a dropped
    ``;`` do not change it. It is cut at a header's closing parenthesis, so ``if (ok) { send(x); }`` and
    ``if (ok) send(x);`` give a header and a body inside it.
    """

    lexed, read = _script_lex(rows)
    reliable = read
    units: list[_Unit] = []
    stack: list[Any] = []  # "(" | "[" | a block tuple (kind, part's own, place, braceless body)
    statement: list[str] = []
    ordinals: Counter[tuple[Any, ...]] = Counter()  # blocks opened so far under one parent, by whose they are
    closes = 0
    structural = False
    previous: _Tok | None = None
    seg: list[_Tok] = []
    seg_chain: tuple[Any, ...] | None = None
    seg_cut = False
    in_names = False

    def at_block_level() -> bool:
        return not stack or isinstance(stack[-1], tuple)

    def chain_now() -> tuple[Any, ...]:
        return (("outer", closes), *(e[:3] for e in stack if isinstance(e, tuple)))

    def add(tok: _Tok, mine: bool) -> None:
        nonlocal seg_chain
        if not seg:
            seg_chain = chain_now()
        seg.append(_with(tok, mine=mine))

    def flush() -> None:
        nonlocal seg, seg_chain, seg_cut
        unit = _unit_of(seg, seg_chain or chain_now(), "script", seg_cut)
        if unit is not None:
            units.append(unit)
        seg, seg_chain, seg_cut = [], None, False

    def open_block(head: tuple[str, ...], mine: bool, kind: str | None = None, bare: bool = False) -> None:
        if kind is None:
            kind = next((_SCRIPT_KINDS[t] for t in head if t in _SCRIPT_KINDS), "arrow" if "=>" in head else "block")
        chain = tuple(e[:3] for e in stack if isinstance(e, tuple))
        ordinals[(chain, mine)] += 1
        stack.append((kind, mine, ordinals[(chain, mine)] - 1, bare))

    def end_bare() -> None:
        while stack and isinstance(stack[-1], tuple) and stack[-1][3]:
            stack.pop()

    for (_, mine), tokens in zip(rows, lexed, strict=True):
        if not tokens:
            continue
        if seg and at_block_level() and not _continues(previous, tokens[0]):
            flush()  # the line ended the statement
        if at_block_level() and not _continues(previous, tokens[0]):
            statement = []
        rest = tokens
        if tokens[0].kind == "id" and tokens[0].text in ("case", "default") and at_block_level():
            colon = next((i for i, t in enumerate(tokens) if t.kind == "punct" and t.text == ":"), None)
            if colon is None:
                reliable = False
            else:
                while stack and isinstance(stack[-1], tuple) and stack[-1][0] == "case":
                    stack.pop()
                flush()
                open_block((), mine, "case")
                for tok in tokens[: colon + 1]:
                    add(tok, mine)
                flush()
                rest = tokens[colon + 1 :]
        control: str | None = None
        header_at: int | None = None
        for index, tok in enumerate(rest):
            after = rest[index + 1] if index + 1 < len(rest) else None
            behind = rest[index - 1] if index else previous
            dotted = behind is not None and behind.text == "."
            if tok.kind == "id" and not dotted:
                if tok.text in ("if", "for", "while", "switch", "catch", "with") and after is not None:
                    control = tok.text if after.text in ("(", "await") else None
                elif tok.text in ("else", "do"):
                    add(tok, mine)
                    statement.append(tok.key)
                    flush()
                    if after is None:
                        reliable = False  # a body without braces on the next row
                    elif after.text not in ("{", "if", ";"):
                        open_block((tok.text,), mine, _SCRIPT_KINDS[tok.text], bare=True)
                    continue
            if tok.kind == "punct" and tok.text in "([":
                if tok.text == "(" and control is not None and header_at is None and at_block_level():
                    header_at = len(stack)
                stack.append(tok.text)
                add(tok, mine)
                statement.append(tok.key)
            elif tok.kind == "punct" and tok.text in ")]":
                if not stack:
                    seg_cut = True  # closes a bracket opened above the hunk: its statement is cut
                elif isinstance(stack[-1], str) and "([".index(stack[-1]) == ")]".index(tok.text):
                    stack.pop()
                else:
                    reliable = False
                add(tok, mine)
                statement.append(tok.key)
                if header_at is not None and len(stack) == header_at and control is not None:
                    flush()
                    kind = _SCRIPT_KINDS.get(control, "block")
                    header_text = tuple(statement)
                    control, header_at = None, None
                    if after is None:
                        reliable = False  # a body without braces on the next row
                    elif after.text not in ("{", ";"):
                        open_block(header_text, mine, kind, bare=True)
            elif tok.kind == "punct" and tok.text == "{" and _name_list(statement):
                in_names = True  # `import { a, b } from "x"`: names, not a block
                add(tok, mine)
            elif tok.kind == "punct" and tok.text == "}" and in_names:
                in_names = False
                add(tok, mine)
            elif tok.kind == "punct" and tok.text == "{":
                flush()
                structural = structural or mine
                open_block(tuple(statement), mine)
                statement = []
            elif tok.kind == "punct" and tok.text == "}":
                flush()
                structural = structural or mine
                end_bare()
                while stack and isinstance(stack[-1], tuple) and stack[-1][0] == "case":
                    stack.pop()
                if not stack:
                    closes += 1  # closes a block opened above the hunk: the rows after it are in another one
                elif isinstance(stack[-1], tuple):
                    stack.pop()
                else:
                    reliable = False
                statement = []
            elif tok.kind == "punct" and tok.text == ";" and at_block_level():
                add(tok, mine)
                flush()
                end_bare()
                statement = []
            else:
                add(tok, mine)
                statement.append(tok.key)
        if stack and isinstance(stack[-1], tuple) and stack[-1][3] and tokens[-1].text not in (";", "}"):
            reliable = False  # a braceless body that goes on past this row
        previous = tokens[-1]
    if seg and not at_block_level():
        seg_cut = True  # the hunk ends inside a statement
    flush()
    return _Units(
        units, reliable, (closes, sum(1 for e in stack if isinstance(e, tuple) and e[0] != "case")), structural, read
    )


def _units(rows: Sequence[tuple[str, bool]], language: str) -> _Units:
    """The statements of ``rows``, read once for the part: every check of a part asks for the same two sides."""

    run = _run()
    key = (tuple(rows), language)
    found = run.units.get(key)
    if found is None:
        _read(rows)
        found = _remember(
            run.units, _Run.UNITS, key, _python_units(rows) if language == "python" else _script_units(rows)
        )
    return found  # type: ignore[no-any-return]


def _block_moves(old_rows: Sequence[tuple[str, bool]], new_rows: Sequence[tuple[str, bool]], language: str) -> str:
    """``loop_bound`` | ``block_moved`` | ``\"\"``: statements that ended up in another block. May raise ``_Unresolved``."""

    old, new = _units(old_rows, language), _units(new_rows, language)
    found = ""
    undecided: str | None = None

    def note(before: Sequence[Any], after: Sequence[Any]) -> None:
        nonlocal found, undecided
        seen = [e for e in before if e[0] != "open"], [e for e in after if e[0] != "open"]
        if seen[0] == seen[1]:
            undecided = undecided or "a moved line's block is not in the diff"  # only the unseen headers above differ
        elif any(_is_loop_chain_element(e) for e in set(seen[0]) ^ set(seen[1])):
            found = LOOP_BOUND
        else:
            found = found or BLOCK_MOVED

    # Statements the part left alone are the same rows on both sides; one whose blocks differ was moved by a
    # header or a brace the part added or removed around it.
    still_old, still_new = [u for u in old.units if not u.mine], [u for u in new.units if not u.mine]
    aligned = [u.key for u in still_old] == [u.key for u in still_new]
    for left, right in zip(still_old, still_new, strict=False):
        if aligned and left.chain != right.chain:
            note(left.chain, right.chain)
    gone = Counter((u.key, u.chain) for u in old.units if u.mine)
    came = Counter((u.key, u.chain) for u in new.units if u.mine)
    shared_keys = {k for k, _ in gone} & {k for k, _ in came}
    loose_imports = {u.key for side in (old, new) for u in side.units if u.imported}
    cut_keys = {u.key for side in (old, new) for u in side.units if u.cut and u.mine}
    unsure = shared_keys - loose_imports  # an import that moved changes no run: it needs no certain chain
    if unsure and not (aligned and old.reliable and new.reliable and old.shape == new.shape):
        undecided = "the blocks around a moved line could not be read"
    elif unsure & cut_keys:
        undecided = "the blocks around a moved line could not be read"  # the hunk cuts the statement that moved
    elif not aligned and (old.structural or new.structural):
        undecided = "the blocks around a moved line could not be read"  # the part opens or closes a block here
    stayed = gone & came
    outs, ins = gone - stayed, came - stayed
    for key in shared_keys:
        for before in [c for (k, c) in outs if k == key]:
            for after in [c for (k, c) in ins if k == key]:
                note(before, after)
    if found or undecided is None:
        return found
    raise _Unresolved(undecided)


def _hunk_key(
    old_rows: Sequence[tuple[str, bool]], new_rows: Sequence[tuple[str, bool]], language: str
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """A hunk by its text alone: the parts that read it differ only in which rows they flag as their own."""

    return (tuple(text for text, _ in old_rows), tuple(text for text, _ in new_rows), language)


def classify_hunk(old_rows: Sequence[tuple[str, bool]], new_rows: Sequence[tuple[str, bool]], language: str) -> str:
    """``loop_bound`` | ``arithmetic`` | ``comparison`` | ``block_moved`` | ``""`` for one hunk's two sides (rows are ``(text, is_part_line)``).

    Raises ``_Unreadable`` for part lines it cannot tokenize, and ``_Unresolved`` when the hunk cannot be told.

    A hunk too large to compare for one part is too large for its siblings (they read the same rows, each cutting
    out only its own moved lines), so the first refusal is remembered by the hunk's text and the rest are refused
    before any work. That holds a sibling that might have fit on its own: stricter, never looser.
    """

    # lc-debt: a hunk that fits the comparison budget is still read once per part (lines, blocks, units: about 5 us a
    # row), so parts x rows grows quadratically in a very large hunk with a part per change. It is charged (_read) and
    # bounded by the hunk and call budgets. Upgrade path: a per-hunk pass that reads the rows once and marks each
    # part's own lines afterwards.
    with _scope() as run:
        # The same rows with the same rows flagged (a part of a hunk the call has read already, in another file) are
        # classified once: the answer is the one a fresh read would give, so a bound that held work back changes nothing.
        seen = (tuple(old_rows), tuple(new_rows), language)
        if seen in run.classes:
            return run.classes[seen]
        hunk = _hunk_key(old_rows, new_rows, language)
        if hunk in run.too_large or run.spent > _CALL_BUDGET:
            run.too_large.add(hunk)
            raise _TooBig
        outer, run.charge_to = run.charge_to, hunk
        try:
            return _remember(run.classes, _Run.CLASSES, seen, _classify_hunk(old_rows, new_rows, language))  # type: ignore[no-any-return]
        except _TooBig:
            run.too_large.add(hunk)
            raise
        finally:
            run.charge_to = outer


def _classify_hunk(old_rows: Sequence[tuple[str, bool]], new_rows: Sequence[tuple[str, bool]], language: str) -> str:
    _read(old_rows, new_rows)
    old_lines, new_lines = _cancel_moves(_side(old_rows, language), _side(new_rows, language))
    old_lines, new_lines = _cancel_reindents(old_lines, new_lines)
    old, new = _flatten(old_lines), _flatten(new_lines)
    changes = []
    # First, so a comparison too large to finish holds the part before anything else is read (it raises _Unresolved).
    for tag, i1, i2, j1, j2 in _opcodes([tok.key for tok in old], [tok.key for tok in new]):
        if tag == "equal":
            continue
        removed, added = old[i1:i2], new[j1:j2]
        if not any(tok.mine for tok in (*removed, *added) if tok.kind != "nl"):
            continue
        if not _significant([*removed, *added]) and not any(tok.kind == "indent" for tok in (*removed, *added)):
            continue
        changes.append((removed, added))
    moved, undecided = "", None
    try:
        moved = _block_moves(old_rows, new_rows, language)
    except _Unresolved as exc:
        undecided = exc

    # A rename counts only when it is the same one-to-one swap everywhere, touches a binding site and leaves no
    # trace of the old name on the new side (or of the new name on the old side).
    pairs: dict[tuple[str, str], bool] = {}
    for removed, added in changes:
        rs, ad = _significant(removed), _significant(added)
        if len(rs) == 1 and len(ad) == 1 and rs[0].kind == ad[0].kind == "id":
            pairs[(rs[0].text, ad[0].text)] = (
                pairs.get((rs[0].text, ad[0].text), False) or rs[0].binding or ad[0].binding
            )
    old_names = {tok.text for tok in old if tok.kind == "id"}
    new_names = {tok.text for tok in new if tok.kind == "id"}
    sources = Counter(a for a, _ in pairs)
    targets = Counter(b for _, b in pairs)
    renames = {
        (a, b)
        for (a, b), binds in pairs.items()
        if binds and sources[a] == 1 and targets[b] == 1 and a not in new_names and b not in old_names
    }

    found: set[str] = {moved} if moved else set()
    unresolved = False
    for removed, added in changes:
        rs, ad = _significant(removed), _significant(added)
        reindented = any(tok.kind == "indent" for tok in (*removed, *added))
        if not reindented and len(rs) == 1 and len(ad) == 1 and (rs[0].text, ad[0].text) in renames:
            continue
        lines = {("o", tok.line) for tok in removed} | {("n", tok.line) for tok in added}
        context = [tok for tok in old if ("o", tok.line) in lines] + [tok for tok in new if ("n", tok.line) in lines]
        changed = [*rs, *ad]
        if reindented and any(tok.unresolved for tok in context if tok.kind == "indent"):
            unresolved = True
        if any(tok.kind == "id" and tok.text in _LOOP_WORDS for tok in context):
            found.add(LOOP_BOUND)
        elif reindented and any(tok.in_loop for tok in context):
            found.add(LOOP_BOUND)  # a line entered or left a loop body
        elif any(tok.kind == "id" and tok.text in ("break", "continue") for tok in changed):
            found.add(LOOP_BOUND)
        elif any((tok.kind == "id" and tok.text in _BOUND_IDS) or tok.kind == "slice" for tok in context):
            found.add(LOOP_BOUND)
        elif (
            any(tok.kind == "op" and tok.text in _ARITH_OPS for tok in context)
            or any(tok.kind == "id" and tok.text in _MATH_IDS for tok in context)
            or any(tok.kind == "num" for tok in changed)
        ):
            found.add(ARITHMETIC)
        elif any(tok.kind == "op" and tok.text in _COMPARE_OPS for tok in context):
            found.add(COMPARISON)
    for name in (LOOP_BOUND, ARITHMETIC, COMPARISON, BLOCK_MOVED):
        if name in found:
            return name
    if undecided is not None:
        raise undecided
    if unresolved:
        raise _Unresolved
    return ""


def classify_change(old_rows: Sequence[str], new_rows: Sequence[str], language: str) -> str:
    """:func:`classify_hunk` for plain changed lines, every one of them the part's own."""

    return classify_hunk([(t, True) for t in old_rows], [(t, True) for t in new_rows], language)


@dataclass(frozen=True)
class BatchGuard:
    guarded: bool
    code: str  # "" | loop_bound | arithmetic | comparison | block_moved | block_changed | unverifiable
    reason: str

    def payload(self) -> dict[str, Any]:
        return {"guarded": self.guarded, "code": self.code, "reason": self.reason}


_CHANGED = {
    LOOP_BOUND: "the loop bound changed",
    ARITHMETIC: "the arithmetic changed",
    COMPARISON: "a comparison changed",
    BLOCK_MOVED: "a line moved into a different block",
    BLOCK_CHANGED: "the change alters a block",
}


def _closed(why: str) -> BatchGuard:
    return BatchGuard(True, UNVERIFIABLE, f"Could not be checked: {why}. This part stays out of the batch.")


def _hunks_of(entry: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
    """The file's hunk mappings and whether any has diff text, built once per call however many parts read the file."""

    run = _run()
    held = run.hunks.get(id(entry))
    if held is None or held[0] is not entry:  # the entry is held with its list, so its id cannot be reused in the call
        hunks = [hunk for hunk in entry.get("hunks") or () if isinstance(hunk, Mapping)]
        held = (entry, hunks, any(hunk.get("patch") for hunk in hunks))
        run.hunks[id(entry)] = held
    return held[1], held[2]


def _hunk_views(
    entry: Mapping[str, Any], spans: Sequence[Any]
) -> list[tuple[list[tuple[str, bool]], list[tuple[str, bool]]]] | None:
    """Per hunk the part touches, both sides as ``(text, is_part_line)``. ``None``: no diff text to read."""

    hunks, has_patch = _hunks_of(entry)
    if not spans or not has_patch:
        return None
    views = []
    # The part's spans by the hunk they sit in, so it walks its own hunks and not the file's: a file of H hunks with a
    # part each would otherwise cost H squared here, uncharged, and never reach _CALL_BUDGET.
    own: dict[int, list[Any]] = {}
    for span in spans:
        found = span.hunk_ordinal
        if type(found) is not int:
            # Matched by ``==`` before: 1.0 or True is hunk 1, None or "1" is no hunk. Dropping it would leave rows of
            # the part unread (fail-open), so it is read as the whole number it equals, or ignored when it equals none.
            try:
                whole = int(found)
                if whole != found:
                    continue
            except Exception:
                continue
            found = whole
        own.setdefault(found, []).append(span)
    for ordinal in sorted(own):
        if not 0 <= ordinal < len(hunks):
            continue  # a span of no hunk of this file: ignored, as when every hunk was tested against it
        hunk, mine = hunks[ordinal], own[ordinal]
        if not hunk.get("patch"):
            continue
        rows = list(_rows(hunk))
        _charge(_VIEW_STEPS * len(rows) * (1 + len(mine)))  # every row is tested against every span of the part
        if _run().spent > _CALL_BUDGET:
            raise _TooBig  # refused before the view is built
        old: list[tuple[str, bool]] = []
        new: list[tuple[str, bool]] = []
        for row in rows:
            if row.tag == "+":
                new.append((row.text, any(s.side == "new" and s.start_line <= row.new <= s.end_line for s in mine)))
            elif row.tag == "-":
                old.append((row.text, any(s.side == "old" and s.start_line <= row.old <= s.end_line for s in mine)))
            else:
                old.append((row.text, False))
                new.append((row.text, False))
        if any(flag for _, flag in (*old, *new)):
            views.append((old, new))
    return views or None


def _moved_between_hunks(views: Sequence[tuple[Any, Any]], language: str) -> None:
    """A statement removed in one hunk and added in another: the blocks around the two cannot be compared."""

    def keys(rows: Any) -> set[tuple[str, ...]]:
        return {u.key for u in _units(rows, language).units if u.mine}

    gone = [keys(old) for old, _ in views]
    came = [keys(new) for _, new in views]
    added_in: dict[tuple[str, ...], set[int]] = {}  # a statement -> the hunks that add it: one pass, not hunks squared
    for j, added in enumerate(came):
        for key in added:
            added_in.setdefault(key, set()).add(j)
    for i, removed in enumerate(gone):
        if any(added_in.get(key, set()) - {i} for key in removed):
            raise _Unresolved("a line moved from one changed spot to another")


_PY_BLOCKISH = re.compile(
    r"^\s*(?:@|(?:async\s+)?(?:if|elif|else|for|while|try|except|finally|with|def|class)\b|(?:match|case)\b[^=]*:)"
)
_IMPORT = re.compile(r"^\s*(?:import|from|export)\b")
_SCRIPT_LABEL = re.compile(r"^\s*(?:case|default)\b")
_SCRIPT_BLOCKISH = re.compile(r"^\s*(?:if|else|for|while|do|switch|case|default|try|catch|finally)\b")


def _touches_block(text: str, language: str) -> bool:
    if language == "python":
        stripped = text.rstrip()
        return bool(
            _PY_BLOCKISH.match(text) or stripped.endswith((":", "\\")) or any(quote in text for quote in _TRIPLE)
        )
    if _SCRIPT_BLOCKISH.match(text):
        return True
    return ("{" in text or "}" in text) and not _value_braces(text)


_VALUE_BEFORE = frozenset(
    {"=", "(", ",", "[", "?", "&&", "||", "??", "return", "...", "|", "&", "!", "import", "export"}
)
_NOT_GENERIC = frozenset({"return", "case", "in", "of", "typeof", "void", "delete", "throw", "new", "await", "yield"})


def _generic_close(tokens: Sequence[_Tok], close: int) -> bool:
    """Is the ``>`` at ``close`` the end of type arguments (``Promise<void>``) rather than a JSX tag?"""

    for index in range(close - 1, -1, -1):
        tok = tokens[index]
        if tok.kind == "op" and tok.text == "<":
            return index > 0 and tokens[index - 1].kind == "id" and tokens[index - 1].text not in _NOT_GENERIC
        if not (tok.kind == "id" or tok.text in (",", ".", "[", "]", "|", "&", "?")):
            return False
    return False


def _value_braces(text: str) -> bool:
    """Every brace in this row opens an object, a JSX expression, a type or an import list and closes in the row."""

    try:
        tokens = _tokenize_text(text, "script")
    except _Unreadable:
        return False
    depth = 0
    for index, tok in enumerate(tokens):
        previous = tokens[index - 1].text if index else ""
        if tok.kind == "punct" and tok.text == "{":
            if previous == ":":
                value = not (index == 2 and tokens[0].kind == "id")  # `name: {` at the start of a row is a label
            elif previous == ">":
                value = not _generic_close(tokens, index - 1)  # `f(): Promise<void> {` is a function body
            else:
                value = previous in _VALUE_BEFORE
            if not value:
                return False
            depth += 1
        elif tok.kind == "punct" and tok.text == "}":
            if not depth:
                return False
            depth -= 1
        elif tok.kind == "punct" and tok.text == ";" and depth:
            return False
    return not depth


_WORDS = {
    "python": frozenset(keyword.kwlist) | frozenset(keyword.softkwlist) | {"async", "await"},
    "script": frozenset(
        "break case catch class const continue debugger default delete do else enum export extends finally for "
        "function if import in instanceof new return static super switch this throw try typeof var void while with "
        "yield let async await of from as type interface implements public private protected readonly abstract "
        "declare namespace module get set".split()
    ),
}


def _shape(text: str, language: str) -> tuple[str, ...] | None:
    """The row with names, numbers and strings blanked: its keywords, operators and punctuation. ``None``: unreadable."""

    if language == "python" and any(quote in text for quote in _TRIPLE):
        return None
    try:
        tokens = _tokenize_text(text, language)
    except _Unreadable:
        return None
    words = _WORDS[language]
    return tuple(
        tok.text if tok.kind in ("op", "punct") or tok.text in words else ("NUM" if tok.kind == "num" else "ID")
        for tok in tokens
        if tok.kind != "str"
    ) + tuple("STR" for tok in tokens if tok.kind == "str")


def _indent(text: str) -> int:
    expanded = text.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


_STRUCTURE_WORDS = {
    "python": _PY_COMPOUND | {"async", "match", "case", "lambda"},
    "script": frozenset(
        "if else for while do switch case default try catch finally function class interface enum namespace".split()
    ),
}


def _skeleton(text: str, language: str) -> tuple[str, ...] | None:
    """What in a row can make or end a block: braces, block keywords, arrows (Python: brackets, colons, backslashes)."""

    if language == "python" and any(quote in text for quote in _TRIPLE):
        return None
    try:
        tokens = _tokenize_text(text, language)
    except _Unreadable:
        return None
    marks = "{}" if language == "script" else "{}()[]:\\"
    return tuple(
        tok.text
        for tok in tokens
        if (tok.kind == "punct" and tok.text in marks)
        or tok.text == "=>"
        or (tok.kind == "id" and tok.text in _STRUCTURE_WORDS[language])
    )


def _in_place_pairs(old: Sequence[tuple[str, bool]], new: Sequence[tuple[str, bool]]) -> list[tuple[int, int]]:
    """Row pairs a row-by-row edit script replaces in place: same place in the file, so no block can have moved."""

    pairs: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in _opcodes([t for t, _ in old], [t for t, _ in new]):
        if tag == "replace" and i2 - i1 == j2 - j1:
            pairs.extend((i1 + k, j1 + k) for k in range(i2 - i1) if old[i1 + k][1] and new[j1 + k][1])
    return pairs


def _reformat_only(old: Sequence[tuple[str, bool]], new: Sequence[tuple[str, bool]], language: str) -> bool:
    """Indentation moved but every statement the part touched still sits in the same blocks, all of them in view."""

    if language != "python":
        return True  # braces carry the blocks; indentation is looks only
    before, after = _units(old, "python"), _units(new, "python")
    if not (before.reliable and after.reliable):
        return False
    mine = [[(u.key, u.chain) for u in side.units if u.mine] for side in (before, after)]
    unseen = any(e[0] == "open" and e[1] for side in mine for _, chain in side for e in chain)
    return not unseen and Counter(mine[0]) == Counter(mine[1])


def _self_contained(texts: Sequence[str], language: str) -> bool:
    """Does this run of rows open and close everything it opens, and leave no header waiting for a body?

    A run like that, added whole or removed whole, cannot change which block the rows around it are in.
    """

    if not texts:
        return True
    depth = 0
    for text in texts:
        if language == "python" and any(quote in text for quote in _TRIPLE):
            return False
        try:
            tokens = _tokenize_text(text, language)
        except _Unreadable:
            return False
        for tok in tokens:
            if tok.kind == "punct" and tok.text in "([{":
                depth += 1
            elif tok.kind == "punct" and tok.text in ")]}":
                depth -= 1
                if depth < 0:
                    return False
    if depth:
        return False
    last = texts[-1].rstrip()
    if language == "python":
        return not last.endswith((":", "\\"))
    if any(_SCRIPT_LABEL.match(text) for text in texts):
        return False
    return not (_SCRIPT_BLOCKISH.match(last) and not last.endswith((";", "}")))


def _why(text: str, language: str) -> str:
    if language == "python":
        stripped = text.rstrip()
        if any(q in text for q in _TRIPLE):
            return "triple"
        if _PY_BLOCKISH.match(text):
            return "kw:" + text.split()[0].rstrip(":(")
        return "colon" if stripped.endswith(":") else "backslash"
    if "{" in text or "}" in text:
        return "brace"
    return "kw"


def _block_altered(views: Sequence[tuple[Any, Any]], language: str) -> str:
    """The coarse net under the precise reading: does the part's own change reach block structure at all?

    Returns why ("" when it does not). Holds when, on the part's own lines,
    (a) a row it removed or added opens, closes or is a block (a compound keyword, a trailing colon or backslash, a
    triple quote; in JavaScript a brace or a control keyword), (b) in Python, a row keeps its text and changes its
    indentation, (c) a statement is removed in one hunk and added in another, or (d) a removed and an added
    statement differ only in names, numbers and strings (one that moved and was edited on the way). (a) and (b)
    read rows; (c) and (d) read whole statements with their formatting taken out (a trailing ``;``, a comma before
    a closing bracket, the way a call is wrapped over rows), so a reformat does not hide a move. It looks at text
    only, so it holds for anything the reading above could not follow.

    Exempt, because none of them can change which block a row is in: a row the edit replaces in place with one that
    has the same braces, block keywords and arrows (a rename, a changed operand or comment, another argument; in
    Python also the same brackets, colons and backslashes, and the same indentation or a reindent that leaves every
    touched statement in the same blocks; JavaScript indentation is for looks only and is not checked); a run of rows
    added whole or removed whole that opens and closes everything it opens; and a row whose braces only hold an
    object, a JSX expression, a type annotation or an import or export list. For (c) and (d) also a statement edited
    in place (same block, same keywords and punctuation); for (d) also import and export statements and the members
    of ``interface``, ``type`` and ``enum`` bodies.
    """

    removed: list[set[tuple[str, ...]]] = []
    added: list[set[tuple[str, ...]]] = []
    shapes_gone: dict[tuple[str, ...], set[tuple[str, ...]]] = {}  # statements with no edited twin in their place
    shapes_came: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    shapes_gone_all: dict[tuple[str, ...], set[tuple[str, ...]]] = {}  # all of the part's statements, edited or not
    shapes_came_all: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    run = _run()
    for old, new in views:
        run.charge_to = _hunk_key(old, new, language)  # what the comparisons below cost is this hunk's
        _read(old, new)
        exempt_old: set[int] = set()
        exempt_new: set[int] = set()
        formatted: bool | None = None
        for i, j in _in_place_pairs(old, new):
            skeleton = _skeleton(old[i][0], language)
            if skeleton is None or skeleton != _skeleton(new[j][0], language):
                continue
            if _indent(old[i][0]) != _indent(new[j][0]):
                formatted = _reformat_only(old, new, language) if formatted is None else formatted
                if not formatted:
                    continue
            exempt_old.add(i)
            exempt_new.add(j)
        gone = [text for k, (text, mine) in enumerate(old) if mine and k not in exempt_old]
        came = [text for k, (text, mine) in enumerate(new) if mine and k not in exempt_new]
        contained_old: set[int] = set()
        contained_new: set[int] = set()
        for tag, i1, i2, j1, j2 in _opcodes([t for t, _ in old], [t for t, _ in new]):
            # Only a run added whole or removed whole: swapping one run for another can move a statement between them.
            if tag == "delete" and _self_contained([t for t, _ in old[i1:i2]], language):
                contained_old.update(range(i1, i2))
            elif tag == "insert" and _self_contained([t for t, _ in new[j1:j2]], language):
                contained_new.update(range(j1, j2))
        loose = [t for k, (t, m) in enumerate(old) if m and k not in exempt_old and k not in contained_old]
        loose += [t for k, (t, m) in enumerate(new) if m and k not in exempt_new and k not in contained_new]
        if any(_touches_block(text, language) for text in loose):
            return "a-" + next(_why(t, language) for t in loose if _touches_block(t, language))
        if language == "python":
            indents_old: dict[str, Counter[int]] = {}
            indents_new: dict[str, Counter[int]] = {}
            for rows, table in ((gone, indents_old), (came, indents_new)):
                for text in rows:
                    table.setdefault(text.strip(), Counter())[_indent(text)] += 1
            for text in indents_old.keys() & indents_new.keys():
                if (indents_old[text] - indents_new[text]) and (indents_new[text] - indents_old[text]):
                    return "b"
        # (c) and (d): statements, not rows. A statement another version of which sits in the same place is not one that moved.
        read_before, read_after = _units(old, language), _units(new, language)
        if not (read_before.read and read_after.read) and any(m for _, m in (*old, *new)):
            return "e"  # a string or comment cut by the hunk may hide the part's own rows
        before, after = read_before.units, read_after.units
        edited_before: set[int] = set()
        edited_after: set[int] = set()
        for tag, i1, i2, j1, j2 in _opcodes([u.key for u in before], [u.key for u in after]):
            if tag == "replace" and i2 - i1 == j2 - j1:
                for k in range(i2 - i1):
                    left, right = before[i1 + k], after[j1 + k]
                    same_place = left.chain == right.chain and read_before.reliable and read_after.reliable
                    if left.mine and right.mine and left.shape == right.shape and same_place:
                        edited_before.add(i1 + k)
                        edited_after.add(j1 + k)
        away = [u for k, u in enumerate(before) if u.mine and k not in edited_before]
        here = [u for k, u in enumerate(after) if u.mine and k not in edited_after]
        removed.append({u.key for u in away})
        added.append({u.key for u in here})
        for units, shapes in (
            (away, shapes_gone),
            (here, shapes_came),
            ([u for u in before if u.mine], shapes_gone_all),
            ([u for u in after if u.mine], shapes_came_all),
        ):
            for unit in units:
                declaration = any(e[0] in ("interface", "type", "enum") for e in unit.chain)
                if "ID" in unit.shape and not unit.imported and not declaration:
                    shapes.setdefault(unit.shape, set()).add(unit.key)
    if any(a & b for i, a in enumerate(removed) for j, b in enumerate(added) if i != j):
        return "c"
    # (d) a statement with no counterpart in its place whose twin in shape was rewritten or taken away elsewhere
    for shape in shapes_gone_all.keys() | shapes_came_all.keys():
        gone_all, came_all = shapes_gone_all.get(shape, set()), shapes_came_all.get(shape, set())
        unmatched_came = shapes_came.get(shape, set()) - gone_all
        unmatched_gone = shapes_gone.get(shape, set()) - came_all
        if (unmatched_came and gone_all - came_all) or (unmatched_gone and came_all - gone_all):
            return "d"
    return ""


# Reads one file on one side: ``read(path, "old" | "new")``. ``None``: the text is not available.
SideReader = Callable[[str, str], "str | None"]


def _tree_code(target: Any, entry: Mapping[str, Any], reader: SideReader, cache: dict[str, Any]) -> str:
    """The whole-file reading's verdict for this part (see :mod:`batch_guard_tree`); ``TreeClosed`` when it cannot decide."""

    from lemoncrow.pro.capabilities.review import batch_guard_tree as tree

    path = str(target.path)
    if path not in cache:
        budget = cache.setdefault("\0budget", tree.Budget())
        before = budget.chars
        try:
            cache[path] = tree.read_file(entry, path, reader, budget)
        except tree.TreeClosed as exc:
            cache[path] = exc
        except Exception:
            cache[path] = tree.TreeClosed("the file could not be read as a whole")
        _charge(_TREE_CHAR_STEPS * (before - budget.chars))  # the characters parsed count against the call as well
    _charge(_TREE_PART_STEPS)
    loaded = cache[path]
    if isinstance(loaded, Exception):
        raise loaded
    own_old, own_new = tree.own_lines(target.spans)
    return tree.verdict(loaded, own_old, own_new)


def guard_for_target(
    target: Any,
    entry: Mapping[str, Any] | None,
    reader: SideReader | None = None,
    cache: dict[str, Any] | None = None,
) -> BatchGuard:
    """The guard for one part. With a ``reader`` of the file on each side, the whole-file reading is added to the hunk reading."""

    from lemoncrow.pro.capabilities.review.differential import is_test_path

    path = str(target.path)
    if is_test_path(path) or path.lower().endswith(_NON_CODE_SUFFIXES):
        return BatchGuard(False, "", "")
    language = safeguard_language(path)
    if not language:
        return _closed("this file type is not read")
    if entry is None:
        return _closed("the changed lines were not available")
    with _scope():
        return _guard_for_target(target, entry, language, reader, cache)


def _altered(views: Sequence[tuple[Any, Any]], language: str) -> str:
    """``_block_altered``, once for the same rows with the same rows flagged (a part read already, in another file)."""

    run = _run()
    key = (tuple((tuple(old), tuple(new)) for old, new in views), language)
    found = run.altered.get(key)
    if found is None:
        found = _remember(run.altered, _Run.CLASSES, key, _block_altered(views, language))
    return found  # type: ignore[no-any-return]


def _guard_for_target(
    target: Any, entry: Mapping[str, Any], language: str, reader: SideReader | None, cache: dict[str, Any] | None
) -> BatchGuard:
    run = _run()
    keys: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []
    try:
        if run.spent > _CALL_BUDGET:
            # The call has used its budget: no further part is read, not even into its view of a hunk. Refusing a part
            # costs about 35 us, which is not charged: nothing could stop the parts of a call being refused one by one.
            raise _TooBig
        _charge(_PART_STEPS)
        views = _hunk_views(entry, target.spans)
        if views is None:
            return _closed("the changed lines were not available")
        keys = [_hunk_key(old, new, language) for old, new in views]
        if run.too_large.intersection(keys):
            raise _TooBig
        codes = {classify_hunk(old, new, language) for old, new in views}
        _moved_between_hunks(views, language)
        altered = (
            ""
            if any(code in codes for code in (LOOP_BOUND, ARITHMETIC, COMPARISON, BLOCK_MOVED))
            else _altered(views, language)
        )
    except _TooBig as exc:
        run.too_large.update(keys)  # wherever it came from, the parts that read these hunks next are refused unread
        return _closed(str(exc))
    except _Unresolved as exc:
        return _closed(str(exc))
    except Exception:  # fail closed on anything the reader cannot take
        return _closed("the changed text could not be read")
    finally:
        run.charge_to = None
    for code in (LOOP_BOUND, ARITHMETIC, COMPARISON, BLOCK_MOVED):
        if code in codes:
            return BatchGuard(True, code, f"Test didn't run and {_CHANGED[code]}.")
    if reader is not None:
        from lemoncrow.pro.capabilities.review.batch_guard_tree import TreeClosed

        try:
            seen = _tree_code(target, entry, reader, cache if cache is not None else {})
        except TreeClosed as exc:
            return _closed(str(exc))
        except Exception:  # fail closed on anything the parser cannot take
            return _closed("the file could not be read as a whole")
        if seen:
            return BatchGuard(True, seen, f"Test didn't run and {_CHANGED[seen]}.")
    if altered:
        return BatchGuard(True, BLOCK_CHANGED, f"Test didn't run and {_CHANGED[BLOCK_CHANGED]}.")
    return BatchGuard(False, "", "")


def batch_guards(
    packet: Mapping[str, Any] | None, targets: Sequence[Any], reader: SideReader | None = None
) -> dict[str, dict[str, Any]]:
    """``batch_guard`` per quick-check target id (other parts are not batched, so they carry none).

    ``reader`` gives the text of a file on each side; with it every part is also read against the whole file.
    """

    cache: dict[str, Any] = {}
    files = {
        str(item.get("path") or ""): item for item in (packet or {}).get("files") or () if isinstance(item, Mapping)
    }
    with _scope():  # every memo lives for this call only: no source text outlives the request that read it
        return {
            str(target.target_id): guard_for_target(target, files.get(str(target.path)), reader, cache).payload()
            for target in targets
            if target.answer.category == "quick_check"
        }


__all__ = [
    "ARITHMETIC", "BLOCK_CHANGED", "BLOCK_MOVED", "COMPARISON", "LOOP_BOUND", "UNVERIFIABLE", "BatchGuard",
    "batch_guards", "classify_change", "classify_hunk", "guard_for_target",
]  # fmt: skip
