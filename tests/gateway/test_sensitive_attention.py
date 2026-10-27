"""Reviewers get a sourced path-attention hint without an invented bug or verification."""

from __future__ import annotations

from lemoncrow.pro.capabilities.review.criticality import (
    sensitive_path_attention,
    sensitivity_attention_reasons,
    triage_policy_for,
)
from lemoncrow.pro.capabilities.review.review_priority import (
    TIER_DONE,
    TIER_REVIEW_FIRST,
    TIER_UNDERSTAND,
    assign_review_priority,
)
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetAnnotationCounts, TargetVerification
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument


def target(path: str, *, state: str = "unreviewed") -> ReviewTarget:
    return ReviewTarget(
        target_id=path,
        unit_key=path,
        kind="symbol",
        path=path,
        label=path,
        symbol="modified",
        start_line=1,
        end_line=4,
        hunk_ordinals=(0,),
        spans=(),
        state=state,
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        structural_level="normal",
        reasons=(),
        additions=4,
        deletions=1,
        fingerprint_method="symbol_body_sha256",
        verification=TargetVerification(),
        annotation_counts=TargetAnnotationCounts(),
    )


def policy(config: str = ""):
    document = TrustedBaseDocument(".lemoncrow/review.yaml", config, "present", "trusted base")
    return triage_policy_for({}, document)


def test_sensitive_paths_are_narrow_explicit_and_do_not_promote_tests_or_docs():
    assert sensitive_path_attention("src/auth/login.py") == "authentication"
    assert sensitive_path_attention("services/payments/charge.ts") == "payments"
    assert sensitive_path_attention("db/migrations/002_accounts.sql") == "database migration"
    assert sensitive_path_attention(".github/workflows/deploy.yml") == "release automation"
    assert sensitive_path_attention("infra/main.tf") == "infrastructure configuration"
    for path in (
        "src/catalog/search.py",
        "tests/auth/test_login.py",
        "docs/payments.md",
        "src/auth/login.test.ts",
        "vendor/payments/api.go",
        "lib/auth.ts.md",
    ):
        assert sensitive_path_attention(path) == "", path


def test_canonical_priority_promotes_only_outstanding_risky_paths_without_bugs():
    targets = [
        target("src/catalog/search.py"),
        target("src/payments/charge.ts"),
        target("src/auth/login.py"),
        target("tests/auth/test_login.py"),
        target("src/auth/reviewed.py", state="reviewed"),
    ]
    hints = sensitivity_attention_reasons(targets, policy())
    ranked = assign_review_priority(targets, policy_reasons=hints)
    result = {row.path: row for row in ranked}
    for path in ("src/payments/charge.ts", "src/auth/login.py"):
        assert result[path].priority.tier == TIER_REVIEW_FIRST
        assert result[path].priority.reasons[0].code == "sensitive_path_attention"
        assert "not a verified finding" in result[path].priority.reasons[0].detail
    assert result["src/catalog/search.py"].priority.tier == TIER_UNDERSTAND
    assert result["tests/auth/test_login.py"].priority.tier == TIER_UNDERSTAND
    assert result["src/auth/reviewed.py"].priority.tier == TIER_DONE
    assert ranked[0].path in {"src/payments/charge.ts", "src/auth/login.py"}


def test_explicit_policy_retains_authority_and_hints_do_not_change_depth():
    config = """review:
  focus:
    - paths: ["src/auth/**"]
      priority: last
    - paths: ["src/catalog/**"]
      priority: first
"""
    resolved = policy(config)
    targets = [target("src/auth/login.py"), target("src/catalog/search.py")]
    hints = sensitivity_attention_reasons(targets, resolved)
    assert "src/auth/login.py" not in hints
    assert "src/catalog/search.py" not in hints
    assert resolved.effort_floors() == ()
