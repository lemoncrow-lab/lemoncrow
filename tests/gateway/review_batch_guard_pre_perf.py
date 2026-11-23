"""FROZEN COPY of batch_guard.py at 48def1d8f, before the RR-BATCHPERF caches and the match budget: the differential
proof in test_review_batch_guard_perf.py compares verdicts against it. Do not edit.

The Quick check batch guard: a part whose change touches arithmetic or a loop bound stays out of every batch.

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

import keyword
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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


class _Unreadable(Exception):
    pass


class _Unresolved(Exception):
    """A line whose enclosing block cannot be told: it may have entered or left a loop or another block."""

    def __init__(self, why: str = "a moved line's block is not in the diff") -> None:
        super().__init__(why)


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
        return []  # the body of a block comment
    return out


def _side(rows: Sequence[tuple[str, bool]], language: str, cut: list[str] | None = None) -> list[list[_Tok]]:
    """Logical lines: rows joined while brackets are open. A context row that cannot be read is left out.

    ``cut`` collects a note whenever a bracket is closed that the hunk never opened, or left open at its end: the
    statement around it is cut by the edge of the hunk.
    """

    lines: list[list[_Tok]] = []
    pending: list[_Tok] = []
    depth = 0
    stack: list[str] = []
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
                elif cut is not None:
                    cut.append("closed")
            elif tok.text == ":" and tok.kind == "punct" and stack and stack[-1] == "[":
                kind = "slice"
            pending.append(replace(tok, kind=kind, mine=mine))
        depth = len(stack)
        if depth == 0 and pending:
            lines.append(pending)
            pending = []
    if pending:
        lines.append(pending)
        if stack and cut is not None:
            cut.append("open")
    marked = [_mark_bindings(line) for line in lines]
    return _annotate_blocks(marked) if language == "python" else marked


def _block_key(line: Sequence[_Tok]) -> tuple[str, ...]:
    return tuple(tok.key for tok in line if tok.kind != "indent")


def _annotate_blocks(lines: list[list[_Tok]]) -> list[list[_Tok]]:
    """Give each Python line the headers of the blocks around it, as far as the diff shows them."""

    def indent_of(line: Sequence[_Tok]) -> int:
        return int(line[0].text[1:]) if line and line[0].kind == "indent" else 0

    out: list[list[_Tok]] = []
    for index, line in enumerate(lines):
        current = indent_of(line)
        chain: list[tuple[str, ...]] = []
        in_loop = False
        for back in range(index - 1, -1, -1):
            other = lines[back]
            if indent_of(other) < current and other and other[-1].text == ":":
                key = _block_key(other)
                chain.append(key)
                head = [t for t in key if t != "async"]
                in_loop = in_loop or bool(head and head[0] in _LOOP_WORDS)
                current = indent_of(other)
        unresolved = current > 0
        out.append([replace(tok, in_loop=in_loop, chain=tuple(chain), unresolved=unresolved) for tok in line])
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
        out.append(replace(tok, binding=binding))
    return out


def _flatten(lines: Sequence[Sequence[_Tok]]) -> list[_Tok]:
    out: list[_Tok] = []
    for number, line in enumerate(lines):
        out.extend(replace(tok, line=number) for tok in line)
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

    def ident(back: int) -> tuple[Any, ...]:
        header = headers[back]
        assert header is not None
        part = lines[back][: header[1]]
        mine = any(t.mine for t in part)
        place = 0
        for sibling in range(back - 1, -1, -1):  # lc-debt: quadratic in hunk size; index siblings if hunks grow
            if indents[sibling] < indents[back]:
                break
            other = headers[sibling]
            if indents[sibling] == indents[back] and other is not None:
                place += any(t.mine for t in lines[sibling][: other[1]]) == mine
        return (header[0], mine, place)

    for index, line in enumerate(lines):
        current = indents[index]
        chain: list[Any] = []
        for back in range(index - 1, -1, -1):
            if indents[back] < current and lines[back] and lines[back][-1].text == ":":
                chain.append(ident(back))
                current = indents[back]
        chain.append(("open", current))  # indentation still unexplained: the headers above are outside the diff
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
        seg.append(replace(tok, mine=mine))

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
    return _python_units(rows) if language == "python" else _script_units(rows)


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


def classify_hunk(old_rows: Sequence[tuple[str, bool]], new_rows: Sequence[tuple[str, bool]], language: str) -> str:
    """``loop_bound`` | ``arithmetic`` | ``comparison`` | ``block_moved`` | ``""`` for one hunk's two sides (rows are ``(text, is_part_line)``).

    Raises ``_Unreadable`` for part lines it cannot tokenize.
    """

    moved, undecided = "", None
    try:
        moved = _block_moves(old_rows, new_rows, language)
    except _Unresolved as exc:
        undecided = exc
    old_lines, new_lines = _cancel_moves(_side(old_rows, language), _side(new_rows, language))
    old_lines, new_lines = _cancel_reindents(old_lines, new_lines)
    old, new = _flatten(old_lines), _flatten(new_lines)
    matcher = SequenceMatcher(None, [tok.key for tok in old], [tok.key for tok in new], autojunk=False)
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed, added = old[i1:i2], new[j1:j2]
        if not any(tok.mine for tok in (*removed, *added) if tok.kind != "nl"):
            continue
        if not _significant([*removed, *added]) and not any(tok.kind == "indent" for tok in (*removed, *added)):
            continue
        changes.append((removed, added))

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


def _hunk_views(
    entry: Mapping[str, Any], spans: Sequence[Any]
) -> list[tuple[list[tuple[str, bool]], list[tuple[str, bool]]]] | None:
    """Per hunk the part touches, both sides as ``(text, is_part_line)``. ``None``: no diff text to read."""

    hunks = [hunk for hunk in entry.get("hunks") or () if isinstance(hunk, Mapping)]
    if not spans or not any(hunk.get("patch") for hunk in hunks):
        return None
    views = []
    for ordinal, hunk in enumerate(hunks):
        mine = [span for span in spans if span.hunk_ordinal == ordinal]
        if not mine or not hunk.get("patch"):
            continue
        old: list[tuple[str, bool]] = []
        new: list[tuple[str, bool]] = []
        for row in _rows(hunk):
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
    for i, removed in enumerate(gone):
        if any(removed & added for j, added in enumerate(came) if j != i):
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

    matcher = SequenceMatcher(None, [t for t, _ in old], [t for t, _ in new], autojunk=False)
    pairs: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace" and i2 - i1 == j2 - j1:
            pairs.extend((i1 + k, j1 + k) for k in range(i2 - i1) if old[i1 + k][1] and new[j1 + k][1])
    return pairs


def _reformat_only(old: Sequence[tuple[str, bool]], new: Sequence[tuple[str, bool]], language: str) -> bool:
    """Indentation moved but every statement the part touched still sits in the same blocks, all of them in view."""

    if language != "python":
        return True  # braces carry the blocks; indentation is looks only
    before, after = _python_units(old), _python_units(new)
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
    for old, new in views:
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
        matcher = SequenceMatcher(None, [t for t, _ in old], [t for t, _ in new], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
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
        statements = SequenceMatcher(None, [u.key for u in before], [u.key for u in after], autojunk=False)
        for tag, i1, i2, j1, j2 in statements.get_opcodes():
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
        try:
            cache[path] = tree.read_file(entry, path, reader, budget)
        except tree.TreeClosed as exc:
            cache[path] = exc
        except Exception:
            cache[path] = tree.TreeClosed("the file could not be read as a whole")
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
    try:
        views = _hunk_views(entry, target.spans)
        if views is None:
            return _closed("the changed lines were not available")
        codes = {classify_hunk(old, new, language) for old, new in views}
        _moved_between_hunks(views, language)
        altered = (
            ""
            if any(code in codes for code in (LOOP_BOUND, ARITHMETIC, COMPARISON, BLOCK_MOVED))
            else (_block_altered(views, language))
        )
    except _Unresolved as exc:
        return _closed(str(exc))
    except Exception:  # fail closed on anything the reader cannot take
        return _closed("the changed text could not be read")
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
    return {
        str(target.target_id): guard_for_target(target, files.get(str(target.path)), reader, cache).payload()
        for target in targets
        if target.answer.category == "quick_check"
    }


__all__ = [
    "ARITHMETIC", "BLOCK_CHANGED", "BLOCK_MOVED", "COMPARISON", "LOOP_BOUND", "UNVERIFIABLE", "BatchGuard",
    "batch_guards", "classify_change", "classify_hunk", "guard_for_target",
]  # fmt: skip
