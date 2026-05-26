from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "integrations" / "claude" / "plugin"


def _frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return text.split("---", 2)[1]


def test_plugin_mcp_server_is_loaded_at_session_start() -> None:
    config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["atelier"]
    assert server["type"] == "stdio"
    assert server["alwaysLoad"] is True
    assert server["command"] == "atelier-mcp"
    assert server["args"] == ["--host", "claude"]
    assert server["env"]["CLAUDE_PLUGIN_ROOT"] == "${CLAUDE_PLUGIN_ROOT}"


def test_main_agent_dev_variant_bans_native_file_tools() -> None:
    """code.dev.md is the dev-mode agent definition.

    It bans native file tools to enforce use of Atelier MCP equivalents.
    The stable code.md variant keeps native fallback available.
    """
    frontmatter = _frontmatter(PLUGIN / "agents" / "code.dev.md")
    # Native file tools must be banned — enforces Atelier tool use in dev mode
    for tool_name in ["Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"]:
        assert tool_name in frontmatter
    # Atelier dev tool names must NOT appear in frontmatter (they're in the body)
    assert "mcp__atelier__search" not in frontmatter

    stable = _frontmatter(PLUGIN / "agents" / "code.md")
    assert "disallowedTools" not in stable


def test_explore_agent_is_fast_read_only_and_uses_native_fallback() -> None:
    frontmatter = _frontmatter(PLUGIN / "agents" / "explore.md")
    body = (PLUGIN / "agents" / "explore.md").read_text(encoding="utf-8")
    assert "model: haiku" in frontmatter
    for tool_name in ["Read", "Grep", "Glob"]:
        assert tool_name in frontmatter
    assert "Edit" in frontmatter
    assert "mcp__atelier__edit" not in frontmatter
    assert "Agent" in frontmatter
    assert "12 tool calls" in body


def test_explore_dev_variant_can_use_mcp_read_search() -> None:
    frontmatter = _frontmatter(PLUGIN / "agents" / "explore.dev.md")
    assert "model: haiku" in frontmatter
    assert "mcp__atelier__search" in frontmatter
    assert "mcp__atelier__read" in frontmatter


def test_plugin_skills_are_packaged_locally() -> None:
    expected = {
        "code",
        "explore",
        "review",
        "repair",
        "research",
    }
    found = {path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md")}
    assert expected <= found
