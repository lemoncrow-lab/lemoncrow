from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atelier.core.capabilities.team import TeamWorkspaceManager, summarize_workspace_usage


def test_summarize_workspace_usage_rolls_up_by_user(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    TeamWorkspaceManager(root).init_workspace(name="Acme", admin_email="admin@example.com")
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for session_id, user_id, cost in (
        ("run-1", "admin@example.com", 0.25),
        ("run-2", "member@example.com", 1.5),
    ):
        (runs / f"{session_id}.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "status": "done",
                    "created_at": datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC).isoformat(),
                    "updated_at": datetime(2026, 5, 19, 10, 5, 0, tzinfo=UTC).isoformat(),
                    "agent_settings": {"user_id": user_id},
                    "cost": {"total_cost_usd": cost},
                    "events": [],
                }
            ),
            encoding="utf-8",
        )

    payload = summarize_workspace_usage(root)

    assert payload["session_count"] == 2
    assert payload["total_cost_usd"] == 1.75
    assert {row["user_id"] for row in payload["users"]} == {"admin@example.com", "member@example.com"}
