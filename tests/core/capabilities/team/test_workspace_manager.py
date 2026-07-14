from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.team import (
    TeamPermissionError,
    TeamWorkspaceError,
    TeamWorkspaceManager,
)


def test_join_rejects_user_id_not_matching_invite_email(tmp_path: Path) -> None:
    manager = TeamWorkspaceManager(tmp_path / ".lemoncrow")
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
    manager = TeamWorkspaceManager(tmp_path / ".lemoncrow")
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


def test_concurrent_invites_do_not_lose_updates(tmp_path: Path) -> None:
    # Regression: invite_members() used a lock-free load->append->save, so two
    # invites racing on the shared team_workspace.json clobbered each other
    # (last-writer-wins), dropping an invite while its code was already returned.
    manager = TeamWorkspaceManager(tmp_path / ".lemoncrow")
    manager.init_workspace(name="Acme", admin_email="admin@example.com")

    emails = [f"member{i}@example.com" for i in range(16)]
    ready = threading.Barrier(len(emails))
    returned: list[str] = []
    returned_lock = threading.Lock()

    def worker(email: str) -> None:
        ready.wait()  # rendezvous so the load->save windows overlap
        invite = manager.invite_members([email], role="member", actor_user_id="admin@example.com")[0]
        with returned_lock:
            returned.append(invite.code)

    threads = [threading.Thread(target=worker, args=(email,)) for email in emails]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    workspace = manager.load_workspace()
    persisted_emails = {invite.email for invite in workspace.invites}
    assert persisted_emails == set(emails)
    # Every code handed back to a caller must be redeemable (present on disk).
    persisted_codes = {invite.code for invite in workspace.invites}
    assert set(returned) == persisted_codes


def test_workspace_init_invite_join_and_role_change(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
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
