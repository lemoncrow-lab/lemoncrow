"""Tests for Rubric Gate V2 (escalate status, audit-service rubric)."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.foundation.models import Rubric
from lemoncrow.core.foundation.rubric_gate import run_rubric


def test_rubric_returns_escalate_when_any_escalation_condition_true() -> None:
    rub = Rubric(
        id="r",
        domain="d",
        required_checks=["a"],
        block_if_missing=[],
        warning_checks=[],
        escalation_conditions=["danger"],
    )
    res = run_rubric(rub, {"a": True, "danger": True})
    assert res.status == "escalate"
    assert "danger" in res.escalations


def test_rubric_returns_blocked_when_required_missing_in_block_list() -> None:
    rub = Rubric(
        id="r",
        domain="d",
        required_checks=["a"],
        block_if_missing=["a"],
    )
    res = run_rubric(rub, {})
    assert res.status == "blocked"


def test_rubric_returns_warn_when_required_missing_not_in_block_list() -> None:
    rub = Rubric(
        id="r",
        domain="d",
        required_checks=["a"],
        block_if_missing=[],
    )
    res = run_rubric(rub, {"a": False})
    assert res.status == "warn"


def test_rubric_returns_pass_when_all_required_pass() -> None:
    rub = Rubric(id="r", domain="d", required_checks=["a"], block_if_missing=[])
    res = run_rubric(rub, {"a": True})
    assert res.status == "pass"


def test_change_gate_rubric_loads(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "rubric.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "rubric_change_gate_discipline",
                "domain": "policy.change",
                "required_checks": ["concrete_anchor_identified"],
                "block_if_missing": ["concrete_anchor_identified"],
            }
        ),
        encoding="utf-8",
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rub = Rubric.model_validate(data)
    assert rub.id == "rubric_change_gate_discipline"
    res = run_rubric(rub, {})
    assert res.status == "blocked"  # required+blocking checks missing


class TestLoadPackagedRubrics:
    def test_returns_empty_list(self) -> None:
        from lemoncrow.core.foundation.rubric_gate import load_packaged_rubrics

        rubrics = load_packaged_rubrics()
        assert rubrics == []

    def test_code_review_rubric_blocks_when_findings_unclassified(self) -> None:
        rub = Rubric(
            id="rubric_code_review",
            domain="coding.review",
            required_checks=["all_findings_severity_classified"],
            block_if_missing=["all_findings_severity_classified"],
        )
        res = run_rubric(rub, {})
        assert res.status == "blocked"

    def test_verification_ladder_blocks_when_existence_missing(self) -> None:
        rub = Rubric(
            id="rubric_verification_ladder",
            domain="coding.verification",
            required_checks=["existence_confirmed"],
            block_if_missing=["existence_confirmed"],
        )
        res = run_rubric(rub, {})
        assert res.status == "blocked"

    def test_verification_ladder_passes_when_all_required_pass(self) -> None:
        rub = Rubric(
            id="rubric_verification_ladder",
            domain="coding.verification",
            required_checks=["existence_confirmed", "substantive_not_stub"],
            block_if_missing=["existence_confirmed", "substantive_not_stub"],
        )
        res = run_rubric(
            rub,
            {
                "existence_confirmed": True,
                "substantive_not_stub": True,
                "wired_to_callsites": True,
                "data_flow_verified_or_not_applicable": True,
                "error_path_handled": True,
                "cross_module_consistency_checked": True,
            },
        )
        assert res.status == "pass"

    def test_store_init_does_not_seed_packaged_rubrics(self, tmp_path: Path) -> None:
        from lemoncrow.core.foundation.store import ContextStore

        store = ContextStore(tmp_path)
        store.init()

        assert store.get_rubric("rubric_code_review") is None
        assert store.get_rubric("rubric_verification_ladder") is None
