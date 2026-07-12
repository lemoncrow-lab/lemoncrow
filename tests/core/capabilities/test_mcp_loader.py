"""Tests for MCP server config discovery and process lifecycle."""

from __future__ import annotations

import json

import pytest

from lemoncrow.core.capabilities.mcp_integration import loader
from lemoncrow.core.capabilities.mcp_integration.loader import (
    MCPServerConfig,
    MCPServerProcess,
    discover_mcp_configs,
)


@pytest.fixture(autouse=True)
def _isolate_home_configs(monkeypatch, tmp_path):
    """Prevent tests from reading the real developer machine's host configs
    (~/.claude.json, ~/.cursor/mcp.json, installed Claude Code plugins) --
    tests that exercise these sources point the constants at tmp_path fixtures
    explicitly.
    """
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", tmp_path / "_no_claude.json")
    monkeypatch.setattr(loader, "_CURSOR_MCP_JSON_PATH", tmp_path / "_no_cursor_mcp.json")
    monkeypatch.setattr(loader, "_CLAUDE_PLUGINS_CACHE_DIR", tmp_path / "_no_plugins_cache")


def test_discover_returns_empty_when_no_configs(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    assert discover_mcp_configs() == []


def test_discover_parses_mcp_json(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".mcp.json"
    cfg_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "weather": {
                        "command": "weather-server",
                        "args": ["--port", "8080"],
                        "env": {"API_KEY": "abc"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [cfg_path])
    # A config outside a trusted root is only auto-spawned with explicit opt-in.
    monkeypatch.setenv("LEMONCROW_MCP_ALLOW_UNTRUSTED", "1")
    configs = discover_mcp_configs()
    assert len(configs) == 1
    assert configs[0].name == "weather"
    assert configs[0].command == "weather-server"
    assert configs[0].args == ["--port", "8080"]
    assert configs[0].env == {"API_KEY": "abc"}


def test_stop_safe_when_not_started():
    proc = MCPServerProcess(MCPServerConfig(name="x", command="noop"))
    # Should not raise when stopping an unstarted process.
    proc.stop()


def test_discover_also_resolves_configs_under_workspace_root(monkeypatch, tmp_path):
    """The gateway's cwd is not necessarily the workspace root: passing
    workspace_root must find a `.mcp.json` living there even when the default
    cwd-relative paths don't resolve to anything."""
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"weather": {"command": "weather-server"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace_root))

    assert discover_mcp_configs() == []
    configs = discover_mcp_configs(workspace_root=workspace_root)
    assert [c.name for c in configs] == ["weather"]


def test_discover_default_behavior_unchanged_without_workspace_root(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".mcp.json"
    cfg_path.write_text(
        json.dumps({"mcpServers": {"weather": {"command": "weather-server"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [cfg_path])
    monkeypatch.setenv("LEMONCROW_MCP_ALLOW_UNTRUSTED", "1")

    assert [c.name for c in discover_mcp_configs()] == ["weather"]
    assert [c.name for c in discover_mcp_configs(workspace_root=None)] == ["weather"]


def test_discover_reads_claude_json_user_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["@playwright/mcp"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", claude_json)

    configs = discover_mcp_configs()

    assert [c.name for c in configs] == ["playwright"]
    assert configs[0].command == "npx"


def test_discover_reads_claude_json_per_project_for_matching_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    other_root = tmp_path / "other"
    other_root.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "projects": {
                    str(workspace_root): {"mcpServers": {"project-server": {"command": "project-cmd"}}},
                    str(other_root): {"mcpServers": {"other-server": {"command": "other-cmd"}}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", claude_json)

    configs = discover_mcp_configs(workspace_root=workspace_root)

    assert [c.name for c in configs] == ["project-server"]

    # No workspace_root given -> no project matched (and no user-scope entries
    # in this fixture), so nothing is discovered.
    assert discover_mcp_configs() == []


def test_discover_claude_json_project_scope_overrides_user_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {"shared": {"command": "user-global-cmd"}},
                "projects": {str(workspace_root): {"mcpServers": {"shared": {"command": "project-cmd"}}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", claude_json)

    configs = discover_mcp_configs(workspace_root=workspace_root)

    assert [c.name for c in configs] == ["shared"]
    assert configs[0].command == "project-cmd"


def test_discover_skips_url_only_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {"url": "https://example.com/mcp", "type": "http"},
                    "local": {"command": "local-cmd"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", claude_json)

    configs = discover_mcp_configs()

    assert [c.name for c in configs] == ["local"]


def test_discover_reads_claude_plugin_mcp_configs(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    plugin_dir = tmp_path / "plugins_cache" / "claude-plugins-official" / "playwright" / "unknown"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_PLUGINS_CACHE_DIR", tmp_path / "plugins_cache")

    configs = discover_mcp_configs()

    assert [c.name for c in configs] == ["playwright"]


def test_discover_excludes_lemoncrow_self_entry_from_plugin_config(monkeypatch, tmp_path):
    """The installed lemoncrow plugin's own .mcp.json (cache/lemoncrowhq/lemoncrow/<ver>/.mcp.json)
    must never be spawned as a discovered server -- discover_mcp_configs() is the
    shared primitive behind the TUI's spawn-everything loop."""
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    plugin_dir = tmp_path / "plugins_cache" / "lemoncrow" / "lemoncrow" / "0.1.0"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lc": {"command": "lc", "args": ["mcp", "--host", "claude"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_PLUGINS_CACHE_DIR", tmp_path / "plugins_cache")

    configs = discover_mcp_configs()

    assert configs == []


def test_discover_excludes_lemoncrow_self_entry_from_claude_json_user_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"mcpServers": {"lc": {"command": "lc", "args": ["mcp", "--host", "claude"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", claude_json)

    assert discover_mcp_configs() == []


def test_discover_excludes_lemoncrow_named_self_entry(monkeypatch, tmp_path):
    """Cursor/Antigravity/Copilot installers register LemonCrow's own server
    under the "lemoncrow" name with {"command": "lemoncrow", ...} (the
    guaranteed console-script, not the removable `lc` alias) -- this shape
    must be excluded from discovery just like the "lc"-named entry, or the
    proxy risks spawning LemonCrow's own server recursively."""
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"mcpServers": {"lemoncrow": {"command": "lemoncrow", "args": ["mcp", "--host", "cursor"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", claude_json)

    assert discover_mcp_configs() == []


def test_discover_reads_cursor_mcp_json(monkeypatch, tmp_path):
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    cursor_json = tmp_path / "cursor_mcp.json"
    cursor_json.write_text(
        json.dumps({"mcpServers": {"vector-search": {"command": "uv", "args": ["run", "mcp-vector-search"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CURSOR_MCP_JSON_PATH", cursor_json)

    configs = discover_mcp_configs()

    assert [c.name for c in configs] == ["vector-search"]


def test_discover_workspace_configs_take_precedence_over_claude_json(monkeypatch, tmp_path):
    """First-seen wins: a workspace .mcp.json entry overrides a same-named
    ~/.claude.json user-scope entry."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"command": "workspace-cmd"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_MCP_CONFIG_PATHS", [tmp_path / "missing.json"])
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(workspace_root))
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"mcpServers": {"shared": {"command": "user-global-cmd"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_CLAUDE_JSON_PATH", claude_json)

    configs = discover_mcp_configs(workspace_root=workspace_root)

    assert [c.name for c in configs] == ["shared"]
    assert configs[0].command == "workspace-cmd"


def test_call_tool_max_chars_none_returns_full_text(monkeypatch):
    proc = MCPServerProcess(MCPServerConfig(name="x", command="noop"))
    long_text = "y" * 100_000
    monkeypatch.setattr(proc, "_rpc", lambda method, params=None: {"content": [{"type": "text", "text": long_text}]})

    assert proc.call_tool("big", {}) == long_text[: loader._MAX_TOOL_RESULT_CHARS]
    assert proc.call_tool("big", {}, max_chars=None) == long_text
    assert len(proc.call_tool("big", {}, max_chars=None)) == 100_000


def test_call_tool_max_chars_default_unchanged_for_plain_string_result(monkeypatch):
    proc = MCPServerProcess(MCPServerConfig(name="x", command="noop"))
    monkeypatch.setattr(proc, "_rpc", lambda method, params=None: "plain-result")

    assert proc.call_tool("t", {}) == "plain-result"
    assert proc.call_tool("t", {}, max_chars=None) == "plain-result"
