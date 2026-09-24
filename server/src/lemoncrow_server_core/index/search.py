"""Server-side MCP code intelligence over one revision-bound index authority.

The thin client uploads view state once; this dispatcher keeps every
index-backed tool on the enterprise three-layer index. ``code_search`` and
``search`` are native here, structural graph analytics read Layer 3 directly,
and shared public MCP wrappers receive a request-scoped server-index adapter.
They never construct the legacy per-worktree CodeContext/Zoekt/semantic file
indexes in hosted or loopback thin-client mode. An optional server-owned Zoekt
accelerator is a derived cache over already-materialized Views: one process and
one shard directory for the whole server, always hard-scoped back to one View.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lemoncrow.gateway.tools.state import (
    clear_request_code_context_engine,
    set_request_code_context_engine,
)

from ..dispatch import ToolDispatcher, ToolInvocation, ToolResult
from ..errors import AgentAction, ErrorCode, ServerError
from .analysis import parse_query
from .contracts import IndexBackend, validate_path
from .engine_adapter import ServerIndexEngineAdapter
from .graph import SERVER_GRAPH_KINDS, ServerGraphAnalytics
from .query import IndexQuery, QueryAnswer, QueryHit
from .runtime_policy import apply_native_search_policy
from .search_markdown import render_code_search_markdown
from .zoekt_accelerator import SharedZoektAccelerator, build_shared_zoekt_accelerator

_ALIASES = {
    "pattern": "query",
    "regex": "query",
    "path": "paths",
    "projectPath": "paths",
    "include_paths": "paths",
    "maxFiles": "limit",
    "max_results": "limit",
    "max_files": "limit",
    "max_candidates": "limit",
}


class IndexSearchDispatcher:
    """Keep HTTP/tool contracts while making the server index authoritative."""

    def __init__(
        self,
        backend: IndexBackend,
        fallback: ToolDispatcher,
        *,
        content_size_cap: int,
        timeout_s: float,
        accelerator: SharedZoektAccelerator | None = None,
    ) -> None:
        self._backend = backend
        self._fallback = fallback
        self._content_size_cap = content_size_cap
        self._query = IndexQuery(backend, content_size_cap=content_size_cap)
        self._timeout_s = min(1.0, timeout_s)
        self._accelerator = accelerator if accelerator is not None else build_shared_zoekt_accelerator()

    @property
    def available(self) -> bool:
        return True

    @property
    def tools(self) -> frozenset[str]:
        return self._fallback.tools | {"code_search"}

    def dispatch(self, invocation: ToolInvocation) -> ToolResult:
        if invocation.tool == "search":
            return self._dispatch_semantic_search(invocation)
        if invocation.tool == "graph" and str(invocation.arguments.get("kind", "blast_radius")) in SERVER_GRAPH_KINDS:
            return self._dispatch_graph(invocation)
        if invocation.tool != "code_search":
            return self._dispatch_registry_with_server_index(invocation)
        query, paths, limit, client_hydrate = _arguments(invocation.arguments)
        tenant = invocation.tenant
        if tenant.view_id is None:
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "code_search requires an open view",
                action=AgentAction.REOPEN_VIEW,
            )
        self._check_revision(invocation)
        accelerated = None
        if self._accelerator is not None and invocation.workspace_root is not None:
            accelerated = self._accelerator.search(
                invocation.workspace_root,
                revision=int(tenant.view_revision or 0),
                query=query,
                limit=max(32, limit * 4),
            )
        answer = self._query.search(
            org_id=tenant.org_id,
            view_id=tenant.view_id,
            query=query,
            paths=paths,
            limit=limit,
            timeout_s=self._timeout_s,
            # The resident Zoekt shard is the old LemonCrow substring/trigram
            # channel. When it answered successfully, keep SQLite on its cheap
            # exact term/symbol path and use Zoekt only as additive recall. If
            # Zoekt is disabled, cold-failed, or rejects the query syntax, the
            # complete SQLite gram path remains the transparent fallback.
            # SQLite's optimized rare-gram path remains authoritative for
            # ranking. Zoekt is additive recall only; disabling gram evidence
            # here changes MRR because appended accelerator hits cannot recover
            # the original fused ordering.
            pipe_include_grams=True,
        )
        if accelerated is not None and accelerated.ready and accelerated.paths:
            answer = self._append_accelerator_hits(
                tenant.org_id, tenant.view_id, answer, accelerated.paths, limit=limit
            )
        hydration = self._hydration_for_answer(tenant.org_id, tenant.view_id, answer)
        answer, runtime_policy = apply_native_search_policy(
            self._backend,
            org_id=tenant.org_id,
            view_id=tenant.view_id,
            query=query,
            answer=answer,
            source_paths=hydration,
            limit=limit,
            repo_root=invocation.workspace_root,
        )
        # An experiment may add relation-backed native hits; hydrate exactly the
        # final answer so the thin client can still render them in the same turn.
        hydration = self._hydration_for_answer(tenant.org_id, tenant.view_id, answer)
        content = self._render(invocation, query, answer, hydrate_source=not client_hydrate)
        # An overlay may commit while the worker is searching/rendering.
        # Refuse that answer so the client retries at the committed revision.
        self._check_revision(invocation)
        diagnostics: dict[str, Any] = {"runtime_policy": runtime_policy}
        if accelerated is not None and accelerated.ready:
            diagnostics["search_accelerator"] = "zoekt"
            if accelerated.cold_build:
                diagnostics["search_accelerator_cold_build"] = True
        structured = answer.to_wire()
        if hydration:
            structured["_hydration"] = hydration
        return ToolResult(
            tool="code_search",
            content=({"type": "text", "text": content},),
            structured=structured,
            diagnostics=diagnostics,
        )

    def _hydration_for_answer(self, org_id: str, view_id: str, answer: QueryAnswer) -> dict[str, str]:
        hydration: dict[str, str] = {}
        for hit in answer.hits:
            entry = self._backend.views.entry(org_id, view_id, hit.path)
            if entry is not None and hit.detail.get("content_indexed") is not False:
                hydration[hit.path] = entry.content_digest
        return hydration

    def prewarm_workspace(self, workspace_root: str | Path, revision: int) -> bool:
        accelerator = self._accelerator
        return False if accelerator is None else accelerator.prepare(workspace_root, revision=revision)

    def close(self) -> None:
        accelerator, self._accelerator = self._accelerator, None
        if accelerator is not None:
            accelerator.close()

    def forget_workspace(self, workspace_root: str | Path) -> None:
        if self._accelerator is not None:
            self._accelerator.forget(workspace_root)

    def _append_accelerator_hits(
        self,
        org_id: str,
        view_id: str,
        answer: QueryAnswer,
        candidates: tuple[str, ...],
        *,
        limit: int,
    ) -> QueryAnswer:
        # Additive only: the authoritative term/symbol ranking never moves.
        # Zoekt fills otherwise-unused slots, recovering files the exact-token
        # channel cannot see without bringing the expensive all-view gram scan
        # back onto the hot path.
        hits = list(answer.hits)
        seen = {hit.path for hit in hits}
        for path in candidates:
            if len(hits) >= limit:
                break
            if path in seen:
                continue
            entry = self._backend.views.entry(org_id, view_id, path)
            if entry is None:
                continue
            metadata = self._backend.analysis.metadata(org_id, entry.content_digest, entry.parser_profile)
            hits.append(
                QueryHit(
                    path=path,
                    score=0.0,
                    language=metadata[0] if metadata is not None else "",
                    line_count=metadata[1] if metadata is not None else 0,
                    size=entry.size,
                    detail={"content_indexed": True, "accelerator": "zoekt"},
                )
            )
            seen.add(path)
        return QueryAnswer(
            kind=answer.kind,
            hits=tuple(hits),
            at_view_revision=answer.at_view_revision,
            searched_paths=answer.searched_paths,
            degraded=answer.degraded,
            degraded_reason=answer.degraded_reason,
            truncated=answer.truncated or len(candidates) > max(0, limit - len(answer.hits)),
        )

    def _dispatch_graph(self, invocation: ToolInvocation) -> ToolResult:
        tenant = invocation.tenant
        if tenant.view_id is None:
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "graph requires an open view",
                action=AgentAction.REOPEN_VIEW,
            )
        self._check_revision(invocation)
        arguments = invocation.arguments
        allowed = {"kind", "path", "paths", "limit", "synthesize", "query", "enable"}
        unknown = set(arguments) - allowed
        if unknown:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, f"unknown graph argument(s): {', '.join(sorted(unknown))}")
        kind = str(arguments.get("kind", "blast_radius"))
        path = arguments.get("path")
        if path is not None and not isinstance(path, str):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "path must be a string")
        raw_paths = arguments.get("paths")
        if raw_paths is None:
            paths = None
        elif isinstance(raw_paths, list) and all(isinstance(value, str) for value in raw_paths):
            paths = [str(value) for value in raw_paths]
        else:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "paths must be a list of strings")
        limit = arguments.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "limit must be an integer from 1 to 200")
        if bool(arguments.get("synthesize", False)):
            raise ServerError(
                ErrorCode.PAYLOAD_INVALID,
                "synthesize=true is not supported by the server-index graph; use local capture/tooling for heuristic edges",
                action=AgentAction.FIX_REQUEST,
            )
        analytics = ServerGraphAnalytics(
            self._backend,
            org_id=tenant.org_id,
            view_id=tenant.view_id,
            content_size_cap=self._content_size_cap,
            repo_root=invocation.workspace_root,
        )
        try:
            payload, status = analytics.run(kind, path=path, paths=paths, limit=limit)
        except ValueError as exc:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, str(exc), action=AgentAction.FIX_REQUEST) from exc
        self._check_revision(invocation)
        from lemoncrow.pro.capabilities.code_context.renderer import render_graph_payload

        rendered = render_graph_payload(payload)
        text = rendered if rendered is not None else json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return ToolResult(
            tool="graph",
            content=({"type": "text", "text": text},),
            structured=payload,
            degraded=not status.ready,
            degraded_reason=f"link:{status.reason or status.state.value}" if not status.ready else "",
        )

    def _dispatch_semantic_search(self, invocation: ToolInvocation) -> ToolResult:
        tenant = invocation.tenant
        if tenant.view_id is None:
            raise ServerError(
                ErrorCode.VIEW_UNKNOWN,
                "search requires an open view",
                action=AgentAction.REOPEN_VIEW,
            )
        self._check_revision(invocation)
        query, path, limit, include_meta = _search_arguments(invocation.arguments)

        # The legacy wrapper treated shell-style globs as literal text and
        # returned no semantic matches. Keep that harmless behavior without
        # handing the string to a local regex/Zoekt stack.
        hits: tuple[QueryHit, ...]
        if any(ch in query for ch in "*?[]"):
            hits = ()
            degraded = False
            degraded_reason = ""
        else:
            lexical = self._query.run(
                org_id=tenant.org_id,
                view_id=tenant.view_id,
                kind="lexical",
                query=query,
                limit=max(limit * 4, 20),
            )
            semantic = self._query.run(
                org_id=tenant.org_id,
                view_id=tenant.view_id,
                kind="semantic",
                query=query,
                limit=max(limit * 4, 20),
            )
            merged: list[QueryHit] = list(lexical.hits)
            seen = {hit.path for hit in merged}
            merged.extend(hit for hit in semantic.hits if hit.path not in seen)
            hits = tuple(merged)
            degraded = semantic.degraded
            degraded_reason = semantic.degraded_reason

        matches: list[dict[str, Any]] = []
        for hit in hits:
            if path != "." and not (hit.path == path or hit.path.startswith(path.rstrip("/") + "/")):
                continue
            entry = self._backend.views.entry(tenant.org_id, tenant.view_id, hit.path)
            if entry is None:
                continue
            data = self._backend.content.get(tenant.org_id, entry.content_digest)
            if data is None:
                continue
            lines = data.decode("utf-8", errors="replace").splitlines()
            terms = parse_query(query).terms
            line = next(
                (number for number, text in enumerate(lines, 1) if any(term in text.lower() for term in terms)),
                1,
            )
            first, last = max(1, line - 2), min(len(lines), line + 8)
            text = "\n".join(lines[first - 1 : last])
            matches.append(
                {
                    "path": hit.path,
                    "lang": hit.language,
                    "snippets": [{"line_start": first, "line_end": last, "text": text}],
                    "outline": None,
                    "follow_up": {
                        "read": {"tool": "read", "path": hit.path},
                        "context": {"tool": "context", "mode": "symbols", "task": query, "files": [hit.path]},
                    },
                }
            )
            if len(matches) >= limit:
                break

        match_paths = [str(match["path"]) for match in matches]
        payload: dict[str, Any] = {
            "query": query,
            "task": query,
            "path": path,
            "mode": "chunks",
            "discovery": {"tool": "search", "mode": "chunks"},
            "matches": matches,
            "match_paths": match_paths,
            "calls_saved": len(match_paths),
            "handoff": {
                "read": {"tool": "read"},
                "context": {"tool": "context", "mode": "symbols", "task": query, "files": match_paths},
                "memory": {
                    "tool": "context",
                    "mode": "procedures",
                    "task": query,
                    "files": match_paths,
                    "recall": True,
                },
                "relations": {"tool": "grep", "relation": "usages", "symbol": query},
            },
        }
        if include_meta:
            payload.update(
                {
                    "backend": "server_index",
                    "index_age_seconds": None,
                    "cache_hit": False,
                    "total_tokens": 0,
                }
            )
        self._check_revision(invocation)
        return ToolResult(
            tool="search",
            content=({"type": "text", "text": json.dumps(payload, sort_keys=True, separators=(",", ":"))},),
            structured=payload,
            degraded=degraded,
            degraded_reason=degraded_reason,
        )

    def _dispatch_registry_with_server_index(self, invocation: ToolInvocation) -> ToolResult:
        """Run shared public wrappers with the revision-bound server index injected.

        The public wrappers call ``_code_context_engine`` for relations, semantic
        search, symbol context/recall and graph centrality.  On the enterprise
        path that function must never construct its historical per-worktree
        SQLite/Zoekt engine.  The request-scoped override keeps one wrapper
        implementation while making the server index the sole authority.
        """
        tenant = invocation.tenant
        if tenant.view_id is None:
            return self._fallback.dispatch(invocation)
        self._check_revision(invocation)
        adapter = ServerIndexEngineAdapter(
            self._backend,
            org_id=tenant.org_id,
            view_id=tenant.view_id,
            content_size_cap=self._content_size_cap,
            repo_root=invocation.workspace_root,
        )
        prior = set_request_code_context_engine(adapter)
        try:
            result = self._fallback.dispatch(invocation)
        finally:
            clear_request_code_context_engine(prior)
        self._check_revision(invocation)
        return result

    def _check_revision(self, invocation: ToolInvocation) -> None:
        tenant = invocation.tenant
        assert tenant.view_id is not None
        revision = self._backend.views.get(tenant.org_id, tenant.view_id).view_revision
        if revision != tenant.view_revision:
            raise ServerError(
                ErrorCode.VIEW_REVISION_STALE,
                "the view changed during search",
                details={"committed_view_revision": revision},
                action=AgentAction.REFRESH_VIEW_REVISION,
            )

    def _render(
        self,
        invocation: ToolInvocation,
        query: str,
        answer: QueryAnswer,
        *,
        hydrate_source: bool = True,
    ) -> str:
        tenant = invocation.tenant
        assert tenant.view_id is not None
        view_id = tenant.view_id

        def load_source(hit: QueryHit) -> bytes | None:
            if not hydrate_source:
                return None
            indexed = bool(hit.detail.get("definitions")) or hit.detail.get("content_indexed") is True
            if not indexed:
                return None
            entry = self._backend.views.entry(tenant.org_id, view_id, hit.path)
            if entry is None:
                return None
            return self._backend.content.get(tenant.org_id, entry.content_digest)

        return render_code_search_markdown(query, answer, load_source=load_source)


def _search_arguments(arguments: Mapping[str, Any]) -> tuple[str, str, int, bool]:
    allowed = {
        "query",
        "path",
        "limit",
        "max_files",
        "max_chars_per_file",
        "include_outline",
        "budget_tokens",
        "include_meta",
        "format",
    }
    unknown = set(arguments) - allowed
    if unknown:
        raise ServerError(ErrorCode.PAYLOAD_INVALID, f"unknown search argument(s): {', '.join(sorted(unknown))}")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 1024:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "query must be a non-empty string of at most 1024 characters",
            details={"field": "query"},
            action=AgentAction.FIX_REQUEST,
        )
    raw_path = arguments.get("path", ".")
    if not isinstance(raw_path, str):
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "path must be a string")
    path = raw_path.strip() or "."
    # Search's public syntax permits a :Lx-Ly suffix. The semantic server path
    # scopes by file/directory; line trimming remains a presentation concern.
    path = re.sub(r":L?\d+(?:-L?\d+)?$", "", path) or "."
    if path.startswith("/"):
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "path must be repository-relative")
    path = path.removeprefix("./").rstrip("/") or "."
    if path != ".":
        validate_path(path, "path")
    limit = arguments.get("limit", arguments.get("max_files", 10))
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "limit must be an integer from 1 to 50")
    include_meta = bool(arguments.get("include_meta", False))
    return query.strip(), path, limit, include_meta


def _arguments(arguments: Mapping[str, Any]) -> tuple[str, tuple[str, ...], int, bool]:
    if not arguments:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "code_search: empty arguments -- the client likely dropped them in transit. "
            "Retry with fewer items per call and \\uXXXX escapes for non-ASCII.",
            action=AgentAction.FIX_REQUEST,
        )
    normalized: dict[str, Any] = {}
    for key, value in arguments.items():
        target = _ALIASES.get(key, key)
        if target in normalized:
            raise ServerError(ErrorCode.PAYLOAD_INVALID, f"duplicate search argument: {target}")
        normalized[target] = value
    if normalized.keys() - {"query", "paths", "limit", "include_source", "force", "_client_hydrate"}:
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "unknown code_search argument")
    query = normalized.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 1024:
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            "query must be a non-empty string of at most 1024 characters",
            details={"field": "query"},
            action=AgentAction.FIX_REQUEST,
        )
    limit = normalized.get("limit", 8)
    if limit is None:
        limit = 8
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "limit must be an integer from 1 to 32")
    raw = normalized.get("paths")
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(path, str) for path in raw):
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "paths must be a string or list of strings")
    scopes: list[str] = []
    for path in raw:
        if path.strip().startswith("/"):
            raise ServerError(ErrorCode.PAYLOAD_INVALID, "paths must be repository-relative")
        scope = path.strip().removeprefix("./").rstrip("/")
        if not scope:
            scope = "."
        if scope != ".":
            validate_path(scope, "paths")
        scopes.append(scope)
    client_hydrate = normalized.get("_client_hydrate", False)
    if type(client_hydrate) is not bool:
        raise ServerError(ErrorCode.PAYLOAD_INVALID, "_client_hydrate must be a boolean")
    return query.strip(), tuple(scopes), limit, client_hydrate
