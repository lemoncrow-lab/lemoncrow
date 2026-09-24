from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import tools_edit
from lemoncrow.gateway.tools.registry import tool_spec


def test_edit_handler_is_registered_from_extracted_module() -> None:
    spec = tool_spec("edit")
    assert spec is not None
    assert spec["handler"] is tools_edit.tool_smart_edit
    assert mcp_server.tool_smart_edit is tools_edit.tool_smart_edit
    assert mcp_server.EDIT_TOOL_INPUT_SCHEMA is tools_edit.EDIT_TOOL_INPUT_SCHEMA
    assert mcp_server._lift_flattened_edit_args is tools_edit.recover_edit_args


def test_mcp_server_no_longer_defines_edit_registration() -> None:
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/lemoncrow/gateway/adapters/mcp_server.py").read_text(encoding="utf-8")
    assert 'name="edit"' not in source
    assert "def tool_smart_edit(" not in source
    assert "def _lift_flattened_edit_args(" not in source


def test_edit_hooks_late_bind_worktree_and_caps(monkeypatch) -> None:
    marker = object()

    def fake_worktree(_root):
        return marker

    monkeypatch.setattr(mcp_server, "_session_worktree_root", fake_worktree)
    monkeypatch.setattr(mcp_server, "_EDIT_DIAG_CAP", 3)
    monkeypatch.setattr(mcp_server, "_EDIT_VCS_CAP", 4)
    hooks = mcp_server._edit_handler_hooks()
    assert hooks.session_worktree_root is fake_worktree
    assert hooks.session_worktree_root(None) is marker
    assert hooks.edit_diag_cap == 3
    assert hooks.edit_vcs_cap == 4
