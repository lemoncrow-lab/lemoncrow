"""Read MCP handler registration and request-shape orchestration.

The heavy file/symbol readers remain host-composed hooks while this module owns
the stable public tool surface, batching, alias recovery, and per-call savings
aggregation. It intentionally does not import the legacy mcp_server module.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool
from lemoncrow.gateway.tools.state import tool_call_tokens_saved as _tool_call_tokens_saved


@dataclass(frozen=True, slots=True)
class ReadHandlerHooks:
    split_file_opts: Callable[..., Any]
    op_node: Callable[..., Any]
    parse_symbol: Callable[..., Any]
    smart_read_single: Callable[..., dict[str, Any]]
    apply_batch_read_budget: Callable[..., list[str]]


_HooksFactory = Callable[[], ReadHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_read_handler_hooks(factory: _HooksFactory) -> None:
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> ReadHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("read handler hooks are not configured")
    return factory()


def _split_file_opts(*args: Any, **kwargs: Any) -> Any:
    return _hooks().split_file_opts(*args, **kwargs)


def _op_node(*args: Any, **kwargs: Any) -> Any:
    return _hooks().op_node(*args, **kwargs)


def _parse_symbol(*args: Any, **kwargs: Any) -> Any:
    return _hooks().parse_symbol(*args, **kwargs)


def _smart_read_single(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _hooks().smart_read_single(*args, **kwargs)


def _apply_batch_read_budget(*args: Any, **kwargs: Any) -> list[str]:
    return _hooks().apply_batch_read_budget(*args, **kwargs)


_SYMBOL_LIKE_RE = re.compile(r"^[A-Za-z_][\w.]*$")


def recover_read_stray_query(args: dict[str, Any], known_params: frozenset[str]) -> dict[str, Any]:
    """Fold a stray ``query``/``q`` on ``read`` into a usable shape.

    ``read`` has no ``query`` param -- but models that just called ``code_search``
    habitually re-send ``query`` on the follow-up read, which would cost a full
    unknown-argument round-trip that throws away the whole call. If the call
    already names a real target (files/symbol/path/range/...), the query is a
    stray label -> drop it and read the target. If the query is the ONLY locator
    and looks like an identifier, promote it to a symbol read (best-effort
    "show me this named thing"); otherwise just drop it.
    """
    if not isinstance(args, dict):
        return args
    stray_keys = [k for k in ("query", "q") if k in args and k not in known_params]
    if not stray_keys:
        return args
    out = {k: v for k, v in args.items() if k not in stray_keys}
    has_target = any(
        out.get(k) not in (None, "", [])
        for k in ("files", "symbol", "path", "filePath", "file_path", "range", "start_line", "offset")
    )
    if not has_target:
        val = args[stray_keys[0]]
        if isinstance(val, str) and _SYMBOL_LIKE_RE.match(val.strip()):
            out["symbol"] = val.strip()
    return out


@mcp_tool(
    name="read",
    hidden_params=(
        "path",
        "range",
        "start_line",
        "end_line",
        "full",
        "lines",
        "projection_kind",
        "format",
        "include_meta",
        "filePath",
        "offset",
        "limit",
        "force",
    ),
    description=(
        "Read files or exact symbols. Batch the paths/ranges you ALREADY need into ONE call: "
        "files=['a.py', 'b.py:L10-L20', 'c.py:full', 'd.py:head=50', 'e.py:tail=20', 'f.py:summary', 'g.py:outline']. "
        "Need, not might-need — a speculative :full costs more than the turn it saves. "
        ":Lx-Ly exact range; :full full source; :summary gist; :outline structure at any "
        "size (mutually exclusive). Whole file → ONE :full or wide range, never "
        "successive narrow ones. One function/test → symbol='name' or ['a','b'], not a "
        "whole-file read. Default line numbers are disk-accurate for editing; :full is "
        "for exact formatting only. Images (png/jpg/gif/webp/bmp ≤4MB) come back "
        "viewable — read to SEE them, don't write OCR/pixel code."
    ),
    param_aliases={
        "max_lines": "lines",
        "filePath": "path",
        "file_path": "path",
    },
    recover_args=recover_read_stray_query,
)
def tool_smart_read(
    path: str = "",
    range: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    full: bool = False,
    lines: int | None = None,
    include_meta: bool = False,
    files: Annotated[
        list[str | dict[str, Any]] | None,
        Field(description="Entries: path string or {path, range?, full?, ...} dict."),
    ] = None,
    symbol: Annotated[
        str | list[str] | None,
        Field(description="Exact symbol name or names."),
    ] = None,
    project_id: Annotated[
        str | None,
        Field(description="Optional registered project id returned by multi-project search."),
    ] = None,
    projection_kind: str | None = None,
    filePath: str = "",
    offset: int | None = None,
    limit: int | None = None,
    force: Annotated[
        bool,
        Field(
            description=(
                "Hidden: bypass within-session content dedup and re-emit the full "
                "body even if byte-identical to an earlier read this session."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Read a file (or batch of files) by path, or a single symbol by name.

    Symbol mode: read(symbol="name") or read(symbol="pkg.Class.method") returns the
    verbatim source of exactly that symbol — direct index lookup, no FTS expansion.
    Pass a list to fetch multiple: read(symbol=["Foo", "Bar.baz"]).

    File modes: outline (structure only — default for files >200 LOC), range
    (range="42-118", "L42-L118", or open-ended "L42-" for an exact line slice),
    full (small files, or any file with full=true), and compact (safe
    whitespace-only transformation of full reads — not byte-identical source).

    Prefer over native `Read`/`cat` unless the file is known to be small;
    outline mode typically saves 50-90% of tokens on large files. Re-read with
    full=true (or a range) before editing against an outline/compact view.

    BATCH: 2+ files you already know you need → one files=[{path, range?}, ...]
    call, not separate turns (each turn re-reads the whole conversation, ~$0.49
    on large context windows). Need, not might-need: a speculative :full costs
    more than the turn it saves, and an over-budget batch ships as outlines.

    Cross-tool: after editing a file via `edit`, don't re-read it — the edit
    response already confirms the change. When you don't yet know which file
    holds something, use `grep` with mode="with_content" to
    discover and read in one step instead of grep-then-read.
    """
    _ = force, project_id  # transport/dedup metadata is consumed before the handler
    if files is not None and symbol is not None:
        # Recovery (don't reject): both given means 'this symbol AS DEFINED IN this
        # file' -- the most precise read the model can ask for. Resolve the symbol
        # scoped to the given file instead of costing a turn on a validation error.
        _scope_path: str | None = None
        if files:
            _first = files[0]
            if isinstance(_first, str):
                _scope_path = _split_file_opts(_first)[0] or None
            elif isinstance(_first, dict):
                _scope_path = str(_first.get("path") or "") or None
        try:
            if isinstance(symbol, list):
                return {"symbols": [_op_node(**_parse_symbol(s), path=_scope_path) for s in symbol]}
            return _op_node(**_parse_symbol(symbol), path=_scope_path)
        except Exception:
            symbol = None
    # `filePath` is an accepted alias for `path` (host Read-tool habit); fold it
    # in before any dispatch so both name the same file.
    if not path and filePath:
        path = filePath
    # offset/limit: the built-in Read tool's paging params (offset = 1-based
    # first line, limit = line count). Fold into range so a vanilla-habit call
    # read(file_path=..., offset=.., limit=..) works unchanged.
    if range is None and start_line is None and offset is not None:
        first = max(1, offset)
        range = f"{first}-{first + limit - 1}" if limit is not None else f"{first}-"
    elif limit is not None and range is None and start_line is None and lines is None:
        lines = limit
    # start_line/end_line integer aliases → range string (wins over any suffix in path).
    if start_line is not None and range is None:
        end = end_line or start_line
        range = f"{start_line}-{end}"

    # Symbol addressing: direct index lookup, no file I/O or FTS expansion.
    if symbol is not None:
        if isinstance(symbol, list):
            return {"symbols": [_op_node(**_parse_symbol(s)) for s in symbol]}
        return _op_node(**_parse_symbol(symbol))

    # Batch mode: process each file spec and return aggregated results.
    if files is not None:
        results = []
        # Parallel to `results`: the path when the entry is a whole-file body
        # eligible for budget downgrade, None when the caller already narrowed it.
        entry_specs: list[str | None] = []
        batch_saved = 0
        for item in files:
            if isinstance(item, str):
                raw_path, item_range, item_expand, item_head, item_tail, item_summary, item_outline = _split_file_opts(
                    item
                )
                spec: dict[str, Any] = {"path": raw_path}
                if item_range is not None:
                    spec["range"] = item_range
                if item_expand:
                    spec["full"] = True
                if item_head is not None:
                    spec["lines"] = item_head
                if item_tail is not None:
                    spec["tail"] = item_tail
                if item_summary:
                    spec["summary"] = True
                if item_outline:
                    spec["outline"] = True
            else:
                spec = item  # dict passthrough for internal callers
            spec_path = str(spec.get("path") or spec.get("file_path") or spec.get("filePath") or "")
            if not spec_path:
                results.append({"error": "path is required in each files entry"})
                entry_specs.append(None)
                continue
            entry_specs.append(
                None
                if (
                    spec.get("range") is not None
                    or spec.get("outline")
                    or spec.get("summary")
                    or spec.get("lines") is not None
                    or spec.get("max_lines") is not None
                    or spec.get("tail") is not None
                )
                else spec_path
            )
            # _smart_read_single writes each file's saving to the thread-local
            # (last write wins). Capture it per file, stamp it on the entry so
            # per-entry baseline de-dup can zero an already-credited file, and
            # accumulate the batch total instead of letting the last file clobber
            # the rest. Reset between iterations so a stale value can't bleed in.
            _tool_call_tokens_saved.value = 0
            try:
                single = _smart_read_single(
                    path=spec_path,
                    range=spec.get("range"),
                    expand=bool(spec.get("full", full)),
                    max_lines=spec.get("lines", spec.get("max_lines", lines)),
                    tail_lines=spec.get("tail"),
                    include_meta=include_meta,
                    projection_kind=spec.get("projection_kind", projection_kind),
                    summary=bool(spec.get("summary", False)),
                    outline=bool(spec.get("outline", False)),
                )
                entry_saved = int(getattr(_tool_call_tokens_saved, "value", 0) or 0)
                if entry_saved > 0:
                    single["tokens_saved"] = entry_saved
                    batch_saved += entry_saved
                results.append(single)
            except Exception as exc:
                results.append({"path": spec_path, "error": str(exc)})
        downgraded = _apply_batch_read_budget(results, entry_specs, include_meta)
        _tool_call_tokens_saved.value = batch_saved
        batch_result: dict[str, Any] = {"files": results}
        if downgraded:
            batch_result["budget_downgraded"] = downgraded
            batch_result["notice"] = (
                f"{len(downgraded)} file(s) exceeded the batch read budget and were served as "
                "outlines. Re-read any of them with an explicit :Lx-Ly range (or :full) for the body."
            )
        # Batched reads collapse N would-be single-file read calls into 1 -- the
        # same honest cross-call credit the edit (distinct files) and sql
        # (batched queries) surfaces already get. Errored entries earned nothing.
        ok_entries = sum(1 for entry in results if isinstance(entry, dict) and not entry.get("error"))
        if ok_entries > 1:
            batch_result["calls_saved"] = ok_entries - 1
        return batch_result

    return _smart_read_single(
        path=path,
        range=range,
        expand=full,
        max_lines=lines,
        include_meta=include_meta,
        projection_kind=projection_kind,
    )


__all__ = [
    "ReadHandlerHooks",
    "configure_read_handler_hooks",
    "recover_read_stray_query",
    "tool_smart_read",
]
