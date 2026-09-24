"""Code-intel MCP wrappers with late-bound engine operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from lemoncrow.gateway.adapters.mcp.framework import mcp_tool


@dataclass(frozen=True, slots=True)
class CodeIntelHandlerHooks:
    graph: Callable[..., dict[str, Any]]
    pattern: Callable[..., dict[str, Any]]
    index: Callable[..., dict[str, Any]]
    blame: Callable[..., dict[str, Any]]
    cache_status: Callable[..., dict[str, Any]]
    cache_invalidate: Callable[..., dict[str, Any]]
    callers: Callable[..., dict[str, Any]]
    callees: Callable[..., dict[str, Any]]
    usages: Callable[..., dict[str, Any]]
    node: Callable[..., dict[str, Any]]


_HooksFactory = Callable[[], CodeIntelHandlerHooks]
_hooks_factory: _HooksFactory | None = None


def configure_code_intel_handler_hooks(factory: _HooksFactory) -> None:
    """Install the process composition used by code-intel wrappers."""
    global _hooks_factory
    _hooks_factory = factory


def _hooks() -> CodeIntelHandlerHooks:
    factory = _hooks_factory
    if factory is None:
        raise RuntimeError("code-intel handler hooks are not configured")
    return factory()


def _parse_symbol(symbol: str) -> dict[str, Any]:
    if "." in symbol:
        return {"qualified_name": symbol}
    return {"symbol_name": symbol}


@mcp_tool(name="graph")
def tool_graph(
    kind: str = "blast_radius",
    path: str | None = None,
    paths: list[str] | None = None,
    limit: int = 50,
    synthesize: bool = False,
    query: str | None = None,
    enable: bool | None = None,
) -> dict[str, Any]:
    """Repo graph analytics + code health & history: blast radius, dead code, cycles,
    coupling, centrality, doc/code drift, PR risk, commit provenance, design-doc recall.

    kind:
      - blast_radius (default): reverse-dependency closure + affected tests + risk tier for `path`.
      - dead_code: files with no inbound importers (likely removable), ranked by complexity.
      - cycles: import cycles (strongly-connected components, size >= 2).
      - coupling: per-file afferent/efferent coupling + Martin instability.
      - centrality: top symbols by call-graph centrality (degree + eigenvector).
      - design_gaps (G15): doc-referenced symbols absent from the index (stale/aspirational refs).
      - verify_design (G15): doc-referenced symbols with drifted signatures.
      - pr_risk (G16): blast-radius + complexity + churn + test-gap → 0..1 risk score + tier
        for the changed `paths` (or `path`).
      - commit_provenance (G16): heuristic bugfix/refactor/feature/perf/rename/revert/docs/test
        classification of commits touching `path` (or repo), tagged confidence.
      - index_docs (N17): opt-in heading-tree indexing of Markdown design docs into a SEPARATE
        retrieval store (`enable=true` or LEMONCROW_DOC_INDEXING=1; off by default).
      - recall_docs (N17): design-doc chunks for `query` from the separate doc store.
    `paths` = fold files into the index first (dead_code/cycles/coupling/pr_risk); for
    design_gaps/verify_design/index_docs it selects the docs/dirs to scan.
    `synthesize=true` (with `paths`, kind=centrality) → heuristic route/event edges as a
    SEPARATE `synthesized_edges` list (never merged into the static call graph).
    """
    return _hooks().graph(
        kind=kind,
        path=path,
        paths=paths,
        limit=limit,
        synthesize=synthesize,
        query=query,
        enable=enable,
    )


@mcp_tool(
    name="codemod",
    description=(
        "AST-shape search and rewrite via ast-grep. Matches structure, not text "
        "(formatting-safe; ignores strings/comments). `$X` = one node, `$$$` = list; "
        "e.g. pattern `$X == None`, rewrite `$X is None`. Scope with `language`/`glob`. "
        "`dry_run=true` (default) previews a diff, false applies. Returns matches "
        "(snippet, path, line); with rewrite: diff + files_changed."
    ),
    param_aliases={"file_glob": "glob"},
)
def tool_pattern(
    pattern: str,
    language: str | None = None,
    glob: str | None = None,
    rewrite: str | None = None,
    limit: int = 20,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Structural code search and safe rewrite (codemod) by AST shape, via ast-grep.

    Use over `grep` when matching code *shape*, not text: it is formatting-
    independent and never matches inside strings or comments. Metavariables:
    `$X` binds one node, `$$$` binds a list -- e.g. `isinstance($X, $Y)`,
    `$X == None`, `requests.get($URL)`.

    Pass `rewrite` to transform every match (the codemod); captured metavariables
    are reusable in the replacement, e.g. pattern `$X == None`, rewrite `$X is None`.
    `dry_run=True` (default) returns a unified-diff preview and writes nothing;
    `dry_run=False` applies the rewrite across all matched files. Scope with
    `language` (e.g. 'python') and `file_glob`.
    Returns: matches (snippet, file_path, line); with `rewrite`, a diff and `files_changed`.
    """
    return _hooks().pattern(
        pattern=pattern,
        rewrite=rewrite,
        language=language,
        file_glob=glob,
        dry_run=dry_run,
        limit=limit,
    )


