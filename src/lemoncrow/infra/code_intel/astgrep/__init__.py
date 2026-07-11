"""Ast-grep structural search helpers for the existing `code` MCP surface."""

from lemoncrow.infra.code_intel.astgrep.adapter import (
    AstGrepAdapter,
    AstGrepToolUnavailable,
    PatternMatch,
    PatternRewriteResult,
    PatternSearchResult,
    RuleMatch,
    RuleScanResult,
)
from lemoncrow.infra.code_intel.astgrep.binaries import (
    AstGrepBinaryResolution,
    ManagedAstGrepAsset,
    bootstrap_managed_astgrep,
    discover_astgrep_binary,
)
from lemoncrow.infra.code_intel.astgrep.rewrite import (
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
    "RuleMatch",
    "RuleScanResult",
    "bootstrap_managed_astgrep",
    "discover_astgrep_binary",
    "execute_rewrite",
]
