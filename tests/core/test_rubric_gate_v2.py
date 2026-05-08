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
