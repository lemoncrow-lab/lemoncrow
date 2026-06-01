from __future__ import annotations

from atelier.core.foundation.models import TraceLearning, ValidationResult
from atelier.gateway.trace_payloads import serialize_trace_learnings, serialize_validation_results


def test_trace_serializers_drop_default_fields() -> None:
    learnings = serialize_trace_learnings(
        [
            TraceLearning(text="Observed fix"),
            TraceLearning(text="Promote this", promote_to="reasonblock"),
            {"text": "already serialized", "kind": "risk"},
        ]
    )
    validations = serialize_validation_results(
        [
            ValidationResult(name="pytest", passed=True),
            ValidationResult(name="mypy", passed=False, detail="type error"),
        ]
    )

    assert learnings == [
        {"text": "Observed fix"},
        {"text": "Promote this", "promote_to": "reasonblock"},
        {"text": "already serialized", "kind": "risk"},
    ]
    assert validations == [
        {"name": "pytest", "passed": True},
        {"name": "mypy", "passed": False, "detail": "type error"},
    ]
