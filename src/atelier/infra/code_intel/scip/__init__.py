"""Fixture-friendly SCIP routing support for code-intel symbol lookups."""

from atelier.infra.code_intel.scip.adapter import ScipSymbolIntelProvider
from atelier.infra.code_intel.scip.binaries import discover_scip_binary, discover_scip_binaries
from atelier.infra.code_intel.scip.indexer import ScipIndexer, default_scip_cache_root
from atelier.infra.code_intel.scip.reader import ScipArtifactError, ScipArtifactReader
from atelier.infra.code_intel.scip.watcher import ScipArtifactWatcher

__all__ = [
    "ScipArtifactError",
    "ScipArtifactReader",
    "ScipArtifactWatcher",
    "ScipIndexer",
    "ScipSymbolIntelProvider",
    "default_scip_cache_root",
    "discover_scip_binaries",
    "discover_scip_binary",
]
