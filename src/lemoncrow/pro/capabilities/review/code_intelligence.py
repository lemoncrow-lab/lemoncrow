"""Review-facing code-intelligence contract.

Review owns immutable Snapshot truth. Implementations of this protocol expose only
bounded *current* repository intelligence and must never imply Snapshot exactness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ReadinessState = Literal["ready", "building", "degraded", "unavailable"]


@dataclass(frozen=True, slots=True)
class CodeIntelligenceLayerReadiness:
    state: ReadinessState
    reason: str = ""
    native_state: str = ""
    covered_paths: int = 0
    at_view_revision: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "native_state": self.native_state,
            "covered_paths": self.covered_paths,
            "at_view_revision": self.at_view_revision,
        }


@dataclass(frozen=True, slots=True)
class CodeIntelligenceReadiness:
    backend: str
    lexical: CodeIntelligenceLayerReadiness
    structural: CodeIntelligenceLayerReadiness
    graph: CodeIntelligenceLayerReadiness
    semantic: CodeIntelligenceLayerReadiness
    view_id: str = ""
    view_revision: int = 0
    generation_id: str = ""
    base_revision: str = ""
    manifest_root: str = ""
    reason: str = ""
    degraded_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return any(layer.state != "unavailable" for layer in (self.lexical, self.structural, self.graph, self.semantic))

    def to_wire(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "view_id": self.view_id,
            "view_revision": self.view_revision,
            "generation_id": self.generation_id,
            "base_revision": self.base_revision,
            "manifest_root": self.manifest_root,
            "reason": self.reason,
            "degraded_reasons": list(self.degraded_reasons),
            "lexical": self.lexical.to_wire(),
            "structural": self.structural.to_wire(),
            "graph": self.graph.to_wire(),
            "semantic": self.semantic.to_wire(),
        }


def unavailable_readiness(reason: str, *, backend: str = "none") -> CodeIntelligenceReadiness:
    layer = CodeIntelligenceLayerReadiness("unavailable", reason=reason)
    return CodeIntelligenceReadiness(
        backend=backend,
        lexical=layer,
        structural=layer,
        graph=layer,
        semantic=layer,
        reason=reason,
    )


class CodeIntelligenceProvider(Protocol):
    """Small bounded capability seam consumed by Review.

    Values returned here are current-index context. They are never immutable
    Review Snapshot evidence, even if the provider's base revision equals the
    Review head SHA.
    """

    def readiness(self) -> CodeIntelligenceReadiness: ...

    def find_symbol(
        self,
        symbol_name: str,
        *,
        path: str = "",
        qualified_name: str = "",
        start_line: int = 0,
        limit: int = 12,
    ) -> Mapping[str, Any] | None: ...

    def neighborhood(
        self,
        symbol: Mapping[str, Any],
        *,
        depth: int = 1,
        limit: int = 40,
    ) -> Mapping[str, Any]: ...

    def references(self, symbol: Mapping[str, Any], *, limit: int = 20) -> Mapping[str, Any]: ...

    def semantic_search(self, query: str, *, limit: int = 8) -> Mapping[str, Any]:
        """Bounded current-index semantic retrieval for Review Ask."""
        ...

    def ranking_metadata(self, symbol: Mapping[str, Any]) -> Mapping[str, Any]: ...
