"""Semantic-search MCP handler and range-scoping helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from lemoncrow_client.kit.read_path import split_read_range_suffix
from lemoncrow_client.kit.search import GREP_MODE_ALIASES, GREP_PARAM_ALIASES, normalize_grep_mode
from pydantic import Field

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool
from lemoncrow.gateway.tools.state import tool_call_tokens_saved

_FORMAT_FIELD = Field(
    default="auto",
    description=(
        "Output encoding: auto (default, unchanged), json (force raw JSON), or compact (N6-gated columnar encoding)."
    ),
)


@dataclass(frozen=True, slots=True)
class SearchHandlerHooks:
    workspace_root: Callable[[], Path]
    code_context_engine: Callable[[str], Any]
    run_native_grep: Callable[..., dict[str, Any]]
    grep_badge_provider: Callable[[str, list[str]], str | None]
    apply_search_verdict: Callable[..., dict[str, Any]]
    count_grep_hits: Callable[[dict[str, Any]], int]
    check_repeat_query: Callable[[str], bool]
    workspace_code_router: Callable[[str], Any]
    resolve_query_as_existing_file: Callable[..., str | None]
    lean_code_search_view: Callable[..., dict[str, Any]]
    whole_file_fallback_section: Callable[..., dict[str, Any] | None]
    outline_lean_view: Callable[..., dict[str, Any]]
    attach_code_search_savings: Callable[..., dict[str, Any]]
    observe_code_search_evidence: Callable[..., dict[str, Any] | None]
    code_search_engine_max_files: int


_HooksFactory = Callable[[], SearchHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_search_handler_hooks(factory: _HooksFactory) -> None:
    """Install process composition used by semantic search."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> SearchHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("search handler hooks are not configured")
    return factory()


def scope_search_matches_to_range(payload: dict[str, Any], line_range: tuple[int, int]) -> None:
    """Restrict ranked-search matches to snippets overlapping [lo, hi].

    A "path:Lx-Ly" search scopes results to that line window. Snippets carry
    line_start/line_end; matches with no overlapping snippet are dropped. Matches
    lacking snippet line data are kept (they cannot be filtered).
    """
    lo, hi = line_range
    matches = payload.get("matches")
    if not isinstance(matches, list):
        return
    kept: list[dict[str, Any]] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        snippets = match.get("snippets")
        if isinstance(snippets, list) and snippets:
            in_window = [
                snip
                for snip in snippets
                if isinstance(snip, dict)
                and int(snip.get("line_start", 0) or 0) <= hi
                and int(snip.get("line_end", snip.get("line_start", 0)) or 0) >= lo
            ]
            if not in_window:
                continue
            match = {**match, "snippets": in_window}
        kept.append(match)
    payload["matches"] = kept
    payload["match_paths"] = [str(match.get("path")) for match in kept if isinstance(match, dict) and match.get("path")]


