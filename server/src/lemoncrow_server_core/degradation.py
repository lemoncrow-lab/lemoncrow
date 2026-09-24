"""Structured degradation.

The design's degradation table, implemented rather than described:

============================================  ==================================
Condition                                     Behaviour
============================================  ==================================
Version mismatch                               Refuse explicitly (``protocol``)
Server-side tool with the server unreachable   Client-side concern; the typed
                                               error the client surfaces comes
                                               from this module's vocabulary
Blob miss on ``read``/``code_search``          ``{need:[...]}`` then one retry
                                               (:mod:`.blobmiss`)
Link index cold                                Answer returns with
                                               ``degraded: true`` and a reason
View not fully materializable                  Same: ``degraded: true``, with
                                               the count of paths the tree
                                               could not contain
============================================  ==================================

The important half is the last two rows. A cold Layer 3 must not fail a session
and must not silently return a thinner answer: the answer comes back, flagged,
with the layer and the reason named, so an agent can decide whether to trust a
call graph or fall back to lexical search itself.

The ``workspace`` layer is the same contract applied to the materialized view
(:mod:`.workspace`). A view can name files whose bytes the server does not hold
-- one over the content size cap, which is manifested but by policy never
uploaded, or one whose upload has not happened -- and those files are not in
the tree the tools scan. An answer computed over such a tree is narrower than
the view it claims to describe, so it is flagged with the count rather than
returned as if the tree were whole.

Ordering is deliberate: a cold derived layer is reported before an incomplete
workspace, because the derived layer is the one the tool was reaching for.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .index.contracts import IndexBackend, IndexLayerStatus
from .workspace import WorkspaceStatus

__all__ = ["LINK_DEPENDENT_TOOLS", "SEMANTIC_DEPENDENT_TOOLS", "DegradationPolicy", "DegradationSignal"]

#: Tools whose answers are resolved through the per-view link layer.
LINK_DEPENDENT_TOOLS: Final[frozenset[str]] = frozenset({"relations", "graph"})

#: Tools backed by embeddings.
SEMANTIC_DEPENDENT_TOOLS: Final[frozenset[str]] = frozenset({"search"})

#: Tools that answer from exact paths and lexical rows only. Listed so the
#: "lexical stays available" half of the contract is explicit, and so a test
#: can assert these never come back degraded for a link-layer reason.
LEXICAL_TOOLS: Final[frozenset[str]] = frozenset({"read", "code_search"})


@dataclass(frozen=True, slots=True)
class DegradationSignal:
    """Why an answer is degraded."""

    layer: str
    reason: str
    status: IndexLayerStatus
    #: Scalar counts that make the flag actionable -- how many paths the tree
    #: could not contain, for instance. Counts only: never a path, never bytes.
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def wire_reason(self) -> str:
        return f"{self.layer}:{self.reason}"

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "layer": self.layer,
            "reason": self.reason,
            "status": self.status.to_wire(),
        }
        if self.counts:
            payload["counts"] = dict(self.counts)
        return payload


class DegradationPolicy:
    """Maps a tool plus index state to a degradation signal."""

    __slots__ = ("_backend", "_link_tools", "_semantic_tools")

    def __init__(
        self,
        backend: IndexBackend,
        *,
        link_tools: frozenset[str] = LINK_DEPENDENT_TOOLS,
        semantic_tools: frozenset[str] = SEMANTIC_DEPENDENT_TOOLS,
    ) -> None:
        self._backend = backend
        self._link_tools = link_tools
        self._semantic_tools = semantic_tools

    def evaluate(
        self,
        *,
        tool: str,
        org_id: str,
        view_id: str | None,
        workspace: WorkspaceStatus | None = None,
    ) -> DegradationSignal | None:
        """``None`` when the answer is fully backed.

        Without a bound view there is no per-view layer to be cold, so a
        session that has not opened a view is not reported as degraded -- it is
        simply not using the layers.

        ``workspace`` is the materialization report for the call, when one was
        produced. It applies to *every* tool, not just the link- and
        semantic-dependent ones, because a file missing from the tree is missing
        from whatever the tool did with the tree.
        """
        if view_id is None:
            return None
        if tool in self._link_tools:
            status = self._backend.links.status(org_id, view_id)
            if not status.ready:
                return DegradationSignal(layer="link", reason=status.reason or status.state.value, status=status)
        if tool in self._semantic_tools:
            status = self._backend.semantic.status(org_id, view_id)
            if not status.ready:
                return DegradationSignal(layer="semantic", reason=status.reason or status.state.value, status=status)
        if workspace is not None and not workspace.complete:
            return DegradationSignal(
                layer="workspace",
                reason=workspace.reason,
                status=workspace.as_layer_status(),
                counts=workspace.counts(),
            )
        return None

    def layer_report(self, *, org_id: str, view_id: str) -> Mapping[str, Any]:
        """Per-view readiness, for the session and view status routes."""
        return {
            "link": self._backend.links.status(org_id, view_id).to_wire(),
            "semantic": self._backend.semantic.status(org_id, view_id).to_wire(),
        }
