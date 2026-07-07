from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.team import (
    TeamPermissionError,
    TeamWorkspaceError,
    TeamWorkspaceManager,
)


def test_join_rejects_user_id_not_matching_invite_email(tmp_path: Path) -> None:
    manager = TeamWorkspaceManager(tmp_path / ".atelier")
    manager.init_workspace(name="Acme", admin_email="admin@example.com")
    invite = manager.invite_members(["member@example.com"], role="member")[0]

    # Cannot impersonate an arbitrary identity that the invite does not bind to.
    with pytest.raises(TeamWorkspaceError):
        manager.join_workspace(invite.code, user_id="attacker@example.com")

    # Cannot hijack/overwrite an existing member (e.g. the admin).
    with pytest.raises(TeamWorkspaceError):
        manager.join_workspace(invite.code, user_id="admin@example.com")

    # The admin's role/identity is untouched after the rejected joins.
    admin = manager.get_member("admin@example.com")
    assert admin is not None
    assert admin.role == "admin"
    assert manager.get_member("attacker@example.com") is None


def test_join_rejects_overwriting_existing_member(tmp_path: Path) -> None:
    manager = TeamWorkspaceManager(tmp_path / ".atelier")
    manager.init_workspace(name="Acme", admin_email="admin@example.com")
    invite = manager.invite_members(["member@example.com"], role="member")[0]
    manager.join_workspace(invite.code)

    # A second invite to the same email cannot overwrite the joined member.
    second = manager.invite_members(["member@example.com"], role="admin", actor_user_id="admin@example.com")[0]
    with pytest.raises(TeamWorkspaceError):
        manager.join_workspace(second.code)
    member = manager.get_member("member@example.com")
    assert member is not None
    assert member.role == "member"


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
