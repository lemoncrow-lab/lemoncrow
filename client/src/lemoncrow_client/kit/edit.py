"""The batch edit engine behind every ``edit`` tool.

The thin client's ``edit`` executor and the main package's ``edit`` handler both
call :func:`apply_edits`, so an edit lands the same way wherever it runs.

What the engine does, in order, for each descriptor of a batch:

* parses the target (``path:Lx-Ly``, ``:head=``, ``:tail=``, ``:full``,
  ``#cell=``) and confines it to the root, the allowed roots and away from
  :data:`PROTECTED_PARTS`;
* translates line ranges back to PRE-BATCH line numbers, so every range in one
  call refers to the file as the caller read it, and refuses loudly when an
  earlier hunk makes that ambiguous;
* locates an ``old_string`` anchor exactly, then with typography normalized,
  then with ``...`` placeholders, adapting indentation for inexact matches, and
  recognizes an edit that is already applied;
* handles notebook cells and whole-file rewrites;
* refuses Python that no longer parses, writes each touched file once and
  atomically, and on any failure rolls the whole batch back with a retry hint.

What needs the main package arrives through :class:`EditExtensions`: symbol and
projection edits, minified-view and fuzzy matching, reindexing and preserving a
large payload. Without a hook, the matching rung is skipped or the edit kind is
refused with a clear error.
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .fsio import atomic_write, read_text, restore_bytes

__all__ = [
    "NEW_ALIASES",
    "OLD_ALIASES",
    "PROTECTED_PARTS",
    "EditExtensions",
    "ProjectionResult",
    "SymbolTarget",
    "TargetSpec",
    "apply_edits",
    "normalize_edit_aliases",
    "parse_target",
    "require_content",
]

logger = logging.getLogger(__name__)

#: Directory names no edit may write into.
PROTECTED_PARTS: Final[frozenset[str]] = frozenset({".git", ".lemoncrow", "node_modules", ".venv"})

# Every spelling a model might use for the old/new pair, in priority order. The
# first key found wins; canonical ``old_string``/``new_string`` always wins over
# an alias.
OLD_ALIASES: Final[tuple[str, ...]] = (
    "old",
    "old_str",
    "oldStr",
    "old_text",
    "oldText",
    "oldString",
    "search",
    "find",
    "original",
    "before",
    "source",
)
NEW_ALIASES: Final[tuple[str, ...]] = (
    "new",
    "new_str",
    "newStr",
    "new_text",
    "newText",
    "newString",
    "replace",
    "replacement",
    "after",
    "target",
    "result",
    "content",
    "contents",
)

_SMART_QUOTES: Final[dict[int, str]] = str.maketrans(
    {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-"}
)

# Elided-body detection for the native full-file-rewrite recovery: a {path, new}
# whose body says "... existing code ..." is a fragment, not a complete file.
_ELISION_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"\.\.\.[^\n]{0,40}(existing|unchanged|omitted|rest of|snip|more code)"
    r"|(existing|unchanged|omitted|rest of|snip|more) code[^\n]{0,40}\.\.\.",
    re.IGNORECASE,
)

_ALL_WS: Final[re.Pattern[str]] = re.compile(r"\s+")
_TRAILING_COMMA_BEFORE_CLOSER: Final[re.Pattern[str]] = re.compile(r",([)\]\}])")
# Minimum stripped length before a contained new_string counts as "already
# applied" -- guards against trivially short coincidental matches.
_REFORMAT_NOOP_MIN_CHARS: Final[int] = 24
# A `new` payload at least this long is worth preserving on failure, so a retry
# never has to regenerate it.
_PRESERVE_PAYLOAD_MIN_CHARS: Final[int] = 2000


def normalize_edit_aliases(edit: Mapping[str, Any]) -> dict[str, Any]:
    """Promote any known spelling of old/new to ``old_string``/``new_string``.

    The tool advertises ``old``/``new``, but models often send ``oldText``,
    ``search``/``replacement`` and the like. Canonical names already present are
    never overwritten.
    """
    normalized = dict(edit)
    if "old_string" not in normalized:
        for key in OLD_ALIASES:
            if key in normalized:
                normalized["old_string"] = normalized[key]
                break
    if "new_string" not in normalized:
        for key in NEW_ALIASES:
            # Strings only: ``replace`` doubles as the whole-file flag, and a
            # bool must stay a flag rather than become the replacement content.
            if isinstance(normalized.get(key), str):
                normalized["new_string"] = normalized[key]
                break
    return normalized


def require_content(edits: Sequence[Mapping[str, Any]], *, sources: str = "new") -> None:
    """Refuse a batch whose plain edits carry no replacement content.

    A bare ``{path, old}`` must not silently delete the matched text. Structured
    kinds (symbol, projection) carry their own fields; replace/overwrite edits
    pass because the engine already refuses truncating a non-empty file.
    """
    if not edits:
        raise ValueError("edits must include at least one descriptor")
    for index, edit in enumerate(edits):
        if not isinstance(edit, Mapping) or edit.get("kind"):
            continue
        if "new_string" in edit or edit.get("replace") or edit.get("overwrite"):
            continue
        raise ValueError(f"edits[{index}] has no replacement content: provide {sources}")


@dataclass(frozen=True, slots=True)
class SymbolTarget:
    """Where a symbol edit resolved to, as the main package's index reports it."""

    scoped_file_path: str
    old_string: str
    new_string: str
    symbol_id: str
    #: The hook's own record of the resolution, handed back to ``after_symbol_edits``.
    handle: object = None


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """A projection edit applied to a file's content."""

    content: str
    hunks: tuple[tuple[int, int], ...]
    projection_kind: str


