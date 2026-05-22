"""Tests for WP-32 — cost-quality proof gate capability.

Verifies:
- ProofGateConfig holds correct default thresholds
- ProofReport model validates correctly
- ProofGateCapability.run() returns pass when all thresholds met
- ProofGateCapability.run() returns fail with named thresholds when any is breached
- Failed cheap attempts count against cost and regression rate
- missing trace_id causes missing_trace_evidence failure
- context_reduction_pct below threshold causes failure
- Host enforcement matrix includes all 5 hosts
- Feature boundary labels are present and categorised
- Proof report is saved to .atelier/proof/proof-report.json
- Proof report can be reloaded via load()
- Markdown report is written to .atelier/proof/proof-report.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.proof_gate.capability import (
    BenchmarkCase,
    ProofGateCapability,
    ProofGateConfig,
    ProofReport,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passing_cases(session_id: str) -> list[BenchmarkCase]:
    """Minimal passing benchmark cases: 3 cheap (2 accepted), 1 mid, 1 premium."""
    return [
        BenchmarkCase(
            case_id=f"{session_id}:cheap-01",
            tier="cheap",
            accepted=True,
            cost_usd=0.002,
            trace_id=f"{session_id}:trace:cheap-01",
            session_id=session_id,
            verifier_outcome="pass",
        ),
        BenchmarkCase(
            case_id=f"{session_id}:cheap-02",
            tier="cheap",
            accepted=False,
            cost_usd=0.002,
            trace_id=f"{session_id}:trace:cheap-02",
            session_id=session_id,
            verifier_outcome="fail",
        ),
        BenchmarkCase(
            case_id=f"{session_id}:cheap-03",
            tier="cheap",
            accepted=True,
            cost_usd=0.002,
            trace_id=f"{session_id}:trace:cheap-03",
            session_id=session_id,
            verifier_outcome="pass",
        ),
        BenchmarkCase(
            case_id=f"{session_id}:mid-01",
            tier="mid",
            accepted=True,
            cost_usd=0.008,
            trace_id=f"{session_id}:trace:mid-01",
            session_id=session_id,
            verifier_outcome="pass",
        ),
        BenchmarkCase(
            case_id=f"{session_id}:premium-01",
            tier="premium",
            accepted=True,
            cost_usd=0.05,
            trace_id=f"{session_id}:trace:premium-01",
            session_id=session_id,
            verifier_outcome="pass",
        ),
    ]


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


def test_proof_gate_config_defaults() -> None:
    cfg = ProofGateConfig()
    assert cfg.context_reduction_pct_min == 50.0
    assert cfg.routing_regression_rate_max == 0.02
    assert cfg.min_cheap_success_rate == 0.60
    assert cfg.premium_only_baseline_cost_per_accepted_patch > 0
    assert 0.0 < cfg.premium_only_baseline_accepted_patch_rate <= 1.0


def test_proof_gate_config_custom() -> None:
    cfg = ProofGateConfig(context_reduction_pct_min=60.0, min_cheap_success_rate=0.70)
    assert cfg.context_reduction_pct_min == 60.0
    assert cfg.min_cheap_success_rate == 0.70


def test_proof_gate_config_extra_fields_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProofGateConfig.model_validate({"unknown_field": 1})


# ---------------------------------------------------------------------------
# BenchmarkCase model
# ---------------------------------------------------------------------------


def test_benchmark_case_defaults() -> None:
    case = BenchmarkCase(
        case_id="c1",
        tier="cheap",
        accepted=True,
        cost_usd=0.001,
        trace_id="t1",
    )
    assert case.escalated is False
    assert case.regression is False
    assert case.verifier_outcome is None


def test_benchmark_case_extra_fields_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate({"case_id": "c1", "tier": "cheap", "accepted": True, "cost_usd": 0.001, "bad": 1})


# ---------------------------------------------------------------------------
# ProofGateCapability.run() — pass path
# ---------------------------------------------------------------------------


def test_proof_run_returns_pass_when_all_thresholds_met(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cases = _passing_cases("test-run")
    report = cap.run(
        session_id="test-run",
        context_reduction_pct=55.0,
        benchmark_cases=cases,
        save=False,
    )
    assert report.status == "pass"
    assert report.failed_thresholds == []


def test_proof_run_returns_proofReport_instance(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cases = _passing_cases("test-run")
    report = cap.run(
        session_id="test-run",
        context_reduction_pct=55.0,
        benchmark_cases=cases,
        save=False,
    )
    assert isinstance(report, ProofReport)
    assert report.session_id == "test-run"


def test_proof_run_computes_correct_accepted_patch_rate(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cases = _passing_cases("test-run")
    # 4 out of 5 accepted
    report = cap.run(
        session_id="test-run",
        context_reduction_pct=55.0,
        benchmark_cases=cases,
        save=False,
    )
    assert report.accepted_patch_rate == pytest.approx(0.8, abs=1e-4)


def test_proof_run_computes_cheap_success_rate(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cases = _passing_cases("test-run")
    # 2 of 3 cheap cases accepted
    report = cap.run(
        session_id="test-run",
        context_reduction_pct=55.0,
        benchmark_cases=cases,
        save=False,
    )
    assert report.cheap_success_rate == pytest.approx(2 / 3, abs=1e-4)


# ---------------------------------------------------------------------------
# ProofGateCapability.run() — fail paths
# ---------------------------------------------------------------------------


def test_proof_fails_when_context_reduction_below_threshold(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cases = _passing_cases("test-run")
    report = cap.run(
        session_id="test-run",
        context_reduction_pct=40.0,  # below 50.0 threshold
        benchmark_cases=cases,
        save=False,
    )
    assert report.status == "fail"
    assert "context_reduction_pct" in report.failed_thresholds


def test_proof_fails_when_regression_rate_too_high(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cases = _passing_cases("reg-run")
    # Mark 2 of 5 as regressions → 40% > 2% limit
    cases[0] = BenchmarkCase(**{**cases[0].model_dump(), "regression": True})
    cases[1] = BenchmarkCase(**{**cases[1].model_dump(), "regression": True})
    report = cap.run(
        session_id="reg-run",
        context_reduction_pct=55.0,
        benchmark_cases=cases,
        save=False,
    )
    assert report.status == "fail"
    assert "routing_regression_rate" in report.failed_thresholds


def test_proof_fails_when_missing_trace_evidence(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cases = _passing_cases("trace-run")
    # Remove trace_id from one case
    cases[0] = BenchmarkCase(**{**cases[0].model_dump(), "trace_id": None})
    report = cap.run(
        session_id="trace-run",
        context_reduction_pct=55.0,
        benchmark_cases=cases,
        save=False,
    )
    assert report.status == "fail"
    assert "missing_trace_evidence" in report.failed_thresholds


def test_proof_failed_cheap_count_against_cost(tmp_path: Path) -> None:
    """Failed cheap attempts must count against total cost — they cannot be elided."""
    cap = ProofGateCapability(tmp_path)
    # Only cheap cases, all failing, cost each $0.50 — total cost $2.50, 0 accepted
    cases = [
        BenchmarkCase(
            case_id=f"fail-cheap-{i}",
            tier="cheap",
            accepted=False,
            cost_usd=0.50,
            trace_id=f"trace-fail-{i}",
        )
        for i in range(5)
    ]
    cfg = ProofGateConfig(
        premium_only_baseline_cost_per_accepted_patch=1.0,
        min_cheap_success_rate=0.01,  # very low, so only cost threshold trips
    )
    report = cap.run(
        session_id="fail-cheap-run",
        context_reduction_pct=55.0,
        benchmark_cases=cases,
        config=cfg,
        save=False,
    )
    # cost_per_accepted_patch = total_cost (2.5) since accepted_count=0
    assert report.cost_per_accepted_patch == pytest.approx(2.5, abs=1e-4)
    assert "cost_per_accepted_patch" in report.failed_thresholds


# ---------------------------------------------------------------------------
# Host enforcement matrix
# ---------------------------------------------------------------------------


def test_proof_host_enforcement_matrix_has_all_hosts(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    report = cap.run(
        session_id="host-check",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("host-check"),
        save=False,
    )
    host_names = {h.host for h in report.host_enforcement_matrix}
    assert host_names == {"claude", "codex", "copilot", "opencode", "antigravity"}


def test_proof_host_enforcement_matrix_provider_enforced_disabled(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    report = cap.run(
        session_id="pe-check",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("pe-check"),
        save=False,
    )
    for h in report.host_enforcement_matrix:
        assert h.provider_enforced_disabled is True


def test_proof_host_enforcement_copilot_is_advisory(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    report = cap.run(
        session_id="copilot-check",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("copilot-check"),
        save=False,
    )
    copilot = next(h for h in report.host_enforcement_matrix if h.host == "copilot")
    assert copilot.mode == "advisory"
    assert copilot.can_block_start is False


# ---------------------------------------------------------------------------
# Feature boundary labels
# ---------------------------------------------------------------------------


def test_proof_feature_labels_present(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    report = cap.run(
        session_id="label-check",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("label-check"),
        save=False,
    )
    labels = report.feature_boundary_labels
    assert "routing_decision" in labels
    assert labels["routing_decision"] == "Atelier augmentation"
    assert "model_selection" in labels
    assert labels["model_selection"] == "Host-native"
    assert "provider_model_override" in labels
    assert labels["provider_model_override"] == "Future-only"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_proof_report_saved_to_disk(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cap.run(
        session_id="save-test",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("save-test"),
        save=True,
    )
    json_path = tmp_path / "proof" / "proof-report.json"
    assert json_path.exists()
    assert json_path.stat().st_size > 0


def test_proof_markdown_saved_to_disk(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    cap.run(
        session_id="md-test",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("md-test"),
        save=True,
    )
    md_path = tmp_path / "proof" / "proof-report.md"
    assert md_path.exists()
    content = md_path.read_text(encoding="utf-8")
    assert "Cost-Quality Proof Report" in content
    assert "Host Enforcement Matrix" in content


def test_proof_report_can_be_reloaded(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    original = cap.run(
        session_id="reload-test",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("reload-test"),
        save=True,
    )
    loaded = cap.load()
    assert loaded is not None
    assert loaded.session_id == original.session_id
    assert loaded.status == original.status


def test_proof_load_returns_none_when_no_report(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    assert cap.load() is None


def test_proof_report_status_is_pass_or_fail(tmp_path: Path) -> None:
    cap = ProofGateCapability(tmp_path)
    report = cap.run(
        session_id="status-check",
        context_reduction_pct=55.0,
        benchmark_cases=_passing_cases("status-check"),
        save=False,
    )
    assert report.status in ("pass", "fail")
