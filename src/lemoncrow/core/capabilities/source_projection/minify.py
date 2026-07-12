"""Tree-sitter minified source projection (read view + mapped edits).

Unlike ``compact`` (regex whitespace cleanup), this builds the projection from
the concrete syntax tree: comments, blank lines and redundant whitespace are
removed while **newlines are preserved** as statement separators, so no
semicolon-insertion logic is needed and the projected text stays line-diffable.

The disk file is never minified. Edits expressed against the minified view are
matched in projected space and mapped back to original byte spans through the
segment mapping (``exact`` / ``whitespace`` / ``dropped`` segments emitted
natively during the token walk), then spliced into the untransformed source.

Fail-closed guards:

* the original parse must be error-free (otherwise comment classification is
  untrustworthy);
* every inter-token gap must be pure whitespace once dropped comments are
  excluded (hidden grammar bytes abort the projection);
* the minified output is re-parsed and must also be error-free;
* an edit span that would swallow a dropped comment is rejected with a hint
  to re-read the exact text.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

from lemoncrow.core.capabilities.prompt_compilation.tokens import (
    count_tokens as _count_tokens,
)
from lemoncrow.core.capabilities.source_projection.models import (
    ProjectionMapping,
    ProjectionSegment,
    SourceRange,
)
from lemoncrow.infra.code_intel.languages import language_by_name, language_for_path

_logger = logging.getLogger(__name__)

# Languages where leading indentation is syntax (or convention-critical) and
# must be preserved verbatim in the minified view.
_KEEP_INDENT: frozenset[str] = frozenset({"python", "yaml"})

# Markdown prose has no token structure worth minifying; whitespace is content.
_EXCLUDED_LANGS: frozenset[str] = frozenset({"markdown", "text", ""})

_COMMENT_TYPES: frozenset[str] = frozenset(
    {
        "comment",
        "line_comment",
        "block_comment",
        "doc_comment",
        "documentation_comment",
    }
)

# CST containers whose interior bytes are string-like content: emitted verbatim
# without descending, so inline whitespace inside them is never collapsed.
_ATOMIC_TYPES: frozenset[str] = frozenset(
    {
        "string",
        "f_string",
        "string_literal",
        "raw_string_literal",
        "raw_string",
        "interpreted_string_literal",
        "verbatim_string_literal",
        "interpolated_string_expression",
        "interpolated_string",
        "encapsed_string",
        "char_literal",
        "character_literal",
        "rune_literal",
        "template_string",
        "template_literal",
        "text_block",
        "heredoc",
        "heredoc_body",
        "heredoc_redirect",
        "nowdoc",
        "ansi_c_string",
        "regex",
        "regex_literal",
        "regex_pattern",
        "line_string_literal",
        "multi_line_string_literal",
        "string_expression",
    }
)

# Per-language node types treated as atomic leaves *in addition to*
# ``_ATOMIC_TYPES``. Scoped per language because these names carry whitespace
# (or unnamed grammar content) that is syntactically load-bearing only here:
#
# * html ``doctype`` wraps unnamed content (``<!DOCTYPE html>``) the named-leaf
#   walk would drop, leaving non-whitespace in the inter-token gap;
# * bash ``command`` nodes embed ``\``-newline line continuations between word
#   children, which must stay verbatim (collapsing them would change meaning).
#
# Keyed by canonical language name (``language_by_name(...).name``) so the type
# names are never made atomic globally.
_LANG_EXTRA_ATOMIC: dict[str, frozenset[str]] = {
    "html": frozenset({"doctype"}),
    "bash": frozenset({"command"}),
}


@dataclass(frozen=True)
class MinifiedProjectionResult:
    content: str
    original_tokens: int
    projected_tokens: int
    applied: bool
    reason: str = ""
    mapping: ProjectionMapping | None = None

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.projected_tokens)


class MinifiedEditError(ValueError):
    """Raised when an edit against the minified view cannot be applied safely."""

    def __init__(self, message: str, *, code: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def language_for_minify(path: str) -> str | None:
    """Resolve a file path to a minify-eligible language name, or ``None``."""
    lang = language_for_path(path)
    if lang is None or lang.name in _EXCLUDED_LANGS:
        return None
    return lang.name


def build_minified_projection(
    text: str,
    lang: str,
    *,
    path: str = "",
    include_mapping: bool = False,
    keep_comments: bool = False,
) -> MinifiedProjectionResult:
    """Return the tree-sitter minified projection for ``text``."""
    original_tokens = _count_tokens(text)

    def _skip(reason: str) -> MinifiedProjectionResult:
        return MinifiedProjectionResult(
            content=text,
            original_tokens=original_tokens,
            projected_tokens=original_tokens,
            applied=False,
            reason=reason,
        )

    normalized = (lang or "").strip().lower()
    if normalized in _EXCLUDED_LANGS:
        return _skip(f"language not minifiable: {normalized or 'unknown'}")
    if not text.strip():
        return _skip("empty source")

    parser = _parser_for(normalized)
    if parser is None:
        return _skip(f"no tree-sitter grammar for {normalized}")

    source_bytes = text.encode("utf-8")
    try:
        try:
            tree = parser.parse(source_bytes)
        except TypeError:
            # Some tree-sitter-language-pack grammars expect str, not bytes.
            tree = parser.parse(text)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return _skip("parse failed")
    root = tree.root_node
    if root is None or root.has_error:
        return _skip("original source has parse errors")

    record = language_by_name(normalized)
    extra_atomic = _LANG_EXTRA_ATOMIC.get(record.name if record else normalized, frozenset())
    tokens = _collect_tokens(root, extra_atomic=extra_atomic)
    if not tokens:
        return _skip("no tokens")

    built = _emit(
        source_bytes,
        text,
        tokens,
        keep_indent=normalized in _KEEP_INDENT,
        keep_comments=keep_comments,
    )
    if built is None:
        return _skip("non-whitespace bytes between tokens")
    minified, segments = built

    if minified == text:
        return _skip("already minimal")

    # Revalidate: the minified text must still parse cleanly.
    try:
        try:
            check = parser.parse(minified.encode("utf-8"))
        except TypeError:
            # Some tree-sitter-language-pack grammars expect str, not bytes.
            check = parser.parse(minified)
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return _skip("revalidation parse failed")
    if check.root_node is None or check.root_node.has_error:
        return _skip("minified output introduced parse errors")

    if "".join(_projected_slice(minified, seg) for seg in segments) != minified:
        return _skip("segment reconstruction mismatch")

    projected_tokens = _count_tokens(minified)
    if projected_tokens >= original_tokens:
        return _skip("no token saving")

    mapping = None
    if include_mapping:
        mapping = ProjectionMapping(
            version="v1",
            projection_kind="minified",
            path=path,
            lang=lang,
            source_length=len(text),
            projected_length=len(minified),
            source_hash=ProjectionMapping.digest(text),
            projected_hash=ProjectionMapping.digest(minified),
            source_line_offsets=_line_offsets(text),
            segments=tuple(segments),
        )

    return MinifiedProjectionResult(
        content=minified,
        original_tokens=original_tokens,
        projected_tokens=projected_tokens,
        applied=True,
        mapping=mapping,
    )


def resolve_minified_span(
    mapping: ProjectionMapping,
    *,
    projected_start: int,
    projected_end: int,
) -> SourceRange | None:
    """Map a projected span back to the source byte span it covers.

    Edges snap through synthesized whitespace separators to the nearest token
    boundary. Returns ``None`` when an edge cannot be anchored or when a
    dropped segment (removed comment) lies strictly inside the span — callers
    must treat that as fail-closed.
    """
    if projected_start < 0 or projected_end < projected_start or projected_end > mapping.projected_length:
        return None
    if _dropped_inside(mapping, projected_start, projected_end):
        return None

    if projected_start == projected_end:
        return _resolve_minified_insertion(mapping, projected_start)

    source_start: int | None = None
    for segment in mapping.segments:
        if segment.kind != "exact" or segment.projected_end <= projected_start:
            continue
        delta = max(0, projected_start - segment.projected_start)
        source_start = segment.source.start_offset + delta
        break

    source_end: int | None = None
    for segment in reversed(mapping.segments):
        if segment.kind != "exact" or segment.projected_start >= projected_end:
            continue
        delta = min(projected_end, segment.projected_end) - segment.projected_start
        source_end = segment.source.start_offset + delta
        break

    if source_start is None or source_end is None or source_end <= source_start:
        return None
    line_offsets = mapping.source_line_offsets or (0,)
    return SourceRange(
        start_offset=source_start,
        end_offset=source_end,
        start_line=_line_for_offset(line_offsets, source_start),
        end_line=_line_for_offset(line_offsets, max(source_start, source_end - 1)),
    )


def _resolve_minified_insertion(mapping: ProjectionMapping, projected_point: int) -> SourceRange | None:
    """Map a zero-length projected point to a zero-length source insertion span.

    A point that touches a lossy (whitespace/dropped) segment is rejected: the
    source location is then ambiguous between the dropped/collapsed bytes. Only
    points anchored inside an exact segment yield an unambiguous insertion.
    """
    for segment in mapping.segments:
        if segment.kind != "exact" and segment.projected_start <= projected_point <= segment.projected_end:
            return None
    for segment in mapping.segments:
        if segment.kind != "exact":
            continue
        if segment.projected_start <= projected_point <= segment.projected_end:
            offset = segment.source.start_offset + (projected_point - segment.projected_start)
            line_offsets = mapping.source_line_offsets or (0,)
            line = _line_for_offset(line_offsets, offset)
            return SourceRange(start_offset=offset, end_offset=offset, start_line=line, end_line=line)
    return None


def apply_minified_edit(
    content: str,
    lang: str,
    old_string: str,
    new_string: str,
    *,
    path: str = "",
) -> tuple[str, int, int]:
    """Replace ``old_string`` (as seen in the minified view) inside ``content``.

    Returns ``(new_content, line_start, line_end)`` against the original text.
    Raises :class:`MinifiedEditError` (codes: ``unsupported``, ``no_match``,
    ``ambiguous``, ``comment_inside_span``, ``resolve_failed``,
    ``revalidate_failed``).
    """
    if not old_string:
        raise MinifiedEditError("old_string is empty", code="no_match")
    result = build_minified_projection(content, lang, path=path, include_mapping=True)
    if not result.applied or result.mapping is None:
        raise MinifiedEditError(
            f"minified projection unavailable: {result.reason}",
            code="unsupported",
        )

    needle = old_string.strip("\n")
    count = result.content.count(needle)
    if count == 0:
        raise MinifiedEditError("old_string not found in minified view", code="no_match")
    if count > 1:
        raise MinifiedEditError(
            f"old_string matches {count} locations in minified view",
            code="ambiguous",
            hint="Add surrounding lines to make the match unique.",
        )

    index = result.content.index(needle)
    mapping = result.mapping
    if _dropped_inside(mapping, index, index + len(needle)):
        raise MinifiedEditError(
            "matched span contains a removed comment; editing it from the minified view would delete the comment",
            code="comment_inside_span",
            hint="Re-read with full=true and edit using the exact text.",
        )
    span = resolve_minified_span(mapping, projected_start=index, projected_end=index + len(needle))
    if span is None:
        raise MinifiedEditError(
            "could not map minified span back to source",
            code="resolve_failed",
            hint="Re-read with full=true and edit using the exact text.",
        )

    replacement = new_string.strip("\n") if old_string != needle else new_string
    updated = content[: span.start_offset] + replacement + content[span.end_offset :]

    parser = _parser_for((lang or "").strip().lower())
    if parser is not None:
        try:
            try:
                before_tree = parser.parse(content.encode("utf-8"))
            except TypeError:
                # Some tree-sitter-language-pack grammars expect str, not bytes.
                before_tree = parser.parse(content)
            try:
                after_tree = parser.parse(updated.encode("utf-8"))
            except TypeError:
                after_tree = parser.parse(updated)
            before_ok = not before_tree.root_node.has_error
            after_ok = not after_tree.root_node.has_error
        except Exception:
            logging.exception("Recovered from broad exception handler")
            raise MinifiedEditError(
                "could not revalidate the edited file; refusing to apply unchecked",
                code="revalidate_failed",
                hint="Re-read with full=true and edit using the exact text.",
            ) from None
        if before_ok and not after_ok:
            raise MinifiedEditError(
                "edit would introduce syntax errors in the original file",
                code="revalidate_failed",
                hint="Re-read with full=true and edit using the exact text.",
            )

    return updated, span.start_line, span.end_line


# --------------------------------------------------------------------------- #
# Internals                                                                   #
# --------------------------------------------------------------------------- #


def _parser_for(normalized_lang: str) -> Any | None:
    if not normalized_lang:
        return None
    record = language_by_name(normalized_lang)
    parser_name = record.parser_name if record else normalized_lang
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language

        # Use the standard tree-sitter binding (bytes parse, byte offsets);
        # the pack's native get_parser exposes an incompatible str-based API.
        # Parser instances are unsendable; create per call.
        return Parser(get_language(parser_name))
    except Exception:
        logging.exception("Recovered from broad exception handler")
        _logger.debug("tree-sitter parser unavailable for %s", normalized_lang)
        return None


def _collect_tokens(
    root: Any,
    *,
    extra_atomic: frozenset[str] = frozenset(),
) -> list[tuple[int, int, bool]]:
    """DFS over the CST yielding ``(start_byte, end_byte, is_comment)`` leaves.

    Atomic containers are treated as single leaves so their interior bytes are
    never touched:

    * ``_COMMENT_TYPES`` nodes are captured whole (``is_comment=True``) without
      descending, so structured doc comments (rust/scala) never leak their
      inner markers into the inter-token gap as non-whitespace;
    * ``_ATOMIC_TYPES`` string-like containers keep their interior whitespace;
    * ``extra_atomic`` adds per-language container types (e.g. html ``doctype``,
      bash ``command``) whose interior bytes are syntactically load-bearing.
    """
    tokens: list[tuple[int, int, bool]] = []
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        node_type = node.type
        if node_type in _COMMENT_TYPES:
            if node.end_byte > node.start_byte:
                tokens.append((node.start_byte, node.end_byte, True))
            continue
        if node_type in _ATOMIC_TYPES or node_type in extra_atomic or node.child_count == 0:
            if node.end_byte > node.start_byte:
                tokens.append((node.start_byte, node.end_byte, False))
            continue
        for i in range(node.child_count - 1, -1, -1):
            child = node.children[i]
            if child is not None:
                stack.append(child)
    tokens.sort()
    return tokens


def _emit(
    source_bytes: bytes,
    text: str,
    tokens: list[tuple[int, int, bool]],
    *,
    keep_indent: bool,
    keep_comments: bool,
) -> tuple[str, list[ProjectionSegment]] | None:
    """Re-emit tokens with minimal separators; returns ``None`` on unsafe gaps."""
    out: list[str] = []
    segments: list[ProjectionSegment] = []
    line_offsets = _line_offsets(text)
    byte_to_char = _byte_to_char_table(source_bytes, text)
    seg_index = 0
    projected_pos = 0
    prev_end = 0  # byte offset after last consumed token (kept or dropped)
    gap_start = 0  # byte offset where the current inter-token gap run began
    pending_dropped: list[tuple[int, int]] = []
    last_kept_end = 0
    emitted_any = False

    def _add_segment(kind: str, src_start: int, src_end: int, p_start: int, p_end: int) -> None:
        nonlocal seg_index
        if src_start >= src_end and p_start >= p_end:
            return
        c_start = byte_to_char[src_start] if byte_to_char else src_start
        c_end = byte_to_char[src_end] if byte_to_char else src_end
        if kind == "exact" and segments:
            last = segments[-1]
            if last.kind == "exact" and last.source.end_offset == c_start and last.projected_end == p_start:
                segments[-1] = ProjectionSegment(
                    segment_id=last.segment_id,
                    kind="exact",
                    source=SourceRange(
                        start_offset=last.source.start_offset,
                        end_offset=c_end,
                        start_line=last.source.start_line,
                        end_line=_line_for_offset(line_offsets, max(c_start, c_end - 1)),
                    ),
                    projected_start=last.projected_start,
                    projected_end=p_end,
                    exact=True,
                )
                return
        segments.append(
            ProjectionSegment(
                segment_id=f"seg:{seg_index:04d}",
                kind=kind,  # type: ignore[arg-type]
                source=SourceRange(
                    start_offset=c_start,
                    end_offset=c_end,
                    start_line=_line_for_offset(line_offsets, c_start),
                    end_line=_line_for_offset(line_offsets, max(c_start, c_end - 1)),
                ),
                projected_start=p_start,
                projected_end=p_end,
                exact=kind == "exact",
            )
        )
        seg_index += 1

    for start, end, is_comment in tokens:
        if start < prev_end:
            return None  # overlapping leaves — bail out
        gap_piece = source_bytes[prev_end:start]
        if gap_piece.strip():
            return None  # hidden non-whitespace bytes between tokens
        if is_comment and not keep_comments:
            pending_dropped.append((start, end))
            prev_end = end
            continue

        token_text = source_bytes[start:end].decode("utf-8", errors="replace")
        full_gap = source_bytes[gap_start:start]
        separator = ""
        if emitted_any:
            if b"\n" in full_gap:
                separator = "\n"
                if keep_indent:
                    line_start = source_bytes.rfind(b"\n", 0, start) + 1
                    indent = source_bytes[line_start:start]
                    if indent.strip():
                        return None
                    separator += indent.decode("utf-8", errors="replace")
            elif full_gap:
                separator = " "

        # Segments in source order: gap pieces and dropped comments interleave.
        cursor = gap_start if emitted_any else 0
        sep_assigned = False
        for d_start, d_end in pending_dropped:
            if cursor < d_start:
                p0 = projected_pos
                if not sep_assigned and separator:
                    out.append(separator)
                    projected_pos += len(separator)
                    sep_assigned = True
                _add_segment("whitespace", cursor, d_start, p0, projected_pos)
            _add_segment("dropped", d_start, d_end, projected_pos, projected_pos)
            cursor = d_end
        if cursor < start or (separator and not sep_assigned):
            p0 = projected_pos
            if not sep_assigned and separator:
                out.append(separator)
                projected_pos += len(separator)
            _add_segment("whitespace", cursor, start, p0, projected_pos)
        pending_dropped = []

        out.append(token_text)
        _add_segment("exact", start, end, projected_pos, projected_pos + len(token_text))
        projected_pos += len(token_text)
        emitted_any = True
        prev_end = end
        gap_start = end
        last_kept_end = end

    if not emitted_any:
        return None

    # Tail: trailing comments + whitespace map to nothing (plus final newline).
    for d_start, d_end in pending_dropped:
        if last_kept_end < d_start:
            _add_segment("whitespace", last_kept_end, d_start, projected_pos, projected_pos)
        _add_segment("dropped", d_start, d_end, projected_pos, projected_pos)
        last_kept_end = d_end
    tail = source_bytes[last_kept_end:]
    if tail.strip():
        return None
    if tail:
        p0 = projected_pos
        if b"\n" in tail:
            out.append("\n")
            projected_pos += 1
        _add_segment("whitespace", last_kept_end, len(source_bytes), p0, projected_pos)

    minified = "".join(out)
    return minified, segments


def _dropped_inside(mapping: ProjectionMapping, projected_start: int, projected_end: int) -> bool:
    return any(
        segment.kind == "dropped" and projected_start < segment.projected_start < projected_end
        for segment in mapping.segments
    )


def _projected_slice(projected_text: str, segment: ProjectionSegment) -> str:
    return projected_text[segment.projected_start : segment.projected_end]


def _byte_to_char_table(source_bytes: bytes, text: str) -> list[int] | None:
    """Byte-offset → char-offset table; ``None`` for pure-ASCII (identity)."""
    if len(source_bytes) == len(text):
        return None
    table = [0] * (len(source_bytes) + 1)
    byte_index = 0
    for char_index, char in enumerate(text):
        width = len(char.encode("utf-8"))
        for k in range(width):
            table[byte_index + k] = char_index
        byte_index += width
    table[len(source_bytes)] = len(text)
    return table


def _line_offsets(text: str) -> tuple[int, ...]:
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n" and index + 1 < len(text):
            offsets.append(index + 1)
    return tuple(offsets)


def _line_for_offset(line_offsets: tuple[int, ...], offset: int) -> int:
    return bisect_right(line_offsets, offset) or 1


__all__ = [
    "MinifiedEditError",
    "MinifiedProjectionResult",
    "apply_minified_edit",
    "build_minified_projection",
    "language_for_minify",
    "resolve_minified_span",
]