@dataclass(frozen=True, slots=True)
class EditExtensions:
    """Capabilities only the main package has. Every field is optional.

    ``minified_replace(content, path, old, new)`` and
    ``fuzzy_replace(scoped, old, new)`` return ``(new_content, line_start,
    line_end)`` or ``None`` when they do not apply; ``fuzzy_replace`` raises
    ``ValueError`` for a match below its confidence floor. Exceptions of a type
    in ``structured_errors`` must offer ``to_dict()`` and are reported verbatim.
    """

    resolve_symbol: Callable[[dict[str, Any], Path], SymbolTarget] | None = None
    after_symbol_edits: Callable[[list[SymbolTarget]], None] | None = None
    apply_projection: Callable[[dict[str, Any], str, Path], ProjectionResult] | None = None
    minified_replace: Callable[[str, str, str, str], tuple[str, int, int] | None] | None = None
    fuzzy_replace: Callable[[str, str, str], tuple[str, int, int] | None] | None = None
    after_write: Callable[[Path, list[Path]], None] | None = None
    preserve_payload: Callable[[str], str | None] | None = None
    structured_errors: tuple[type[Exception], ...] = ()


_NO_EXTENSIONS: Final[EditExtensions] = EditExtensions()


@dataclass(frozen=True)
class TargetSpec:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    cell: str | int | None = None
    whole_file: bool = False
    head_lines: int | None = None
    tail_lines: int | None = None
    to_end: bool = False
    # Explicit :minified suffix on a range edit: start_line/end_line are the
    # caller's MINIFIED-view line numbers, not disk lines. Only a caller with the
    # projection can translate them; the engine itself treats ranges as disk lines.
    minified: bool = False


def parse_target(raw_path: str) -> TargetSpec:
    """Split ``path[:suffix...][#cell=N]`` into the path and its selectors."""
    whole_file = False
    head_lines: int | None = None
    tail_lines: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    to_end = False
    minified = False
    parts = raw_path.split(":")
    while len(parts) > 1:
        token = parts[-1]
        if token in {"full", "full=true", "full=1"}:
            whole_file = True
        elif token in {"minified", "minified=true", "minified=1"}:
            minified = True
        elif token in {"summary", "summary=true", "summary=1", "outline", "outline=true", "outline=1"}:
            pass
        elif token.startswith("head="):
            try:
                head_lines = int(token[5:])
            except ValueError:
                break
        elif token.startswith("tail="):
            try:
                tail_lines = int(token[5:])
            except ValueError:
                break
        else:
            match = re.fullmatch(r"L?(\d+)(?:-L?(\d*))?", token, re.IGNORECASE)
            if match is None:
                break
            start_line = int(match.group(1))
            to_end = match.group(2) == ""
            end_line = int(match.group(2) or match.group(1))
        parts.pop()
    path = ":".join(parts)
    if "#cell=" in path:
        path, cell = path.split("#cell=", 1)
        return TargetSpec(path=path, cell=cell, whole_file=whole_file)
    return TargetSpec(
        path=path,
        start_line=None if whole_file else start_line,
        end_line=None if whole_file else end_line,
        whole_file=whole_file,
        head_lines=None if whole_file else head_lines,
        tail_lines=None if whole_file else tail_lines,
        to_end=False if whole_file else to_end,
        minified=minified,
    )


def _read_scope_for_edit(spec: TargetSpec, content: str) -> TargetSpec:
    """Translate read-only head/tail selectors into their equivalent line scope."""
    if spec.head_lines is None and spec.tail_lines is None and not spec.to_end:
        return spec
    line_count = len(content.splitlines())
    if spec.head_lines is not None:
        return TargetSpec(path=spec.path, start_line=1, end_line=min(line_count, max(spec.head_lines, 0)))
    if spec.to_end:
        return TargetSpec(path=spec.path, start_line=spec.start_line, end_line=line_count)
    assert spec.tail_lines is not None
    return TargetSpec(
        path=spec.path,
        start_line=max(1, line_count - max(spec.tail_lines, 0) + 1),
        end_line=line_count,
    )


def _resolve(root: Path, raw_path: str, allowed_roots: Sequence[Path] = ()) -> Path:
    spec = parse_target(raw_path)
    path = Path(spec.path)
    resolved = path if path.is_absolute() else root / path
    resolved = resolved.resolve()
    roots = [root, *allowed_roots]
    if not any(resolved == allowed or resolved.is_relative_to(allowed) for allowed in roots):
        raise ValueError(
            f"path escape denied: {raw_path} is outside the workspace root {root} — "
            "use the host's native tools for files outside the workspace"
        )
    if any(part in PROTECTED_PARTS for part in resolved.parts):
        raise ValueError(f"protected path denied: {raw_path}")
    return resolved


