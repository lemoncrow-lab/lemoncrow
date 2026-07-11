from __future__ import annotations

import pytest
from pydantic import ValidationError

from lemoncrow.core.foundation.savings_models import ContextBudget


def test_context_budget_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ContextBudget(
            session_id="run-1",
            turn_index=0,
            model="codex",
            input_tokens=100,
            cache_read_tokens=10,
            cache_write_tokens=5,
            output_tokens=20,
            naive_input_tokens=200,
            lever_savings={"playbook_inject": 80},
            tool_calls=1,
            unexpected=True,  # type: ignore[call-arg]
        )


def test_context_budget_instantiates_with_default_uuid7_id() -> None:
    budget = ContextBudget(
        session_id="run-1",
        turn_index=0,
        model="codex",
        input_tokens=100,
        cache_read_tokens=10,
        cache_write_tokens=5,
        output_tokens=20,
        naive_input_tokens=200,
        lever_savings={"playbook_inject": 80},
        tool_calls=1,
    )
    assert budget.id.startswith("cb-")
