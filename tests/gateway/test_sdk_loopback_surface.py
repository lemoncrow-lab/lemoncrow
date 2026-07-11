"""WS11 G18 -- in-process SDK loopback exposes the full registered tool surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.sdk.mcp import _LoopbackTransport


def test_previously_unreachable_tools_are_registered() -> None:
    # G18: these were unreachable via the in-process SDK before; the loopback now
    # resolves the full TOOLS registry, so each must be present and dispatchable.
    # (rename is performed via `codemod`, not a standalone tool -- see the SDK
    # loopback note "rename-via-codemod".)
    for name in ("workflow", "codemod", "bash", "grep", "graph", "scan"):
        assert name in mcp_server.TOOLS, name


def test_loopback_dispatches_grep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "mod.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
    transport = _LoopbackTransport()
    result = transport.call_tool("grep", {"content_regex": "alpha", "path": "mod.py"})
    assert isinstance(result, dict)


def test_loopback_unknown_tool_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        _LoopbackTransport().call_tool("does_not_exist_tool", {})
