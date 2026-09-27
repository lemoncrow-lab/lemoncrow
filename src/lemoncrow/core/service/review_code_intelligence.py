"""Compatibility adapters for Review code intelligence.

This module lives outside the Review package deliberately: the legacy
CodeContextEngine is a deployment fallback, not Review's architecture.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.code_intelligence import (
    CodeIntelligenceLayerReadiness,
    CodeIntelligenceReadiness,
    ReadinessState,
)


class LegacyReviewCodeIntelligence:
    """Explicit degraded fallback for deployments with no native View."""

    __slots__ = ("_engine", "_repo_root")

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root.expanduser().resolve()
        self._engine: Any | None = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            from lemoncrow.core.service import code_map

            self._engine = code_map.get_engine(str(self._repo_root))
        return self._engine

    def _head(self) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self._repo_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def readiness(self) -> CodeIntelligenceReadiness:
        degraded_reason = "legacy_code_context_engine"
        try:
            engine = self._get_engine()
            status = engine.tool_status(auto_index=False, budget_tokens=4_000)
            index = status.get("index") if isinstance(status, Mapping) else {}
            symbols = int(index.get("symbols_indexed") or 0) if isinstance(index, Mapping) else 0
            state: ReadinessState = "degraded" if symbols > 0 else "building"
            reason = degraded_reason if symbols > 0 else "legacy_index_has_no_symbols"
        except Exception:
            state = "unavailable"
            reason = "legacy_index_unavailable"
        layer = CodeIntelligenceLayerReadiness(state, reason=reason, native_state="legacy")
        unavailable = CodeIntelligenceLayerReadiness(
            "unavailable", reason="legacy_layer_not_exposed", native_state="legacy"
        )
        return CodeIntelligenceReadiness(
            backend="legacy",
            lexical=layer,
            structural=layer,
            graph=layer,
            semantic=unavailable,
            base_revision=self._head(),
            reason=reason,
            degraded_reasons=(degraded_reason,),
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
        from lemoncrow.core.service import code_map

        candidates = code_map.search_symbols(self._get_engine(), symbol_name, limit=limit)
        matches = [
            item
            for item in candidates
            if (not path or str(item.get("path") or "") == path) and str(item.get("label") or "") == symbol_name
        ]
        if qualified_name:
            exact = [item for item in matches if str(item.get("qualified_name") or "") == qualified_name]
            if len(exact) == 1:
                return exact[0]
        if start_line > 0:
            exact_line = [item for item in matches if int(item.get("line") or 0) == start_line]
            if len(exact_line) == 1:
                return exact_line[0]
        return matches[0] if len(matches) == 1 else None

    def neighborhood(
        self,
        symbol: Mapping[str, Any],
        *,
        depth: int = 1,
        limit: int = 40,
    ) -> Mapping[str, Any]:
        from lemoncrow.core.service import code_map

        return code_map.build_neighborhood(
            self._get_engine(),
            str(symbol.get("id") or ""),
            depth=max(1, min(depth, 1)),
            limit=max(1, min(limit, 40)),
        )

    def references(self, symbol: Mapping[str, Any], *, limit: int = 20) -> Mapping[str, Any]:
        from lemoncrow.core.service import code_map

        return code_map.find_references(
            self._get_engine(),
            str(symbol.get("id") or ""),
            limit=max(1, min(limit, 20)),
        )

    def semantic_search(self, query: str, *, limit: int = 8) -> Mapping[str, Any]:
        del query, limit
        return {
            "hits": [],
            "degraded": True,
            "degraded_reason": "legacy_semantic_unavailable",
            "truncated": False,
        }

    def ranking_metadata(self, symbol: Mapping[str, Any]) -> Mapping[str, Any]:
        del symbol
        return {}


def open_legacy_snapshot_impact_engine(repo_root: Path) -> Any | None:
    """Capture-time adapter retained for Snapshot impact enrichment only.

    Review target identity and geometry remain source/Snapshot-derived. This
    legacy engine is used only for the older capture-time caller/centrality
    signals whose attribution guards are not yet exposed by the native query
    contract.
    """

    try:
        from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

        return CodeContextEngine(repo_root, autosync_enabled=False)
    except Exception:
        return None
