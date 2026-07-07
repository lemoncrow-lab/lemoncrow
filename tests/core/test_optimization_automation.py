from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.optimization.automation import (
    PROPOSAL_ARTIFACT_PATH,
    _evaluate_proposal,
)
from atelier.core.capabilities.optimization.policy import AutomationConfig, BenchmarkEvidence


def test_evaluate_proposal_fails_closed_without_evidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("atelier.core.capabilities.optimization.automation.emit_product_local", lambda *a, **k: None)

    result = _evaluate_proposal(
        repo_root=tmp_path,
        store_root=tmp_path / ".atelier",
        source="cli",
        open_pr=True,
        dry_run=False,
        advisor={"has_recommendation": True, "weekly_savings_usd": 3.0},
        legacy_report={"estimated_tokens_saved": 5000},
        automation=AutomationConfig(enabled=False, minimum_projected_tokens_saved=1000),
        evidence=BenchmarkEvidence(),
    )

    assert result["action"] == "missing_non_inferiority_evidence"
    assert result["artifact_path"] is None
    assert result["open_pr"] is None


def test_evaluate_proposal_writes_repo_root_artifact(monkeypatch, tmp_path: Path) -> None:
    class _Verdict:
        passed = True

        def to_dict(self) -> dict[str, object]:
            return {"passed": True, "delta_lower_bound": 0.02}

    monkeypatch.setattr("atelier.core.capabilities.optimization.automation.emit_product_local", lambda *a, **k: None)
    monkeypatch.setattr(
        "atelier.core.capabilities.optimization.automation.load_terminalbench_records",
        lambda path: [],
    )
    monkeypatch.setattr(
        "atelier.core.capabilities.optimization.automation.evaluate_non_inferiority",
        lambda *a, **k: _Verdict(),
    )

    repo_root = tmp_path / "repo"
    store_root = repo_root / ".atelier"
    store_root.mkdir(parents=True)

    result = _evaluate_proposal(
        repo_root=repo_root,
        store_root=store_root,
        source="servicectl",
        open_pr=False,
        dry_run=False,
        advisor={
            "has_recommendation": True,
            "recommended_preset": "economy",
            "recommended_quality_floor": 0.9,
            "recommended_confidence_required": "medium",
            "weekly_savings_usd": 4.2,
        },
        legacy_report={"estimated_tokens_saved": 5000},
        automation=AutomationConfig(enabled=True, minimum_projected_tokens_saved=1000),
        evidence=BenchmarkEvidence(
            runs_path="runs.jsonl",
            baseline_cost_usd=10.0,
            candidate_cost_usd=7.5,
        ),
    )

    artifact_path = repo_root / PROPOSAL_ARTIFACT_PATH
    assert result["action"] == "artifact_written"
    assert result["artifact_path"] == str(artifact_path)
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["proposal"]["recommended_preset"] == "economy"
    assert payload["proposal"]["estimated_tokens_saved"] == 5000


def test_evaluate_proposal_uses_nested_advisor_recommendation(monkeypatch, tmp_path: Path) -> None:
    class _Verdict:
        passed = True

        def to_dict(self) -> dict[str, object]:
            return {"passed": True, "delta_lower_bound": 0.03}

    monkeypatch.setattr("atelier.core.capabilities.optimization.automation.emit_product_local", lambda *a, **k: None)
    monkeypatch.setattr(
        "atelier.core.capabilities.optimization.automation.load_terminalbench_records",
        lambda path: [],
    )
    monkeypatch.setattr(
        "atelier.core.capabilities.optimization.automation.evaluate_non_inferiority",
        lambda *a, **k: _Verdict(),
    )

    repo_root = tmp_path / "repo"
    store_root = repo_root / ".atelier"
    store_root.mkdir(parents=True)

    result = _evaluate_proposal(
        repo_root=repo_root,
        store_root=store_root,
        source="servicectl",
        open_pr=False,
        dry_run=False,
        advisor={
            "has_recommendation": True,
            "recommended_candidate_id": "balanced",
            "recommended_policy": {
                "preset": "recommended",
                "quality_floor": 0.96,
                "confidence_required": "medium",
                "routing": {"policy": "complexity_escalate"},
            },
            "weekly_savings_usd": 4.2,
            "estimation": {"source": "stored_atelier_traces", "savings_are_estimates": True},
        },
        legacy_report={"estimated_tokens_saved": 5000},
        automation=AutomationConfig(enabled=True, minimum_projected_tokens_saved=1000),
        evidence=BenchmarkEvidence(
            runs_path="runs.jsonl",
            baseline_cost_usd=10.0,
            candidate_cost_usd=7.5,
        ),
    )

    artifact_path = repo_root / PROPOSAL_ARTIFACT_PATH
    assert result["action"] == "artifact_written"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["proposal"]["recommended_candidate_id"] == "balanced"
    assert payload["proposal"]["recommended_preset"] == "balanced"
    assert payload["proposal"]["quality_floor"] == 0.96
    assert payload["proposal"]["confidence_required"] == "medium"
    assert payload["proposal"]["recommended_policy"]["routing"]["policy"] == "complexity_escalate"
    assert payload["proposal"]["estimation"]["savings_are_estimates"] is True