def _normalize_typography(text: str) -> str:
    return text.translate(_SMART_QUOTES)


def _strip_formatting(text: str) -> str:
    """Strip all whitespace and trailing commas so formatter rewraps compare equal."""
    return _TRAILING_COMMA_BEFORE_CLOSER.sub(r"\1", _ALL_WS.sub("", text))


def _placeholder_pattern(old_string: str) -> re.Pattern[str] | None:
    if "..." not in old_string and "<...>" not in old_string:
        return None
    escaped = re.escape(old_string)
    escaped = escaped.replace(re.escape("<...>"), r"[\s\S]{0,4000}").replace(re.escape("..."), r"[\s\S]{0,2000}")
    return re.compile(escaped)


def _leading_whitespace(line: str) -> str:
    match = re.match(r"\s*", line)
    return match.group(0) if match else ""


def _adapt_indentation(old: str, new: str, matched: str) -> str:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    matched_lines = matched.splitlines()
    if len(new_lines) <= 1 or not old_lines or not matched_lines:
        return new
    trailing_newline = "\n" if new.endswith("\n") else ""
    base_indent_text = _leading_whitespace(matched_lines[0]) if matched_lines else ""
    if not base_indent_text and len(old_lines) > 1:
        base_indent_text = _leading_whitespace(old_lines[1])
    if base_indent_text:
        result: list[str] = [new_lines[0]]
        consecutive_blanks = 0
        for line in new_lines[1:]:
            if not line.strip():
                consecutive_blanks += 1
                result.append(line)
            elif not line.startswith((" ", "\t")) and consecutive_blanks < 2:
                result.append(base_indent_text + line)
                consecutive_blanks = 0
            else:
                result.append(line)
                consecutive_blanks = 0
        new_lines = result
    old_indent = len(old_lines[0]) - len(old_lines[0].lstrip())
    matched_indent = len(matched_lines[0]) - len(matched_lines[0].lstrip())
    delta = matched_indent - old_indent
    if delta <= 0:
        return "\n".join(new_lines) + trailing_newline
    prefix = " " * delta
    return "\n".join((prefix + line if line.strip() else line) for line in new_lines) + trailing_newline


def _replace_in_scope(
    content: str,
    spec: TargetSpec,
    old_string: str,
    new_string: str,
    extensions: EditExtensions,
) -> tuple[str, int, int, str]:
    """Replace old_string with new_string, returning (new_content, line_start, line_end, match_mode).

    match_mode is one of: "noop", "exact", "normalized", "placeholder",
    "minified", "fuzzy". Raises ValueError when the string cannot be located
    with sufficient confidence.
    """
    lines = content.splitlines(keepends=True)
    start_offset = 0
    end_offset = len(content)
    if spec.start_line is not None:
        start = max(1, spec.start_line)
        end = min(len(lines), spec.end_line or start)
        start_offset = sum(len(line) for line in lines[: start - 1])
        end_offset = sum(len(line) for line in lines[:end])
    scoped = content[start_offset:end_offset]

    index = scoped.find(old_string)
    matched = old_string
    match_mode = "exact"
    if index != -1 and old_string and scoped.count(old_string) > 1:
        raise ValueError(
            "old_string is not unique within the resolved scope; add surrounding context to identify a single match"
        )
    if index == -1:
        normalized_content = _normalize_typography(scoped)
        normalized_old = _normalize_typography(old_string)
        normalized_index = normalized_content.find(normalized_old)
        if normalized_index != -1:
            index = normalized_index
            matched = scoped[index : index + len(old_string)]
            match_mode = "normalized"
    if index == -1:
        placeholder = _placeholder_pattern(old_string)
        if placeholder:
            placeholder_match = placeholder.search(scoped)
            if placeholder_match:
                index = placeholder_match.start()
                matched = placeholder_match.group(0)
                match_mode = "placeholder"
    if index != -1:
        # Exact matches replace verbatim: the caller's new_string is authoritative.
        # Indentation adaptation is only a courtesy for whitespace-divergent matches
        # (normalized/placeholder). Applying it to an exact match whose anchor
        # begins inside an indented block wrongly re-indents replacement lines that
        # legitimately dedent (e.g. a module-level constant inserted after a list
        # literal), turning a valid edit into a SyntaxError.
        if match_mode == "exact":
            replacement = new_string
        else:
            replacement = _adapt_indentation(old_string, new_string, matched)
        absolute = start_offset + index
        line_start = content[:absolute].count("\n") + 1
        # A trailing newline in the match terminates the last changed line rather
        # than opening a new one; counting it would push line_end one past the
        # real inclusive last-modified line (1-indexed inclusive convention).
        line_end = line_start + matched.count("\n")
        if matched.endswith("\n"):
            line_end -= 1
        return (
            content[:absolute] + replacement + content[absolute + len(matched) :],
            line_start,
            line_end,
            match_mode,
        )

    # Idempotency fallback: every locate rung missed old_string, but the edit
    # may simply have been applied already (stale retry). Only a non-blank
    # new_string that occurs exactly once is evidence of that; a blank or
    # repeated one is present by coincidence, and calling it applied would
    # report success for an edit that never happened.
    if old_string and new_string.strip() and scoped.count(new_string) == 1:
        found = scoped.find(new_string)
        absolute = start_offset + found
        line_start = content[:absolute].count("\n") + 1
        line_end = line_start + new_string.count("\n")
        return content, line_start, line_end, "noop"

    # Formatter-tolerant variant: a post-edit formatter may have rewrapped the
    # previously applied new_string, so compare with all whitespace stripped.
    if old_string and new_string:
        flat_new = _strip_formatting(new_string)
        if len(flat_new) >= _REFORMAT_NOOP_MIN_CHARS and flat_new in _strip_formatting(scoped):
            line = spec.start_line or 1
            return content, line, line, "noop"

    # Minified-view fallback: old_string may have been copied from a minified
    # read (comments and blank lines stripped, whitespace collapsed). Skipped for
    # line-scoped edits, whose disk line numbers are authoritative.
    if spec.start_line is None and extensions.minified_replace is not None:
        minified = extensions.minified_replace(content, spec.path, old_string, new_string)
        if minified is not None:
            minified_content, minified_start, minified_end = minified
            return minified_content, minified_start, minified_end, "minified"

    if extensions.fuzzy_replace is not None:
        fuzzy = extensions.fuzzy_replace(scoped, old_string, new_string)
        if fuzzy is not None:
            fuzzed, fuzzy_start, fuzzy_end = fuzzy
            prefix_lines = content[:start_offset].count("\n")
            return (
                content[:start_offset] + fuzzed + content[end_offset:],
                fuzzy_start + prefix_lines,
                fuzzy_end + prefix_lines,
                "fuzzy",
            )
    raise ValueError("old_string not found in file")


