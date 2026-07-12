from __future__ import annotations

import json
from pathlib import Path

import lemoncrow.core.capabilities.workspace_host_overrides as overrides
from lemoncrow.core.capabilities.workspace_host_overrides import (
    rewrite_agent_model,
    rewrite_agent_name,
    workspace_claude_agent_text,
    workspace_copilot_agent_text,
    write_workspace_agents_md,
    write_workspace_claude_overrides,
    write_workspace_codex_agent_config,
    write_workspace_codex_agents,
    write_workspace_copilot_agents,
    write_workspace_opencode_agents,
)


def test_rewrite_agent_model_inserts_and_removes_model_line() -> None:
    original = "---\nname: code\ndescription: Main agent\nmaxTurns: 100\n---\n\nBody\n"
    pinned = rewrite_agent_model(original, "claude-opus-4.8")
    assert "model: claude-opus-4.8" in pinned

    removed = rewrite_agent_model(pinned, None)
    assert "model:" not in removed

    existing = "---\ndescription: Agent\nmodel: gpt-5.4\nmaxTurns: 100\n---\n\nBody\n"
    replaced = rewrite_agent_model(existing, "claude-opus-4.8")
    assert replaced.count("model: claude-opus-4.8") == 1


def test_rewrite_agent_name_replaces_existing_name_line() -> None:
    original = "---\nname: code\ndescription: Main agent\n---\n\nBody\n"
    renamed = rewrite_agent_name(original, "lc:code")
    assert "name: lc:code" in renamed
    assert "name: code" not in renamed


