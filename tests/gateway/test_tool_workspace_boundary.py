from __future__ import annotations

import inspect
from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import workspace


def test_mcp_server_reexports_canonical_workspace_state() -> None:
    assert mcp_server._request_project is workspace.request_project
    assert mcp_server._workspace_root is workspace.workspace_root
    assert mcp_server._workspace_path is workspace.workspace_path
    assert mcp_server._set_request_project is workspace.set_request_project
    assert mcp_server._clear_request_project is workspace.clear_request_project
    assert mcp_server._extract_request_project is workspace.extract_request_project
    assert mcp_server._is_within_root is workspace.is_within_root


def test_workspace_ownership_no_longer_lives_in_mcp_server() -> None:
    source = inspect.getsource(mcp_server)
    for definition in (
        "def _workspace_root(",
        "def _workspace_path(",
        "def _set_request_project(",
        "def _clear_request_project(",
        "def _extract_request_project(",
    ):
        assert definition not in source


def test_workspace_module_does_not_depend_on_mcp_adapter() -> None:
    source = inspect.getsource(workspace)
    assert "gateway.adapters" not in source
    assert "mcp_server" not in source


def test_workspace_path_preserves_absolute_paths(tmp_path: Path) -> None:
    absolute = tmp_path / "a.py"
    assert workspace.workspace_path(str(absolute)) == absolute
