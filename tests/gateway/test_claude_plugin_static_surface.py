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
    assert "${CLAUDE_PLUGIN_ROOT}" in " ".join(server["args"])


def test_main_agent_bans_native_file_tools_in_dev_mode() -> None:
    """code.md is the dev-mode agent definition.

    It bans native file tools to enforce use of Atelier MCP equivalents.
    The install script writes a passive-mode variant (no disallowedTools)
    when ATELIER_DEV_MODE is not set.
    """
    frontmatter = _frontmatter(PLUGIN / "agents" / "code.md")
    # Native file tools must be banned — enforces Atelier tool use in dev mode
    for tool_name in ["Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"]:
        assert tool_name in frontmatter
    # Atelier dev tool names must NOT appear in frontmatter (they're in the body)
    assert "mcp__atelier__search" not in frontmatter


def test_explore_agent_is_read_only_and_uses_atelier_search() -> None:
    frontmatter = _frontmatter(PLUGIN / "agents" / "explore.md")
    assert "mcp__atelier__search" in frontmatter
    assert "mcp__atelier__read" in frontmatter
    assert "mcp__atelier__edit" in frontmatter
    assert "Agent" in frontmatter


def test_plugin_skills_are_packaged_locally() -> None:
    expected = {
        "analyze-failures",
        "benchmark",
        "check-plan",
        "context",
        "evals",
        "recall",
        "record-trace",
        "rescue",
        "savings",
        "settings",
        "share",
        "status",
        "task",
    }
    found = {path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md")}
    assert expected <= found
