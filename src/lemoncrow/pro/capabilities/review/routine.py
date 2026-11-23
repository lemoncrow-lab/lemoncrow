"""Routine eligibility by deterministic parser proof (spec §4.4).

Routine is granted only when a parser shows a change is semantically inert:

* **comment_only** -- every changed line differs only in comments, the code
  bytes are identical, and no changed comment is a directive or doc comment;
* **formatting_only** -- the parsed tree, ignoring layout and comments, is
  identical for the whole file (never for layout-significant Python).

Anything else, anything unparseable, and every category the spec lists as
"never Routine in v1" stays Understand. A missing proof is never a verdict.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

ROUTINE_PROOF_VERSION = 1
RoutineRule = Literal["comment_only", "formatting_only"]

_MAX_BYTES = 1 << 20
_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".java": "java",
}
_DISPLAY: dict[str, str] = {
    "python": "Python",
    "go": "Go",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "tsx": "TSX",
    "rust": "Rust",
    "java": "Java",
}
# Indentation is semantic: never a whitespace/formatting proof.
_LAYOUT_SIGNIFICANT = frozenset({"python"})
_COMMENT_TYPES = frozenset({"comment", "line_comment", "block_comment"})
# Grammars where a line terminator ends a statement or is forbidden at some positions (automatic semicolon insertion).
# A block comment holding a newline *is* a line terminator there, yet tree-sitter skips every comment, so the parsed
# shape alone cannot see it. Rust and Java ignore line breaks; Python is never a formatting proof.
_NEWLINE_SENSITIVE = frozenset({"javascript", "typescript", "tsx", "go"})
# ECMAScript restricted productions: no line terminator may follow these tokens, or stand before ``++``, ``--``, ``=>``.
# The TypeScript contextual keywords start a declaration only when no line terminator follows.
_TYPED = frozenset({"typescript", "tsx"})
_NO_BREAK_AFTER = frozenset(
    {
        "return", "throw", "break", "continue", "yield", "async", "await",
        "type", "declare", "abstract", "namespace", "module", "global", "interface", "enum", "readonly",
        "accessor", "using", "asserts",
    }
)  # fmt: skip
_NO_BREAK_BEFORE = frozenset({"++", "--", "=>"})
# TypeScript ends the expression at a line break before ``as`` or ``satisfies``: ``(x\nas T)`` is a syntax error there,
# although tree-sitter parses it (outside parentheses it does not, which is why only the parenthesised form shows).
_NO_BREAK_BEFORE_TYPED = frozenset({"as", "satisfies"})
# TypeScript reads ``T\n[]`` as two things where ``T[]`` is an array type, and rejects ``Map\n<string>`` where
# ``Map<string>`` is a type reference (``'>' expected``), so ``[`` or ``<`` after something that can end a type may not
# change sides of a line break. After an operator or an opening token it starts an expression and may.
_TYPE_CONTINUATION = frozenset({"[", "<"})
_TYPE_END = re.compile(r"[\w$)\]>}]$")
# Nodes whose every byte is content: a string, a raw string, a template, a regex, a character, a Rust macro's tokens. tree-sitter treats
# whitespace and comments inside some of them as extras (Rust ``r" ^ab$"``, a comment inside a regex), so walking their
# leaves would not see a change there. The shape holds each one as a single entry of its exact source bytes.
# Only ``string``, ``raw_string_literal``, ``regex`` and ``token_tree`` change a verdict with the
# grammar versions pinned today (removing any other entry leaves the literal differential at zero fail-open): the rest
# are defence in depth, because those grammars keep a literal's whitespace as a leaf of its own and an upgrade that
# moved it into an extra would otherwise reopen the hole silently.
_LITERAL_TYPES = frozenset(
    {
        "string",  # Python, JavaScript, TypeScript, TSX
        "template_string",
        "template_literal_type",
        "regex",
        "interpreted_string_literal",  # Go
        "raw_string_literal",  # Go, Rust
        "rune_literal",
        "string_literal",  # Rust, Java (text blocks too)
        "char_literal",
        "character_literal",  # Java
        # A Rust macro invocation's arguments (and an attribute's): ``stringify!(a+b)`` and a proc macro see the spacing,
        # and the grammar does not parse inside it. Deliberate fail-safe cost: a rustfmt-only change inside a macro or an
        # attribute gets no proof. An allowlist of macro names would be unsound, since any ``macro_rules!`` macro can
        # pass its arguments on to ``stringify!``.
        "token_tree",
    }
)
_LINE_TERMINATOR = re.compile(rb"[\n\r]|\xe2\x80[\xa8\xa9]")
_PROVABLE_KINDS = frozenset({"symbol", "hunk", "file"})
_NEVER_CATEGORIES = frozenset({"generated", "vendor", "config"})
_MIGRATION = re.compile(r"(^|/)(migrations?|alembic)/", re.IGNORECASE)
_DOC_PREFIXES = ("///", "//!", "/**", "/*!")
# lc-debt: conservative marker list; prose that merely mentions a marker loses
# its proof. Expand only from sampled false negatives (spec §4.4).
_DIRECTIVE_MARKERS = (
    "type:",
    "noqa",
    "pragma",
    "pylint:",
    "mypy:",
    "pyright:",
    "pyre-",
    "fmt:",
    "isort:",
    "ruff:",
    "nosec",
    "yapf:",
    "eslint",
    "@ts-",
    "prettier-ignore",
    "istanbul",
    "c8 ignore",
    "v8 ignore",
    "nolint",
    "clang-format",
    "fallthrough",
    "fall through",
    "@flow",
    "@jsx",
    "__pure__",
    "webpack",
    "<reference",
    "@license",
    "@preserve",
    "@generated",
    "safety:",
    "go:",
    "+build",
    "//export",
    "//line",
    "lint:",
    "jshint",
    "jslint",
    "global ",
    "biome-ignore",
    "deno-",
    "tslint",
    "sourcemappingurl",
    "sourceurl",
    "@vite",
    "frozen_string_literal",
    "noinspection",
    "nosonar",
)
_DIRECTIVE = re.compile("|".join(re.escape(marker) for marker in _DIRECTIVE_MARKERS) + r"|coding[:=]", re.IGNORECASE)


@dataclass(frozen=True)
class RoutineProof:
    path: str
    rule: RoutineRule
    language: str
    detail: str


@dataclass(frozen=True)
class _Comment:
    text: str
    start_row: int
    end_row: int


@dataclass(frozen=True)
class _Break:
    real: bool
    """A line terminator outside any comment sits before the token."""
    comment: bool
    """A comment holding a line terminator sits before the token (it ends the line for the parser's grammar)."""
    text: str
    """The token's own text, to tell the positions where a line break changes the meaning."""


@dataclass(frozen=True)
class _Side:
    lines: tuple[str, ...]
    code: tuple[str, ...]
    comment_only: tuple[bool, ...]
    comments: tuple[_Comment, ...]
    shape: tuple[tuple[str, str], ...]
    fixed: tuple[str, ...]
    """Per line: code plus any disallowed comment, which must keep its place."""
    skip: tuple[bool, ...]
    """Per line: nothing but allowed comments (free to appear, vanish or change)."""
    cgo: bool
    breaks: tuple[_Break, ...]
    """Per leaf token, in order: which kinds of line terminator stand between it and the token before it."""


def routine_language(path: str) -> str:
    """The proof language for *path*, or ``""`` when no proof is supported."""

    return _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "")


