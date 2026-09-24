from __future__ import annotations

import inspect

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp import session_identity


def test_mcp_server_reexports_canonical_session_identity() -> None:
    assert mcp_server._workspace_bridge_file is session_identity.workspace_bridge_file
    assert mcp_server._read_workspace_session_bridge is session_identity.read_workspace_session_bridge
    assert mcp_server._workspace_bridge_session_id is session_identity.workspace_bridge_session_id
    assert mcp_server._resolved_host_session is session_identity.resolved_host_session
    assert mcp_server._resolved_host_session_id is session_identity.resolved_host_session_id
    assert mcp_server._get_mcp_model is session_identity.get_mcp_model


def test_session_identity_module_does_not_import_mcp_server() -> None:
    source = inspect.getsource(session_identity)
    assert "from lemoncrow.gateway.adapters import mcp_server" not in source
    assert "from lemoncrow.gateway.adapters.mcp_server import" not in source