@mcp_tool(
    name="search",
    description=(
        "Semantic/embedding code search: relevance-ranked snippets for a natural-language query. "
        "Hidden until an embedding backend is configured; deterministic regex/glob/symbol/map search "
        "lives on `grep`."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language ranked search query.",
            },
            "path": {
                "type": "string",
                "default": ".",
                "description": "Workspace-relative file or directory; a single file may carry ':Lx-Ly' to scope results.",
            },
            "limit": {
                "type": "integer",
                "default": 10,
                "description": "Maximum number of ranked files to return.",
            },
        },
        "required": ["query"],
    },
    param_aliases={"max_files": "limit"},
)
def tool_smart_search(
    query: Annotated[
        str | None,
        Field(description="Natural-language ranked search query."),
    ] = None,
    path: Annotated[
        str,
        Field(
            description=(
                "Workspace-relative file or directory to search. A single file may carry a "
                "':Lx-Ly' suffix (e.g. 'store.py:L60-L100') to scope ranked results to that line range."
            ),
        ),
    ] = ".",
    limit: Annotated[
        int,
        Field(description="Maximum number of ranked files to return."),
    ] = 10,
    max_chars_per_file: Annotated[
        int,
        Field(description=("Cap the returned characters per ranked file before the overall token budget is applied.")),
    ] = 2000,
    include_outline: Annotated[
        bool,
        Field(description="Include outline metadata for ranked files when the backend can provide it."),
    ] = True,
    budget_tokens: Annotated[
        int,
        Field(description="Total token budget for ranked search output."),
    ] = 2000,
    include_meta: Annotated[
        bool,
        Field(description="Include backend/cache metadata fields in the response."),
    ] = False,
    format: Annotated[str, _FORMAT_FIELD] = "auto",
) -> dict[str, Any]:
    """Semantic/embedding ranked search over code and docs (hidden until embeddings are wired up).

    Returns relevance-ranked snippets for a natural-language `query`, with read/context
    follow-up handoffs. Deterministic regex/glob/symbol-locate/repo-map search lives on `grep`.
    """
    hooks = _hooks()
    # A "path:Lx-Ly" suffix scopes ranked results to a line window of one file.
    line_range: tuple[int, int] | None = None
    path, suffix_range = split_read_range_suffix(path)
    if suffix_range is not None:
        lo_text, _, hi_text = suffix_range.partition("-")
        lo = int(re.sub(r"\D", "", lo_text) or 0)
        hi = int(re.sub(r"\D", "", hi_text) or 0) if hi_text else lo
        if lo:
            line_range = (lo, hi or lo)
    if query is None:
        raise ValueError("query is required for semantic search; use grep for regex/glob/symbol search")
    from lemoncrow.pro.capabilities.grounded_loop.search_first import search_first

    workspace_root = hooks.workspace_root()

    def indexed_search(
        *,
        query: str,
        path: str,
        max_files: int,
        budget_tokens: int,
    ) -> dict[str, Any]:
        requested = Path(path)
        resolved = requested if requested.is_absolute() else workspace_root / requested
        resolved = resolved.resolve()
        file_glob: str | None = None
        if resolved != workspace_root:
            relative = str(resolved.relative_to(workspace_root))
            file_glob = relative if resolved.is_file() else f"{relative}/**"
        return cast(
            dict[str, Any],
            hooks.code_context_engine(str(workspace_root)).tool_search(
                query,
                limit=max(max_files * 4, 20),
                mode="hybrid",
                intent="auto",
                snippet="head",
                snippet_lines=12,
                file_glob=file_glob,
                budget_tokens=budget_tokens,
            ),
        )

    payload = search_first(
        query=query,
        task=query,
        path=path,
        max_files=limit,
        max_chars_per_file=max_chars_per_file,
        include_outline=include_outline,
        budget_tokens=budget_tokens,
        indexed_search=indexed_search,
    )
    if line_range is not None:
        scope_search_matches_to_range(payload, line_range)
    # Plumb savings via thread-local and strip from the LLM-facing payload.
    ts = int(payload.pop("tokens_saved", 0) or 0)
    if ts > 0:
        tool_call_tokens_saved.value = ts
    if include_meta:
        return payload
    payload.pop("cache_hit", None)
    payload.pop("backend", None)
    payload.pop("index_age_seconds", None)
    payload.pop("total_tokens", None)
    return payload