# A comment carrying a line terminator the parser did not end it on, or a Java
# unicode escape (decoded before lexing), can hide executable code.
_HIDDEN_TERMINATOR = re.compile(r"[\r\x85\u2028\u2029]|\\u+[0-9a-fA-F]{4}")


def _allowed_comment(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith(_DOC_PREFIXES) or stripped.startswith("#!"):
        return False
    if _HIDDEN_TERMINATOR.search(text):
        return False
    return _DIRECTIVE.search(stripped) is None


def _parse(language: str, source: bytes) -> Any | None:
    try:
        from tree_sitter_language_pack import get_parser

        # Parser instances are unsendable; one per call.
        tree = get_parser(language).parse(source)  # type: ignore[arg-type]
    except Exception:  # any parser failure means "no proof", never an error
        return None
    if tree.root_node.has_error:
        return None
    return tree


def _side(language: str, text: str) -> _Side | None:
    source = text.encode("utf-8")
    tree = _parse(language, source)
    if tree is None:
        return None
    comments: list[tuple[int, int, _Comment]] = []
    shape: list[tuple[str, str]] = []
    cgo = False
    breaks: list[_Break] = []
    gap_from = 0
    gap_real = gap_comment = False
    stack: list[tuple[Any, bool]] = [(tree.root_node, False)]
    while stack:
        node, closing = stack.pop()
        if closing:
            shape.append((")", ""))
            continue
        if node.type in _COMMENT_TYPES:
            start_row = node.start_point[0]
            end_row = node.end_point[0]
            if end_row > start_row and node.end_point[1] == 0:
                end_row -= 1
            raw = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
            gap_real = gap_real or bool(_LINE_TERMINATOR.search(source[gap_from : node.start_byte]))
            gap_comment = gap_comment or bool(_LINE_TERMINATOR.search(source[node.start_byte : node.end_byte]))
            gap_from = node.end_byte
            comments.append((node.start_byte, node.end_byte, _Comment(raw.strip(), start_row, end_row)))
            if not _allowed_comment(raw):
                # A directive's position is semantic: it stays in the shape.
                shape.append(("comment", raw.strip()))
            continue
        if node.type == "import_spec":
            path_node = node.child_by_field_name("path")
            if path_node is not None and source[path_node.start_byte : path_node.end_byte] == b'"C"':
                cgo = True  # the preamble comment is C source
        if node.child_count == 0 or node.type in _LITERAL_TYPES:
            leaf_text = source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
            gap_real = gap_real or bool(_LINE_TERMINATOR.search(source[gap_from : node.start_byte]))
            breaks.append(_Break(gap_real, gap_comment, leaf_text))
            gap_from, gap_real, gap_comment = node.end_byte, False, False
            shape.append((node.type, leaf_text))
            continue
        shape.append(("(", node.type))
        stack.append((node, True))
        stack.extend((child, False) for child in reversed(node.children))

    kept = bytearray()
    cursor = 0
    stripped_rows: set[int] = set()
    fixed_extra: dict[int, list[str]] = defaultdict(list)
    for start, end, comment in sorted(comments, key=lambda row: row[0]):
        kept += source[cursor:start]
        newlines = source[start:end].count(b"\n")
        # Keep line structure: a comment's own newlines survive its removal;
        # a one-line comment leaves a space so neighbours cannot fuse
        # (``return/* c */x`` is not ``returnx``).
        kept += b"\n" * newlines if newlines else b" "
        cursor = max(cursor, end)
        stripped_rows.add(comment.start_row)
        if not _allowed_comment(comment.text):
            fixed_extra[comment.start_row].append(comment.text)
    kept += source[cursor:]
    original = text.split("\n")
    code_lines = kept.decode("utf-8", errors="replace").split("\n")
    # Trailing whitespace is compared exactly (it can live inside a string);
    # only the gap a removed trailing comment leaves behind is trimmed.
    code = tuple(line.rstrip() if row in stripped_rows else line for row, line in enumerate(code_lines))
    comment_only = tuple(
        bool(before.strip()) and not after.strip() for before, after in zip(original, code_lines, strict=False)
    )
    fixed = tuple(line + "".join(f"\x00{text}" for text in fixed_extra.get(row, ())) for row, line in enumerate(code))
    # Blank lines are compared: one inside a multi-line string is content.
    skip = tuple(
        row < len(comment_only) and comment_only[row] and row not in fixed_extra for row in range(len(original))
    )
    return _Side(
        tuple(original),
        code,
        comment_only,
        tuple(item[2] for item in comments),
        tuple(shape),
        fixed,
        skip,
        cgo,
        tuple(breaks),
    )


def _same_structure(old: _Side, new: _Side, language: str) -> bool:
    """The two files parse to one structure, and no comment or line break has changed what a line break means.

    ``shape`` ignores layout and comments, which is right for spacing and wrong where a line terminator carries
    meaning: ``return /* c */ x`` and ``return /*\\n*/ x`` share a shape and differ in behavior. For the grammars
    with automatic semicolon insertion every token also keeps whether a line terminator stands before it. A
    difference is refused when a comment alone supplies the terminator on either side (the parse cannot see it),
    or at a position where the grammar forbids or reads a line break differently. Real line breaks anywhere else
    are plain reflow and pass.
    """

    if old.shape != new.shape:
        return False
    if language not in _NEWLINE_SENSITIVE:
        return True
    if len(old.breaks) != len(new.breaks):
        return False
    previous = ""
    for before, after in zip(old.breaks, new.breaks, strict=True):
        if before.text != after.text:
            return False
        had, has = before.real or before.comment, after.real or after.comment
        if had != has:
            comment_only = (before.comment and not before.real) or (after.comment and not after.real)
            restricted = (
                previous in _NO_BREAK_AFTER
                or before.text in _NO_BREAK_BEFORE
                or (before.text in _NO_BREAK_BEFORE_TYPED and language in _TYPED)
                or (before.text in _TYPE_CONTINUATION and language in _TYPED and _TYPE_END.search(previous) is not None)
            )
            if comment_only or restricted:
                return False
        previous = before.text
    return True


def _rows(target: ReviewTarget, side: str) -> set[int]:
    rows: set[int] = set()
    for span in target.spans:
        if span.side == side and span.start_line > 0:
            rows.update(range(span.start_line - 1, max(span.start_line, span.end_line)))
    return rows


def _region_comments(side: _Side, rows: set[int]) -> list[_Comment] | None:
    """Comments touching *rows*; ``None`` when one reaches outside them."""

    touching: list[_Comment] = []
    for comment in side.comments:
        extent = set(range(comment.start_row, comment.end_row + 1))
        if not extent & rows:
            continue
        if not extent <= rows:
            return None
        touching.append(comment)
    return touching


def _changed_comments(old: Sequence[_Comment], new: Sequence[_Comment]) -> list[str]:
    before = Counter(comment.text for comment in old)
    after = Counter(comment.text for comment in new)
    return list(((before - after) + (after - before)).elements())


def _block(side: _Side, start: int, end: int) -> list[str]:
    return [side.fixed[row] for row in range(start, min(end, len(side.fixed))) if not side.skip[row]]


def _comment_only(
    old: _Side,
    new: _Side,
    old_rows: set[int],
    new_rows: set[int],
    opcodes: Sequence[tuple[str, int, int, int, int]],
) -> bool:
    if not old_rows and not new_rows:
        return False
    # Position-aware: every changed block the target touches must carry the
    # same code -- and the same directives -- on both sides, so code cannot
    # move between blocks behind a comment edit.
    touched = False
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if not (set(range(i1, i2)) & old_rows or set(range(j1, j2)) & new_rows):
            continue
        touched = True
        if _block(old, i1, i2) != _block(new, j1, j2):
            return False
    if not touched:
        return False
    old_comments = _region_comments(old, old_rows)
    new_comments = _region_comments(new, new_rows)
    if old_comments is None or new_comments is None:
        return False
    changed = _changed_comments(old_comments, new_comments)
    return bool(changed) and all(_allowed_comment(text) for text in changed)


def prove_file(path: str, old_text: str, new_text: str, targets: Sequence[ReviewTarget]) -> dict[str, RoutineProof]:
    """Proofs for *targets* of one modified file; absent targets are unproven."""

    language = routine_language(path)
    if not language:
        return {}
    if len(old_text.encode("utf-8")) > _MAX_BYTES or len(new_text.encode("utf-8")) > _MAX_BYTES:
        return {}
    if old_text == new_text or "�" in old_text or "�" in new_text:
        return {}  # a byte change lost in decoding is not a proof of anything
    if language == "go" and ('import "C"' in old_text or 'import "C"' in new_text):
        return {}  # the cgo preamble comment is C source
    old = _side(language, old_text)
    new = _side(language, new_text)
    if old is None or new is None or old.cgo or new.cgo:
        return {}
    opcodes = difflib.SequenceMatcher(None, old.lines, new.lines, autojunk=False).get_opcodes()
    display = _DISPLAY[language]
    formatting = (
        language not in _LAYOUT_SIGNIFICANT
        and _same_structure(old, new, language)
        and all(_allowed_comment(text) for text in _changed_comments(old.comments, new.comments))
    )
    proofs: dict[str, RoutineProof] = {}
    for target in targets:
        if target.kind not in _PROVABLE_KINDS or target.path != path:
            continue
        if _comment_only(old, new, _rows(target, "old"), _rows(target, "new"), opcodes):
            proofs[target.target_id] = RoutineProof(
                path, "comment_only", language, f"Comment-only change; code is byte-identical ({display})"
            )
        elif formatting:
            proofs[target.target_id] = RoutineProof(
                path, "formatting_only", language, f"Formatting-only change; parsed structure is unchanged ({display})"
            )
    return proofs


class SideMemo:
    """The parsed form of one file, kept while several regions of that same file are proved.

    Proving k regions of one file against the same new text parses that text once, not k times. One entry only: a
    caller that walks its regions file by file holds one parse at a time.
    """

    def __init__(self) -> None:
        self._key: tuple[str, str] | None = None
        self._side: _Side | None = None

    def get(self, language: str, text: str) -> _Side | None:
        if self._key != (language, text):
            self._key, self._side = (language, text), _side(language, text)
        return self._side


def prove_region(
    path: str,
    old_text: str,
    new_text: str,
    old_span: tuple[int, int],
    new_span: tuple[int, int],
    new_side: SideMemo | None = None,
) -> RoutineProof | None:
    """Proof that one line region of a modified file changed only in comments or formatting.

    *old_span* and *new_span* are inclusive 1-based line ranges of the same region on each side. The proof
    is the one :func:`prove_file` grants a target, with one difference: ``formatting_only`` here also needs the
    whole of both files to parse and to have the same structure, so a change elsewhere in the file withholds it
    (fail closed). ``None`` means no proof, never a verdict.
    """

    language = routine_language(path)
    if not language:
        return None
    if len(old_text.encode("utf-8")) > _MAX_BYTES or len(new_text.encode("utf-8")) > _MAX_BYTES:
        return None
    if old_text == new_text or "�" in old_text or "�" in new_text:
        return None
    if language == "go" and ('import "C"' in old_text or 'import "C"' in new_text):
        return None
    old = _side(language, old_text)
    new = new_side.get(language, new_text) if new_side is not None else _side(language, new_text)
    if old is None or new is None or old.cgo or new.cgo:
        return None
    if not (0 < old_span[0] <= old_span[1] and 0 < new_span[0] <= new_span[1]):
        return None
    old_rows = set(range(old_span[0] - 1, old_span[1]))
    new_rows = set(range(new_span[0] - 1, new_span[1]))
    opcodes = difflib.SequenceMatcher(None, old.lines, new.lines, autojunk=False).get_opcodes()
    display = _DISPLAY[language]
    if _comment_only(old, new, old_rows, new_rows, opcodes):
        return RoutineProof(path, "comment_only", language, f"Comment-only change; code is byte-identical ({display})")
    if (
        language not in _LAYOUT_SIGNIFICANT
        and _same_structure(old, new, language)
        and all(_allowed_comment(text) for text in _changed_comments(old.comments, new.comments))
    ):
        return RoutineProof(
            path, "formatting_only", language, f"Formatting-only change; parsed structure is unchanged ({display})"
        )
    return None


def routine_proofs(
    targets: Sequence[ReviewTarget],
    packet: Mapping[str, Any] | None,
    read_text: Callable[[str, str], str | None],
) -> dict[str, RoutineProof]:
    """Target id -> proof for every provable target of this Snapshot.

    ``read_text(path, side)`` returns the exact immutable text of one side, or
    ``None`` when unavailable (which simply leaves the file unproven).
    """

    rows: dict[str, Mapping[str, Any]] = {}
    if isinstance(packet, Mapping):
        for row in packet.get("files") or ():
            if isinstance(row, Mapping) and isinstance(row.get("path"), str):
                rows[row["path"]] = row
    by_path: dict[str, list[ReviewTarget]] = defaultdict(list)
    for target in targets:
        if target.kind in _PROVABLE_KINDS:
            by_path[target.path].append(target)

    proofs: dict[str, RoutineProof] = {}
    for path, group in by_path.items():
        row = rows.get(path)
        if row is None or row.get("status") != "modified" or row.get("is_binary"):
            continue
        if str(row.get("category") or "") in _NEVER_CATEGORIES or _MIGRATION.search(path):
            continue
        if not routine_language(path):
            continue
        try:
            old_text = read_text(path, "old")
            new_text = read_text(path, "new")
        except (OSError, RuntimeError, ValueError):
            continue
        if old_text is None or new_text is None:
            continue
        proofs.update(prove_file(path, old_text, new_text, group))
    return proofs


__all__ = [
    "ROUTINE_PROOF_VERSION",
    "RoutineProof",
    "RoutineRule",
    "SideMemo",
    "prove_file",
    "prove_region",
    "routine_language",
    "routine_proofs",
]
