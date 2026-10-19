"""
test_agent_cli_install_artifacts.py — Verify all install/verify script artifacts exist.

These tests do NOT require any agent CLI (claude, codex, opencode, etc.) to be installed.
They verify that all expected files and scripts exist with correct permissions.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.default_definitions import build_default_registry
from lemoncrow.core.environment import skill_visible

LEMONCROW_ROOT = Path(__file__).parent.parent.parent
SCRIPTS = LEMONCROW_ROOT / "scripts"
INTEGRATIONS = LEMONCROW_ROOT / "integrations"
DOCS_HOSTS = LEMONCROW_ROOT / "docs" / "hosts"
MAKEFILE = LEMONCROW_ROOT / "Makefile"


def is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def expected_visible_skill_names() -> set[str]:
    return {
        path.parent.name for path in (INTEGRATIONS / "skills").glob("*/SKILL.md") if skill_visible(path.parent.name)
    }


def test_build_host_skills_generates_stable_bundle_by_default(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    subprocess.run(
        ["bash", str(SCRIPTS / "build_host_skills.sh"), "--host", "codex", "--dest", str(dest)],
        cwd=LEMONCROW_ROOT,
        check=True,
    )
    generated = {path.name for path in dest.iterdir() if path.is_dir()}
    registry = build_default_registry(LEMONCROW_ROOT)
    expected = set(registry.surfaced_role_ids("shared_skill")) | {"lemoncrow"}
    assert generated == expected
    assert not expected_visible_skill_names() - {"lemoncrow"} & generated


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
        cwd=LEMONCROW_ROOT,
        check=True,
    )
    generated = {path.name for path in dest.iterdir() if path.is_dir()}
    registry = build_default_registry(LEMONCROW_ROOT)
    expected = set(registry.surfaced_role_ids("shared_skill")) | {"benchmark", "swarm", "lemoncrow"}
    assert generated == expected


def test_lemoncrow_meta_skill_ships_by_default_while_public_skills_stay_optional(tmp_path: Path) -> None:
    from lemoncrow.core.environment import skill_installed_by_default
    from lemoncrow.gateway.cli.commands.agents_skills import PUBLIC_SKILL_NAMES

    assert skill_installed_by_default("lemoncrow") is True
    for name in PUBLIC_SKILL_NAMES:
        assert skill_installed_by_default(name) is False, f"{name} must stay opt-in"
    for host in ("claude", "codex"):
        dest = tmp_path / host
        subprocess.run(
            ["bash", str(SCRIPTS / "build_host_skills.sh"), "--host", host, "--dest", str(dest)],
            cwd=LEMONCROW_ROOT,
            check=True,
        )
        assert (dest / "lemoncrow" / "SKILL.md").exists(), f"{host}: lemoncrow skill must ship by default"
        for name in PUBLIC_SKILL_NAMES:
            assert not (dest / name).exists(), f"{host}: {name} must not ship by default"


def test_codex_installer_contract() -> None:
    """case: test_codex_installers_stage_plugin_agent_surface"""
    for script in (SCRIPTS / "install_codex.sh",):
        content = script.read_text(encoding="utf-8")
        assert "integrations/codex/plugin/agents" in content
        assert "agents/openai.yaml" in content
        assert "write_codex_agent_config" in content
        assert "write_workspace_codex_agent_config" in content
        assert "agents\\.lemoncrow_code" in content
    "case: test_codex_installer_enables_plugin_hooks_with_restorable_marker"
    install = (SCRIPTS / "install_codex.sh").read_text(encoding="utf-8")
    uninstall = (SCRIPTS / "uninstall_codex.sh").read_text(encoding="utf-8")
    assert '"hooks": ("true", "LemonCrow lifecycle capture requires Codex plugin hooks")' in install
    assert "LEMONCROW_KEEP_NATIVE_TOOLS=" in install
    assert '"--keep-native-tools: leaving Codex native tools enabled; lifecycle hooks remain enabled"' in install
    assert 'CODEX_TOOL_MARKER="# lemoncrow:native-tool-substitution"' in install
    assert 'vpass "Codex lifecycle hook feature enabled"' in install
    assert 'vfail "Codex lifecycle hook feature is disabled"' in install
    assert 'restore_prefix = f"{marker}:restore "' in uninstall
    "case: test_codex_installers_auto_approve_exposed_lemoncrow_tools"
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
    for script in (SCRIPTS / "install_codex.sh",):
        content = script.read_text(encoding="utf-8")
        for tool in expected_tools:
            assert f'"{tool}"' in content
        assert '"context"' not in content
    "case: test_install_codex_merges_existing_agents_file"
    content = (SCRIPTS / "install_codex.sh").read_text()
    assert "merge_agents_file()" in content
    assert 'source "${SCRIPT_DIR}/lib/managed_context.sh"' in content
    assert 'backup_file "$dest_file"' in content
    assert "merged LemonCrow Codex instructions into $dest_file" in content
    assert 'lemoncrow_upsert_managed_block "$source_file" "$dest_file" "$DRY_RUN"' in content
    assert "integrations/codex/hooks" in content
    "case: test_uninstall_codex_removes_managed_agents_block"
    content = (SCRIPTS / "uninstall_codex.sh").read_text()
    assert 'source "${SCRIPT_DIR}/lib/managed_context.sh"' in content
    assert "Removed managed LemonCrow Codex instructions from $AGENTS_FILE" in content


def test_host_installer_selection_contract() -> None:
    """case: test_host_installer_default_selection_uses_detection"""
    content = (SCRIPTS / "install_hosts.sh").read_text()
    assert "host_is_detected()" in content
    assert "enable_detected_hosts_by_default" in content
    assert "enable_detected_hosts_by_default" in content.split("# Default: all hosts", 1)[1]
    "case: test_pi_is_default_selected_and_bundled_for_fresh_installs"
    common = (SCRIPTS / "lib" / "common.sh").read_text()
    hosts = (SCRIPTS / "install_hosts.sh").read_text()
    build = (SCRIPTS / "build.sh").read_text()
    pi_installer = SCRIPTS / "install_pi.sh"
    pi_choice_tail = common.split('HOST_CHOICES+=("Pi|will be installed")', 1)[1]
    assert "HOST_DEFAULT_SELECTION+=(1)" in pi_choice_tail
    assert "4) HOST_FLAGS+=(--pi)" in common
    assert "--pi" in hosts
    assert "$DO_PI        && run_installer pi" in hosts
    assert pi_installer.exists() and is_executable(pi_installer)
    assert "host install --engine pi" in pi_installer.read_text()
    assert "scripts/install_pi.sh" in build
    assert "scripts/uninstall_pi.sh" in build
    assert "pi_integration.py bundle/scripts/lib/pi_integration.py" in build
    "case: test_opencode_code_agent_uses_native_free_tier_transport"
    agent = INTEGRATIONS / "opencode" / "agents" / "code.md"
    frontmatter = agent.read_text(encoding="utf-8").split("---", 2)[1]
    assert "model: opencode/big-pickle" in frontmatter
    "case: test_opencode_installer_keeps_native_tools_available_for_console_free_tier"
    installer = (SCRIPTS / "install_opencode.sh").read_text(encoding="utf-8")
    for tool in ("read", "edit", "grep", "glob", "list", "bash", "webfetch", "lsp"):
        assert f'"{tool}": "deny"' not in installer
    assert "permission.pop(tool)" in installer


def test_installer_environment_contract() -> None:
    """case: test_model_gateway_installers_do_not_reuse_service_url"""
    lemoncode = (SCRIPTS / "install_lemoncode.sh").read_text()
    assert "LEMONCROW_GATEWAY_URL" in lemoncode
    assert "LEMONCROW_GATEWAY_BASE" in lemoncode
    assert "LEMONCROW_SERVICE_URL" not in lemoncode
    assert "LEMONCROW_SERVICE_BASE" not in lemoncode
    opencode = (SCRIPTS / "install_opencode.sh").read_text()
    assert "LEMONCROW_GATEWAY_URL" not in opencode
    assert "LEMONCROW_GATEWAY_BASE" not in opencode
    "case: test_opencode_install_passes_config_path_via_env_not_source_interpolation"
    "Regression: OC_FILE must not be interpolated into Python heredoc source.\n\n    A config path containing single quotes or backslashes (e.g. /home/o'brien/...)\n    broke `Path('$OC_FILE')` with a SyntaxError at compile time, leaving the config\n    unwritten. The path must be passed through the environment and read with\n    os.environ inside the heredoc instead.\n    "
    content = (SCRIPTS / "install_opencode.sh").read_text()
    assert "Path('$OC_FILE')" not in content, "opencode install.sh must not interpolate $OC_FILE into Python source"
    # OpenCode V2 now invokes the dedicated Python config helper for its
    # merge; the remaining inline configuration and inspection heredocs must
    # still pass paths through environment variables, not interpolate source.
    assert content.count('LEMONCROW_OC_FILE="$OC_FILE"') == 3
    assert content.count("os.environ['LEMONCROW_OC_FILE']") == 3
    assert '"${SCRIPT_DIR}/opencode_v2_config.py" merge "$OC_FILE" "$NEW_ENTRY"' in content
    "case: test_claude_install_passes_workspace_paths_via_env_not_source_interpolation"
    "Regression: CLAUDE_LOCAL_SETTINGS / WORKSPACE must not be interpolated into Python heredoc source.\n\n    A workspace path containing backslashes or single quotes (e.g. /tmp/a\\b, /home/o'brien)\n    broke `Path('${CLAUDE_LOCAL_SETTINGS}')`: Python parsed backslash escapes into a mangled,\n    nonexistent path (FileNotFoundError) or produced a SyntaxError, aborting the install under\n    set -e. The paths must be exported to the subprocess and read with os.environ inside the\n    heredoc, which must use a quoted delimiter to suppress shell interpolation.\n    "
    content = (SCRIPTS / "install_claude.sh").read_text()
    assert (
        "Path('${CLAUDE_LOCAL_SETTINGS}')" not in content
    ), "claude install.sh must not interpolate ${CLAUDE_LOCAL_SETTINGS} into Python source"
    assert (
        "CLAUDE_WORKSPACE_ROOT'] = '${WORKSPACE}'" not in content
    ), "claude install.sh must not interpolate ${WORKSPACE} into Python source"
    assert (
        'LEMONCROW_CLAUDE_LOCAL_SETTINGS="${CLAUDE_LOCAL_SETTINGS}" LEMONCROW_WORKSPACE="${WORKSPACE}" python3 - <<\'PYEOF\''
        in content
    )
    assert "os.environ['LEMONCROW_CLAUDE_LOCAL_SETTINGS']" in content
    assert "os.environ['LEMONCROW_WORKSPACE']" in content
    "case: test_local_server_script_persists_machine_authenticated_thin_client_target"
    local_server = (SCRIPTS / "local_server.sh").read_text()
    assert 'URL="http://127.0.0.1:${PORT}"' in local_server
    assert "LEMONCROW_URL=%s" in local_server
    assert "LEMONCROW_INSTALL_MODE=local" in local_server
    assert 'TOKEN_FILE="${LEMONCROW_LOCAL_TOKEN_FILE:-${HOME_DIR}/token}"' in local_server
    assert "LEMONCROW_TOKEN_FILE=%s" in local_server
    assert "--token-file" in local_server
    assert local_server.count("--local-no-auth") == 1
    assert 'grep -Fq -- "--local-no-auth" "$file"' in local_server
    assert '--frontend-dir "$FRONTEND_DIR"' in local_server
    assert 'SOURCE_FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)/frontend/dist"' in local_server
    assert "sync_source_frontend" in local_server
    assert '[[ -z "${LEMONCROW_FRONTEND_DIR:-}" ]] || return 0' in local_server
    assert "--allow-local-fs" in local_server
    assert "LEMONCROW_LOCAL_FS=1" in local_server
    assert "--local-review-root" not in local_server
    assert "retire_legacy_review_workspaces" in local_server
    assert "lemoncrow_server_core up" in local_server
    assert "lemoncrow_server.ops up" not in local_server
    assert 'SYSTEMD_UNIT="lemoncrow-local-server${SERVICE_SUFFIX}.service"' in local_server
    assert 'HEALTH_TIMEOUT_SECONDS="${LEMONCROW_LOCAL_SERVER_START_TIMEOUT:-120}"' in local_server
    assert "while (( SECONDS < deadline ))" in local_server
    assert "Restart=always" in local_server
    assert "StartLimitIntervalSec=0" in local_server


def test_distribution_boundary_contract() -> None:
    """case: test_distribution_constraints_exclude_local_paths_and_bundle_local_wheels"""
    build = (SCRIPTS / "build.sh").read_text()
    bundle = (SCRIPTS / "bundle.sh").read_text()
    sessions = (SCRIPTS / "sessions.sh").read_text()
    assert "--no-emit-local" in build
    assert "uv build --wheel --package lemoncrow-client --out-dir bundle/vendor" in build
    assert "cp vendor/babel-*.whl bundle/vendor/" in build
    assert 'find_links_arg=(--find-links "${vendor_dir}")' in bundle
    assert 'find_links_args=(--find-links "$vendor_dir")' in sessions
    assert "constraints.resolved.txt" not in bundle
    constraints_block = bundle.split("local constraints_arg=()", 1)[1].split("# litellm", 1)[0]
    assert "sed -E" not in constraints_block
    "case: test_local_and_hosted_install_modes_are_separate_distributions"
    makefile = (SCRIPTS.parent / "Makefile").read_text()
    install = (SCRIPTS / "engine_install.sh").read_text()
    hosted = (SCRIPTS / "hosted.sh").read_text()
    review_build = (SCRIPTS / "build_review_client.sh").read_text()
    bundle = (SCRIPTS / "bundle.sh").read_text()
    build = (SCRIPTS / "build.sh").read_text()
    common = (SCRIPTS / "lib" / "common.sh").read_text()
    assert "bash scripts/build_review_client.sh" in makefile
    assert makefile.split("\nhosted-test:", 1)[1].split("##", 1)[0].split() == ["review-client-build"]
    hosted_build = makefile.split("\nreview-client-build:", 1)[1].split("\n\n", 1)[0]
    assert "bash scripts/hosted.sh --from-build dist/lemoncrow-review-client.tar.gz" in makefile
    hosted_target = makefile.split("\nhosted-prod:", 1)[1].split("\n\n", 1)[0]
    assert "hosted-test" in hosted_target and "--product review --cloud" in hosted_target
    assert "scripts/build.sh" not in hosted_target
    assert "build-lemoncode-host" not in hosted_target
    assert "https://api.lemoncrow.com" in hosted
    assert "scripts/install.sh" not in hosted
    assert 'exec bash "$installer"' not in hosted
    assert "scripts/bundle.sh" not in hosted
    assert 'bash "$BUNDLE_SH"' not in hosted
    assert "lemoncrow-distribution-" not in hosted
    assert "lemoncrow-review-client.tar.gz" in hosted
    assert "--no-index --no-deps --force-reinstall" in hosted
    assert "--local was renamed to --from-build" in hosted
    assert "missing or invalid SHA-256 checksum; refusing" in hosted
    assert 'REVIEW_MODE="hosted"' in hosted
    assert 'REVIEW_MODE="local"' in hosted
    assert "build_review_client.sh" in hosted_build
    assert "lemoncrow-review-client.tar.gz" in review_build
    assert "build_runtime_client.sh" not in hosted_build
    assert "engine_install.sh is local-only" in install
    assert 'exec bash "$hosted_installer"' not in install
    assert "No valid checksum published" in install
    assert "uv build --wheel server --out-dir bundle/server" in build
    assert 'server_with_args=(--with "$server_wheel")' in bundle
    assert 'bash "$server_script" restart' in bundle
    assert 'bash "${SCRIPT_DIR}/local_server.sh" stop' in bundle
    assert "Hosted install requires LEMONCROW_HOSTED_URL" not in bundle
    assert "LEMONCROW_INSTALL_MODE=hosted" not in bundle
    assert "scripts/hosted.sh" not in build
    assert "LEMONCROW_NO_SERVICECTL" not in bundle
    assert "LEMONCROW_NO_STACK" not in bundle
    assert '"$mcp_python" -m lemoncrow_client runtime mcp service repair' in common
    assert "SessionStart" in common
    server_mode_init = common.split('case "${LEMONCROW_INSTALL_MODE:-legacy}" in', 1)[1].split(
        "local code_display=", 1
    )[0]
    assert "$lemoncrow_cli runtime init" in server_mode_init
    assert "Workspace sync:" not in server_mode_init
    "case: test_pinned_tool_versions_stay_in_sync_across_installer_and_runtime"
    common = (SCRIPTS / "lib" / "common.sh").read_text()
    binaries = (LEMONCROW_ROOT / "src/lemoncrow/infra/code_intel/astgrep/binaries.py").read_text()
    compactors = (LEMONCROW_ROOT / "src/lemoncrow/pro/capabilities/tool_supervision/external_compactors.py").read_text()
    astgrep = re.search('LEMONCROW_ASTGREP_VERSION="([^"]+)"', common)
    assert astgrep, "installer must pin LEMONCROW_ASTGREP_VERSION"
    assert f'_MANAGED_VERSION = "{astgrep.group(1)}"' in binaries
    assert f"ast-grep/releases/download/{astgrep.group(1)}/" in common
    shas = re.findall('sha256="([0-9a-f]{64})"', binaries)
    assert len(shas) == 4
    assert all(sha in common for sha in shas)
    rtk = re.search('LEMONCROW_RTK_TAG="\\$\\{LEMONCROW_RTK_TAG-([^}]+)\\}"', common)
    assert rtk, "installer must pin LEMONCROW_RTK_TAG"
    assert f"--tag {rtk.group(1)}" in compactors
    "case: test_installer_version_compare_only_upgrades_older_installs"
    for have, want, older in [
        ("0.44.0", "0.45.1", True),
        ("0.9.0", "0.45.1", True),
        ("0.45.1", "0.45.1", False),
        ("0.46.0", "0.45.1", False),
        ("", "0.45.1", False),
    ]:
        common = (SCRIPTS / "lib" / "common.sh").read_text()
        funcs = re.search("^_ver_lt\\(\\) \\{.*?^\\}", common, re.S | re.M)
        assert funcs
        result = subprocess.run(["bash", "-c", f'{funcs.group(0)}\n_ver_lt "$1" "$2"', "_", have, want], check=False)
        assert (result.returncode == 0) is older
    "case: test_legacy_runtime_cleanup_is_shipped_wired_and_public"
    cleanup = SCRIPTS / "cleanup_legacy_runtime.sh"
    assert cleanup.exists()
    assert is_executable(cleanup)
    assert (
        "cp -f scripts/cleanup_legacy_runtime.sh bundle/scripts/cleanup_legacy_runtime.sh"
        in (SCRIPTS / "build.sh").read_text()
    )
    assert "cleanup_legacy_runtime" in (SCRIPTS / "bundle.sh").read_text()
    assert "scripts/cleanup_legacy_runtime.sh" in (SCRIPTS / "public-paths.txt").read_text().splitlines()


def test_distribution_install_failure_contract() -> None:
    """case: test_distribution_installer_always_installs_wheel_and_propagates_failures"""
    content = (SCRIPTS / "engine_install.sh").read_text()
    assert '[[ "$LEMONCROW_NO_HOSTS" == "1" ]] && SETUP_ARGS+=(--no-hosts)' in content
    assert "Skipping setup (LEMONCROW_NO_HOSTS=1)" not in content
    assert '|| _fail_install "LemonCrow setup failed while installing the bundled wheel."' in content
    assert '_fail_install "bundle.sh not found at ${BUNDLE_SH} — the distribution archive is incomplete."' in content
    "case: test_installer_never_swallows_tar_or_foreign_cli_status"
    content = (SCRIPTS / "engine_install.sh").read_text()
    assert 'done < <(tar -xvzf "$arc" -C "$dest" 2>&1; printf \'%s\' "$?" >"$rc_file")' in content
    assert '|| _fail_install "Could not extract ${ASSET_NAME}' in content
    assert "command -v lc >/dev/null 2>&1" not in content
    assert re.search(
        'if\\s*\\[\\[\\s*-n\\s*"\\$LEMONCROW_INSTALLED_VERSION"\\s*\\]\\]\\s*;\\s*then\\b(?:(?!\\nfi\\b).)*?echo\\s+"  Version:',
        content,
        re.S,
    ), "the success banner must be gated on a version proven by a successful --version"
    assert "--version 2>/dev/null || echo" not in content
    "case: test_bundle_sh_requires_a_wheel_or_an_already_installed_binary"
    content = (SCRIPTS / "bundle.sh").read_text()
    assert 'verbose "No bundled wheel found — assuming LemonCrow already installed"' not in content
    assert 'fail "No LemonCrow wheel in ${LEMONCROW_INSTALL_DIR}/bin' in content
    assert "_record_resolved_bin_dir()" in content
    assert ".lemoncrow-bin-dir" in content
    assert ".lemoncrow-bin-dir" in (SCRIPTS / "engine_install.sh").read_text()
    "case: test_install_script_handles_missing_cli"
    for host, cli in [
        ("codex", "codex"),
        ("opencode", "opencode"),
        ("lemoncode", "lemoncode"),
        ("copilot", "code"),
        ("antigravity", "antigravity"),
    ]:
        script = SCRIPTS / f"install_{host}.sh"
        content = script.read_text()
        assert "exit 0" in content, f"scripts/install_{host}.sh should exit 0 when CLI absent"
        assert cli in content, f"scripts/install_{host}.sh should check for '{cli}' CLI"


def test_claude_plugin_static_contract() -> None:
    """case: test_new_claude_plugin_does_not_bundle_mcp"""
    "The plugin must NOT ship .mcp.json -- see install_claude.sh's matching\n    structural-validation guard for why (duplicate plugin:lemoncrow:lc\n    namespace / doubled token cost)."
    mcp_json = CLAUDE_PLUGIN_NEW / ".mcp.json"
    assert not mcp_json.exists(), "integrations/claude/plugin/.mcp.json must not exist"
    "case: test_new_claude_plugin_hooks_enabled"
    "Hooks must be active (no enabled:false disabling them)."
    hooks_json = CLAUDE_PLUGIN_NEW / "hooks" / "hooks.json"
    if not hooks_json.exists():
        pytest.skip("integrations/claude/plugin/hooks/hooks.json not found")
    data = json.loads(hooks_json.read_text())
    hooks_map = data.get("hooks", {})
    assert isinstance(hooks_map, dict), "hooks should be a dict of event→groups"
    for event, groups in hooks_map.items():
        for group in groups:
            assert (
                group.get("enabled", True) is not False
            ), f"Hook group for event '{event}' is disabled (enabled:false). Remove the 'enabled' field or set it to true: {group}"
    "case: test_new_claude_plugin_settings_uses_supported_keys"
    "Plugin settings stay supported without pinning every session to one agent."
    settings = CLAUDE_PLUGIN_NEW / "settings.json"
    assert settings.exists(), "integrations/claude/plugin/settings.json must exist"
    data = json.loads(settings.read_text())
    allowed = {"subagentStatusLine"}
    extra = set(data) - allowed
    assert not extra, f"settings.json contains unsupported keys: {extra}"
    assert (
        "agent" not in data
    ), "the plugin bundle must not re-assert a static default agent; the installer owns that user setting so dormancy and explicit host selection can clear it"
    "case: test_new_claude_plugin_subagent_statusline_wired"
    "settings.json must wire subagentStatusLine to scripts/statusline.sh."
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
    "case: test_new_claude_plugin_stop_hook_uses_valid_decision"
    'stop.py must NOT emit `decision: "ask"` — only "block" is a valid Stop decision.\n\n    For non-blocking display, use `systemMessage` instead.\n    '
    stop_py = (CLAUDE_PLUGIN_NEW / "hooks" / "stop.py").read_text()
    assert (
        '"decision": "ask"' not in stop_py and "'decision': 'ask'" not in stop_py
    ), 'stop.py emits invalid `decision: "ask"`. Stop hooks only accept `decision: "block"`. Use `systemMessage` for non-blocking display.'
    "case: test_verify_claude_checks_workflow_assets_and_version_note"
    script = (SCRIPTS / "verify_claude.sh").read_text()
    assert 'WORKFLOWS_DIR="${PLUGIN_DIR}/workflows"' in script
    assert 'WORKFLOW_FILE="${WORKFLOWS_DIR}/code-audit.js"' in script
    assert 'GATE_WORKFLOW_FILE="${WORKFLOWS_DIR}/gate-benchmark.js"' in script
    assert 'WORKFLOW_README="${WORKFLOWS_DIR}/README.md"' in script
    assert "workflow assets bundled" in script
    assert "/workflows" in script
    assert "v2.1.154" in script


def test_claude_plugin_install_contract() -> None:
    """case: test_install_claude_uses_new_plugin_path"""
    script = SCRIPTS / "install_claude.sh"
    content = script.read_text()
    assert "integrations/claude/plugin" in content, "install_claude.sh must reference integrations/claude/plugin"
    "case: test_install_claude_stages_statusline_assets"
    script = SCRIPTS / "install_claude.sh"
    content = script.read_text()
    assert 'run cp -r "${SOURCE_PLUGIN_DIR}/scripts" "$STAGING_DIR/"' in content
    assert 'run cp "${SOURCE_PLUGIN_DIR}/settings.json" "$STAGING_DIR/"' in content
    assert '"subagentStatusLine": {"type": "command", "command": "${STATUSLINE_SCRIPT}", "padding": 1},' in content
    "case: test_install_claude_always_loads_only_short_lc_server"
    content = (SCRIPTS / "install_claude.sh").read_text()
    assert "claude mcp add-json --scope user lc" in content
    assert "alwaysLoad" in content
    assert '"alwaysLoad": True' in content
    assert 'data.setdefault("mcpServers", {})["lc"]' in content
    assert 'MCP_ACTION="lite-mcp"' in content
    assert '$HOSTED && MCP_ACTION="mcp"' in content
    "case: test_install_claude_manages_dev_headroom_apply_env"
    content = (SCRIPTS / "install_claude.sh").read_text()
    assert "configure_dev_headroom_env" in content
    assert 'env["LEMONCROW_HEADROOM_MCP_TAIL_MODE"] = "apply"' in content
    assert 'env["LEMONCROW_HEADROOM_TAIL_STATS"]' in content
    assert "env.pop(key, None)" in content
    assert "headroom-tail-dev.jsonl" in content
    "case: test_install_claude_stages_workflow_assets"
    script = SCRIPTS / "install_claude.sh"
    content = script.read_text()
    assert 'run cp -r "${SOURCE_PLUGIN_DIR}/workflows" "$STAGING_DIR/"' in content


def test_build_host_skills_ignores_removed_dev_bundle_flag(tmp_path: Path) -> None:
    host = "antigravity"
    dest = tmp_path / "skills"
    subprocess.run(
        ["bash", str(SCRIPTS / "build_host_skills.sh"), "--host", host, "--dest", str(dest)],
        cwd=LEMONCROW_ROOT,
        check=True,
    )
    generated = {path.name for path in dest.iterdir() if path.is_dir()}
    build_default_registry(LEMONCROW_ROOT)
    assert generated == {"lemoncrow"}


@pytest.mark.parametrize("workspace_install", [False, True])
def test_opencode_installer_preserves_host_model_and_defaults(tmp_path: Path, workspace_install: bool) -> None:
    command = ["bash", str(SCRIPTS / "install_opencode.sh"), "--print-only"]
    if workspace_install:
        command.extend(["--workspace", str(tmp_path)])
    result = subprocess.run(command, cwd=LEMONCROW_ROOT, capture_output=True, text=True, check=True)
    config = json.loads(result.stdout.split("Merge/create config:\n", 1)[1])
    assert "default_agent" not in config
    assert "provider" not in config
    assert "model" not in config
    assert config["mcp"]["lc"]["command"] == ["lemoncrow", "runtime", "lite-mcp", "--host", "opencode"]


ANTIGRAVITY_INTEGRATION = INTEGRATIONS / "antigravity"
CODEX_PLUGIN = INTEGRATIONS / "codex" / "plugin"


def test_legacy_runtime_cleanup_preserves_persistent_mcp_services(tmp_path: Path) -> None:
    cleanup = SCRIPTS / "cleanup_legacy_runtime.sh"
    home = tmp_path / "home"
    user_units = home / ".config" / "systemd" / "user"
    wants = user_units / "default.target.wants"
    wants.mkdir(parents=True)
    for name in ("lemoncrow-controller.service", "lemoncrow-stack.service", "lemoncrow-mcp.service"):
        (user_units / name).write_text("legacy\n")
        (wants / name).symlink_to(user_units / name)
    persistent = user_units / "lemoncrow-mcp-keep-example-com.service"
    persistent.write_text("supported persistent MCP\n")
    state_root = home / ".lemoncrow"
    (state_root / "servicectl").mkdir(parents=True)
    (state_root / "servicectl" / "state.json").write_text("{}")
    (state_root / "mcp_daemons").mkdir()
    (state_root / "mcp_daemons" / "old.lock").write_text("")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    systemctl = fake_bin / "systemctl"
    systemctl.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$FAKE_SYSTEMD_LOG"\nexit 0\n')
    systemctl.chmod(493)
    uname = fake_bin / "uname"
    uname.write_text("#!/usr/bin/env bash\nprintf 'Linux\\n'\n")
    uname.chmod(493)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(state_root),
            "FAKE_SYSTEMD_LOG": str(log),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    result = subprocess.run(["bash", str(cleanup)], env=env, capture_output=True, text=True, check=True)
    assert "persistent MCP services were preserved" in result.stdout
    for name in ("lemoncrow-controller.service", "lemoncrow-stack.service", "lemoncrow-mcp.service"):
        assert not (user_units / name).exists()
        assert not (wants / name).exists()
    assert persistent.read_text() == "supported persistent MCP\n"
    assert not (state_root / "servicectl").exists()
    assert not (state_root / "mcp_daemons").exists()
    commands = log.read_text()
    assert "lemoncrow-mcp-keep-example-com.service" not in commands


_STUB_COMMON_SH = '#!/usr/bin/env bash\nLEMONCROW_BIN_DIR="${LEMONCROW_BIN_DIR:-${HOME}/.lemoncrow/bin}"\nLEMONCROW_INSTALL_DIR="${LEMONCROW_INSTALL_DIR:-$(pwd)}"\nLEMONCROW_TOOL_DIR="${LEMONCROW_TOOL_DIR:-${HOME}/.lemoncrow/uv-tools}"\nLEMONCROW_MEMORY_BACKEND=""\nLEMONCROW_TELEGRAPHIC=""\nLEMONCROW_ADVANCED=0\nLEMONCROW_VERBOSE=0\nLEMONCROW_NON_INTERACTIVE=1\nLEMONCROW_NO_HOSTS=0\nLEMONCROW_ZOEKT=0\nHOST_SCOPE_ARGS=()\nHOST_FLAGS=()\nFINAL_EXIT_CODE=0\nfail() { printf \'error: %s\\n\' "$*" >&2; exit 1; }\nwarn() { printf \'warn: %s\\n\' "$*" >&2; }\ninfo() { printf \'info: %s\\n\' "$*"; }\n# Mirrors the real helper: bundle.sh routes every local_server.sh call through\n# it so the standalone script\'s stdout cannot land at column 0 inside the box.\nrelay_output() {\n    local _out _line _rc=0 _color\n    _out="$("$@")" || _rc=$?\n    if [[ "$_rc" -eq 0 ]]; then _color="${C_GREEN:-}"; else _color="${C_RED:-}"; fi\n    while IFS= read -r _line; do\n        [[ -n "$_line" ]] || continue\n        printf "%b│%b  %b◇%b  %s\\n" "${C_FRAME:-}" "${C_RESET:-}" "$_color" "${C_RESET:-}" "$_line"\n    done <<<"$_out"\n    return "$_rc"\n}\nverbose() { :; }\nneed_cmd() { command -v "$1" >/dev/null 2>&1 || fail "missing $1"; }\nsupports_interactive_selector() { return 1; }\nprint_installer_header() { :; }\nprint_installer_footer() { :; }\nhost_wizard() { :; }\nprompt_memory_selection() { :; }\nprompt_auto_optimize_selection() { :; }\nprompt_local_zoekt_selection() { :; }\nprompt_rtk_selection() { :; }\nprompt_update_selection() { :; }\ninstall_uv_if_needed() { :; }\ninstall_node_if_needed() { :; }\n_capture_install_previous_version() { :; }\nassert_install_tree_consistent() { :; }\nassert_install_tree_unchanged() { :; }\npersist_install_record() { :; }\nstop_existing_lemoncrow_processes() { :; }\nwarn_on_foreign_cli_collision() { :; }\nensure_lc_alias() { :; }\nspin_tail() { shift; "$@"; }\nrun_setup() { :; }\n'


def _installer_sandbox(
    tmp_path: Path, bundle_body: str = "", *, use_real_bundle: bool = False, foreign_cli: bool = False
) -> tuple[Path, Path]:
    """A throwaway HOME plus a local bundle whose bundle.sh is `bundle_body`.

    `uv` and `curl` are shimmed so the real machine's uv tools and the network
    can never influence the result. With `use_real_bundle`, the repo's real
    bundle.sh is staged instead (against `_STUB_COMMON_SH`); with `foreign_cli`,
    an unrelated `lemoncrow` sits on PATH the way pipx/brew/an older install
    would.
    """
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / "uv-bin").mkdir()
    shims = home / "shims"
    shims.mkdir()
    uv_shim = shims / "uv"
    uv_shim.write_text(
        f'#!/usr/bin/env bash\nif [[ "$*" == "tool dir --bin" ]]; then echo "{home}/uv-bin"; exit 0; fi\nexit 0\n'
    )
    uv_shim.chmod(493)
    curl_shim = shims / "curl"
    curl_shim.write_text("#!/usr/bin/env bash\nexit 1\n")
    curl_shim.chmod(493)
    if foreign_cli:
        foreign = shims / "lemoncrow"
        foreign.write_text('#!/usr/bin/env bash\necho "lemoncrow 0.0.0-foreign"\n')
        foreign.chmod(493)
    src = tmp_path / "bundle"
    (src / "scripts").mkdir(parents=True)
    (src / "frontend" / "assets").mkdir(parents=True)
    (src / "frontend" / "index.html").write_text("<html>fresh review frontend</html>\n")
    (src / "frontend" / "assets" / "ReviewReader-test.js").write_text("// review reader\n")
    bundle = src / "scripts" / "bundle.sh"
    if use_real_bundle:
        (src / "scripts" / "lib").mkdir()
        (src / "scripts" / "lib" / "common.sh").write_text(_STUB_COMMON_SH)
        local_server = src / "scripts" / "local_server.sh"
        local_server.write_text("#!/usr/bin/env bash\nexit 0\n")
        local_server.chmod(493)
        bundle.write_text((SCRIPTS / "bundle.sh").read_text())
        server = src / "scripts" / "local_server.sh"
        server.write_text("#!/usr/bin/env bash\nexit 0\n")
        server.chmod(493)
    else:
        bundle.write_text(bundle_body)
    bundle.chmod(493)
    return (home, src)


def _run_installer(home: Path, src: Path, *, detached: bool = False) -> subprocess.CompletedProcess[str]:
    """Run engine_install.sh against the sandbox.

    `detached` starts a new session, so the installer has no controlling
    terminal and `/dev/tty` cannot be opened at all — the curl|bash-in-CI shape.
    """
    return subprocess.run(
        ["bash", str(SCRIPTS / "engine_install.sh"), "--local"],
        env={
            "HOME": str(home),
            "PATH": f"{home / 'shims'}:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "TERM": "dumb",
            "LEMONCROW_NON_INTERACTIVE": "1",
            "LEMONCROW_INSTALL_MODE": "local",
            "LEMONCROW_LOCAL_SRC": str(src),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        start_new_session=detached,
    )


_HAPPY_BUNDLE = '#!/usr/bin/env bash\nset -euo pipefail\nreal_bin="${HOME}/.lemoncrow/uv-tools-bin"\nmkdir -p "$real_bin"\nfor n in lemoncrow lc lcd; do\n    printf \'#!/usr/bin/env bash\\necho "lemoncrow 9.9.9"\\n\' >"$real_bin/$n"\n    chmod +x "$real_bin/$n"\ndone\nprintf \'%s\\n\' "$real_bin" >"${LEMONCROW_INSTALL_DIR%/}/.lemoncrow-bin-dir"\n'
_NOOP_BUNDLE = "#!/usr/bin/env bash\nexit 0\n"
_BROKEN_BINARY_BUNDLE = '#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p "$LEMONCROW_BIN_DIR"\nfor n in lemoncrow lc lcd; do\n    printf \'#!/usr/bin/env bash\\necho "ModuleNotFoundError: No module named \'"\'"\'lemoncrow\'"\'"\'" >&2\\nexit 1\\n\' >"$LEMONCROW_BIN_DIR/$n"\n    chmod +x "$LEMONCROW_BIN_DIR/$n"\ndone\nprintf \'%s\\n\' "$LEMONCROW_BIN_DIR" >"${LEMONCROW_INSTALL_DIR%/}/.lemoncrow-bin-dir"\n'


def test_installer_fails_when_the_installed_binary_cannot_run(tmp_path: Path) -> None:
    """`-x` is not proof of a working install (GH #41 follow-up).

    The readiness banner used to interpolate `lemoncrow --version` with
    `|| echo ''`, so a binary that died on every invocation still printed the
    success banner and the installer exited 0.
    """
    home, src = _installer_sandbox(tmp_path, _BROKEN_BINARY_BUNDLE)
    result = _run_installer(home, src)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "Quick start:" not in output
    assert "does not run" in output
    assert "ModuleNotFoundError" in output
    assert not (home / ".local" / "bin" / "lemoncrow").exists(), output


def test_installer_fails_loudly_when_bundle_installs_nothing(tmp_path: Path) -> None:
    home, src = _installer_sandbox(tmp_path, _NOOP_BUNDLE)
    link = home / ".local" / "bin" / "lemoncrow"
    link.symlink_to(home / ".lemoncrow" / "bin" / "lemoncrow")
    assert link.is_symlink() and (not link.exists())
    result = _run_installer(home, src)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "installed no lemoncrow binary" in output
    assert "Quick start:" not in output
    assert not link.is_symlink() and (not link.exists()), output


def test_local_installer_replaces_stale_frontend_tree(tmp_path: Path) -> None:
    home, src = _installer_sandbox(tmp_path, _HAPPY_BUNDLE)
    installed = home / ".lemoncrow" / "install" / "frontend"
    (installed / "assets").mkdir(parents=True)
    (installed / "index.html").write_text("<html>stale dashboard</html>\n")
    (installed / "assets" / "ReviewWorkspace-old.js").write_text("// stale\n")
    result = _run_installer(home, src)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (installed / "index.html").read_text() == (src / "frontend" / "index.html").read_text()
    assert (installed / "assets" / "ReviewReader-test.js").is_file()
    assert not (installed / "assets" / "ReviewWorkspace-old.js").exists()


def test_installer_repairs_dangling_link_and_spares_foreign_entries(tmp_path: Path) -> None:
    home, src = _installer_sandbox(tmp_path, _HAPPY_BUNDLE)
    local_bin = home / ".local" / "bin"
    (local_bin / "lemoncrow").symlink_to(home / ".lemoncrow" / "bin" / "lemoncrow")
    (local_bin / "lc").symlink_to(home / "elsewhere" / "lc")
    (local_bin / "lcd").write_text("not ours\n")
    result = _run_installer(home, src)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Quick start:" in output
    assert os.readlink(local_bin / "lemoncrow") == str(home / ".lemoncrow" / "uv-tools-bin" / "lemoncrow")
    assert os.readlink(local_bin / "lc") == str(home / "elsewhere" / "lc")
    assert (local_bin / "lcd").read_text() == "not ours\n"


def test_wheelless_distribution_fails_even_with_a_foreign_lemoncrow_on_path(tmp_path: Path) -> None:
    """A wheel-less archive must fail even when some other lemoncrow is on PATH.

    Accepting any `command -v lemoncrow` hit reopened GH #41: the foreign
    binary's directory was handed back to install.sh, which then reported that
    binary's --version as the installed one.
    """
    home, src = _installer_sandbox(tmp_path, use_real_bundle=True, foreign_cli=True)
    result = _run_installer(home, src)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "the distribution is incomplete" in output, output
    assert "Quick start:" not in output, output
    assert "0.0.0-foreign" not in output, output
    assert not (home / ".lemoncrow" / "install" / ".lemoncrow-bin-dir").exists(), output
    assert not (home / ".local" / "bin" / "lemoncrow").exists(), output


def test_wheelless_rerun_still_accepts_a_lemoncrow_owned_binary(tmp_path: Path) -> None:
    """The legitimate re-run/source-checkout case must keep working."""
    home, src = _installer_sandbox(tmp_path, use_real_bundle=True, foreign_cli=True)
    owned_bin = home / ".lemoncrow" / "bin"
    owned_bin.mkdir(parents=True)
    for name in ("lemoncrow", "lc", "lcd"):
        binary = owned_bin / name
        binary.write_text('#!/usr/bin/env bash\necho "lemoncrow 9.9.9"\n')
        binary.chmod(493)
    result = _run_installer(home, src)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Version:      9.9.9" in output, output
    assert "0.0.0-foreign" not in output, output
    recorded = (home / ".lemoncrow" / "install" / ".lemoncrow-bin-dir").read_text().strip()
    assert recorded == str(owned_bin), recorded


def test_non_tty_install_emits_no_dev_tty_noise(tmp_path: Path) -> None:
    """`: </dev/tty 2>/dev/null` leaked bash's own redirection error to stderr."""
    home, src = _installer_sandbox(tmp_path, _HAPPY_BUNDLE)
    result = _run_installer(home, src, detached=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/dev/tty" not in result.stderr, result.stderr
    assert "No such device or address" not in result.stderr, result.stderr


_RELAY_HARNESS = (
    '\nset -euo pipefail\nC_FRAME=""; C_RESET=""\nsource {common} >/dev/null 2>&1 || true\nrelay_output bash {child}\n'
)


def _run_relay(
    child: Path, home: Path, *, force_color: bool = False, verbose: bool = True
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "SHELL": "/bin/bash",
            "LEMONCROW_VERBOSE": "1" if verbose else "0",
            "LEMONCROW_INSTALL_LOG_FILE": str(home / "install.log"),
        }
    )
    if force_color:
        env["FORCE_COLOR"] = "1"
    else:
        env.pop("FORCE_COLOR", None)
        env.pop("CLICOLOR_FORCE", None)
    return subprocess.run(
        ["bash", "-c", _RELAY_HARNESS.format(common=SCRIPTS / "lib" / "common.sh", child=child)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


GREEN_DIAMOND = "\x1b[32m◇\x1b[0m"
RED_DIAMOND = "\x1b[31m◇\x1b[0m"


def test_relay_output_rails_a_child_that_has_no_rail_of_its_own(tmp_path: Path) -> None:
    """local_server.sh stands alone; its bare prints tore a hole in the box.

    Both installers box their output behind a `│  ◇  ` left rail, and a child
    process that prints straight to stdout lands at column 0 and breaks the
    column for every railed line around it.
    """
    child = tmp_path / "child.sh"
    child.write_text("#!/usr/bin/env bash\nprintf 'first\\n\\nsecond\\n'\n", encoding="utf-8")
    result = _run_relay(child, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [line for line in result.stdout.splitlines() if line] == ["│  ◇  first", "│  ◇  second"]


def test_relay_output_keeps_success_details_in_log_by_default(tmp_path: Path) -> None:
    child = tmp_path / "child.sh"
    child.write_text("#!/usr/bin/env bash\nprintf 'supervisor details\\n'\n", encoding="utf-8")
    result = _run_relay(child, tmp_path, verbose=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "supervisor details" in (tmp_path / "install.log").read_text()


def test_relay_output_still_fails_the_caller(tmp_path: Path) -> None:
    """`set -e` callers must still abort when the child fails.

    Relay must not become a `|| true`: the installers rely on the failure to stop
    before they report a successful install.
    """
    child = tmp_path / "child.sh"
    child.write_text("#!/usr/bin/env bash\nprintf 'partial\\n'\nexit 3\n", encoding="utf-8")
    result = _run_relay(child, tmp_path, verbose=False)
    assert result.returncode == 3
    assert "│  ◇  partial" in result.stdout


def test_relay_output_colors_the_diamond_by_exit_status(tmp_path: Path) -> None:
    """A partial report must read as a failure without a separate error line."""
    good = tmp_path / "good.sh"
    good.write_text("#!/usr/bin/env bash\nprintf 'all good\\n'\n", encoding="utf-8")
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/usr/bin/env bash\nprintf 'half done\\n'\nexit 3\n", encoding="utf-8")
    assert GREEN_DIAMOND + "  all good" in _run_relay(good, tmp_path, force_color=True).stdout
    assert RED_DIAMOND + "  half done" in _run_relay(bad, tmp_path, force_color=True).stdout
    assert GREEN_DIAMOND + "  half done" not in _run_relay(bad, tmp_path, force_color=True).stdout


CLAUDE_PLUGIN_NEW = INTEGRATIONS / "claude" / "plugin"


def test_local_server_closes_inherited_installer_lock_fd(tmp_path: Path) -> None:
    script = SCRIPTS / "local_server.sh"
    assert "9>&-" in script.read_text(), "local server must close the installer lock fd before daemonizing"
    if not Path("/proc/self/fd").exists():
        pytest.skip("dynamic fd inheritance check requires procfs")
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\ncase "${1:-}" in\n  -c) exit 0 ;;\n  -)\n    if [[ $# -eq 1 ]]; then printf \'lc_local_test_token\\n\'; fi\n    exit 0\n    ;;\n  -m)\n    shift 2\n    directory=""\n    port=""\n    while [[ $# -gt 0 ]]; do\n      case "$1" in\n        --directory) directory="$2"; shift 2 ;;\n        --port) port="$2"; shift 2 ;;\n        *) shift ;;\n      esac\n    done\n    mkdir -p "$directory"\n    printf \'pid=%s port=%s\n\' "$$" "$port" >"$directory/server.lock"\n    trap \'exit 0\' TERM INT\n    while :; do sleep 1; done\n    ;;\n  *) exit 2 ;;\nesac\n'
    )
    fake_python.chmod(493)
    home = tmp_path / "home"
    state = home / ".lemoncrow" / "server"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LEMONCROW_HOME": str(home / ".lemoncrow"),
            "LEMONCROW_SERVER_PYTHON": str(fake_python),
            "LEMONCROW_LOCAL_SERVER_SUPERVISOR": "none",
        }
    )
    lock = tmp_path / "install.lock"
    subprocess.run(
        ["bash", "-c", 'exec 9>"$1"; bash "$2" start', "bash", str(lock), str(script)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    pid = int((state / "server.pid").read_text().strip())
    try:
        assert not Path(f"/proc/{pid}/fd/9").exists(), "local server inherited installer fd 9"
    finally:
        subprocess.run(["bash", str(script), "stop"], env=env, check=True)


def test_hosted_build_artifact_is_canonical_review_only(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    env = os.environ.copy()
    env["LEMONCROW_REVIEW_DIST_DIR"] = str(out)
    subprocess.run(["bash", str(SCRIPTS / "build_review_client.sh")], cwd=LEMONCROW_ROOT, env=env, check=True)
    canonical = out / "lemoncrow-review-client.tar.gz"
    assert canonical.is_file()
    assert not (out / "lemoncrow-hosted-client.tar.gz").exists()
    assert not (out / "lemoncrow-hosted-client.tar.gz.sha256").exists()
    listing = subprocess.run(
        ["tar", "-tzf", str(canonical)], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    files = [line for line in listing if not line.endswith("/")]
    assert len(files) == 1
    assert re.fullmatch("vendor/lemoncrow_review_client-[^-]+-py3-none-any\\.whl", files[0])
    expected = (out / "lemoncrow-review-client.tar.gz.sha256").read_text().strip()
    actual = subprocess.run(["sha256sum", str(canonical)], capture_output=True, text=True, check=True).stdout.split()[0]
    assert expected == actual


def test_hosted_from_build_installs_only_review(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    env = os.environ.copy()
    env["LEMONCROW_REVIEW_DIST_DIR"] = str(out)
    subprocess.run(["bash", str(SCRIPTS / "build_review_client.sh")], cwd=LEMONCROW_ROOT, env=env, check=True)
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    state = home / ".lemoncrow"
    env.update({"HOME": str(home), "LEMONCROW_HOME": str(state), "LEMONCROW_BIN_DIR": str(bin_dir)})
    install_argv = [
        "bash",
        str(SCRIPTS / "hosted.sh"),
        "--from-build",
        str(out / "lemoncrow-review-client.tar.gz"),
        "--url",
        "https://api.example.test",
        "--no-hosts",
    ]
    subprocess.run(install_argv, cwd=LEMONCROW_ROOT, env=env, check=True)
    subprocess.run(install_argv, cwd=LEMONCROW_ROOT, env=env, check=True)
    version = subprocess.run([str(bin_dir / "lc"), "--version"], env=env, capture_output=True, text=True, check=True)
    assert version.stdout.startswith("lemoncrow-client ")
    assert (bin_dir / "lemoncrow-review-client").is_symlink()
    assert (bin_dir / "lc").is_symlink()
    assert (bin_dir / "lcr").is_symlink()
    assert not (bin_dir / "lemoncrow-client").exists()
    env_text = (state / "review" / "env").read_text()
    assert "LEMONCROW_URL=https://api.example.test" in env_text
    assert "LEMONCROW_INSTALL_MODE=hosted" in env_text
    assert not (state / "env").exists()
    assert not (state / "runtime").exists()
    freeze = subprocess.run(
        [str(state / "review/client/bin/python"), "-m", "pip", "list", "--format=freeze"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lemoncrow_packages = [line for line in freeze.splitlines() if line.lower().startswith("lemoncrow")]
    # The artifact is versioned with the source project, not the old 0.1.0 development pin.
    import tomllib

    with (LEMONCROW_ROOT / "client/pyproject.toml").open("rb") as metadata:
        version = tomllib.load(metadata)["project"]["version"]
    assert lemoncrow_packages == [f"lemoncrow-review-client=={version}"]
    assert not (state / "install/frontend").exists()
    assert not (state / "install/server").exists()


def test_hosted_from_build_refuses_a_mismatched_checksum(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    env = os.environ.copy()
    env["LEMONCROW_REVIEW_DIST_DIR"] = str(out)
    subprocess.run(["bash", str(SCRIPTS / "build_review_client.sh")], cwd=LEMONCROW_ROOT, env=env, check=True)
    archive = out / "lemoncrow-review-client.tar.gz"
    (out / "lemoncrow-review-client.tar.gz.sha256").write_text("0" * 64 + "\n")
    home = tmp_path / "home"
    env.update(
        {"HOME": str(home), "LEMONCROW_HOME": str(home / ".lemoncrow"), "LEMONCROW_BIN_DIR": str(tmp_path / "bin")}
    )
    result = subprocess.run(
        [
            "bash",
            str(SCRIPTS / "hosted.sh"),
            "--from-build",
            str(archive),
            "--url",
            "https://api.example.test",
            "--no-hosts",
        ],
        cwd=LEMONCROW_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "checksum mismatch; refusing Review install" in result.stderr
    assert not (home / ".lemoncrow/review/client").exists()
