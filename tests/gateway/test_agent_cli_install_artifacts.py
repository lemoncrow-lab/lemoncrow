"""
test_agent_cli_install_artifacts.py — Verify all install/verify script artifacts exist.

These tests do NOT require any agent CLI (claude, codex, opencode, etc.) to be installed.
They verify that all expected files and scripts exist with correct permissions.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from atelier.core.capabilities.default_definitions import build_default_registry
from atelier.core.environment import skill_visible

ATELIER_ROOT = Path(__file__).parent.parent.parent
SCRIPTS = ATELIER_ROOT / "scripts"
INTEGRATIONS = ATELIER_ROOT / "integrations"
DOCS_HOSTS = ATELIER_ROOT / "docs" / "hosts"
MAKEFILE = ATELIER_ROOT / "Makefile"


def is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def expected_visible_skill_names() -> set[str]:
    return {
        path.parent.name for path in (INTEGRATIONS / "skills").glob("*/SKILL.md") if skill_visible(path.parent.name)
    }


# ---------------------------------------------------------------------------
# 1. All per-host install scripts exist and are executable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["codex", "opencode", "copilot", "antigravity"])
def test_install_script_exists(host: str) -> None:
    script = SCRIPTS / f"install_{host}.sh"
    assert script.exists(), f"Missing: scripts/install_{host}.sh"
    assert is_executable(script), f"Not executable: scripts/install_{host}.sh"


# atelier-status was folded into `atelier status` — its test moved to the CLI test suite.

# ---------------------------------------------------------------------------
# 3. Unified scripts
# ---------------------------------------------------------------------------


def test_install_hosts_script_exists() -> None:
    script = SCRIPTS / "install_hosts.sh"
    assert script.exists()
    assert is_executable(script)


def test_build_host_skills_script_exists() -> None:
    script = SCRIPTS / "build_host_skills.sh"
    assert script.exists(), "Missing: scripts/build_host_skills.sh"
    assert is_executable(script), "Not executable: scripts/build_host_skills.sh"


def test_build_host_skills_generates_stable_bundle_by_default(tmp_path: Path) -> None:
    # No --include-skills: none of the 6 optional public skills ship by
    # default, only the role-name skills codex uses for mode switching plus
    # the always-on `atelier` on-demand install/remove/list discovery skill.
    dest = tmp_path / "skills"
    subprocess.run(
        ["bash", str(SCRIPTS / "build_host_skills.sh"), "--host", "codex", "--dest", str(dest)],
        cwd=ATELIER_ROOT,
        check=True,
    )
    generated = {path.name for path in dest.iterdir() if path.is_dir()}
    registry = build_default_registry(ATELIER_ROOT)
    expected = set(registry.surfaced_role_ids("shared_skill")) | {"atelier"}
    assert generated == expected
    assert not ((expected_visible_skill_names() - {"atelier"}) & generated)


def test_build_host_skills_include_skills_flag_opts_in_public_skills(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    subprocess.run(
        [
            "bash",
            str(SCRIPTS / "build_host_skills.sh"),
            "--host",
            "codex",
            "--dest",
            str(dest),
            "--include-skills=benchmark,swarm",
        ],
        cwd=ATELIER_ROOT,
        check=True,
    )
    generated = {path.name for path in dest.iterdir() if path.is_dir()}
    registry = build_default_registry(ATELIER_ROOT)
    expected = set(registry.surfaced_role_ids("shared_skill")) | {"benchmark", "swarm", "atelier"}
    assert generated == expected


def test_atelier_meta_skill_ships_by_default_while_public_skills_stay_optional(tmp_path: Path) -> None:
    # The `atelier` discovery skill (install/remove/list surface) is the one
    # skill that ships by default -- it's how a user finds the rest of the
    # opt-in surface in the first place. The 6 optional public skills never do.
    from atelier.core.environment import skill_installed_by_default
    from atelier.gateway.cli.commands.agents_skills import PUBLIC_SKILL_NAMES

    assert skill_installed_by_default("atelier") is True
    for name in PUBLIC_SKILL_NAMES:
        assert skill_installed_by_default(name) is False, f"{name} must stay opt-in"

    for host in ("claude", "codex"):
        dest = tmp_path / host
        subprocess.run(
            ["bash", str(SCRIPTS / "build_host_skills.sh"), "--host", host, "--dest", str(dest)],
            cwd=ATELIER_ROOT,
            check=True,
        )
        assert (dest / "atelier" / "SKILL.md").exists(), f"{host}: atelier skill must ship by default"
        for name in PUBLIC_SKILL_NAMES:
            assert not (dest / name).exists(), f"{host}: {name} must not ship by default"


def test_codex_plugin_agent_surface_exists() -> None:
    surface = INTEGRATIONS / "codex" / "plugin" / "agents" / "openai.yaml"
    assert surface.exists()
    content = surface.read_text(encoding="utf-8")
    assert 'display_name: "Atelier Agents"' in content
    assert "atelier_code" in content
    assert "atelier_review" in content


def test_codex_plugin_prompt_uses_real_discovery_path() -> None:
    manifest = INTEGRATIONS / "codex" / "plugin" / ".codex-plugin" / "plugin.json"
    manifest_text = manifest.read_text(encoding="utf-8")
    data = json.loads(manifest_text)
    prompt = "\n".join(data["interface"]["defaultPrompt"])
    assert "atelier_code" in prompt
    assert all(len(item) <= 128 for item in data["interface"]["defaultPrompt"])
    assert "mcp__atelier__context" not in manifest_text
    assert "context first" not in manifest_text


def test_codex_installers_stage_plugin_agent_surface() -> None:
    for script in (SCRIPTS / "install_codex.sh", INTEGRATIONS / "codex" / "install.sh"):
        content = script.read_text(encoding="utf-8")
        assert "integrations/codex/plugin/agents" in content
        assert "agents/openai.yaml" in content
        assert "write_codex_agent_config" in content
        assert "write_workspace_codex_agent_config" in content
        assert "agents\\.atelier_code" in content


def test_codex_installers_auto_approve_exposed_atelier_tools() -> None:
    expected_tools = [
        "bash",
        "read",
        "grep",
        "edit",
        "callees",
        "codemod",
        "memory",
        "callers",
        "explore",
        "web_fetch",
        "search",
        "usages",
    ]
    for script in (SCRIPTS / "install_codex.sh", INTEGRATIONS / "codex" / "install.sh"):
        content = script.read_text(encoding="utf-8")
        for tool in expected_tools:
            assert f'"{tool}"' in content
        assert '"context"' not in content


def test_build_host_skills_ignores_removed_dev_bundle_flag(tmp_path: Path) -> None:
    # antigravity has no role-name skills (it uses agents for mode switching),
    # and no --include-skills is passed, so only the always-on `atelier`
    # discovery skill is generated by default.
    host = "antigravity"
    dest = tmp_path / "skills"
    subprocess.run(
        [
            "bash",
            str(SCRIPTS / "build_host_skills.sh"),
            "--host",
            host,
            "--dest",
            str(dest),
        ],
        cwd=ATELIER_ROOT,
        check=True,
    )
    generated = {path.name for path in dest.iterdir() if path.is_dir()}
    build_default_registry(ATELIER_ROOT)
    assert generated == {"atelier"}


def test_verify_agent_clis_script_exists() -> None:
    script = SCRIPTS / "verify_agent_clis.sh"
    assert script.exists()
    assert is_executable(script)


def test_install_hosts_references_all_hosts() -> None:
    content = (SCRIPTS / "install_hosts.sh").read_text()
    for host in ["claude", "codex", "opencode", "copilot", "antigravity"]:
        assert host in content, f"install_hosts.sh missing reference to {host}"


def test_host_installers_stream_output_instead_of_buffering() -> None:
    # The shared run_setup() orchestrator (lib/common.sh) streams host installer
    # output through `tee` rather than capturing it into a variable, and the
    # per-host dispatcher (install_hosts.sh) streams via stream_colored_output.
    common_content = (SCRIPTS / "lib" / "common.sh").read_text()
    host_content = (SCRIPTS / "install_hosts.sh").read_text()

    assert 'host_output="$(bash "$ATELIER_INSTALL_DIR/scripts/install_hosts.sh"' not in common_content
    assert '| tee "$host_output_file"' in common_content
    assert 'output=$(bash "$script"' not in host_content
    assert '| stream_colored_output "$output_file"' in host_content


def test_host_installer_default_selection_uses_detection() -> None:
    content = (SCRIPTS / "install_hosts.sh").read_text()
    assert "host_is_detected()" in content
    assert "enable_detected_hosts_by_default" in content
    assert "enable_detected_hosts_by_default" in content.split("# Default: all hosts", 1)[1]


def test_host_installer_has_timeout_guard() -> None:
    content = (SCRIPTS / "install_hosts.sh").read_text()
    assert 'ATELIER_HOST_INSTALL_TIMEOUT_SECONDS="${ATELIER_HOST_INSTALL_TIMEOUT_SECONDS:-180}"' in content
    assert "run_host_installer()" in content
    assert "host installer timed out after" in content


def test_verify_agent_clis_references_all_hosts() -> None:
    content = (SCRIPTS / "verify_agent_clis.sh").read_text()
    for host in ["claude", "codex", "opencode", "copilot", "antigravity"]:
        assert host in content, f"verify_agent_clis.sh missing reference to {host}"


# ---------------------------------------------------------------------------
# 4. Makefile targets
# ---------------------------------------------------------------------------


def test_makefile_has_single_dev_target() -> None:
    content = MAKEFILE.read_text()
    assert "dev:" in content
    assert "scripts/local.sh" in content


def test_makefile_has_single_verify_target() -> None:
    content = MAKEFILE.read_text()
    assert "verify:" in content
    assert "scripts/verify_agent_clis.sh" in content


# ---------------------------------------------------------------------------
# 5. Host install docs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    [
        "claude-code-install.md",
        "codex-install.md",
        "opencode-install.md",
        "copilot-install.md",
        "antigravity-install.md",
        "all-agent-clis.md",
    ],
)
def test_host_install_doc_exists(doc: str) -> None:
    path = DOCS_HOSTS / doc
    assert path.exists(), f"Missing host install doc: docs/hosts/{doc}"
    content = path.read_text()
    assert len(content) > 100, f"Host install doc too short: {doc}"


# ---------------------------------------------------------------------------
# 6. integrations/ per-host install.sh symlinks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["codex", "opencode", "copilot", "antigravity"])
def test_integrations_install_symlink(host: str) -> None:
    link = INTEGRATIONS / host / "install.sh"
    assert link.exists(), f"Missing integrations/{host}/install.sh"


# ---------------------------------------------------------------------------
# 7. Example configs have correct structure
# ---------------------------------------------------------------------------


def test_opencode_example_has_mcp_key() -> None:
    example = INTEGRATIONS / "opencode" / "opencode.atelier.template.json"
    if not example.exists():
        pytest.skip("opencode example config not found")
    data = json.loads(example.read_text())
    assert "mcp" in data, "opencode example must have 'mcp' key"
    assert "atelier" in data["mcp"], "opencode example must have 'mcp.atelier' key"


ANTIGRAVITY_INTEGRATION = INTEGRATIONS / "antigravity"


def test_antigravity_integration_dir_exists() -> None:
    assert ANTIGRAVITY_INTEGRATION.is_dir(), "integrations/antigravity/ directory must exist"


def test_antigravity_mcp_template_exists() -> None:
    template = ANTIGRAVITY_INTEGRATION / "mcp.atelier.template.json"
    assert template.exists(), "integrations/antigravity/mcp.atelier.template.json must exist"
    data = json.loads(template.read_text())
    assert "atelier" in data.get("servers", {}), "Antigravity template must have 'servers.atelier'"
    assert data["servers"]["atelier"]["command"] == "atelier"
    assert data["servers"]["atelier"]["args"] == ["mcp", "--host", "antigravity"]


def test_copilot_example_has_servers_key() -> None:
    example = INTEGRATIONS / "copilot" / "mcp.atelier.template.json"
    if not example.exists():
        pytest.skip("copilot mcp example config not found")
    data = json.loads(example.read_text())
    assert "servers" in data, "copilot example must have 'servers' key"
    assert "atelier" in data["servers"], "copilot example must have 'servers.atelier'"


CODEX_PLUGIN = INTEGRATIONS / "codex" / "plugin"


def test_codex_plugin_dir_exists() -> None:
    assert CODEX_PLUGIN.is_dir(), "integrations/codex/plugin/ directory must exist"


def test_codex_plugin_manifest_exists_and_names_atelier() -> None:
    plugin_json = CODEX_PLUGIN / ".codex-plugin" / "plugin.json"
    assert plugin_json.exists(), "integrations/codex/plugin/.codex-plugin/plugin.json must exist"
    data = json.loads(plugin_json.read_text())
    assert data.get("name") == "atelier", f"codex plugin name should be 'atelier', got: {data.get('name')}"
    assert data.get("skills") == "./skills/", "codex plugin must bundle ./skills/"
    assert data.get("mcpServers") == "./.mcp.json", "codex plugin must bundle ./.mcp.json"


def test_codex_plugin_mcp_template_exists() -> None:
    mcp_json = CODEX_PLUGIN / ".mcp.json"
    assert mcp_json.exists(), "integrations/codex/plugin/.mcp.json must exist"
    data = json.loads(mcp_json.read_text())
    atelier = data.get("atelier", {})
    assert atelier.get("command") == "atelier", "Codex plugin template must call atelier directly"


def test_codex_hooks_bundle_exists() -> None:
    hooks_dir = INTEGRATIONS / "codex" / "hooks"
    assert hooks_dir.is_dir(), "integrations/codex/hooks must exist"
    for script in ("hooks.json", "savings_reporter.py", "stop.py", "update_notification.py"):
        assert (hooks_dir / script).exists(), f"integrations/codex/hooks/{script} must exist"


# ---------------------------------------------------------------------------
# 9. Codex AGENTS.atelier.md
# ---------------------------------------------------------------------------


def test_codex_agents_atelier_md_mentions_mcp() -> None:
    agents_md = INTEGRATIONS / "AGENTS.atelier.md"
    content = agents_md.read_text()
    assert "mcp" in content.lower() or "MCP" in content, "AGENTS.atelier.md should mention MCP"


# ---------------------------------------------------------------------------
# 12. Each install script has --dry-run support
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["codex", "opencode", "copilot", "antigravity"])
def test_install_script_has_dry_run(host: str) -> None:
    script = SCRIPTS / f"install_{host}.sh"
    content = script.read_text()
    assert "--dry-run" in content, f"scripts/install_{host}.sh missing --dry-run support"


@pytest.mark.parametrize("host", ["codex", "opencode", "copilot", "antigravity"])
def test_install_script_has_print_only(host: str) -> None:
    script = SCRIPTS / f"install_{host}.sh"
    content = script.read_text()
    assert "--print-only" in content, f"scripts/install_{host}.sh missing --print-only support"


def test_install_scripts_use_workspace_not_project_root_flag() -> None:
    for script in SCRIPTS.glob("install_*.sh"):
        content = script.read_text()
        assert "--project-root" not in content, f"{script.name} must use --workspace, not --project-root"


def test_install_scripts_document_global_and_workspace_paths() -> None:
    codex = (SCRIPTS / "install_codex.sh").read_text()
    assert 'AGENTS_FILE="${CODEX_HOME}/AGENTS.md"' in codex
    assert 'AGENTS_FILE="${WORKSPACE}/AGENTS.md"' in codex
    assert 'PLUGIN_DIR="${CODEX_HOME}/plugins/atelier"' in codex
    assert 'PLUGIN_DIR="${WORKSPACE}/.codex/plugins/atelier"' in codex
    assert ".agents/plugins/marketplace.json" in codex
    assert "atelier@atelier-local" in codex
    assert "plugin add atelier@openai-curated" not in codex
    assert "patch_plugin_hooks" in codex
    assert "__ATELIER_PYTHON__" in codex
    assert "__ATELIER_REPO_SRC__" in codex

    copilot = (SCRIPTS / "install_copilot.sh").read_text()
    assert "Code/User" in copilot
    assert ".copilot/instructions/atelier.instructions.md" in copilot
    assert "${HOME}/.vscode" not in copilot
    assert "${HOME}/.github" not in copilot

    opencode = (SCRIPTS / "install_opencode.sh").read_text()
    assert ".config}/opencode" in opencode
    assert 'OC_FILE="${WORKSPACE}/opencode.json"' in opencode
    assert 'PLUGIN_DEST_DIR="${WORKSPACE}/.opencode/plugins"' in opencode
    assert 'PLUGIN_DEST_DIR="${OPENCODE_CONFIG_HOME}/plugins"' in opencode
    assert "atelier-nudge.js" in opencode
    assert "${HOME}/opencode.jsonc" not in opencode
    assert "${HOME}/.opencode" not in opencode

    claude = (SCRIPTS / "install_claude.sh").read_text()
    assert "claude mcp add --scope user atelier" in claude
    assert '.mcp.json"' in claude
    assert '"atelier mcp"' in claude or '"atelier"' in claude

    antigravity = (SCRIPTS / "install_antigravity.sh").read_text()
    assert "antigravity --add-mcp" in antigravity
    assert "mcp.json" in antigravity
    assert "atelier" in antigravity


def test_install_codex_merges_existing_agents_file() -> None:
    content = (SCRIPTS / "install_codex.sh").read_text()
    assert "merge_agents_file()" in content
    assert 'source "${SCRIPT_DIR}/lib/managed_context.sh"' in content
    assert 'backup_file "$dest_file"' in content
    assert "merged Atelier Codex instructions into $dest_file" in content
    assert 'atelier_upsert_managed_block "$source_file" "$dest_file" "$DRY_RUN"' in content
    assert "integrations/codex/hooks" in content


def test_uninstall_codex_removes_managed_agents_block() -> None:
    content = (SCRIPTS / "uninstall_codex.sh").read_text()
    assert 'source "${SCRIPT_DIR}/lib/managed_context.sh"' in content
    assert "Removed managed Atelier Codex instructions from $AGENTS_FILE" in content


def test_managed_context_helper_shared_across_host_installs() -> None:
    helper = (SCRIPTS / "lib" / "managed_context.sh").read_text()
    assert 'ATELIER_CODE_BLOCK_START="<!-- ATELIER START -->"' in helper
    assert 'ATELIER_CODE_BLOCK_END="<!-- ATELIER END -->"' in helper
    assert "atelier_write_managed_copy()" in helper
    assert "atelier_upsert_managed_block()" in helper
    for script_name in [
        "install_codex.sh",
        "install_claude.sh",
        "install_antigravity.sh",
        "install_copilot.sh",
        "install_opencode.sh",
    ]:
        content = (SCRIPTS / script_name).read_text()
        assert (
            'source "${SCRIPT_DIR}/lib/managed_context.sh"' in content
        ), f"{script_name} must use the shared managed context helper"


def test_local_sh_bootstraps_atelier_before_host_installers() -> None:
    # The source installer (local.sh) installs the Atelier console scripts, then
    # delegates to run_setup() in lib/common.sh, which installs host integrations.
    local_content = (SCRIPTS / "local.sh").read_text()
    common_content = (SCRIPTS / "lib" / "common.sh").read_text()

    assert 'step_start "Installing Atelier"' in local_content
    assert 'step_start "Installing host integrations"' in common_content
    # local.sh installs Atelier and only then calls run_setup (which installs hosts).
    install_pos = local_content.index('step_start "Installing Atelier"')
    # rindex: the actual run_setup call in main(), not the comment near the top.
    run_setup_pos = local_content.rindex("run_setup")
    assert install_pos < run_setup_pos, "Atelier console installation must precede run_setup (host integrations)"


def test_local_sh_installs_tool_scripts_not_uv_runtime_wrappers() -> None:
    content = (SCRIPTS / "local.sh").read_text()
    assert "tool install" in content
    assert "UV_TOOL_BIN_DIR" in content
    # The console-script extras the source installer requests. The runtime
    # ("repo-map"/"api"/"telemetry") extras were dropped on this branch.
    assert "mcp,memory,smart,cloud,postgres,vector,parsers,rename" in content


def test_source_installer_is_always_local_mode() -> None:
    # The legacy dev.sh switched between source and binary modes via --local/--remote
    # and a prepare_repo clone step. That mode-switching was removed: local.sh is
    # always the source installer (ATELIER_LOCAL=1), bundle.sh is the binary one.
    content = (SCRIPTS / "local.sh").read_text()
    assert "ATELIER_USE_CURRENT_REPO" not in content
    assert "prepare_repo" not in content
    assert "ATELIER_LOCAL=1" in content
    # --local/--remote/--no-local remain accepted as no-ops for CLI compatibility.
    assert "--local|--remote|--no-local) : ;;" in content


def test_copilot_tasks_include_preflight_wrapper() -> None:
    tasks = json.loads((INTEGRATIONS / "copilot" / "tasks.json").read_text(encoding="utf-8"))
    labels = {task.get("label") for task in tasks.get("tasks", [])}
    assert "Atelier: Copilot Preflight" in labels
    assert "Atelier: Session Summary" in labels
    assert "Atelier: Copilot Preflight" in (SCRIPTS / "install_copilot.sh").read_text()

    preflight_task = next(task for task in tasks.get("tasks", []) if task.get("label") == "Atelier: Copilot Preflight")
    assert preflight_task.get("command") == "bash"
    args = preflight_task.get("args", [])
    assert any("atelier tools call context" in arg for arg in args)

    summary_task = next(task for task in tasks.get("tasks", []) if task.get("label") == "Atelier: Session Summary")
    assert summary_task.get("command") == "atelier"
    assert summary_task.get("args") == ["session", "report", "--no-color"]


# ---------------------------------------------------------------------------
# 13. Each install script gracefully skips if CLI absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,cli",
    [
        ("codex", "codex"),
        ("opencode", "opencode"),
        ("copilot", "code"),
        ("antigravity", "antigravity"),
    ],
)
def test_install_script_handles_missing_cli(host: str, cli: str) -> None:
    script = SCRIPTS / f"install_{host}.sh"
    content = script.read_text()
    assert "exit 0" in content, f"scripts/install_{host}.sh should exit 0 when CLI absent"
    assert cli in content, f"scripts/install_{host}.sh should check for '{cli}' CLI"


# ---------------------------------------------------------------------------
# 14. Claude plugin package structure
# ---------------------------------------------------------------------------


def test_makefile_has_claude_plugin_targets() -> None:
    content = MAKEFILE.read_text()
    assert "install:" in content
    assert "verify:" in content
    assert "install-claude:" not in content
    assert "verify-claude:" not in content


# ---------------------------------------------------------------------------
# 15. New canonical plugin location: integrations/claude/plugin/
# ---------------------------------------------------------------------------

CLAUDE_PLUGIN_NEW = INTEGRATIONS / "claude" / "plugin"


def test_new_claude_plugin_dir_exists() -> None:
    assert CLAUDE_PLUGIN_NEW.is_dir(), "integrations/claude/plugin/ directory must exist"


def test_new_claude_plugin_json_name() -> None:
    plugin_json = CLAUDE_PLUGIN_NEW / ".claude-plugin" / "plugin.json"
    assert plugin_json.exists(), "integrations/claude/plugin/.claude-plugin/plugin.json must exist"
    data = json.loads(plugin_json.read_text())
    assert data.get("name") == "atelier", f"plugin.json name should be 'atelier', got: {data.get('name')}"


def test_new_claude_plugin_json_has_no_commands_key() -> None:
    plugin_json = CLAUDE_PLUGIN_NEW / ".claude-plugin" / "plugin.json"
    if not plugin_json.exists():
        pytest.skip("integrations/claude/plugin/.claude-plugin/plugin.json not found")
    data = json.loads(plugin_json.read_text())
    assert (
        "commands" not in data
    ), "plugin.json must not have 'commands' key — use 'skills' for /atelier:name namespacing"


def test_new_claude_plugin_json_author_is_object() -> None:
    """author must be an object like {"name": "..."} — Claude Code install rejects a plain string."""
    plugin_json = CLAUDE_PLUGIN_NEW / ".claude-plugin" / "plugin.json"
    if not plugin_json.exists():
        pytest.skip("integrations/claude/plugin/.claude-plugin/plugin.json not found")
    data = json.loads(plugin_json.read_text())
    assert isinstance(
        data.get("author"), dict
    ), f'plugin.json \'author\' must be an object like {{"name": "Beseam"}}, got: {data.get("author")!r}'


def test_new_claude_plugin_json_no_manifest_keys() -> None:
    """agents/skills/hooks/mcp are auto-discovered from directory structure.
    Declaring them in plugin.json causes 'Invalid input' errors during install."""
    plugin_json = CLAUDE_PLUGIN_NEW / ".claude-plugin" / "plugin.json"
    if not plugin_json.exists():
        pytest.skip("integrations/claude/plugin/.claude-plugin/plugin.json not found")
    data = json.loads(plugin_json.read_text())
    for forbidden in ("agents", "skills", "hooks", "mcp"):
        assert forbidden not in data, (
            f"plugin.json must NOT declare '{forbidden}' — Claude Code auto-discovers from "
            f"directory structure; listing it causes install validation errors"
        )


@pytest.mark.parametrize("skill_name", sorted(expected_visible_skill_names()))
def test_new_claude_plugin_user_skill_exists(skill_name: str) -> None:
    skill_file = INTEGRATIONS / "skills" / skill_name / "SKILL.md"
    assert skill_file.exists(), f"integrations/skills/{skill_name}/SKILL.md must exist"


@pytest.mark.parametrize("skill_name", sorted(expected_visible_skill_names()))
def test_new_claude_plugin_skill_has_description(skill_name: str) -> None:
    skill_file = INTEGRATIONS / "skills" / skill_name / "SKILL.md"
    if not skill_file.exists():
        pytest.skip(f"skill file not found: {skill_name}")
    content = skill_file.read_text()
    assert "description:" in content, f"skills/{skill_name}/SKILL.md must have 'description:' in frontmatter"


def test_new_claude_plugin_has_agents() -> None:
    agents_dir = CLAUDE_PLUGIN_NEW / "agents"
    assert agents_dir.is_dir(), "integrations/claude/plugin/agents/ directory must exist"
    for name in (
        "code.md",
        "explore.md",
        "execute.md",
        "plan.md",
        "research.md",
        "review.md",
        "solve.md",
    ):
        assert (agents_dir / name).exists(), f"integrations/claude/plugin/agents/{name} must exist"


def test_new_claude_plugin_has_workflows() -> None:
    workflows_dir = CLAUDE_PLUGIN_NEW / "workflows"
    assert workflows_dir.is_dir(), "integrations/claude/plugin/workflows/ directory must exist"
    assert (workflows_dir / "code-audit.js").exists(), "integrations/claude/plugin/workflows/code-audit.js must exist"
    assert (
        workflows_dir / "gate-benchmark.js"
    ).exists(), "integrations/claude/plugin/workflows/gate-benchmark.js must exist"


def test_new_claude_plugin_workflow_readme_documents_discovery_contract() -> None:
    readme = CLAUDE_PLUGIN_NEW / "workflows" / "README.md"
    assert readme.exists(), "integrations/claude/plugin/workflows/README.md must exist"
    content = readme.read_text()
    assert "v2.1.154" in content
    assert "/workflows" in content
    assert "read/search/git-diff" in content
    assert "verify_claude.sh" in content
    assert "gate-benchmark.js" in content


def test_new_claude_plugin_mcp_is_valid() -> None:
    mcp_json = CLAUDE_PLUGIN_NEW / ".mcp.json"
    assert mcp_json.exists(), "integrations/claude/plugin/.mcp.json must exist"
    data = json.loads(mcp_json.read_text())
    assert "mcpServers" in data
    assert "atelier" in data["mcpServers"]
    assert data["mcpServers"]["atelier"]["command"] == "atelier"


def test_new_claude_plugin_hooks_enabled() -> None:
    """Hooks must be active (no enabled:false disabling them)."""
    hooks_json = CLAUDE_PLUGIN_NEW / "hooks" / "hooks.json"
    if not hooks_json.exists():
        pytest.skip("integrations/claude/plugin/hooks/hooks.json not found")
    data = json.loads(hooks_json.read_text())
    hooks_map = data.get("hooks", {})
    assert isinstance(hooks_map, dict), "hooks should be a dict of event→groups"
    for event, groups in hooks_map.items():
        for group in groups:
            assert group.get("enabled", True) is not False, (
                f"Hook group for event '{event}' is disabled (enabled:false). "
                f"Remove the 'enabled' field or set it to true: {group}"
            )


@pytest.mark.parametrize("script", ["post_tool_use_failure.py", "stop.py"])
def test_new_claude_plugin_hook_scripts_exist(script: str) -> None:
    """Python hook scripts must be present in the hooks/ directory."""
    hook_file = CLAUDE_PLUGIN_NEW / "hooks" / script
    assert hook_file.exists(), (
        f"integrations/claude/plugin/hooks/{script} must exist — "
        "it is referenced by hooks.json and required for Atelier hook functionality."
    )


def test_new_claude_plugin_settings_uses_supported_keys() -> None:
    """Plugin settings.json may only use keys supported by Claude Code: `agent` and `subagentStatusLine`."""
    settings = CLAUDE_PLUGIN_NEW / "settings.json"
    assert settings.exists(), "integrations/claude/plugin/settings.json must exist"
    data = json.loads(settings.read_text())
    allowed = {"agent", "subagentStatusLine"}
    extra = set(data.keys()) - allowed
    assert not extra, f"settings.json contains unsupported keys: {extra}. Only {allowed} are honored by Claude Code."
    assert (
        data.get("agent") == "atelier:code"
    ), "settings.json must set `agent` to 'atelier:code' so it appears as the default agent for the atelier plugin."


def test_new_claude_plugin_subagent_statusline_wired() -> None:
    """settings.json must wire subagentStatusLine to scripts/statusline.sh."""
    settings = CLAUDE_PLUGIN_NEW / "settings.json"
    if not settings.exists():
        pytest.skip("settings.json missing")
    data = json.loads(settings.read_text())
    sl = data.get("subagentStatusLine")
    assert isinstance(sl, dict), "subagentStatusLine must be a dict"
    assert sl.get("type") == "command", "subagentStatusLine.type must be 'command'"
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/statusline.sh" in sl.get(
        "command", ""
    ), "subagentStatusLine.command must reference ${CLAUDE_PLUGIN_ROOT}/scripts/statusline.sh"


def test_new_claude_plugin_statusline_script_exists_and_executable() -> None:
    """scripts/statusline.sh must exist and be executable."""
    script = CLAUDE_PLUGIN_NEW / "scripts" / "statusline.sh"
    assert (
        script.exists()
    ), "integrations/claude/plugin/scripts/statusline.sh must exist — wired by settings.json subagentStatusLine."
    assert os.access(script, os.X_OK), f"{script} must be executable (chmod +x)"


def test_new_claude_plugin_stop_hook_uses_valid_decision() -> None:
    """stop.py must NOT emit `decision: "ask"` — only "block" is a valid Stop decision.

    For non-blocking display, use `systemMessage` instead.
    """
    stop_py = (CLAUDE_PLUGIN_NEW / "hooks" / "stop.py").read_text()
    assert '"decision": "ask"' not in stop_py and "'decision': 'ask'" not in stop_py, (
        'stop.py emits invalid `decision: "ask"`. Stop hooks only accept '
        '`decision: "block"`. Use `systemMessage` for non-blocking display.'
    )


# ---------------------------------------------------------------------------
# 16. Repo-root marketplace.json for 'claude plugin marketplace add .'
# ---------------------------------------------------------------------------


def test_root_marketplace_json_exists() -> None:
    mktplace = INTEGRATIONS / "claude" / "plugin" / ".claude-plugin" / "marketplace.json"
    assert mktplace.exists(), "integrations/claude/plugin/.claude-plugin/marketplace.json must exist"


def test_root_marketplace_json_name() -> None:
    mktplace = INTEGRATIONS / "claude" / "plugin" / ".claude-plugin" / "marketplace.json"
    if not mktplace.exists():
        pytest.skip(".claude-plugin/marketplace.json not found")
    data = json.loads(mktplace.read_text())
    assert data.get("name") == "atelier", f"root marketplace.json name should be 'atelier', got: {data.get('name')}"


def test_root_marketplace_json_source_points_to_new_plugin() -> None:
    mktplace = INTEGRATIONS / "claude" / "plugin" / ".claude-plugin" / "marketplace.json"
    if not mktplace.exists():
        pytest.skip(".claude-plugin/marketplace.json not found")
    data = json.loads(mktplace.read_text())
    plugins = data.get("plugins", [])
    assert len(plugins) >= 1, "root marketplace.json must declare at least one plugin"
    source = plugins[0].get("source", "")
    assert (
        "integrations/claude/plugin" in source or source == "./"
    ), f"root marketplace.json source must point to integrations/claude/plugin or './', got: {source}"


# ---------------------------------------------------------------------------
# 17. Streamlined Makefile targets
# ---------------------------------------------------------------------------


def test_makefile_has_claude_targets() -> None:
    content = MAKEFILE.read_text()
    assert "dev:" in content
    assert "verify:" in content
    assert "scripts/install_claude.sh" not in content


def test_makefile_omits_claude_plugin_dev_targets() -> None:
    content = MAKEFILE.read_text()
    for target in ("install-claude-plugin-dev:", "verify-claude-plugin-dev:"):
        assert target not in content, f"Makefile should not expose target: {target}"


# ---------------------------------------------------------------------------
# 18. New scripts exist and are executable
# ---------------------------------------------------------------------------


def test_install_claude_script_exists() -> None:
    script = SCRIPTS / "install_claude.sh"
    assert script.exists(), "Missing: scripts/install_claude.sh"
    assert is_executable(script), "Not executable: scripts/install_claude.sh"


def test_verify_claude_script_exists() -> None:
    script = SCRIPTS / "verify_claude.sh"
    assert script.exists(), "Missing: scripts/verify_claude.sh"
    assert is_executable(script), "Not executable: scripts/verify_claude.sh"


def test_verify_claude_checks_workflow_assets_and_version_note() -> None:
    script = (SCRIPTS / "verify_claude.sh").read_text()
    assert 'WORKFLOWS_DIR="${PLUGIN_DIR}/workflows"' in script
    assert 'WORKFLOW_FILE="${WORKFLOWS_DIR}/code-audit.js"' in script
    assert 'GATE_WORKFLOW_FILE="${WORKFLOWS_DIR}/gate-benchmark.js"' in script
    assert 'WORKFLOW_README="${WORKFLOWS_DIR}/README.md"' in script
    assert "workflow assets bundled" in script
    assert "/workflows" in script
    assert "v2.1.154" in script


def test_install_claude_uses_new_plugin_path() -> None:
    script = SCRIPTS / "install_claude.sh"
    content = script.read_text()
    assert "integrations/claude/plugin" in content, "install_claude.sh must reference integrations/claude/plugin"


def test_install_claude_stages_statusline_assets() -> None:
    script = SCRIPTS / "install_claude.sh"
    content = script.read_text()
    assert "cp -r '${SOURCE_PLUGIN_DIR}/scripts' '$STAGING_DIR/'" in content
    assert "cp '${SOURCE_PLUGIN_DIR}/settings.json' '$STAGING_DIR/'" in content
    assert (
        'data["subagentStatusLine"] = {"type": "command", "command": "${STATUSLINE_SCRIPT}", "padding": 1}' in content
    )


def test_install_claude_stages_workflow_assets() -> None:
    script = SCRIPTS / "install_claude.sh"
    content = script.read_text()
    assert "cp -r '${SOURCE_PLUGIN_DIR}/workflows' '$STAGING_DIR/'" in content


# ---------------------------------------------------------------------------
# 19. Docs use correct /atelier:skill namespacing (not /atelier-skill)
# ---------------------------------------------------------------------------


def test_docs_use_atelier_colon_not_dash_for_skills() -> None:
    doc = DOCS_HOSTS / "claude-code-install.md"
    if not doc.exists():
        pytest.skip("claude-code-install.md not found")
    content = doc.read_text()
    assert "/atelier:code" in content, "claude-code-install.md must document /atelier:code (colon, not dash)"
    # Ensure the wrong form is not present (unless it's mentioned as a legacy note)
    # We allow it if explicitly labelled as deprecated/old
    bad_uses = [
        line
        for line in content.splitlines()
        if "/atelier-code" in line and "deprecated" not in line.lower() and "old" not in line.lower()
    ]
    assert not bad_uses, f"claude-code-install.md uses /atelier-code (dash) without deprecated label: {bad_uses}"


def test_docs_mention_three_install_modes() -> None:
    doc = DOCS_HOSTS / "claude-code-install.md"
    if not doc.exists():
        pytest.skip("claude-code-install.md not found")
    content = doc.read_text()
    assert "marketplace" in content.lower(), "docs must mention marketplace install mode"
    assert "dev" in content.lower() or "plugin-dir" in content.lower(), "docs must mention dev mode (--plugin-dir)"
    assert "mcp-only" in content.lower() or "mcp only" in content.lower(), "docs must mention MCP-only fallback mode"


def test_claude_install_docs_mention_workflows_and_version_gate() -> None:
    doc = DOCS_HOSTS / "claude-code-install.md"
    if not doc.exists():
        pytest.skip("claude-code-install.md not found")
    content = doc.read_text()
    assert "workflows" in content.lower()
    assert "2.1.154" in content
    assert "code-audit.js" in content
    assert "gate-benchmark.js" in content
    assert "/workflows" in content
    assert "verify_claude.sh" in content


# ---------------------------------------------------------------------------
# Universal status helper + per-host atelier identity artifacts
# ---------------------------------------------------------------------------


def test_agents_atelier_md_has_persona() -> None:
    f = INTEGRATIONS / "AGENTS.atelier.md"
    assert f.exists(), "Missing: integrations/AGENTS.atelier.md"
    content = f.read_text()
    assert "atelier:code" in content, "AGENTS.atelier.md must declare atelier:code persona"


def test_opencode_atelier_agent_exists() -> None:
    f = INTEGRATIONS / "opencode" / "agents" / "atelier.md"
    assert f.exists(), "Missing: integrations/opencode/agents/atelier.md"
    text = f.read_text()
    assert "atelier:code" in text
    assert "---" in text, "opencode agent must have frontmatter"


def test_copilot_atelier_agents_exist() -> None:
    code_agent = INTEGRATIONS / "copilot" / "agents" / "atelier.code.agent.md"
    execute_agent = INTEGRATIONS / "copilot" / "agents" / "atelier.execute.agent.md"
    assert code_agent.exists(), "Missing: integrations/copilot/agents/atelier.code.agent.md"
    assert execute_agent.exists(), "Missing: integrations/copilot/agents/atelier.execute.agent.md"
    text = code_agent.read_text()
    assert "atelier:code" in text
    assert "description:" in text, "agent must have description: frontmatter"
    assert "model: gpt-5.4" in text, "Copilot agent must pin the model in frontmatter"


def test_makefile_has_atelier_status_target() -> None:
    content = MAKEFILE.read_text()
    assert "status:" in content
    assert "scripts/status.sh" in content
