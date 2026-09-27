"""Native generation-aware code intelligence adapted for Review."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from lemoncrow.pro.capabilities.review.code_intelligence import (
    CodeIntelligenceLayerReadiness,
    CodeIntelligenceReadiness,
    ReadinessState,
    unavailable_readiness,
)

from .index.contracts import IndexBackend, IndexLayerState, IndexLayerStatus, ViewState
from .index.query import IndexQuery, QueryAnswer


def _layer(status: IndexLayerStatus) -> CodeIntelligenceLayerReadiness:
    state: ReadinessState
    if status.state is IndexLayerState.READY:
        state = "ready"
    elif "content_incomplete" in status.reason:
        # This is not merely queued work: the layer cannot become complete from
        # the content this View may currently address. Preserve that native
        # reason as an explicit degraded state rather than calling it building.
        state = "degraded"
    else:
        state = "building"
    return CodeIntelligenceLayerReadiness(
        state,
        reason=status.reason or ("" if status.ready else status.state.value),
        native_state=status.state.value,
        covered_paths=status.covered_paths,
        at_view_revision=status.at_view_revision,
    )


def _node_id(path: str, line: int, label: str) -> str:
    digest = hashlib.sha256(f"{path}\0{line}\0{label}".encode()).hexdigest()[:16]
    return f"native:{digest}"


class NativeReviewCodeIntelligence:
    """Review adapter over an authorized native View.

    The resolver is intentionally dynamic. Hosted Reader apps are cached across
    requests/principals, while View possession is request-specific. Every query
    therefore resolves a View owned by the currently-bound subject and then
    delegates to IndexQuery, preserving its possession and candidate-first
    semantics.
    """

    __slots__ = (
        "_access_allowed",
        "_backend",
        "_content_size_cap",
        "_keep_view_leased",
        "_org_id",
        "_preferred_view_id",
        "_preferred_view_revision",
        "_queries",
        "_repo_id",
        "_restore_view",
        "_subject",
    )

    def __init__(
        self,
        *,
        backend: IndexBackend,
        queries: IndexQuery,
        org_id: str,
        repo_id: str,
        subject: Callable[[], str],
        content_size_cap: int,
        access_allowed: Callable[[], bool] | None = None,
        preferred_view_id: str = "",
        preferred_view_revision: int = 0,
        restore_view: Callable[[], tuple[str, int] | None] | None = None,
        keep_view_leased: Callable[[str], None] | None = None,
    ) -> None:
        """*restore_view* reopens the Snapshot's View from its stored source tree
        once the bound one lapsed; *keep_view_leased* keeps a View in use alive."""
        self._backend = backend
        self._queries = queries
        self._org_id = org_id
        self._repo_id = repo_id
        self._subject = subject
        self._content_size_cap = max(1, int(content_size_cap))
        self._access_allowed = access_allowed or (lambda: True)
        self._preferred_view_id = preferred_view_id.strip()
        self._preferred_view_revision = max(0, int(preferred_view_revision))
        self._restore_view = restore_view
        self._keep_view_leased = keep_view_leased

    def _resolve_view(self) -> tuple[tuple[IndexBackend, ViewState] | None, str]:
        if not self._access_allowed():
            return None, "content_read_not_allowed"
        subject = self._subject()
        if not subject:
            return None, "no_bound_subject"
        try:
            view_ids = tuple(self._backend.views.view_ids_for_user(self._org_id, subject))
        except Exception:
            return None, "native_view_lookup_failed"

        if self._preferred_view_id:
            state = self._preferred_state(view_ids)
            if state is None and self._restored():
                # The Snapshot's View lapsed with its lease; its exact source
                # tree is stored with the Review, so it was opened again.
                state = self._preferred_state(None)
            return self._bound(state)

        candidates: list[tuple[IndexBackend, ViewState]] = []
        for view_id in view_ids:
            try:
                state = self._backend.views.get(self._org_id, view_id)
                if state.repo_id != self._repo_id:
                    continue
                candidates.append((self._backend.for_view(self._org_id, view_id), state))
            except Exception:
                continue
        if len(candidates) == 1:
            return candidates[0], ""
        # Old Review revisions have no persisted source View binding. Picking the
        # newest lease here can silently cross worktrees, so ambiguity -- or no
        # View at all -- opens the revision's own stored tree instead, exactly.
        if self._restored():
            return self._bound(self._preferred_state(None))
        return None, ("ambiguous_authorized_native_views" if candidates else "no_authorized_native_view")

    def _restored(self) -> bool:
        """Reopen the revision's View from its stored tree; bind to it when it opens."""
        restored = self._restore_view() if self._restore_view is not None else None
        if restored is None:
            return False
        self._preferred_view_id, self._preferred_view_revision = restored[0], max(0, int(restored[1]))
        return True

    def _bound(self, state: ViewState | None) -> tuple[tuple[IndexBackend, ViewState] | None, str]:
        if state is None or state.repo_id != self._repo_id:
            return None, "bound_native_view_unavailable"
        try:
            if self._keep_view_leased is not None:
                self._keep_view_leased(self._preferred_view_id)
            return (self._backend.for_view(self._org_id, self._preferred_view_id), state), ""
        except Exception:
            return None, "bound_native_view_unavailable"

    def _preferred_state(self, owned: tuple[str, ...] | None) -> ViewState | None:
        """The bound View while it is live and, when *owned* is given, among them."""
        if owned is not None and self._preferred_view_id not in owned:
            return None
        try:
            return self._backend.views.get(self._org_id, self._preferred_view_id)
        except Exception:
            return None

    def _view(self) -> tuple[IndexBackend, ViewState] | None:
        resolved, _reason = self._resolve_view()
        return resolved

    def _source_layer_readiness(
        self, leaf: IndexBackend, state: ViewState
    ) -> tuple[CodeIntelligenceLayerReadiness, CodeIntelligenceLayerReadiness, tuple[str, ...]]:
        ready = CodeIntelligenceLayerReadiness(
            "ready",
            covered_paths=state.entry_count,
            at_view_revision=state.view_revision,
        )
        reachability = leaf.view_content_reachability
        if reachability is None:
            return ready, ready, ()
        try:
            report = reachability(self._org_id, state.view_id, self._content_size_cap)
        except Exception:
            degraded = CodeIntelligenceLayerReadiness(
                "degraded",
                reason="content_reachability_unavailable",
                native_state="unknown",
                at_view_revision=state.view_revision,
            )
            return (
                degraded,
                degraded,
                ("lexical:content_reachability_unavailable", "structural:content_reachability_unavailable"),
            )
        if report.missing_paths <= 0:
            return ready, ready, ()
        covered = max(0, report.required_paths - report.missing_paths)
        degraded = CodeIntelligenceLayerReadiness(
            "degraded",
            reason="content_incomplete",
            native_state="content_incomplete",
            covered_paths=covered,
            at_view_revision=state.view_revision,
        )
        return degraded, degraded, ("lexical:content_incomplete", "structural:content_incomplete")

    def readiness(self) -> CodeIntelligenceReadiness:
        resolved, unavailable_reason = self._resolve_view()
        if resolved is None:
            return unavailable_readiness(unavailable_reason or "no_authorized_native_view", backend="native")
        leaf, state = resolved
        if not state.manifest_complete:
            building = CodeIntelligenceLayerReadiness(
                "building",
                reason="manifest_incomplete",
                native_state="building",
                at_view_revision=state.view_revision,
            )
            return CodeIntelligenceReadiness(
                backend="native",
                lexical=building,
                structural=building,
                graph=_layer(leaf.links.status(self._org_id, state.view_id)),
                semantic=_layer(leaf.semantic.status(self._org_id, state.view_id)),
                view_id=state.view_id,
                view_revision=state.view_revision,
                generation_id=state.generation_id,
                base_revision=state.base_revision,
                manifest_root=state.manifest_root,
                reason="manifest_incomplete",
            )
        lexical, structural, source_degraded = self._source_layer_readiness(leaf, state)
        try:
            generation = leaf.views.generation(self._org_id, state.view_id)
            generation_id = generation.generation_id
            base_revision = generation.base_revision
            manifest_root = generation.manifest_root
        except Exception:
            generation_id = state.generation_id
            base_revision = state.base_revision
            manifest_root = state.manifest_root
        graph = _layer(leaf.links.status(self._org_id, state.view_id))
        semantic = _layer(leaf.semantic.status(self._org_id, state.view_id))
        degraded = (
            *source_degraded,
            *(
                reason
                for reason in (
                    f"graph:{graph.reason}" if graph.state != "ready" and graph.reason else "",
                    f"semantic:{semantic.reason}" if semantic.state != "ready" and semantic.reason else "",
                )
                if reason
            ),
        )
        return CodeIntelligenceReadiness(
            backend="native",
            lexical=lexical,
            structural=structural,
            graph=graph,
            semantic=semantic,
            view_id=state.view_id,
            view_revision=state.view_revision,
            generation_id=generation_id,
            base_revision=base_revision,
            manifest_root=manifest_root,
            degraded_reasons=degraded,
        )

    def _run(
        self,
        *,
        kind: str,
        query: str = "",
        path: str = "",
        symbol: str = "",
        limit: int,
    ) -> QueryAnswer | None:
        resolved = self._view()
        if resolved is None:
            return None
        _leaf, state = resolved
        return self._queries.run(
            org_id=self._org_id,
            view_id=state.view_id,
            kind=kind,
            query=query,
            path=path,
            symbol=symbol,
            limit=limit,
        )

    def find_symbol(
        self,
        symbol_name: str,
        *,
        path: str = "",
        qualified_name: str = "",
        start_line: int = 0,
        limit: int = 12,
    ) -> Mapping[str, Any] | None:
        answer = self._run(kind="symbol", symbol=symbol_name, limit=max(1, min(limit, 32)))
        if answer is None:
            return None
        source_revision_exact = bool(self._preferred_view_id) and (
            answer.at_view_revision == self._preferred_view_revision
        )
        candidates: list[dict[str, Any]] = []
        for hit in answer.hits:
            if path and hit.path != path:
                continue
            definitions = hit.detail.get("definitions")
            if not isinstance(definitions, list):
                continue
            for raw in definitions:
                if not isinstance(raw, Mapping) or str(raw.get("name") or "") != symbol_name:
                    continue
                line = int(raw.get("line") or 0)
                candidates.append(
                    {
                        "id": _node_id(hit.path, line, symbol_name),
                        "path": hit.path,
                        "label": symbol_name,
                        # Native Layer-1 currently proves only the bare definition
                        # name. Preserve a Snapshot qualified name only while this
                        # query is still against the exact captured View revision.
                        "qualified_name": qualified_name if source_revision_exact and qualified_name else symbol_name,
                        "kind": str(raw.get("kind") or "symbol"),
                        "line": line,
                        # SymbolDef.start/end are byte offsets of the definition
                        # token, not source line geometry. Do not turn them into a
                        # fake function span and accidentally attribute another
                        # function's outgoing calls to this symbol.
                        "end_line": line,
                    }
                )
        # Snapshot line geometry is an identity aid only while the bound native
        # View is still at the exact revision captured with the Review. Once that
        # View advances, line numbers may now name a different same-name symbol.
        if start_line > 0 and source_revision_exact:
            exact_line = [item for item in candidates if int(item["line"]) == start_line]
            if len(exact_line) == 1:
                return exact_line[0]
        if len(candidates) != 1:
            return None
        if qualified_name and qualified_name != symbol_name and not source_revision_exact:
            # The native index cannot independently prove qualified identity yet.
            # Returning a unique bare-name match here could relabel A.run as B.run
            # after the worktree advanced, so degrade instead of guessing.
            return None
        return candidates[0]

    def neighborhood(
        self,
        symbol: Mapping[str, Any],
        *,
        depth: int = 1,
        limit: int = 40,
    ) -> Mapping[str, Any]:
        del depth  # Native graph query is one bounded surrounding hop.
        path = str(symbol.get("path") or "")
        label = str(symbol.get("label") or symbol.get("qualified_name") or "")
        line = int(symbol.get("line") or 0)
        end_line = int(symbol.get("end_line") or line)
        focus_id = str(symbol.get("id") or _node_id(path, line, label))
        answer = self._run(kind="relations", path=path, limit=max(1, min(limit, 40)))
        if answer is None:
            return {"nodes": [], "edges": [], "degraded": True, "degraded_reason": "index_unavailable"}
        nodes: dict[str, dict[str, Any]] = {
            focus_id: {
                "id": focus_id,
                "path": path,
                "label": label,
                "qualified_name": str(symbol.get("qualified_name") or label),
                "kind": str(symbol.get("kind") or "symbol"),
                "line": line,
                "end_line": end_line,
            }
        }
        edges: list[dict[str, Any]] = []
        for hit in answer.hits:
            detail = hit.detail
            src = str(detail.get("src_path") or "")
            dst = str(detail.get("dst_path") or "")
            edge_symbol = str(detail.get("symbol") or "")
            relation_kind = str(detail.get("kind") or "reference")
            src_line = int(detail.get("src_line") or 0)
            # Native Edge identifies the target symbol exactly but does not yet
            # identify the source/caller symbol span. Inbound call/reference
            # edges are therefore attribution-safe; path-wide outgoing edges are
            # not and must not be attached to this focused symbol.
            if relation_kind not in {"call", "reference"}:
                continue
            if dst == path and (not label or edge_symbol == label):
                caller_id = _node_id(src, src_line, edge_symbol or src)
                nodes.setdefault(
                    caller_id,
                    {
                        "id": caller_id,
                        "path": src,
                        "label": edge_symbol or src,
                        "qualified_name": "",
                        "kind": "caller" if relation_kind == "call" else "reference",
                        "line": src_line,
                        "end_line": src_line,
                    },
                )
                edges.append({"source": caller_id, "target": focus_id, "kind": relation_kind, "weight": 1})
        return {
            "nodes": list(nodes.values())[:40],
            "edges": edges[:40],
            "degraded": answer.degraded,
            "degraded_reason": answer.degraded_reason,
            "truncated": answer.truncated,
        }

    def references(self, symbol: Mapping[str, Any], *, limit: int = 20) -> Mapping[str, Any]:
        label = str(symbol.get("label") or symbol.get("qualified_name") or "")
        path = str(symbol.get("path") or "")
        answer = self._run(kind="relations", symbol=label, limit=max(1, min(limit, 20)))
        if answer is None:
            return {"references": [], "degraded": True, "degraded_reason": "index_unavailable"}
        refs: list[dict[str, Any]] = []
        for hit in answer.hits:
            detail = hit.detail
            if str(detail.get("dst_path") or "") != path:
                continue
            if label and str(detail.get("symbol") or "") != label:
                continue
            relation_kind = str(detail.get("kind") or "reference")
            if relation_kind not in {"call", "reference"}:
                continue
            line = int(detail.get("src_line") or 0)
            refs.append(
                {
                    "path": str(detail.get("src_path") or hit.path),
                    "line": line,
                    "end_line": line,
                    "caller": "",
                    "edge_kind": relation_kind,
                }
            )
            if len(refs) >= limit:
                break
        return {
            "references": refs,
            "degraded": answer.degraded,
            "degraded_reason": answer.degraded_reason,
            "truncated": answer.truncated,
        }

    def semantic_search(self, query: str, *, limit: int = 8) -> Mapping[str, Any]:
        bounded = max(1, min(limit, 12))
        answer = self._run(kind="semantic", query=query, limit=bounded)
        if answer is None:
            return {
                "hits": [],
                "degraded": True,
                "degraded_reason": "index_unavailable",
                "truncated": False,
            }
        return {
            "hits": [hit.to_wire() for hit in answer.hits[:bounded]],
            "degraded": answer.degraded,
            "degraded_reason": answer.degraded_reason,
            "truncated": answer.truncated,
            "view_revision": answer.at_view_revision,
        }

    def ranking_metadata(self, symbol: Mapping[str, Any]) -> Mapping[str, Any]:
        del symbol
        # The native substrate does not currently expose an attribution-safe
        # centrality projection through IndexQuery. Absence is explicit; Review
        # must not synthesize a confidence/rank from another signal.
        return {}
