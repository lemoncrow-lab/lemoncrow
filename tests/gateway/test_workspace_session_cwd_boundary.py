from __future__ import annotations

from pathlib import Path

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import workspace


def test_session_cwd_policy_is_transport_neutral() -> None:
    assert workspace.next_session_cwd(None, "read", {"cwd": "/x"}) is None
    assert workspace.next_session_cwd(None, "bash", {"cwd": " /x "}) == "/x"
    assert workspace.next_session_cwd("/old", "bash", {}) == "/old"


def test_mcp_session_cwd_wrapper_preserves_compat_state(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_last_session_cwd", None)
    mcp_server._record_session_cwd("bash", {"cwd": "/tmp/example"})
    assert mcp_server._last_session_cwd == "/tmp/example"


def test_worktree_resolver_rejects_missing_cwd(tmp_path: Path) -> None:
    assert workspace.session_worktree_root(None, tmp_path) is None
    assert workspace.session_worktree_root(str(tmp_path / "missing"), tmp_path) is None
