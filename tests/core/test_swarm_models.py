from __future__ import annotations

import json

from atelier.core.capabilities.swarm.capability import load_swarm_state
from atelier.core.capabilities.swarm.models import SwarmRunState


def _legacy_state_payload() -> dict[str, object]:
    return {
        "run_id": "swarm-legacy",
        "status": "success",
        "mode": "continuous",
        "repo_root": "/repo",
        "base_worktree": "/repo",
        "base_ref": "abc123",
        "worktree_pool": "/repo-swarmed",
        "integration_worktree": "/repo-swarmed/integration",
        "integration_base_ref": "def456",
        "spec_source_path": "/repo/spec.md",
        "copied_spec_path": "/repo/.atelier-swarm/spec.md",
        "runner_name": "claude",
        "runner_model": "sonnet",
        "child_command": ["python", "-m", "child"],
        "validation_commands": ["pytest tests/unit"],
        "runs": 4,
        "current_wave": 1,
        "winner_child_id": "wave-01-run-01",
        "accepted_child_ids": ["wave-01-run-01", "wave-01-run-02"],
        "ranking_notes": ["legacy ranking note"],
        "limitations": ["legacy limitation"],
        "dirty_paths": ["README.md"],
        "waves": [
            {
                "wave_index": 1,
                "status": "applied",
                "child_ids": ["wave-01-run-01", "wave-01-run-02"],
                "accepted_child_ids": ["wave-01-run-01", "wave-01-run-02"],
                "rejected_child_ids": [],
                "summary": "Legacy accepted two children",
            }
        ],
        "children": [],
        "created_at": "2025-06-01T00:00:00Z",
        "updated_at": "2025-06-01T00:10:00Z",
    }


def test_swarm_run_state_migrates_legacy_payload() -> None:
    state = SwarmRunState.model_validate(_legacy_state_payload())

    assert state.max_runs == 4
    assert state.runs == 4
    assert state.base_snapshot_ref == "def456"
    assert state.primary_winner_child_id == "wave-01-run-01"
    assert state.winner_child_id == "wave-01-run-01"
    assert state.waves[0].planned_runs == 2
    assert state.waves[0].max_runs == 2
    assert state.waves[0].primary_winner_child_id == "wave-01-run-01"
    assert state.waves[0].accepted_commits == []


def test_load_swarm_state_preserves_legacy_runs(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_legacy_state_payload()), encoding="utf-8")

    state = load_swarm_state(state_path)

    assert state.max_runs == 4
    assert state.primary_winner_child_id == "wave-01-run-01"
    assert state.waves[0].planned_runs == 2
    assert state.export_artifacts == []
