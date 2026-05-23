"""Fixture-friendly SCIP routing support for code-intel symbol lookups."""

from atelier.infra.code_intel.scip.adapter import ScipSymbolIntelProvider
from atelier.infra.code_intel.scip.binaries import discover_scip_binaries, discover_scip_binary
from atelier.infra.code_intel.scip.external_artifacts import (
    DiscoveredScipArtifact,
    classify_scip_artifact,
    discover_external_scip_artifacts,
)
from atelier.infra.code_intel.scip.indexer import ScipIndexer, default_scip_cache_root
from atelier.infra.code_intel.scip.reader import ScipArtifactError, ScipArtifactReader
from atelier.infra.code_intel.scip.watcher import ScipArtifactWatcher

__all__ = [
    "DiscoveredScipArtifact",
    "ScipArtifactError",
    "ScipArtifactReader",
    "ScipArtifactWatcher",
    "ScipIndexer",
    "ScipSymbolIntelProvider",
    "classify_scip_artifact",
    "default_scip_cache_root",
    "discover_external_scip_artifacts",
    "discover_scip_binaries",
    "discover_scip_binary",
]