@mcp_tool(
    name="code_search",
    description=(
        "Search the indexed codebase. One call returns top matches' source inline "
        "(bounded), lower-ranked or oversized ones as precise path:Lx-Ly pointers "
        "(`read` exactly that range), a related-symbols map (path:Lx-Ly kind name), "
        "and candidate_files. Use instead of grep/find. Inline source = already read. "
        "One broad query beats several narrow rephrasings — combine every term into "
        "a single call; re-querying rarely surfaces anything new."
    ),
    param_aliases={
        "maxFiles": "limit",
        "max_results": "limit",
        "max_files": "limit",
        "max_candidates": "limit",
        "projectPath": "paths",
        "path": "paths",
        "include_paths": "paths",
        "pattern": "query",
        "regex": "query",
    },
    # include_source hidden from the schema: an un-nudged model reaches for it
    # "to be safe" and pulls thousands of resident chars it then re-reads anyway.
    # One extra precise read beats a speculative full-source dump. Hidden callers
    # (tests, power use) still pass it by name.
    # There used to be a SEPARATE max_files param here (hidden, default 8) that
    # looked like it did the same job as this cap -- it didn't: it fed engine
    # breadth + an inline-source count already fixed at 3 regardless of its
    # value (0 real callers ever moved it). Removed as a public/tool-level
    # concept entirely (see hooks.code_search_engine_max_files) so there is exactly
    # ONE agent-visible count knob. Every vanilla-habit spelling for "how many
    # results" (maxFiles, max_results, max_files, max_candidates) now lands on
    # `limit`, the real parameter.
    hidden_params=("include_source",),
)
def tool_code_search(
    query: Annotated[
        str,
        Field(description="Question, symbol/file names, code terms, or regex."),
    ],
    paths: Annotated[
        str | list[str] | None,
        Field(description="Optional file or directory scope."),
    ] = None,
    project_id: Annotated[
        str | None,
        Field(description="Optional registered project id; routes this call to that project's isolated index."),
    ] = None,
    include_source: Annotated[
        bool,
        Field(description="Hidden: keep the top-2 matches' source inline (bounded)."),
    ] = False,
    limit: Annotated[
        int | None,
        Field(ge=1, le=32, description="Cap candidate_files entries (default 8, or 5 when one file dominates)."),
    ] = None,
    force: Annotated[
        bool,
        Field(description=("Return full source even if this result was already shown in this session.")),
    ] = False,
) -> dict[str, Any]:
    """Relevant symbols' source grouped by file + call-graph relations, in one capped call.

    Seeds from the symbol/text index, pins exact-name matches, expands the call graph,
    and renders budgeted, line-numbered, skeletonized source. Treat the returned source
    as already read -- do not re-open those files with `read`.
    """
    hooks = _hooks()
    _ = project_id  # transport metadata is consumed before the handler
    workspace_root = hooks.workspace_root()
    # Search the current scope/source even for similar queries. The transport's
    # content deduplication can reference an identical result after execution;
    # word overlap alone cannot prove that the answer was already delivered.
    _ = force  # consumed by transport-level content deduplication
    # Normalise: paths param accepts list, comma-sep string, or single path
    # (the legacy `path` kwarg is folded into `paths` by param_aliases).
    raw = paths
    if isinstance(raw, list):
        seed_list = [p.strip() for p in raw if p and p.strip()]
    elif isinstance(raw, str):
        seed_list = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        seed_list = []
    seed_files = seed_list or None
    workspace_router = hooks.workspace_code_router(str(workspace_root))
    # A configured workspace may be a logical parent containing several repos;
    # do not create a meaningless index for that parent just to resolve a file
    # query. Direct workspace-relative file resolution still works, while the
    # routed explore below fans the actual search out to each repo engine.
    engine = None if workspace_router.is_configured else hooks.code_context_engine(str(workspace_root))
    # Fast path: a query that's itself an existing repo file (verbatim path, a
    # mistyped path with a bogus leading segment, or a unique basename) is
    # pinned straight to that file instead of ranked -- only when the caller
    # didn't already supply an explicit paths= scope, which takes precedence.
    resolved_path = None if seed_files else hooks.resolve_query_as_existing_file(workspace_root, query, engine)
    explore_seeds = [resolved_path] if resolved_path else seed_files
    if workspace_router.is_configured:
        result = cast(
            dict[str, Any],
            workspace_router.route(
                "explore",
                query=query,
                max_files=hooks.code_search_engine_max_files,
                seed_files=explore_seeds,
            ),
        )
    else:
        assert engine is not None
        result = cast(
            dict[str, Any],
            engine.tool_explore(query, max_files=hooks.code_search_engine_max_files, seed_files=explore_seeds),
        )
    # paths= is a SOFT scope at every shape (single file, directory, multi-path):
    # the engine reserves only the top-2 spots for in-scope files and lets the
    # neighbouring files its whole-repo channels surfaced race in below, so there is
    # no hard out-of-scope post-filter. The lean view applies the same reserve-2 cap
    # to the agent-visible file ranking.
    if resolved_path:
        result["exact_match"] = True
    # Observe the rich engine payload before lean projection. The observer is
    # fail-open and must not mutate result or add model-facing fields.
    observed = hooks.observe_code_search_evidence(
        query=query,
        payload=result,
        engine=engine,
        seed_files=explore_seeds,
        max_files=hooks.code_search_engine_max_files,
    )
    if isinstance(observed, dict):
        result = observed
    # Project the engine's rich candidate set to a lean, exact view so the agent
    # can go code_search -> edit without grep/read round-trips (seed files are
    # boosted to the top inside the view).
    lean = hooks.lean_code_search_view(
        result,
        max_files=hooks.code_search_engine_max_files,
        seed_files=explore_seeds,
        query=query,
        max_candidates=limit,
    )
    if resolved_path and not any(
        isinstance(e, dict) and e.get("path") == resolved_path and e.get("sections") for e in (lean.get("files") or [])
    ):
        # Fast-path hit on a file type with no symbol index (e.g. a shell
        # script) -- the engine surfaced it in candidate_files but rendered no
        # source. Inject the whole file directly rather than making the agent
        # follow up with a plain `read`.
        fallback = hooks.whole_file_fallback_section(workspace_root, resolved_path)
        if fallback:
            lean.setdefault("files", []).insert(0, fallback)
    # Outline shaping happens HERE (dict level, before any rendering) so the
    # compact text renderer and the JSON fallback stay in lockstep -- and BEFORE
    # savings, so an outlined section is never credited as a replaced read.
    # Adaptive inline/outline split: on an EXACT match the top-2 sections ship
    # inline (bounded at _CODESEARCH_TOP2_MAX_CHARS) -- the agent nearly always
    # edits the top hit, and a pointer there forces a read round-trip on every
    # search (measured ~1-2 extra turns/run on SWE tasks). Ranked-candidate
    # (no-exact-match) results stay pointer-only: the top hit is a gamble, so
    # the agent reads only what it picks. Content-only shaping -- the ranked
    # file/candidate surface (and retrieval MRR) is untouched either way.
    shaped = hooks.outline_lean_view(lean, keep_top2=include_source or bool(lean.get("exact_match")))
    return hooks.attach_code_search_savings(shaped, workspace_root)


