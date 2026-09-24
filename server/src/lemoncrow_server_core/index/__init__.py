"""The three-layer index: contracts, algorithms and two complete stores.

* :mod:`.contracts` -- the shapes and the ports every store must satisfy.
* :mod:`.analysis` -- Layer 1, derived: content-local analysis of one blob.
* :mod:`.manifest` -- Layer 2 on the wire: canonical roots, chunks, the
  ``.gitignore``-respecting walk and the content size cap.
* :mod:`.links` -- Layer 3, the pure resolution and incremental-update
  algorithm the differential oracle tests.
* :mod:`.layers` -- where a build is actually driven, with a bounded budget and
  an explicit readiness contract.
* :mod:`.query` -- Layer-2-filtered queries over Layer 1 and Layer 3.
* :mod:`.lifecycle` -- refcounted blob GC with a retention window, and view
  eviction by lease.
* :mod:`.localfs` -- the negotiated same-host read optimization.
* :mod:`.memory` / :mod:`.sqlite` -- the in-process and durable stores. They
  share every algorithm above and differ only in persistence.
"""

from __future__ import annotations

from .analysis import (
    EMBEDDING_DIM,
    PARSER_PROFILES,
    LexicalQuery,
    ParserProfile,
    analyze,
    parse_query,
    profile_for_path,
    score_match,
)
from .contracts import (
    DEFAULT_PARSER_PROFILE,
    AnalysisArtifact,
    AnalysisStore,
    ContentRef,
    ContentStore,
    Edge,
    EdgeKind,
    ImportRef,
    IndexBackend,
    IndexLayer,
    IndexLayerState,
    IndexLayerStatus,
    LinkBuildMode,
    LinkBuildReport,
    LinkIndex,
    ManifestEntry,
    RepoIdentityClaim,
    RepoIdentityService,
    SemanticIndex,
    SweepResult,
    SymbolDef,
    SymbolRef,
    ViewOpenResult,
    ViewState,
    ViewStore,
    sha256_hex,
    validate_digest,
    validate_path,
)
from .layers import LayerSources, LinkLayer, SemanticLayer
from .lifecycle import Maintenance, MaintenanceReport, StorageReport
from .links import (
    Divergence,
    FileFacts,
    LinkGraph,
    build_graph,
    classify_divergence,
    update_graph,
)
from .localfs import LocalFsGrant, LocalFsSource
from .manifest import (
    EMPTY_MANIFEST_ROOT,
    MANIFEST_FORMAT,
    GitIgnore,
    ManifestChunk,
    chunk_manifest,
    manifest_root,
    required_digests,
    walk_worktree,
)
from .memory import (
    InMemoryAnalysisStore,
    InMemoryContentStore,
    InMemoryRepoIdentityService,
    InMemoryViewStore,
    build_memory_backend,
)
from .query import IndexQuery, QueryAnswer, QueryHit
from .sqlite import SqliteIndex, build_sqlite_backend

__all__ = [
    "DEFAULT_PARSER_PROFILE",
    "EMBEDDING_DIM",
    "EMPTY_MANIFEST_ROOT",
    "MANIFEST_FORMAT",
    "PARSER_PROFILES",
    "AnalysisArtifact",
    "AnalysisStore",
    "ContentRef",
    "ContentStore",
    "Divergence",
    "Edge",
    "EdgeKind",
    "FileFacts",
    "GitIgnore",
    "ImportRef",
    "InMemoryAnalysisStore",
    "InMemoryContentStore",
    "InMemoryRepoIdentityService",
    "InMemoryViewStore",
    "IndexBackend",
    "IndexLayer",
    "IndexLayerState",
    "IndexLayerStatus",
    "IndexQuery",
    "LayerSources",
    "LexicalQuery",
    "LinkBuildMode",
    "LinkBuildReport",
    "LinkGraph",
    "LinkIndex",
    "LinkLayer",
    "LocalFsGrant",
    "LocalFsSource",
    "Maintenance",
    "MaintenanceReport",
    "ManifestChunk",
    "ManifestEntry",
    "ParserProfile",
    "QueryAnswer",
    "QueryHit",
    "RepoIdentityClaim",
    "RepoIdentityService",
    "SemanticIndex",
    "SemanticLayer",
    "SqliteIndex",
    "StorageReport",
    "SweepResult",
    "SymbolDef",
    "SymbolRef",
    "ViewOpenResult",
    "ViewState",
    "ViewStore",
    "analyze",
    "build_graph",
    "build_memory_backend",
    "build_sqlite_backend",
    "chunk_manifest",
    "classify_divergence",
    "manifest_root",
    "parse_query",
    "profile_for_path",
    "required_digests",
    "score_match",
    "sha256_hex",
    "update_graph",
    "validate_digest",
    "validate_path",
    "walk_worktree",
]
