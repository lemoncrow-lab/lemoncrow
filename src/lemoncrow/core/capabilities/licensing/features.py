"""Canonical feature registry and tier grants."""

from __future__ import annotations

# Free features referenced through the entitlement seam. A name must be listed
# here or in PRO_FEATURES; unknown names fail closed so a typo cannot silently
# expose a paid capability.
FREE_FEATURES: frozenset[str] = frozenset(
    {
        "search",
        "source_projection",
        "unlimited_repos",
    }
)

# Stable paid key -> user-facing description.
PRO_FEATURES: dict[str, str] = {
    "code_search": "Zoekt-backed fast code search across large repositories",
    "context_engine": "Native context engine + ANN symbol index for large repos",
    "session_recall": "Semantic recall over all of your past sessions",
    "cross_vendor_memory": "Unified memory across Claude, Codex, and Gemini",
    "reasoning_library": "Reusable procedures, lessons, and the review knowledge base",
    "optimizer": "Apply the optimization policy that activates the savings engine",
    "savings_dashboard": "Full savings breakdown, history, and optimization detail",
    "context_compression": "Context compression and deduplication on the live turn",
    "prefix_cache": "Prefix-cache planning for warmer provider caches",
    "scoped_context": "Scoped-context pruning and line-level skimming",
    "budget_optimizer": "Per-session budget optimization",
    "model_routing": "Automatic routing to cheaper models per turn",
    "cross_vendor_routing": "Cross-vendor routing across providers",
    "swarm": "Multi-worktree swarm runs",
    "large_repo": "Very large repositories with no index or symbol caps",
    "shared_context": "Shared team context across repositories",
    "governance": "Governance policy, audit export, retention, and SSO",
}

# Lite matches the public product: single-repo Zoekt, cross-session recall, and
# the basic optimizer. It does not inherit Pro routing, cross-vendor memory,
# full dashboards, large-repo context, swarm, or Enterprise governance.
LITE_FEATURES: frozenset[str] = frozenset(
    {
        "code_search",
        "session_recall",
        "optimizer",
    }
)

ENTERPRISE_FEATURES: frozenset[str] = frozenset(
    {
        "large_repo",
        "shared_context",
        "governance",
    }
)

PAID_PLANS: frozenset[str] = frozenset({"lite", "pro", "enterprise"})


def features_for_plan(plan: str) -> frozenset[str]:
    """Return the explicit paid feature set for a canonical plan."""

    normalized = plan.strip().lower()
    if normalized == "lite":
        return LITE_FEATURES
    if normalized == "pro":
        return frozenset(PRO_FEATURES).difference(ENTERPRISE_FEATURES)
    if normalized == "enterprise":
        return frozenset(PRO_FEATURES)
    return frozenset()


def plan_grants(plan: str, feature: str) -> bool:
    if feature in FREE_FEATURES:
        return True
    if feature not in PRO_FEATURES:
        return False
    return feature in features_for_plan(plan)


def minimum_plan(feature: str) -> str:
    if feature in LITE_FEATURES:
        return "Lite"
    if feature in ENTERPRISE_FEATURES:
        return "Enterprise"
    return "Pro"


def describe(feature: str) -> str:
    return PRO_FEATURES.get(feature, feature)