def _cell_index(cells: list[dict[str, Any]], target: object) -> int:
    if target is None:
        raise ValueError("notebook cell target is required")
    if target == "last":
        return len(cells) - 1
    index = int(str(target))
    if index < 0 or index >= len(cells):
        raise ValueError("notebook cell target out of range")
    return index


def _cell_source(cell: Mapping[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def _set_cell_source(cell: dict[str, Any], source: str) -> None:
    cell["source"] = source
    if cell.get("cell_type") == "code":
        cell["outputs"] = []
        cell["execution_count"] = None


def _apply_notebook_edit(notebook: dict[str, Any], spec: TargetSpec, edit: Mapping[str, Any]) -> None:
    cells = notebook.setdefault("cells", [])
    if not isinstance(cells, list):
        raise ValueError("notebook cells must be a list")
    action = edit.get("cell_action")
    if action in {"insert_after", "insert_before"}:
        index = _cell_index(cells, spec.cell)
        new_cell: dict[str, Any] = {
            "cell_type": edit.get("cell_type", "code"),
            "metadata": {},
            "source": edit.get("new_string", ""),
        }
        if new_cell["cell_type"] == "code":
            new_cell.update({"outputs": [], "execution_count": None})
        cells.insert(index + (1 if action == "insert_after" else 0), new_cell)
        return
    if action == "delete":
        del cells[_cell_index(cells, spec.cell)]
        return
    if action in {"move_after", "move_before"}:
        index = _cell_index(cells, spec.cell)
        target = _cell_index(cells, edit.get("cell_move_target"))
        cell = cells.pop(index)
        if index < target:
            target -= 1
        cells.insert(target + (1 if action == "move_after" else 0), cell)
        return
    if (edit.get("replace") or edit.get("overwrite")) and spec.cell is not None:
        index = _cell_index(cells, spec.cell)
        try:
            replacement = json.loads(str(edit.get("new_string", "")))
            if isinstance(replacement, dict) and "cell_type" in replacement:
                cells[index] = replacement
                return
        except ValueError:
            logger.debug("notebook replacement is not a JSON cell; treating it as source", exc_info=True)
        _set_cell_source(cells[index], str(edit.get("new_string", "")))
        return
    matches = [cell for cell in cells if str(edit.get("old_string", "")) in _cell_source(cell)]
    if len(matches) != 1:
        raise ValueError("old_string must match exactly one notebook cell")
    only = matches[0]
    _set_cell_source(
        only,
        _cell_source(only).replace(str(edit.get("old_string", "")), str(edit.get("new_string", "")), 1),
    )


def _build_retry_hint(
    root: Path,
    backups: Mapping[Path, bytes | None],
    edit: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build a retry_with hint so the caller can retry without a separate re-read turn.

    Sources the failing edit's own file (pre-edit backup when available, disk
    otherwise), locates the unique line containing the first non-blank line of
    old_string, and ships that exact region back so the model can correct
    old_string inline.
    """
    if not edit:
        return None
    old_string = str(edit.get("old_string") or "")
    raw_path = str(edit.get("file_path") or edit.get("path") or "")
    if not old_string or not raw_path:
        return None
    try:
        path = _resolve(root, raw_path)
    except Exception:
        return None

    payload = backups.get(path)
    if payload is not None:
        try:
            disk_content = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        try:
            disk_content = read_text(path)
        except (OSError, ValueError):
            return None

    # Already-applied detection against the whole file: a line-scoped edit may
    # miss content that a formatter moved or rewrapped outside the scope.
    new_string = str(edit.get("new_string") or "")
    if new_string:
        flat_new = _strip_formatting(new_string)
        if len(flat_new) >= _REFORMAT_NOOP_MIN_CHARS and flat_new in _strip_formatting(disk_content):
            return {
                "already_applied": True,
                "hint": (
                    "edit appears already applied — the file already contains new_string "
                    "(possibly reformatted); do not retry this edit"
                ),
            }

    old_lines = old_string.splitlines()
    first_anchor = next((line.strip() for line in old_lines if line.strip()), None)
    if not first_anchor:
        return None

    disk_lines = disk_content.splitlines(keepends=True)
    anchor_positions = [i for i, line in enumerate(disk_lines) if first_anchor in line]
    if len(anchor_positions) != 1:
        return None
    # Unique anchor -- ship a window the size of old_string plus two lines.
    n_lines = max(len(old_lines), 1)
    start = anchor_positions[0]
    end = min(len(disk_lines), start + n_lines + 2)
    excerpt = "".join(disk_lines[start:end])
    clean_path = re.sub(r":L\d+.*$", "", raw_path, flags=re.IGNORECASE)
    return {
        "path": f"{clean_path}:L{start + 1}-L{end}",
        "old_string": excerpt,
        "hint": "exact disk content at nearest anchor — replace old_string with this",
    }


def _parse_gate_message(
    path: Path,
    new_content: str,
    parse_err: SyntaxError,
    applied: Sequence[Mapping[str, Any]],
) -> str:
    """Build an actionable parse-gate error with the broken region inline.

    Without the snippet agents retry the identical edit (the failure is in the
    would-be content they never see), then defect to shell-based writes.
    """
    lineno = parse_err.lineno or 1
    lines = new_content.splitlines()
    lo = max(0, lineno - 6)
    hi = min(len(lines), lineno + 5)
    snippet = "\n".join(f"{i + 1}: {lines[i]}" for i in range(lo, hi))
    fuzzy_note = ""
    for entry in applied:
        entry_path = re.sub(r":L\d+(-L\d+)?$", "", str(entry.get("path", "")), flags=re.IGNORECASE).split("#")[0]
        mode = entry.get("match_mode")
        if entry_path.endswith(path.name) and mode in ("normalized", "placeholder", "fuzzy"):
            fuzzy_note = (
                f" (old_string matched via {mode} mode — it may have anchored at the"
                " wrong spot or covered less text than intended)"
            )
            break
    return (
        f"post-edit parse error in {path.name} at line {lineno}: {parse_err.msg}"
        f" — edit rolled back{fuzzy_note}. Would-be content around the error:\n{snippet}\n"
        "Do NOT resend the same edit. Extend old_string to cover the full region you are"
        " replacing (e.g. the entire block through its closing brace); scope with"
        ' "file.py:L10-L20" to disambiguate, or rewrite the whole file with replace=true.'
    )


def _ledger_forward_shift(events: Sequence[tuple[int, int, int]], pre_line: int) -> int:
    """Line delta to add to a PRE-BATCH line number so it points at the same
    content in the current (mid-batch) file, given prior splices to that file.
    Splices whose region ends above ``pre_line`` shift it by their net delta."""
    return sum(delta for _pre_start, pre_end, delta in events if pre_end < pre_line)


def _ledger_pre_batch_span(
    events: Sequence[tuple[int, int, int]], cur_start: int, cur_end: int
) -> tuple[int, int] | None:
    """Reverse-translate a CURRENT-frame line span back to pre-batch line numbers
    so a content splice can be recorded in the pre-batch ledger.

    Returns ``None`` when the span overlaps a region an earlier splice inserted or
    rewrote (a chained match): such a span has no pre-batch coordinate, so the
    file must be poisoned for later range edits rather than guessed at.
    """
    shift = 0
    for pre_start, pre_end, delta in sorted(events, key=lambda event: event[0]):
        new_len = (pre_end - pre_start + 1) + delta
        region_start = pre_start + shift  # first current line the splice occupies
        region_end = region_start + new_len - 1  # inclusive; < start when new_len == 0
        if new_len > 0 and cur_start <= region_end and cur_end >= region_start:
            return None  # span lands inside earlier-inserted text -- untranslatable
        if region_end < cur_start:
            shift += delta
    return cur_start - shift, cur_end - shift


def _range_label(spec: TargetSpec) -> str:
    if spec.start_line == spec.end_line:
        return f":L{spec.start_line}"
    return f":L{spec.start_line}-L{spec.end_line}"


def _restore_backups(backups: Mapping[Path, bytes | None]) -> None:
    """Put every touched file back, skipping any whose bytes never changed.

    Most failures happen before the write phase, so skipping unchanged files
    means a failed batch usually touches nothing at all.
    """
    for path, payload in backups.items():
        try:
            if (path.read_bytes() if path.exists() else None) == payload:
                continue
        except OSError:
            pass  # unreadable now: restore unconditionally
        restore_bytes(path, payload)


def apply_edits(
    edits: Sequence[Mapping[str, Any]],
    *,
    root: Path,
    allowed_roots: Sequence[Path] = (),
    atomic: bool = True,
    extensions: EditExtensions | None = None,
) -> dict[str, Any]:
    """Apply a batch of edits in memory, then write each touched file once.

    Returns ``{"applied": [...], "failed": [...], "rolled_back": bool}`` plus
    ``"writes"`` when files were written. With ``atomic`` (the default) any
    failure restores every touched file, so a batch lands whole or not at all.
    """
    ext = extensions or _NO_EXTENSIONS
    root = Path(root).resolve()
    backups: dict[Path, bytes | None] = {}
    file_state: dict[Path, str] = {}
    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    resolved_symbol_edits: list[SymbolTarget] = []
    # Range-edit coordinate ledger: callers copy :Lx-Ly from a read taken
    # BEFORE the batch, so every range in one call refers to PRE-BATCH line
    # numbers. Earlier range splices shift later lines; without translation a
    # batch of two range edits to one file silently replaces the wrong lines
    # (measured: L2 grows by one line -> a later L5-L5 hits pre-batch L4).
    # Per file: [(pre_start, pre_end, net_line_delta)] for applied range edits.
    range_ledger: dict[Path, list[tuple[int, int, int]]] = {}
    # Files POISONED for range edits this batch: touched by an edit whose line
    # shift the ledger can't track -- projection / notebook / whole-file replace,
    # or a content match that reverse-translates into earlier-inserted text
    # (chained). Their line numbering is no longer the pre-batch one and the
    # shift is untracked, so a later range edit would be ambiguous -- reject it
    # loudly instead of guessing. Clean, translatable content edits are NOT here;
    # they are recorded in range_ledger so later range edits translate across them.
    content_edited: set[Path] = set()
    current_edit: Mapping[str, Any] | None = None  # the edit in flight, for error hints
    current_edit_index = -1

    try:
        for current_edit_index, original_edit in enumerate(edits):  # noqa: B007 -- read by the except block
            edit: Mapping[str, Any] = original_edit
            current_edit = edit
            if str(edit.get("kind") or "") == "symbol":
                if ext.resolve_symbol is None:
                    raise ValueError("symbol edits need the LemonCrow code index; use old/new or a line range")
                symbol = ext.resolve_symbol(dict(edit), root)
                resolved_symbol_edits.append(symbol)
                edit = {
                    "file_path": symbol.scoped_file_path,
                    "old_string": symbol.old_string,
                    "new_string": symbol.new_string,
                }
                raw_path = symbol.scoped_file_path
            else:
                raw_path = str(edit.get("file_path") or edit.get("path") or "")
            if not raw_path:
                raise ValueError("file_path is required")
            spec = parse_target(raw_path)
            path = _resolve(root, raw_path, allowed_roots)
            content = file_state.get(path)
            if content is None:
                content = read_text(path) if path.exists() else ""
            if path not in backups:
                backups[path] = path.read_bytes() if path.exists() else None
            scope_spec = _read_scope_for_edit(spec, content)

            replace_whole = bool(edit.get("replace") or edit.get("overwrite") or spec.whole_file)
            # Native full-file rewrite: hosts' built-in edit tools train models to
            # send {path, new} for a whole-file write with no replace flag. When
            # the body is a plausible complete file -- no elision markers, not a
            # drastic shrink of the existing content -- apply it as replace=true
            # instead of burning a round-trip on "old_string is required".
            if not replace_whole and path.exists() and "new_string" in edit and not edit.get("old_string"):
                new_body = str(edit.get("new_string", ""))
                if (
                    scope_spec.start_line is None
                    and new_body.strip()
                    and not _ELISION_MARKER_RE.search(new_body)
                    and len(new_body) * 2 >= len(content)
                ):
                    replace_whole = True

            if path.suffix.lower() == ".ipynb" and not spec.whole_file:
                notebook = json.loads(content or '{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}')
                _apply_notebook_edit(notebook, spec, edit)
                file_state[path] = json.dumps(notebook, indent=2)
                applied.append({"path": raw_path, "kind": "notebook"})
                content_edited.add(path)
                continue

            if str(edit.get("kind") or "") == "projection":
                if ext.apply_projection is None:
                    raise ValueError(
                        "projection edits need the LemonCrow server's projection; use old/new or a line range"
                    )
                projected = ext.apply_projection(dict(edit), content, path)
                file_state[path] = projected.content
                content_edited.add(path)
                applied.append(
                    {
                        "path": raw_path,
                        "kind": "projection",
                        "projection_kind": projected.projection_kind,
                        "hunks": [
                            {"line_start": line_start, "line_end": line_end} for line_start, line_end in projected.hunks
                        ],
                    }
                )
                continue

            if replace_whole or (not path.exists() and not edit.get("old_string")):
                # replace=true replaces the WHOLE file. A line range only ever scopes
                # old_string matching, so replace+range is a contradiction: the
                # range would be silently dropped and the entire file replaced.
                # Reject it loudly rather than truncate what the caller meant to scope.
                if replace_whole and scope_spec.start_line is not None:
                    raise ValueError(
                        f"replace=true replaces the entire file and ignores the {_range_label(scope_spec)} line "
                        f"range on {spec.path!r}; drop the range to replace the whole file, or "
                        "use old_string/projection to edit just those lines"
                    )
                new_string = str(edit.get("new_string", ""))
                # An empty new_string would zero out a non-empty file (and an empty
                # file is valid Python, so the parse gate never catches it). Refuse
                # unless the caller is creating a new/empty file.
                if replace_whole and not new_string and content.strip():
                    raise ValueError(
                        f"overwrite=true with an empty new_string would truncate non-empty file "
                        f"{spec.path!r} to nothing; pass the full replacement content, or use "
                        "old_string to remove a specific region"
                    )
                file_state[path] = new_string
                applied.append({"path": raw_path, "kind": "replace"})
                content_edited.add(path)
                continue

            # Replacement edits (old_string or line-scoped) on a missing file are
            # always an error: the caller must use replace=true or omit old_string.
            if not path.exists():
                raise ValueError(
                    f"file {spec.path!r} does not exist — use replace=true or omit old_string to create a new file"
                )

            # Line-range direct replacement: a :Lx-Ly scope plus an explicit
            # new_string (even "" to delete those lines) replaces the range
            # verbatim, with no old_string. With old_string as well, fall through
            # to the normal scoped search within the narrowed range.
            if scope_spec.start_line is not None and "new_string" in edit and not edit.get("old_string"):
                if path in content_edited:
                    raise ValueError(
                        f"range edit {raw_path!r} follows an untrackable edit to the same file "
                        "in this batch (whole-file replace, projection/notebook, or a chained "
                        "match), so its pre-batch line numbers no longer resolve -- put range "
                        "edits first, use old/new, or split into a second call"
                    )
                pre_start = scope_spec.start_line
                pre_end = scope_spec.end_line or scope_spec.start_line
                # Translate pre-batch line numbers through earlier range splices
                # to this file: edits fully above shift us by their net delta;
                # an overlap is ambiguous and must fail loudly.
                shift = 0
                for event_start, event_end, event_delta in range_ledger.get(path, []):
                    if event_end < pre_start:
                        shift += event_delta
                    elif event_start <= pre_end:
                        raise ValueError(
                            f"range edit {raw_path!r} overlaps lines L{event_start}-L{event_end} already "
                            "replaced in this batch -- merge the two edits or split the call"
                        )
                lines = content.splitlines(keepends=True)
                if pre_start + shift > len(lines):
                    # A range past the end is almost always a stale line number;
                    # appending at EOF would put the text somewhere unintended.
                    pre_batch_lines = len(lines) - sum(delta for _, _, delta in range_ledger.get(path, []))
                    raise ValueError(
                        f"range L{pre_start} starts past the end of {spec.path} ({pre_batch_lines} lines); "
                        "re-read the file for current line numbers, or use old/new"
                    )
                lo = max(0, pre_start - 1 + shift)  # 0-indexed inclusive start
                hi = min(len(lines), pre_end + shift)  # 0-indexed exclusive end
                replacement_lines = str(edit.get("new_string", "")).splitlines(keepends=True)
                # The last replacement line must end with a newline so the
                # surrounding content stays separated after the splice.
                if replacement_lines and not replacement_lines[-1].endswith("\n"):
                    replacement_lines[-1] += "\n"
                range_ledger.setdefault(path, []).append((pre_start, pre_end, len(replacement_lines) - (hi - lo)))
                file_state[path] = "".join(lines[:lo] + replacement_lines + lines[hi:])
                applied.append(
                    {
                        "path": raw_path,
                        # Where the hunk LANDED (pre-batch line plus the net shift
                        # of earlier same-file splices), not the raw :Lx passed in.
                        "hunks": [{"line_start": pre_start + shift, "line_end": pre_end + shift}],
                        "match_mode": "range",
                    }
                )
                continue

            old_string = str(edit.get("old_string", ""))
            if not old_string:
                raise ValueError("old_string is required unless replace=true or creating a new file")
            # A :Lx-Ly scope paired with old_string narrows the search window by
            # line number. A file poisoned by an untrackable edit has no usable
            # translation, so reject the range scope loudly.
            if scope_spec.start_line is not None and path in content_edited:
                raise ValueError(
                    f"range edit {raw_path!r} ({_range_label(scope_spec)}) follows an untrackable edit to the same "
                    "file in this batch (whole-file replace, projection/notebook, or a chained match), "
                    "so its pre-batch line numbers no longer resolve -- drop the line range "
                    "(old_string is searched over the whole file), put range edits first, or "
                    "split into a second call"
                )
            # Translate a range scope through earlier splices so it points at the
            # right window in the mid-batch content, not the stale pre-batch one.
            effective_scope = scope_spec
            if scope_spec.start_line is not None and range_ledger.get(path):
                events = range_ledger[path]
                pre_end_line = scope_spec.end_line or scope_spec.start_line
                effective_scope = TargetSpec(
                    path=scope_spec.path,
                    start_line=scope_spec.start_line + _ledger_forward_shift(events, scope_spec.start_line),
                    end_line=pre_end_line + _ledger_forward_shift(events, pre_end_line),
                )
            new_content, line_start, line_end, match_mode = _replace_in_scope(
                content, effective_scope, old_string, str(edit.get("new_string", "")), ext
            )
            file_state[path] = new_content
            # Record this content splice in the pre-batch ledger so a later range
            # edit to the same file translates across it. A noop changed nothing;
            # a match inside earlier-inserted text has no pre-batch coordinate, so
            # poison the file for later range edits.
            if match_mode != "noop" and path not in content_edited:
                pre_span = _ledger_pre_batch_span(range_ledger.get(path, []), line_start, line_end)
                if pre_span is None:
                    content_edited.add(path)
                else:
                    range_ledger.setdefault(path, []).append(
                        (pre_span[0], pre_span[1], new_content.count("\n") - content.count("\n"))
                    )
            applied_entry: dict[str, Any] = {
                "path": raw_path,
                "hunks": [{"line_start": line_start, "line_end": line_end}],
                "match_mode": match_mode,
            }
            # The resulting lines around the change, so callers can confirm the
            # edit without a separate read turn.
            context_lines = new_content.splitlines()
            context_snippet = "\n".join(context_lines[max(0, line_start - 2) : min(len(context_lines), line_end + 3)])
            if match_mode == "noop":
                applied_entry["already_applied"] = True
                # Explicit note with the current content: stops a retry of an edit
                # that already succeeded on a prior call.
                applied_entry["note"] = (
                    f"already applied at L{line_start} — do NOT retry. Current content:\n{context_snippet}"
                )
            else:
                applied_entry["result"] = context_snippet
            if resolved_symbol_edits and raw_path == resolved_symbol_edits[-1].scoped_file_path:
                applied_entry["kind"] = "symbol"
                applied_entry["symbol_id"] = resolved_symbol_edits[-1].symbol_id
            applied.append(applied_entry)

        # Parse gate: every touched Python file must still parse before anything
        # is written. This catches structural corruption (e.g. an inexact match
        # that ate a neighboring function) without a separate lint turn.
        for path, new_content in file_state.items():
            if path.suffix == ".py":
                try:
                    ast.parse(new_content)
                except SyntaxError as parse_err:
                    raise ValueError(_parse_gate_message(path, new_content, parse_err, applied)) from parse_err

        for path, new_content in file_state.items():
            atomic_write(path, new_content)
        if file_state and ext.after_write is not None:
            try:
                ext.after_write(root, list(file_state))
            except Exception:
                logger.debug("post-write hook failed", exc_info=True)
        if resolved_symbol_edits and ext.after_symbol_edits is not None:
            ext.after_symbol_edits(resolved_symbol_edits)
        return {"applied": applied, "failed": [], "rolled_back": False, "writes": len(file_state)}
    except Exception as exc:
        logger.debug("edit batch failed", exc_info=True)
        structured = getattr(exc, "to_dict", None)
        if ext.structured_errors and isinstance(exc, ext.structured_errors) and callable(structured):
            failed.append(structured())
        else:
            # Identify which edit failed so the caller can fix it without
            # re-reading the whole response.
            edit_file = ""
            old_snippet = ""
            if current_edit is not None:
                failing_path = str(current_edit.get("file_path") or current_edit.get("path") or "")
                edit_file = parse_target(failing_path).path if failing_path else ""
                failing_old = str(current_edit.get("old_string", ""))
                if failing_old:
                    old_snippet = failing_old[:120] + ("…" if len(failing_old) > 120 else "")
            err: dict[str, Any] = {
                "error": str(exc),
                "edit_index": current_edit_index,
                **({"edit_file": edit_file} if edit_file else {}),
                **({"old_string_snippet": old_snippet} if old_snippet else {}),
            }
            # When old_string was not found, ship the nearest disk region so the
            # follow-up edit does not need a re-read.
            if "old_string not found" in str(exc) or "not found in file" in str(exc):
                hint = _build_retry_hint(root, backups, current_edit)
                if hint and hint.get("already_applied"):
                    err["already_applied"] = True
                    err["hint"] = hint["hint"]
                elif hint:
                    err["retry_with"] = hint
            # Preserve a large `new` payload so the retry never regenerates it.
            payload = str(current_edit.get("new_string") or "") if current_edit is not None else ""
            if (
                len(payload) >= _PRESERVE_PAYLOAD_MIN_CHARS
                and not err.get("already_applied")
                and ext.preserve_payload is not None
            ):
                with contextlib.suppress(Exception):
                    preserved = ext.preserve_payload(payload)
                    if preserved is not None:
                        err["new_preserved_at"] = preserved
                        err.setdefault(
                            "hint",
                            f"`new` content is preserved at {preserved}; update/retry with "
                            f"edit(new_file='{preserved}',...) instead of regenerating it",
                        )
            failed.append(err)
        if atomic:
            _restore_backups(backups)
            envelope: dict[str, Any] = {"applied": [], "failed": failed, "rolled_back": True}
            already = [str(entry.get("path")) for entry in applied if entry.get("already_applied")]
            if already:
                envelope["already_applied"] = already
                envelope["note"] = "already_applied edits were found on disk pre-rollback and remain in effect"
            return envelope
        for path, new_content in file_state.items():
            with contextlib.suppress(Exception):
                atomic_write(path, new_content)
        return {
            "applied": applied,
            "failed": failed,
            "rolled_back": False,
            "writes": len(file_state),
        }
