from __future__ import annotations

import inspect

from lemoncrow_client.kit.read_path import split_file_opts

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.cli import runtime
from lemoncrow.gateway.tools import dedup
from lemoncrow.gateway.tools.dedup import DedupResult
from lemoncrow.pro.capabilities import context_dedup


def test_mcp_server_reexports_canonical_read_dedup_helpers() -> None:
    assert mcp_server._DEDUP_TOOLS is dedup.MCP_DEDUP_TOOLS
    assert mcp_server._read_dedup_resource is dedup.read_dedup_resource
    assert mcp_server._split_file_opts is split_file_opts


def test_shared_dedup_policy_preserves_salt_and_saved_chars(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class _Registry:
        def stub_for(self, **kwargs):
            seen.update(kwargs)
            return ("stub", 120)

        def delta_for(self, **kwargs):  # pragma: no cover - stub wins
            raise AssertionError(kwargs)

    monkeypatch.setattr(context_dedup, "registry", lambda: _Registry())
    monkeypatch.setattr(context_dedup, "current_epoch", lambda: 7)
    result = dedup.dedup_output(
        name="read",
        args={"force": False},
        text="payload",
        session_id="s-1",
        eligible_tools=dedup.MCP_DEDUP_TOOLS,
        salt="read:a.py:L1-L2",
        resource="read:a.py:L1-L2",
    )
    assert result == DedupResult("stub", stubbed=True, chars_saved=120)
    assert seen["session_id"] == "s-1"
    assert seen["epoch"] == 7
    assert seen["salt"] == "read:a.py:L1-L2"


def test_cli_renderer_uses_shared_dedup_policy(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _dedup_output(**kwargs):
        seen.update(kwargs)
        return DedupResult("deduped", stubbed=True, chars_saved=44)

    monkeypatch.setattr(runtime, "dedup_output", _dedup_output)
    text = runtime._render_tool_result(
        "read",
        "body",
        {"files": ["src/app.py:L10-L20"]},
        session_id="cli-session",
    )
    assert text == "deduped"
    assert seen["eligible_tools"] is dedup.CLI_DEDUP_TOOLS
    assert seen["session_id"] == "cli-session"
    assert str(seen["resource"]).startswith("read:src/app.py:L10-L20")
    # Preserve existing CLI semantics: exact-repeat lookup did not use the MCP
    # read-resource salt; only delta lookup receives the resource key.
    assert seen.get("salt", "") == ""


def test_cli_renderer_no_longer_implements_context_dedup_directly() -> None:
    source = inspect.getsource(runtime._render_tool_result)
    assert "context_dedup.registry" not in source
    assert "mcp_server import _read_dedup_resource" not in source
    assert "dedup_output(" in source
