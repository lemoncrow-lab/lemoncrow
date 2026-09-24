"""``grep``, and the one offline fallback in the whole routing table.

**``grep`` runs the main package's search engine** (:mod:`lemoncrow_client.kit.search`)
and never shells out: a thin client that required a binary it does not ship,
and must not download, would have a tool that works on some laptops and not
others. What the engine may search is *the same walk* that builds the
manifest -- so what ``grep`` searches and what the server indexes are the same
set of files, ``.gitignore`` and all. A discrepancy between local search and
remote search is the kind of bug nobody reports and everybody distrusts. A
file named outright is searched even when ignored, as ``rg`` does. A result
too large to show inline is spilled to a local file the answer names.

**``read_from_disk`` is the single ``offline`` entry in the table.** ``read``
is a server tool: it resolves ranges, outlines and symbols from the index. When
the server never came up, the design's SessionStart section says the session
continues with "client-side tools only (``bash``, ``grep``, ``edit``, ``read``
from disk)". That is what this is -- a bounded file read, flagged
``degraded: true`` with the reason, and explicitly *not* an index: no symbol
resolution, no outline, no ranking, and nothing written to disk.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

from ..errors import AgentAction, ClientError, ErrorCode
from ..manifest import walk_worktree
from . import LocalContext, LocalResult, remap_aliases

__all__ = ["read_from_disk", "run_grep"]

_MAX_FILE_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_READ_LINES: Final[int] = 4_000

#: The read tool's range token (``L10-L20``, ``10-20``, ``L10``, ``L10-``).
_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"^L?(?P<start>\d+)(?:-L?(?P<end>\d*))?$", re.IGNORECASE)

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})


def run_grep(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """``grep`` through the kit's search engine, over this worktree's walk."""
    from ..kit.present import payload_text, strip_empty_values
    from ..kit.search import (
        GREP_MODE_ALIASES,
        GREP_PARAM_ALIASES,
        SearchHooks,
        SearchOutputMode,
        count_grep_hits,
        normalize_grep_mode,
        render_grep_text,
        search_workspace,
    )

    args = remap_aliases(arguments, GREP_PARAM_ALIASES)
    regex = _optional_text(args.get("regex"))
    hooks = SearchHooks(universe=_universe(context.repo_root), spill=_spill_to(context.config.state_dir))
    try:
        payload = search_workspace(
            path=_optional_text(args.get("path")) or ".",
            content_regex=regex,
            file_glob_patterns=_globs(args.get("glob")),
            output_mode=cast(
                SearchOutputMode,
                GREP_MODE_ALIASES.get(normalize_grep_mode(args.get("mode")), "file_paths_with_content"),
            ),
            lines_before=_int(args.get("before"), 0),
            lines_after=_int(args.get("after"), 0),
            ignore_case=_flag(args.get("i")),
            type=_optional_text(args.get("type")),
            file_limit=_optional_int(args.get("file_limit"), None),
            lines_per_file=_optional_int(args.get("lines_per_file"), 500),
            if_modified_since=_optional_text(args.get("if_modified_since")),
            max_line_length=1000,
            multiline=_flag(args.get("multiline")),
            summary=None if args.get("summary") is None else _flag(args.get("summary")),
            context_budget_tokens=_int(args.get("context_budget_tokens"), 2000),
            include_metadata=_flag(args.get("include_meta")),
            repo_root=context.repo_root,
            hooks=hooks,
        )
    except ValueError as exc:
        raise ClientError(ErrorCode.PAYLOAD_INVALID, str(exc), action=AgentAction.FIX_REQUEST) from exc
    if payload.get("isError"):
        raise ClientError(ErrorCode.PAYLOAD_INVALID, _first_text(payload), action=AgentAction.FIX_REQUEST)
    payload.pop("tokens_saved", None)
    payload = strip_empty_values(payload)
    hits = count_grep_hits(payload)
    rendered = render_grep_text(payload)
    if regex and hits == 0:
        rendered = f"{rendered}\nno matches" if rendered else "no matches"
    raw_blocks = payload.get("content")
    images = tuple(
        dict(block)
        for block in (raw_blocks if isinstance(raw_blocks, list) else ())
        if isinstance(block, dict) and block.get("type") == "image"
    )
    if rendered is None and not images:
        # The main package shows an unrenderable result as compact JSON too.
        rendered = payload_text(payload, None)
    text_blocks = ({"type": "text", "text": rendered},) if rendered else ()
    return LocalResult(
        content=(*text_blocks, *images),
        structured={"matches": hits, "spilled": "artifact" in payload},
    )


def _universe(repo_root: Path) -> Callable[[Path], frozenset[Path]]:
    """The files ``grep`` may search: the manifest walk of this worktree."""

    def build(_root: Path) -> frozenset[Path]:
        # read_limit=0 lists every file the manifest would, hashing none of them.
        walked = walk_worktree(repo_root, size_cap=0, read_limit=0)
        base = repo_root.resolve()
        return frozenset(base / walked_file.path for walked_file in walked.files)

    return build


def _spill_to(state_dir: Path) -> Callable[[dict[str, Any]], Path | None]:
    def spill(payload: dict[str, Any]) -> Path | None:
        from .shell import spill_text

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return spill_text(text, state_dir, kind="search", suffix=".json")

    return spill


def _first_text(payload: Mapping[str, Any]) -> str:
    blocks = payload.get("content")
    for block in blocks if isinstance(blocks, list) else ():
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return "grep failed"


def _optional_text(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _globs(raw: object) -> list[str] | None:
    if isinstance(raw, str):
        return [raw] if raw else None
    if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        return [str(entry) for entry in raw if str(entry)] or None
    return None


def _flag(raw: object) -> bool:
    """A boolean argument, accepting the spellings a JSON host sends."""
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    return bool(raw)


def _optional_int(raw: object, default: int | None) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return default
    return default


def _int(raw: object, default: int) -> int:
    value = _optional_int(raw, default)
    return default if value is None else value


def _read_searchable(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# The offline read fallback                                                   #
# --------------------------------------------------------------------------- #


def _entry_spec(raw: object) -> tuple[str, str | None, int | None, int | None]:
    """``(path, range, head, tail)`` for one ``files[]`` entry, in the read tool's grammar."""
    from ..kit.read_path import split_file_opts

    if isinstance(raw, Mapping):
        path = str(raw.get("path") or raw.get("file_path") or raw.get("filePath") or "")
        spelled_range = raw.get("range")
        head = raw.get("lines", raw.get("max_lines"))
        return (
            path,
            str(spelled_range) if spelled_range not in (None, "") else None,
            _optional_int(head, None),
            _optional_int(raw.get("tail"), None),
        )
    path, line_range, _full, head_count, tail_count, _summary, _outline = split_file_opts(str(raw))
    return path, line_range, head_count, tail_count


