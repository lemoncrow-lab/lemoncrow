"""Ast-grep structural search helpers for the existing `code` MCP surface."""

from atelier.infra.code_intel.astgrep.adapter import (
    AstGrepAdapter,
    AstGrepToolUnavailable,
    PatternMatch,
    PatternRewriteResult,
    PatternSearchResult,
)
from atelier.infra.code_intel.astgrep.binaries import (
    AstGrepBinaryResolution,
    ManagedAstGrepAsset,
    bootstrap_managed_astgrep,
    discover_astgrep_binary,
)
from atelier.infra.code_intel.astgrep.rewrite import (
    RewriteCandidate,
    RewriteOutcome,
    execute_rewrite,
)

__all__ = [
    "AstGrepAdapter",
    "AstGrepBinaryResolution",
    "AstGrepToolUnavailable",
    "ManagedAstGrepAsset",
    "PatternMatch",
    "PatternRewriteResult",
    "PatternSearchResult",
    "RewriteCandidate",
    "RewriteOutcome",
    "bootstrap_managed_astgrep",
    "discover_astgrep_binary",
    "execute_rewrite",
]
