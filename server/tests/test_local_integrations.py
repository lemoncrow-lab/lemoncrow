from __future__ import annotations

import json
from pathlib import Path

from lemoncrow_server_core.local_app import LocalServerApp


def test_local_integrations_expose_component_evidence_and_roadmap_hosts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "lemoncrow_server_core.local_app.shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"claude", "codex"} else None,
    )

    (tmp_path / ".lemoncrow" / "hosts").mkdir(parents=True)
    (tmp_path / ".lemoncrow" / "hosts" / "status.json").write_text(
        json.dumps(
            {
                "claude": "not_installed",
                "pi": "not_installed",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "lc": {
                        "command": "lc",
                        "args": ["mcp", "--host", "claude"],
                        "env": {"LEMONCROW_MCP_TOOL_PROFILE": "core"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    plugin_manifest = tmp_path / ".lemoncrow" / "claude-plugin" / ".claude-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text('{"name":"lemoncrow"}\n', encoding="utf-8")

    pi_binary = tmp_path / ".lemoncrow" / "bin" / "pi-host"
    pi_binary.parent.mkdir(parents=True)
    pi_binary.write_text("managed pi\n", encoding="utf-8")
    pi_metadata = tmp_path / ".lemoncrow" / "hosts" / "pi-host.json"
    pi_metadata.write_text('{"version":"test"}\n', encoding="utf-8")

    opencode_home = tmp_path / ".config" / "opencode"
    opencode_home.mkdir(parents=True)
    (opencode_home / "opencode.json").write_text(
        '{"mcp":{"lc":{"command":["lemoncrow","mcp","--host","opencode"]}}}\n',
        encoding="utf-8",
    )
    agents_dir = opencode_home / "agents"
    agents_dir.mkdir()
    for role in ("code", "execute", "explore", "general", "plan", "research", "review", "solve"):
        (agents_dir / f"lemoncrow.{role}.md").write_text(f"# {role}\n", encoding="utf-8")

    app = object.__new__(LocalServerApp)
    rows, cached_status_available = app._local_integration_rows()
    hosts = {row["id"]: row for row in rows}

    assert cached_status_available is True

    claude = hosts["claude"]
    assert claude["support"] == "supported"
    assert claude["cached_status"] == "not_installed"
    assert claude["configured"] is True
    claude_components = {component["id"]: component for component in claude["components"]}
    assert claude_components["mcp"]["present"] is True
    assert claude_components["mcp"]["path"] == "~/.claude.json"
    assert claude_components["plugin"]["present"] is True
    assert claude_components["hooks"]["present"] is False

    opencode = hosts["opencode"]
    assert opencode["configured"] is True
    opencode_components = {component["id"]: component for component in opencode["components"]}
    assert opencode_components["mcp"]["present"] is True
    assert {component["label"] for component in opencode["components"] if component["id"].startswith("agent:")} == {
        "Agent · code",
        "Agent · execute",
        "Agent · explore",
        "Agent · general",
        "Agent · plan",
        "Agent · research",
        "Agent · review",
        "Agent · solve",
    }

    pi = hosts["pi"]
    assert pi["description"] == "The Pi coding agent, managed by LemonCrow. Start a session with `lc with pi`."
    assert pi["detected"] is True
    assert pi["configured"] is True
    pi_components = {component["id"]: component for component in pi["components"]}
    assert pi_components["binary"]["present"] is True
    assert pi_components["metadata"]["present"] is True
    assert pi_components["settings"]["present"] is False

    for host_id in ("antigravity", "hermes"):
        assert hosts[host_id]["support"] == "coming_soon"
        assert hosts[host_id]["components"] == []