def _slice(lines: list[str], line_range: str | None, head: int | None, tail: int | None) -> tuple[list[str], int]:
    """Apply one entry's selector to a file's lines. Returns ``(lines, first line no)``.

    Only ranges, ``head`` and ``tail`` have an offline answer. A summary or an
    outline comes from the index, and inventing one from the bytes would be a
    different answer wearing the same name, so those read like ``full``.
    """
    if line_range is not None:
        match = _RANGE_RE.match(line_range.strip())
        if match is None:
            raise ClientError(
                ErrorCode.PAYLOAD_INVALID,
                f"unsupported read range {line_range!r}; use L10-L20",
                action=AgentAction.FIX_REQUEST,
            )
        start = max(1, int(match.group("start")))
        spelled_end = match.group("end")
        if spelled_end is None:
            end = start
        elif spelled_end == "":
            end = len(lines)
        else:
            end = int(spelled_end)
        return lines[start - 1 : min(end, start - 1 + _MAX_READ_LINES)], start
    if head is not None:
        return lines[: max(0, min(head, _MAX_READ_LINES))], 1
    if tail is not None:
        count = max(0, min(tail, _MAX_READ_LINES))
        chosen = lines[-count:] if count else []
        return chosen, max(1, len(lines) - len(chosen) + 1)
    return lines[:_MAX_READ_LINES], 1


def read_from_disk(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """Bounded file read, used only when the server never came up.

    Deliberately not an index. ``symbol=`` has no local answer, so it is
    refused by name rather than approximated with a text search that would look
    like a symbol lookup and behave like a grep.
    """
    if arguments.get("symbol"):
        raise ClientError(
            ErrorCode.SERVER_SESSION_UNAVAILABLE,
            "symbol lookup needs the server index; the offline read answers file ranges only",
            action=AgentAction.RETRY_LATER,
        )
    raw_files = arguments.get("files")
    if isinstance(raw_files, (str, Mapping)):
        raw_files = [raw_files]
    if not isinstance(raw_files, Sequence) or not raw_files:
        raise ClientError(
            ErrorCode.PAYLOAD_INVALID,
            "files must be a non-empty array",
            action=AgentAction.FIX_REQUEST,
        )
    blocks: list[str] = []
    for entry in raw_files:
        spelled, line_range, head, tail = _entry_spec(entry)
        if not spelled:
            continue
        target = context.resolve_read(spelled)
        if not target.is_file():
            blocks.append(f"## {spelled}\n[no such file]")
            continue
        text = _read_searchable(target)
        if text is None:
            blocks.append(f"## {spelled}\n[not readable as text]")
            continue
        chosen, first = _slice(text.splitlines(), line_range, head, tail)
        numbered = "\n".join(f"{first + offset}\t{line}" for offset, line in enumerate(chosen))
        label = spelled if Path(spelled).is_absolute() else context.relative(target)
        blocks.append(f"## {label}\n{numbered}")
    return LocalResult(
        content=({"type": "text", "text": "\n\n".join(blocks)},),
        degraded=True,
        degraded_reason="server_unreachable:read_from_disk",
        structured={"files": len(blocks), "source": "disk"},
    )
