"""FROZEN copy of the batch guard at 892bd488a (before block-chain moves), kept only as the old side of the
differential test in test_review_batch_guard_differential.py. Do not edit.

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
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

from lemoncrow.pro.capabilities.review.change_risk import _rows, safeguard_language

LOOP_BOUND = "loop_bound"
ARITHMETIC = "arithmetic"
COMPARISON = "comparison"
UNVERIFIABLE = "unverifiable"

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
    """A re-indented line whose enclosing block is not in the diff: it may have entered or left a loop."""


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


def _side(rows: Sequence[tuple[str, bool]], language: str) -> list[list[_Tok]]:
    """Logical lines: rows joined while brackets are open. A context row that cannot be read is left out."""

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
            elif tok.text in ")]}" and tok.kind == "punct" and stack:
                stack.pop()
            elif tok.text == ":" and tok.kind == "punct" and stack and stack[-1] == "[":
                kind = "slice"
            pending.append(replace(tok, kind=kind, mine=mine))
        depth = len(stack)
        if depth == 0 and pending:
            lines.append(pending)
            pending = []
    if pending:
        lines.append(pending)
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


def classify_hunk(old_rows: Sequence[tuple[str, bool]], new_rows: Sequence[tuple[str, bool]], language: str) -> str:
    """``loop_bound`` | ``arithmetic`` | ``comparison`` | ``""`` for one hunk's two sides (rows are ``(text, is_part_line)``).

    Raises ``_Unreadable`` for part lines it cannot tokenize.
    """

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

    found: set[str] = set()
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
    for name in (LOOP_BOUND, ARITHMETIC, COMPARISON):
        if name in found:
            return name
    if unresolved:
        raise _Unresolved
    return ""


def classify_change(old_rows: Sequence[str], new_rows: Sequence[str], language: str) -> str:
    """:func:`classify_hunk` for plain changed lines, every one of them the part's own."""

    return classify_hunk([(t, True) for t in old_rows], [(t, True) for t in new_rows], language)


@dataclass(frozen=True)
class BatchGuard:
    guarded: bool
    code: str  # "" | loop_bound | arithmetic | comparison | unverifiable
    reason: str

    def payload(self) -> dict[str, Any]:
        return {"guarded": self.guarded, "code": self.code, "reason": self.reason}


_CHANGED = {
    LOOP_BOUND: "the loop bound changed",
    ARITHMETIC: "the arithmetic changed",
    COMPARISON: "a comparison changed",
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


def guard_for_target(target: Any, entry: Mapping[str, Any] | None) -> BatchGuard:
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
    except _Unresolved:
        return _closed("a moved line's block is not in the diff")
    except Exception:  # fail closed on anything the reader cannot take
        return _closed("the changed text could not be read")
    for code in (LOOP_BOUND, ARITHMETIC, COMPARISON):
        if code in codes:
            return BatchGuard(True, code, f"Test didn't run and {_CHANGED[code]}.")
    return BatchGuard(False, "", "")


def batch_guards(packet: Mapping[str, Any] | None, targets: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """``batch_guard`` per quick-check target id (other parts are not batched, so they carry none)."""

    files = {
        str(item.get("path") or ""): item for item in (packet or {}).get("files") or () if isinstance(item, Mapping)
    }
    return {
        str(target.target_id): guard_for_target(target, files.get(str(target.path))).payload()
        for target in targets
        if target.answer.category == "quick_check"
    }


__all__ = [
    "ARITHMETIC", "COMPARISON", "LOOP_BOUND", "UNVERIFIABLE", "BatchGuard",
    "batch_guards", "classify_change", "classify_hunk", "guard_for_target",
]  # fmt: skip
