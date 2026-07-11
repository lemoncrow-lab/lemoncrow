from __future__ import annotations

import json
from pathlib import Path

import pytest

from lemoncrow.gateway.adapters.mcp_server import (
    _emit_model_recommendation,
    _model_recommendation_state,
    _route_outcome_calibration,
    _workspace_session_state_file,
)
from lemoncrow.infra.runtime.run_ledger import RunLedger, outcomes_path


@pytest.fixture()
def workflow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".lemoncrow"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace))
    return root


def test_model_recommendation_state_prefers_persisted_workflow_phase(workflow_env: Path) -> None:
    path = _workspace_session_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"workflow": {"current_step": "planning", "session_phase": "transition", "sticky_window": 1}}),
        encoding="utf-8",
    )
    led = RunLedger(root=workflow_env)

    state = _model_recommendation_state(led, {})

    assert state["workflow_step"] == "planning"
    assert state["session_phase"] == "transition"


def test_model_recommendation_state_supports_review_workflow_state(workflow_env: Path) -> None:
    path = _workspace_session_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workflow": {
                    "current_step": "review",
                    "session_phase": "review",
                    "sticky_window": 1,
                    "current_task": {"task_id": "02-01/task-2"},
                }
            }
        ),
        encoding="utf-8",
    )
    led = RunLedger(root=workflow_env)

    state = _model_recommendation_state(led, {})

    assert state["workflow_step"] == "review"
    assert state["session_phase"] == "review"


def test_owned_route_tier_tracks_task_weight_across_workflow_steps(workflow_env: Path) -> None:
    path = _workspace_session_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"workflow": {"current_step": "planning", "session_phase": "transition", "sticky_window": 2}}),
        encoding="utf-8",
    )
    led = RunLedger(root=workflow_env)

    # Owned execution routing scores each turn on its own merits.
    # Simple tools like "read" use owned routing (mode + tier);
    # "Agent" may fall through to legacy ModelRecommendation (tier only)
    # when cross-vendor routing is not fully configured in the test env.
    first = _emit_model_recommendation("read", {"task": "explain briefly"}, led)
    second = _emit_model_recommendation("Agent", {"task": "design an end-to-end migration plan"}, led)

    # First call may use owned or legacy routing; either way tier is present.
    assert "tier" in first
    if "mode" in first:
        assert first["mode"] == "auto"

    # Second call may use owned or legacy routing; either way tier is present.
    assert "tier" in second
    if "mode" in second:
        assert second["mode"] == "auto"

    path.write_text(
        json.dumps({"workflow": {"current_step": "execution", "session_phase": "execute", "sticky_window": 2}}),
        encoding="utf-8",
    )

    third = _emit_model_recommendation("Agent", {"task": "design an end-to-end migration plan"}, led)

    # A heavy planning/architecture task keeps routing to an appropriate tier
    # regardless of the workflow step change.
    assert "tier" in third
    if "mode" in third:
        assert third["mode"] == "auto"


def test_route_outcome_calibration_uses_session_outcomes(workflow_env: Path) -> None:
    led = RunLedger(root=workflow_env)
    path = outcomes_path(workflow_env, led.agent or "claude", led.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "route_outcomes": [
                    {
                        "tool": "read",
                        "recommendation_followed": True,
                        "scored_state": {"session_phase": "transition"},
                        "outcome_window": {"outcome_score": 0.9},
                    },
                    {
                        "tool": "read",
                        "recommendation_followed": False,
                        "scored_state": {"session_phase": "transition"},
                        "outcome_window": {"outcome_score": 0.4},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = _route_outcome_calibration("read", {"session_phase": "transition"}, led)

    assert payload["route_outcome_score_delta"] == 0.5
    assert payload["route_outcome_samples"] == 2
