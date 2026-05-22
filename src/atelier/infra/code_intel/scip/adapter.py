"""SCIP-backed symbol provider for routed code-intel lookups."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from atelier.core.capabilities.code_context.call_graph import CallGraphNode
from atelier.core.capabilities.code_context.intel_store import ProviderHealth
from atelier.core.capabilities.code_context.models import SymbolRecord, UsageReference
from atelier.infra.code_intel.scip.indexer import ScipIndexer
from atelier.infra.code_intel.scip.reader import (
    LoadedScipArtifact,
    ScipArtifactError,
    ScipArtifactReader,
)
from atelier.infra.code_intel.scip.watcher import ScipArtifactWatcher


class ScipSymbolIntelProvider:
    """Routes symbol lookups through trusted repo-local `.scip` artifacts when available."""

    name = "scip"

    def __init__(
        self,
        *,
        repo_root: Path,
        repo_id: str,
        state_sync: Callable[[str, str], bool],
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_id = repo_id
        self._indexer = ScipIndexer(self.repo_root, repo_id)
        self._reader = ScipArtifactReader(repo_root=self.repo_root, allowed_roots=[self.repo_root, self._indexer.cache_root])
        self._watcher = ScipArtifactWatcher(state_sync=state_sync)
        self._artifacts: list[LoadedScipArtifact] = []
        self._health = ProviderHealth(status="unhealthy", reason="no SCIP artifacts")

    def refresh(self) -> bool:
        discovered_artifacts = self._indexer.discover_artifacts()
        changed = self._watcher.refresh([artifact.path for artifact in discovered_artifacts])
        loaded: list[LoadedScipArtifact] = []
        invalid_count = 0
        for artifact in discovered_artifacts:
            try:
                loaded.append(self._reader.load(artifact.path, origin=artifact.origin))
            except ScipArtifactError:
                invalid_count += 1
        self._artifacts = loaded
        if loaded:
            status: Literal["ok", "degraded"] = "ok" if invalid_count == 0 else "degraded"
            reason = None if invalid_count == 0 else "some SCIP artifacts were rejected"
            self._health = ProviderHealth(status=status, reason=reason)
        elif invalid_count:
            self._health = ProviderHealth(status="degraded", reason="no valid SCIP artifacts")
        else:
            self._health = ProviderHealth(status="unhealthy", reason="no SCIP artifacts")
        return changed

    def health(self) -> ProviderHealth:
        self.refresh()
        return self._health

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
        scope: Literal["repo", "external"] = "repo",
    ) -> list[SymbolRecord]:
        matches: list[SymbolRecord] = []
        seen: set[str] = set()
        for artifact in self._artifacts:
            if scope == "repo" and artifact.origin != "internal":
                continue
            if scope == "external" and artifact.origin != "external":
                continue
            for symbol in artifact.search_symbols(query, limit=limit, kind=kind, language=language):
                if symbol.symbol_id in seen:
                    continue
                seen.add(symbol.symbol_id)
                matches.append(symbol)
                if len(matches) >= limit:
                    return matches
        return matches

    def get_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> dict[str, Any] | None:
        for artifact in self._artifacts:
            payload = artifact.get_symbol(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
            if payload is not None:
                return payload
        return None

    def index_sha(self) -> str | None:
        for artifact in self._artifacts:
            return artifact.index_sha
        return None

    def find_references(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[UsageReference] | None:
        for artifact in self._artifacts:
            payload = artifact.find_references(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
            if payload is not None:
                return payload
        return None

    def find_callers(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
        for artifact in self._artifacts:
            payload = artifact.find_callers(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
            if payload is not None:
                return payload
        return None

    def find_callees(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
        for artifact in self._artifacts:
            payload = artifact.find_callees(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                file_path=file_path,
                symbol_name=symbol_name,
            )
            if payload is not None:
                return payload
        return None


__all__ = ["ScipSymbolIntelProvider"]
