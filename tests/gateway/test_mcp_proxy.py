"""Tests for the `mcp` gateway proxy tool: discovery, calls, self-exclusion, and
the spill/compact pipeline bounding an oversized proxied result.

A tiny stdio JSON-RPC server fixture stands in for a real third-party MCP
server: it answers initialize/tools-list/tools-call and exposes an `echo`
tool and a `big` tool (returns an oversized payload) so the spill path can be
exercised end-to-end through the registered ``TOOLS["mcp"]`` handler.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from atelier.core.capabilities.mcp_integration import loader
from atelier.core.capabilities.mcp_integration.loader import MCPServerConfig
from atelier.gateway.adapters import mcp_proxy, mcp_server

_FAKE_SERVER_SCRIPT = textwrap.dedent("""
    import json
    import sys

    def main() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            method = req.get("method")
            rid = req.get("id")
            params = req.get("params") or {}
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0.0.1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo back the given text.\\nSecond line ignored.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}, "loud": {"type": "boolean"}},
                                "required": ["text"],
                            },
                        },
                        {
                            "name": "big",
                            "description": "Return an oversized payload.",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                    ]
                }
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name == "echo":
                    result = {"content": [{"type": "text", "text": "echo:" + str(arguments.get("text", ""))}]}
                elif name == "big":
                    result = {"content": [{"type": "text", "text": "HEAD" + ("q" * 50000) + "TAIL"}]}
                else:
                    result = {"content": [{"type": "text", "text": "unknown tool " + str(name)}], "isError": True}
            else:
                result = {}
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\\n")
            sys.stdout.flush()

    if __name__ == "__main__":
        main()
    """)


def _write_fake_server(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_SERVER_SCRIPT, encoding="utf-8")
    return script


def _write_mcp_json(tmp_path: Path, *, include_self: bool = False) -> Path:
    script = _write_fake_server(tmp_path)
    servers: dict[str, object] = {
        "fake": {"command": sys.executable, "args": [str(script)]},
    }
    if include_self:
        servers["atelier"] = {"command": "atelier", "args": ["mcp", "--host", "test"]}
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return config_path


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate each test's proxy registry and point discovery/trust at tmp_path."""
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    # Prevent discovery from also picking up this machine's real host configs
    # (~/.claude.json, ~/.cursor/mcp.json, installed Claude Code plugins).
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", tmp_path / "_no_claude.json")
    monkeypatch.setattr(loader, "_CURSOR_MCP_JSON_PATH", tmp_path / "_no_cursor_mcp.json")
    monkeypatch.setattr(loader, "_CLAUDE_PLUGINS_CACHE_DIR", tmp_path / "_no_plugins_cache")
    registry = mcp_proxy._ProxyRegistry()
    monkeypatch.setattr(mcp_proxy, "_registry", registry)
    yield
    registry.shutdown()


def test_catalog_lists_servers_with_slim_param_summary(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)

    result = mcp_proxy.catalog()

    servers = result["servers"]
    assert set(servers) == {"fake"}
    tools = {t["name"]: t for t in servers["fake"]["tools"]}
    assert set(tools) == {"echo", "big"}
    echo = tools["echo"]
    assert echo["description"] == "Echo back the given text."
    assert echo["params"] == {"required": ["text"], "optional": ["loud"]}


def test_catalog_persists_and_cached_server_names_reads_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_mcp_json(tmp_path)
    mcp_proxy.catalog()

    # A fresh registry with configs that would fail to spawn -- if
    # cached_server_names() spawned anything it would see this broken config
    # and either raise or return nothing; it must instead read the persisted
    # snapshot from the *prior* catalog() call untouched.
    assert mcp_proxy.cached_server_names() == ["fake"]


def test_call_happy_path_returns_full_text(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)

    out = mcp_proxy.call("fake", "echo", {"text": "hello"})

    assert out == "echo:hello"


def test_call_unknown_server_lists_known_servers(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)

    out = mcp_proxy.call("nope", "echo", {})

    assert "unknown" in out.lower()
    assert "nope" in out
    assert "fake" in out


def test_call_unknown_tool_lists_known_tools_on_that_server(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)

    out = mcp_proxy.call("fake", "nope", {})

    assert "unknown" in out.lower()
    assert "nope" in out
    assert "echo" in out and "big" in out


def test_self_exclusion_hides_atelier_own_server(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path, include_self=True)

    result = mcp_proxy.catalog()

    assert set(result["servers"]) == {"fake"}
    out = mcp_proxy.call("atelier", "mcp", {})
    assert "unknown" in out.lower()
    assert "atelier" in out


def test_is_self_matches_real_cursor_merge_shape_without_mcp_token() -> None:
    """Cursor's config-merge path (an existing ~/.cursor/mcp.json) registers
    Atelier as {"command": "atelier", "args": ["--host", "cursor"]} -- no
    "mcp"/"serve" token in command or args at all. Name-based matching must
    still catch this real installed shape."""
    config = MCPServerConfig(name="atelier", command="atelier", args=["--host", "cursor"])
    assert mcp_proxy._is_self(config)


def test_is_self_matches_plugin_namespaced_name() -> None:
    config = MCPServerConfig(name="plugin_atelier_atelier", command="/some/venv/bin/python3", args=["-m", "something"])
    assert mcp_proxy._is_self(config)


def test_is_self_does_not_false_positive_on_unrelated_server() -> None:
    config = MCPServerConfig(name="fake", command="fake-server", args=[])
    assert not mcp_proxy._is_self(config)


def test_mcp_registered_in_spill_tool_sets() -> None:
    assert "mcp" in mcp_server._SPILL_TOOLS
    assert "mcp" in mcp_server._SPILL_CHAR_CAP_TOOLS


def test_handle_dispatch_lists_via_registered_tool(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)

    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp", "arguments": {"op": "list"}},
        }
    )

    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert "fake" in payload["servers"]


def test_handle_dispatch_call_happy_path(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)

    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp", "arguments": {"server": "fake", "tool": "echo", "params": {"text": "hi"}}},
        }
    )

    assert resp is not None
    assert resp["result"]["content"][0]["text"] == "echo:hi"


def test_handle_dispatch_call_requires_server_and_tool(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)

    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp", "arguments": {}},
        }
    )

    assert resp is not None
    assert resp["result"]["isError"] is True
    assert "server" in resp["result"]["content"][0]["text"]


def test_handle_dispatch_spills_oversized_proxied_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized result from the proxied `big` tool is bounded by the same
    spill pipeline other capped tools go through -- the full text is recoverable
    from the spilled path rather than silently dropped."""
    monkeypatch.setenv("ATELIER_TOOL_OUTPUT_SPILL", "1")
    monkeypatch.delenv("ATELIER_MCP_SPILL_RESULT_CHARS", raising=False)
    _write_mcp_json(tmp_path)

    resp = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp", "arguments": {"server": "fake", "tool": "big", "params": {}}},
        }
    )

    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    assert len(text) <= mcp_server._spill_result_chars("mcp")
    assert "HEAD" in text
    assert "TAIL" in text
    assert "[atelier: shrunk" in text
    import re

    spill_path = re.search(r"read (\S+\.txt)\]", text)
    assert spill_path is not None
    recovered = Path(spill_path.group(1)).read_text(encoding="utf-8")
    assert "q" * 50000 in recovered