@mcp_tool(
    name="grep",
    description=(
        "Search code by regex/glob/type. mode='with_content' (default) = discover AND "
        "read matched context in one step. Match on a symbol definition → its "
        "caller/callee/usage counts ride along inline; expand the lists via `relations`."
    ),
    hidden_params=(
        "include_meta",
        "format",
        "lines_per_file",
        "context_budget_tokens",
        "file_limit",
        "if_modified_since",
    ),
    param_aliases=GREP_PARAM_ALIASES,
)
def tool_grep(
    path: Annotated[
        str,
        Field(description="Workspace path; single file may carry ':Lx-Ly' (e.g. 'store.py:L60-L100') to scope."),
    ] = ".",
    regex: Annotated[
        str | None,
        Field(description="Regex to match contents; for relation mode this is the symbol when `symbol` is omitted."),
    ] = None,
    glob: Annotated[
        str | list[str] | None,
        Field(description="Globs constraining candidate files (e.g. `src/**/*.py`). List or bare string."),
    ] = None,
    mode: Annotated[
        str,
        Field(
            description=(
                "with_content = matched lines+context (default). ranked_map = ranked file "
                "pointers. paths_only = paths. count_only = path + match count. Old "
                "aliases (file_paths_with_content, ...) accepted."
            )
        ),
    ] = "with_content",
    before: Annotated[
        int,
        Field(description="Lines before match."),
    ] = 0,
    after: Annotated[
        int,
        Field(description="Lines after match."),
    ] = 0,
    i: Annotated[
        bool,
        Field(description="Case-insensitive."),
    ] = False,
    type: Annotated[
        str | None,
        Field(description="File-type filter, e.g. `python`."),
    ] = None,
    file_limit: Annotated[
        int | None,
        Field(description="Max files rendered."),
    ] = None,
    lines_per_file: Annotated[
        int | None,
        Field(description="Max matched lines/file (content mode)."),
    ] = 500,
    if_modified_since: Annotated[
        str | None,
        Field(description="Prior result's timestamp; unchanged files are marked/skipped."),
    ] = None,
    multiline: Annotated[
        bool,
        Field(description="Regex spans newlines."),
    ] = False,
    summary: Annotated[
        bool | None,
        Field(description="Omit: auto-summarize large code. `true`: signatures-only. `false`: raw lines."),
    ] = None,
    context_budget_tokens: Annotated[
        int,
        Field(description="Output token budget (default 2000)."),
    ] = 2000,
    include_meta: Annotated[
        bool,
        Field(description="Include file counts and caps."),
    ] = False,
    format: Annotated[Literal["auto", "compact", "json"], _FORMAT_FIELD] = "auto",
) -> dict[str, Any]:
    """Search code by regex/glob/type, with call-graph counts riding along on definition matches.

    Default mode reads matched context inline. Omit `regex` for path/type listings. When a match
    lands on a symbol's definition, its caller/callee/usage counts are appended to that file's
    header (e.g. `orders.py  OrderService  ↳12 callers ↰3 callees ⌖8 usages`) -- no extra call or
    param needed. To expand a non-trivial count into the actual list, use the `relations` tool.
    Returns: results shaped by `mode` (default `content`: matched lines plus context).
    For natural-language/semantic ranking use `search`.
    """
    hooks = _hooks()
    # Accept a single glob passed as a bare string -- a common shape the model
    # reaches for -- so it does not trip schema validation against the array type.
    if isinstance(glob, str):
        glob = [glob]
    # Short model-facing mode names map to the engine's verbose output_mode.
    native_mode = cast(
        Literal["ranked_file_map", "file_paths_with_content", "file_paths_only", "file_paths_with_match_count"],
        GREP_MODE_ALIASES.get(normalize_grep_mode(mode), "file_paths_with_content"),
    )
    # Ride call-graph counts along content-mode regex matches that land on symbol
    # definitions (best-effort; the provider never fails the search).
    badge_provider = hooks.grep_badge_provider if (regex and native_mode == "file_paths_with_content") else None
    payload = hooks.run_native_grep(
        path=path,
        content_regex=regex,
        file_glob_patterns=glob,
        output_mode=native_mode,
        lines_before=before,
        lines_after=after,
        ignore_case=i,
        type=type,
        file_limit=file_limit,
        lines_per_file=lines_per_file,
        if_modified_since=if_modified_since,
        multiline=multiline,
        summary=summary,
        context_budget_tokens=context_budget_tokens,
        include_meta=include_meta,
        badge_provider=badge_provider,
    )
    # Plumb savings via thread-local (read by _extract_tokens_saved) and
    # strip from the LLM-facing payload to keep responses clean.
    ts = int(payload.pop("tokens_saved", 0) or 0)
    if ts > 0:
        tool_call_tokens_saved.value = ts
    if regex and not payload.get("isError"):
        # Literal grep has no semantic/zoekt channel, so no "dark" verdict -- only
        # found / missed / absent, plus the shared breaker.
        payload = hooks.apply_search_verdict(
            payload, query=regex, hit_count=hooks.count_grep_hits(payload), channels=None
        )
    return payload


__all__ = [
    "SearchHandlerHooks",
    "configure_search_handler_hooks",
    "scope_search_matches_to_range",
    "tool_code_search",
    "tool_grep",
    "tool_smart_search",
]
