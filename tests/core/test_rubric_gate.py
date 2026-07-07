from __future__ import annotations

from atelier.core.foundation.models import Rubric
from atelier.core.foundation.rubric_gate import run_rubric


def _r(**kw: object) -> Rubric:
    base: dict[str, object] = dict(id="r", domain="coding")
    base.update(kw)
    return Rubric(**base)  # type: ignore[arg-type]


def test_pass_when_all_required_pass() -> None:
    rubric = _r(required_checks=["a", "b"])
    result = run_rubric(rubric, {"a": True, "b": True})
    assert result.status == "pass"


def test_blocked_when_required_missing_in_block_list() -> None:
    rubric = _r(required_checks=["a"], block_if_missing=["a"])
    result = run_rubric(rubric, {})
    assert result.status == "blocked"


def test_warn_when_required_missing_outside_block_list() -> None:
    rubric = _r(required_checks=["a", "b"], block_if_missing=["b"])
    result = run_rubric(rubric, {"a": False, "b": True})
    assert result.status == "warn"


def test_warning_check_only_warns() -> None:
    rubric = _r(required_checks=["a"], warning_checks=["w"])
    result = run_rubric(rubric, {"a": True, "w": False})
    assert result.status == "warn"


def test_escalations_collected() -> None:
    rubric = _r(
        required_checks=["a"],
        escalation_conditions=["merchant_visible_drift_detected"],
    )
    result = run_rubric(rubric, {"a": True, "merchant_visible_drift_detected": True})
    assert "merchant_visible_drift_detected" in result.escalations