def test_workspace_copilot_agent_uses_project_model(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".lemoncrow" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"models": {"hosts": {"copilot": {"roles": {"code": "claude-opus-4.8"}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    text = workspace_copilot_agent_text("code", workspace)

    assert "model: claude-opus-4.8" in text


def test_workspace_copilot_agent_inherits_runtime_model_without_host_override(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".lemoncrow" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"models": {"runtime": {"roles": {"code": "gpt-5.5"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    text = workspace_copilot_agent_text("code", workspace)

    assert "model: gpt-5.5" in text


def test_workspace_claude_agent_omits_model_for_auto(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".lemoncrow" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"models": {"hosts": {"claude": {"roles": {"code": "auto"}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    text = workspace_claude_agent_text("code", workspace)

    assert "name: lc:code" in text
    assert "model:" not in text.split("---", 2)[1]


def test_workspace_claude_agent_omits_model_when_runtime_default_only(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".lemoncrow" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"models": {"runtime": {"roles": {"code": "gpt-5.5"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    text = workspace_claude_agent_text("code", workspace)

    assert "name: lc:code" in text
    assert "model:" not in text.split("---", 2)[1]


def test_workspace_claude_agent_injects_model_on_explicit_host_override(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".lemoncrow" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"models": {"hosts": {"claude": {"roles": {"code": "claude-opus-4.8"}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    text = workspace_claude_agent_text("code", workspace)

    assert "name: lc:code" in text
    assert "model: claude-opus-4-8" in text.split("---", 2)[1]


def test_write_workspace_copilot_agents_projects_role_files_and_default_agent(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    written = write_workspace_copilot_agents(workspace)

    assert workspace / ".github" / "agents" / "lemoncrow.code.agent.md" in written
    assert workspace / ".github" / "agents" / "lemoncrow.execute.agent.md" in written
    vs_code_settings = workspace / ".vscode" / "settings.json"
    assert vs_code_settings in written
    assert vs_code_settings.exists()
    payload = json.loads(vs_code_settings.read_text(encoding="utf-8"))
    assert payload.get("github.copilot.chat.defaultAgent") == "lemoncrow.code"


def test_write_workspace_claude_overrides_uses_namespaced_filenames(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    written = write_workspace_claude_overrides(workspace)

    assert workspace / ".claude" / "agents" / "lemoncrow.code.md" in written
    # Default is code-only; other roles are not installed unless requested.
    assert workspace / ".claude" / "agents" / "lemoncrow.review.md" not in written


def test_write_workspace_claude_overrides_role_ids_override_installs_requested_roles(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    written = write_workspace_claude_overrides(workspace, role_ids=("code", "review"))

    assert workspace / ".claude" / "agents" / "lemoncrow.code.md" in written
    assert workspace / ".claude" / "agents" / "lemoncrow.review.md" in written


def test_write_workspace_opencode_agents_projects_workspace_files(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    written = write_workspace_opencode_agents(workspace)

    assert workspace / ".opencode" / "agents" / "lemoncrow.code.md" in written
    # Default is code-only; other roles are not installed unless requested.
    assert workspace / ".opencode" / "agents" / "lemoncrow.review.md" not in written


def test_write_workspace_opencode_agents_role_ids_override_installs_requested_roles(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    written = write_workspace_opencode_agents(workspace, role_ids=("code", "review"))

    assert workspace / ".opencode" / "agents" / "lemoncrow.code.md" in written
    assert workspace / ".opencode" / "agents" / "lemoncrow.review.md" in written
    assert len(written) == 2


def test_write_workspace_opencode_agents_omit_runtime_default_model(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".lemoncrow" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"models": {"runtime": {"roles": {"code": "claude-opus-4.8"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    write_workspace_opencode_agents(workspace)
    content = (workspace / ".opencode" / "agents" / "lemoncrow.code.md").read_text(encoding="utf-8")

    assert "model:" not in content.split("---", 2)[1]


def test_write_workspace_opencode_agents_normalize_explicit_host_model(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".lemoncrow" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"models": {"hosts": {"opencode": {"roles": {"code": "claude-opus-4.8"}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    write_workspace_opencode_agents(workspace)
    content = (workspace / ".opencode" / "agents" / "lemoncrow.code.md").read_text(encoding="utf-8")

    assert "model: anthropic/claude-opus-4-8" in content.split("---", 2)[1]


def test_write_workspace_codex_agents_projects_standalone_files(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    written = write_workspace_codex_agents(workspace)
    content = (workspace / ".codex" / "agents" / "lemoncrow.code.toml").read_text(encoding="utf-8")

    assert workspace / ".codex" / "agents" / "lemoncrow.code.toml" in written
    # Default is code-only; the full SURFACED_ROLE_IDS set is opt-in via role_ids.
    assert len(written) == len(overrides.DEFAULT_ROLE_IDS)
    assert 'name = "lemoncrow.code"' in content
    assert 'developer_instructions = """' in content
    # Catches any mode-doc token (e.g. {{AGENT_RULE}}) missing from the
    # renderer's substitution map, not just {{CORE_DISCIPLINE}}.
    assert "{{" not in content
    # Parity with sync_agent_context: partials splice in verbatim (no injected
    # headings) and inline tool names carry the codex `lc.` prefix — except
    # the "Shell `grep`" phrase, which is about shell grep and stays bare.
    assert "## Core discipline" not in content
    assert "`lc.code_search`" in content
    assert "`lc.grep`" not in content


def test_write_workspace_codex_agent_config_removes_legacy_registration(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))
    config = workspace / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'model = "gpt-5.5"\n\n'
        "# LEMONCROW:CODEX AGENTS START\n"
        "[agents.lemoncrow_code]\n"
        'description = "legacy"\n'
        'config_file = "/tmp/lemoncrow.code.toml"\n'
        "# LEMONCROW:CODEX AGENTS END\n\n"
        "[agents.lemoncrow_review]\n"
        'description = "orphan legacy section"\n\n'
        "[agents]\n"
        "max_threads = 4\n",
        encoding="utf-8",
    )

    written = write_workspace_codex_agent_config(workspace)
    write_workspace_codex_agent_config(workspace)
    content = written.read_text(encoding="utf-8")

    assert written == config
    assert 'model = "gpt-5.5"' in content
    assert "[agents]" in content
    assert "max_threads = 4" in content
    assert "LEMONCROW:CODEX AGENTS" not in content
    assert "[agents.lemoncrow_code]" not in content
    assert "[agents.lemoncrow_review]" not in content


def test_write_workspace_agents_md_installs_generic_managed_block(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "global-root"))

    written = write_workspace_agents_md(workspace)
    write_workspace_agents_md(workspace)
    content = written.read_text(encoding="utf-8")

    assert written == workspace / "AGENTS.md"
    assert content.count("<!-- LEMONCROW START -->") == 1
    assert "# LemonCrow Agent Guide" in content
    assert "LemonCrow's MCP tools" in content


def test_packaged_integration_root_is_used_when_checkout_assets_are_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(overrides, "LEMONCROW_REPO_ROOT", tmp_path / "missing-checkout")
    checkout_root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(overrides.importlib.resources, "files", lambda package: checkout_root)

    assert overrides._resolve_repo_root(None) == checkout_root
