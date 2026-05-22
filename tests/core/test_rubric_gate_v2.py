"""Tests for Rubric Gate V2 (escalate status, audit-service rubric)."""

from __future__ import annotations

from pathlib import Path

from atelier.core.foundation.models import Rubric
from atelier.core.foundation.rubric_gate import run_rubric


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
    from importlib import resources

    import yaml

    pkg = resources.files("atelier") / "core" / "rubrics" / "rubric_change_gate_discipline.yaml"
    with resources.as_file(pkg) as p, open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    rub = Rubric.model_validate(data)
    assert rub.id == "rubric_change_gate_discipline"
    res = run_rubric(rub, {})
    assert res.status == "blocked"  # required+blocking checks missing


class TestLoadPackagedRubrics:
    def test_returns_nonempty_list(self) -> None:
        from atelier.core.foundation.rubric_gate import load_packaged_rubrics

        rubrics = load_packaged_rubrics()
        assert len(rubrics) > 0, "load_packaged_rubrics() must return at least one rubric"

    def test_all_rubrics_parse_as_rubric_model(self) -> None:
        from atelier.core.foundation.rubric_gate import load_packaged_rubrics

        rubrics = load_packaged_rubrics()
        for r in rubrics:
            assert isinstance(r, Rubric)
            assert r.id, f"rubric has empty id: {r}"
            assert r.domain, f"rubric {r.id!r} has empty domain"

    def test_includes_new_rubrics(self) -> None:
        from atelier.core.foundation.rubric_gate import load_packaged_rubrics

        ids = {r.id for r in load_packaged_rubrics()}
        assert "rubric_code_review" in ids, "rubric_code_review must be packaged"
        assert "rubric_verification_ladder" in ids, "rubric_verification_ladder must be packaged"

    def test_code_review_rubric_blocks_when_findings_unclassified(self) -> None:
        from atelier.core.foundation.rubric_gate import load_packaged_rubrics

        rubrics = {r.id: r for r in load_packaged_rubrics()}
        rub = rubrics["rubric_code_review"]
        # Passing no checks should block (all_findings_severity_classified is in block_if_missing)
        res = run_rubric(rub, {})
        assert res.status == "blocked"

    def test_verification_ladder_blocks_when_existence_missing(self) -> None:
        from atelier.core.foundation.rubric_gate import load_packaged_rubrics

        rubrics = {r.id: r for r in load_packaged_rubrics()}
        rub = rubrics["rubric_verification_ladder"]
        res = run_rubric(rub, {})
        assert res.status == "blocked"

    def test_verification_ladder_passes_when_all_required_pass(self) -> None:
        from atelier.core.foundation.rubric_gate import load_packaged_rubrics

        rubrics = {r.id: r for r in load_packaged_rubrics()}
        rub = rubrics["rubric_verification_ladder"]
        res = run_rubric(
            rub,
            {
                "existence_confirmed": True,
                "substantive_not_stub": True,
                "wired_to_callsites": True,
                "data_flow_verified_or_not_applicable": True,
            },
        )
        assert res.status == "pass"

    def test_seed_packaged_rubrics_populates_store(self, tmp_path: Path) -> None:
        """store.init() must seed packaged rubrics so verify tool can find them."""
        from atelier.core.foundation.store import ContextStore

        store = ContextStore(tmp_path)
        store.init()

        rubric = store.get_rubric("rubric_code_review")
        assert rubric is not None, "rubric_code_review must be findable after store.init()"
        assert "all_findings_severity_classified" in rubric.required_checks

        ladder = store.get_rubric("rubric_verification_ladder")
        assert ladder is not None, "rubric_verification_ladder must be findable after store.init()"
        assert "existence_confirmed" in ladder.required_checks
