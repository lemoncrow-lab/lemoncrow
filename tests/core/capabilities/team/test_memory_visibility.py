from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities.team import (
    TeamPermissionError,
    TeamWorkspaceManager,
    ensure_shared_memory_write,
    visible_memory_blocks,
)
from lemoncrow.core.foundation.memory_models import MemoryBlock
from lemoncrow.infra.storage.sqlite_memory_store import SqliteMemoryStore


def test_viewer_sees_shared_memory_only(tmp_path: Path) -> None:
    root = tmp_path / ".lemoncrow"
    manager = TeamWorkspaceManager(root)
    workspace = manager.init_workspace(name="Acme", admin_email="admin@example.com")
    store = SqliteMemoryStore(root)
    store.upsert_block(
        MemoryBlock(
            agent_id="lemoncrow:code",
            label="private",
            value="private fact",
            metadata={"scope": "private", "workspace_id": workspace.id, "owner_user_id": "admin@example.com"},
        ),
        actor="test",
    )
    store.upsert_block(
        MemoryBlock(
            agent_id="lemoncrow:code",
            label="shared",
            value="shared fact",
            metadata={"scope": "shared", "workspace_id": workspace.id, "owner_user_id": "admin@example.com"},
        ),
        actor="test",
    )
    invite = manager.invite_members(["viewer@example.com"], role="viewer", actor_user_id="admin@example.com")[0]
    manager.join_workspace(invite.code, user_id="viewer@example.com")

    visible = visible_memory_blocks(store.list_blocks("lemoncrow:code"), manager=manager)

    assert [block.label for block in visible] == ["shared"]
    with pytest.raises(TeamPermissionError):
        ensure_shared_memory_write(manager.require_member())
