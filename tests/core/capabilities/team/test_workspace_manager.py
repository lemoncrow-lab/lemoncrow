from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.team import TeamPermissionError, TeamWorkspaceManager


def test_workspace_init_invite_join_and_role_change(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    manager = TeamWorkspaceManager(root)

    workspace = manager.init_workspace(name="Acme", admin_email="admin@example.com")
    invites = manager.invite_members(["member@example.com"], role="member")
    member = manager.join_workspace(invites[0].code)

    assert workspace.name == "Acme"
    assert member.user_id == "member@example.com"
    assert manager.load_workspace().current_user_id == "member@example.com"

    with pytest.raises(TeamPermissionError):
        manager.invite_members(["viewer@example.com"], role="viewer")

    updated = manager.set_role("member@example.com", "viewer", actor_user_id="admin@example.com")
    assert updated.role == "viewer"

