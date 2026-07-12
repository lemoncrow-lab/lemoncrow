"""Registry of the paid ("Pro") capability surface.

Every gated feature has a stable key and a human description. The open-source
core *contains* these capabilities; the license check only decides whether the
paid control surfaces (e.g. ``lc optimize apply``) are unlocked. Adding a
new gate is a one-line entry here plus an ``entitlements.require()`` call at the
seam that activates the capability.
"""

from __future__ import annotations

# Stable key -> short human description (shown in `lc license status` and
# in upgrade prompts). Keys are the contract; descriptions can change freely.
# Every key here is paid (Free is locked out). ENTERPRISE_FEATURES is the subset
# that requires the Enterprise plan; everything else is Pro-or-higher.
PRO_FEATURES: dict[str, str] = {
    # Code intelligence at scale
    "code_search": "Zoekt-backed fast code search across large repositories",
    "context_engine": "Native context engine + ANN symbol index for large repos",
    # Memory & recall
    "session_recall": "Semantic recall over all of your past sessions",
    "cross_vendor_memory": "Unified memory across Claude, Codex, and Gemini",
    # Reasoning library
    "reasoning_library": "Reusable procedures, lessons, and the review knowledge base",
    # Cost & savings engine
    "optimizer": "Apply the optimization policy that activates the savings engine",
    "savings_dashboard": "Full savings breakdown, history, and optimization detail",
    "context_compression": "Context compression and deduplication on the live turn",
    "prefix_cache": "Prefix-cache planning for warmer provider caches",
    "scoped_context": "Scoped-context pruning and line-level skimming",
    "budget_optimizer": "Per-session budget optimization",
    # Model routing
    "model_routing": "Automatic routing to cheaper models per turn",
    "cross_vendor_routing": "Cross-vendor routing across providers",
    # Orchestration
    "swarm": "Multi-worktree swarm runs",
    # Enterprise (scale + governance)
    "large_repo": "Very large repositories with no index or symbol caps",
    "shared_context": "Shared team context across repositories",
    "governance": "Governance policy, audit export, retention, and SSO",
}

# Keys that require the Enterprise plan specifically. A Pro license grants every
# PRO_FEATURES key EXCEPT these; an Enterprise license grants all of them.
ENTERPRISE_FEATURES: frozenset[str] = frozenset(
    {
        "large_repo",
        "shared_context",
        "governance",
    }
)


def describe(feature: str) -> str:
    return PRO_FEATURES.get(feature, feature)