@mcp_tool(name="index")
def tool_index(
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    force: bool = False,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    """Build or refresh the code index for the repo (internal/admin)."""
    return _hooks().index(
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        force=force,
        budget_tokens=budget_tokens,
        repo_root=repo_root,
        render_compact=render_compact,
    )


@mcp_tool(name="blame")
def tool_blame(
    query: str | None = None,
    symbol_id: str | None = None,
    qualified_name: str | None = None,
    symbol_name: str | None = None,
    path: str | None = None,
    include_churn: bool = True,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    """Git blame / churn summary for a symbol or file (internal/admin)."""
    return _hooks().blame(
        query=query,
        symbol_id=symbol_id,
        qualified_name=qualified_name,
        symbol_name=symbol_name,
        path=path,
        include_churn=include_churn,
        budget_tokens=budget_tokens,
        repo_root=repo_root,
        render_compact=render_compact,
    )


@mcp_tool(name="cache")
def tool_cache(
    op: Literal["status", "invalidate"] = "status",
    cache_tool: str | None = None,
    budget_tokens: int = 4000,
    repo_root: str | None = None,
    render_compact: bool = False,
) -> dict[str, Any]:
    """Code-intel cache admin (internal/admin).

    op='status' (default) reports cache hit/miss counters; op='invalidate'
    clears caches, optionally scoped to one `cache_tool`. Folds the former
    cache_status / cache_invalidate tools into one hidden admin face.
    """
    if op == "invalidate":
        return _hooks().cache_invalidate(
            cache_tool=cache_tool,
            budget_tokens=budget_tokens,
            repo_root=repo_root,
            render_compact=render_compact,
        )
    return _hooks().cache_status(
        cache_tool=cache_tool,
        budget_tokens=budget_tokens,
        repo_root=repo_root,
        render_compact=render_compact,
    )


@mcp_tool(
    name="relations",
    description=(
        "Expand one symbol's call-graph relation into the actual list: kind=callers|callees|usages|self. "
        "use this only to see WHICH callers/callees/usages when worth drilling into."
    ),
)
def tool_relations(
    symbol: str,
    kind: str = "usages",
    depth: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    """Return the actual callers / callees / usages / definition of one symbol.

    `symbol` is a name, qualified path, or symbol id. `kind` selects the relation
    (default usages); `depth` extends callers/callees transitively. The COUNTS for
    these already ride along on `grep` definition matches — use this only to expand
    a count into the concrete list.
    """
    target = _parse_symbol(symbol)
    rel = kind.strip().lower()
    if rel == "callers":
        return _hooks().callers(**target, depth=depth, limit=limit)
    if rel == "callees":
        return _hooks().callees(**target, depth=depth, limit=limit)
    if rel in ("usages", "refs", "references"):
        return _hooks().usages(**target, limit=limit)
    if rel in ("self", "node", "definition"):
        return _hooks().node(**target)
    raise ValueError(f"unknown kind {kind!r}; use callers, callees, usages, or self")


__all__ = [
    "CodeIntelHandlerHooks",
    "configure_code_intel_handler_hooks",
    "tool_blame",
    "tool_cache",
    "tool_graph",
    "tool_index",
    "tool_pattern",
    "tool_relations",
]
