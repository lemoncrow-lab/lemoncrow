#!/usr/bin/env bash
# install_claude.sh - Install LemonCrow into Claude Code
#
# What it does:
#   1. Validates the Claude plugin package at integrations/claude/plugin/.
#   2. Installs/updates lemoncrow@lemoncrow.
#   3. Global mode: registers MCP with Claude's user scope.
#   4. Workspace mode (--workspace DIR): writes project-local .mcp.json and settings.
#   5. Project enforcement (--project DIR): writes permissions.deny + allow into DIR/.claude/settings.json
#      so Claude Code hard-blocks native Read/Grep in favour of lc MCP equivalents.
#      In global mode without --project, asks interactively when running in a git repo.
# Options:
#   --dry-run        Print what would happen, touch nothing
#   --print-only     Print config snippets for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --project [DIR]  Configure per-project enforcement (default DIR: current directory)
#   --strict         Exit nonzero if 'claude' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"
PLUGIN_DIR="${LEMONCROW_REPO}/integrations/claude/plugin"
SOURCE_PLUGIN_DIR="${PLUGIN_DIR}"
INSTALL_SOURCE_DIR="${PLUGIN_DIR}"
SKILL_BUILDER="${SCRIPT_DIR}/build_host_skills.sh"
MODE_RENDERER="${SCRIPT_DIR}/sync_agent_context.py"

PLUGIN_REF="lemoncrow@lemoncrow"
DRY_RUN=false
PRINT_ONLY=false
STRICT=false
WORKSPACE=""
WORKSPACE_SET=false
PROJECT_ENFORCE=""      # path to project dir to write enforcement deny list, empty = don't
PROJECT_ENFORCE_SET=false
ROLES="code"            # comma-separated role ids to install (--roles=code,explore,...)
INCLUDE_SKILLS=""       # comma-separated public skill names to ship (--include-skills=benchmark,...)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true ;;
        --print-only) PRINT_ONLY=true ;;
        --strict)     STRICT=true ;;
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            WORKSPACE="$2"
            WORKSPACE_SET=true
            shift
            ;;
        --project)
            # Explicitly configure enforcement for a project directory.
            # DIR is optional and defaults to the current directory.
            if [ $# -ge 2 ] && [[ "$2" != -* ]]; then
                PROJECT_ENFORCE="$2"
                shift
            else
                PROJECT_ENFORCE="$PWD"
            fi
            PROJECT_ENFORCE_SET=true
            ;;
        --roles)
            if [ $# -lt 2 ]; then
                echo "Missing value for --roles" >&2
                exit 1
            fi
            ROLES="$2"
            shift
            ;;
        --include-skills)
            if [ $# -lt 2 ]; then
                echo "Missing value for --include-skills" >&2
                exit 1
            fi
            INCLUDE_SKILLS="$2"
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done
IFS=',' read -ra ROLES_ARR <<< "$ROLES"

if $WORKSPACE_SET; then
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
    INSTALL_SCOPE="workspace"
    MCP_JSON="${WORKSPACE}/.mcp.json"
    CLAUDE_SETTINGS_DIR="${WORKSPACE}/.claude"
else
    INSTALL_SCOPE="global"
    MCP_JSON=""
    CLAUDE_SETTINGS_DIR="${HOME}/.claude"
fi

CLAUDE_SETTINGS="${CLAUDE_SETTINGS_DIR}/settings.json"
CLAUDE_LOCAL_SETTINGS="${CLAUDE_SETTINGS_DIR}/settings.local.json"

info()  { [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]] && echo "[lemoncrow:claude] $*" || true; }
warn()  { echo "[lemoncrow:claude] WARN: $*" >&2; }
run()   { if $DRY_RUN; then echo "  [dry-run] $*"; else "$@"; fi; }

# --print-only must not mutate anything (no staging rm/copy, no config writes),
# so it runs before the staging section below. Snippets reference the source
# plugin package, which is a valid install source for a manual setup.
if $PRINT_ONLY; then
    echo ""
    echo "=== LemonCrow Claude Code - Install Steps ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo ""
    echo "Step 1 - Register the local LemonCrow plugin source:"
    echo "  claude plugin marketplace add '${INSTALL_SOURCE_DIR}'"
    echo ""
    echo "Step 2 - Install the plugin:"
    echo "  claude plugin install ${PLUGIN_REF}"
    echo ""
    if $WORKSPACE_SET; then
        echo "Step 3 - Ensure project-level MCP and agent rules (run once per project):"
        echo "  bash scripts/install_agents.sh --workspace '${WORKSPACE}'"
        echo ""
        echo "Step 4 - Project local Claude agents are projected into ${WORKSPACE}/.claude/agents"
    else
        echo "Step 3 - Register MCP in Claude user scope:"
        echo "  claude mcp add-json --scope user lc '{\"type\":\"stdio\",\"command\":\"lc\",\"args\":[\"mcp\",\"--host\",\"claude\"],\"alwaysLoad\":true}'"
    fi
    echo ""
    echo "After install, in Claude Code: /lemoncrow:explore"
    # With --dry-run, fall through to the traced (no-op) staging so callers
    # like install_hosts.sh can preview exactly what would be staged.
    if ! $DRY_RUN; then
        exit 0
    fi
fi

# --------------------------------------------------------------------------- #
# LemonCrow enforcement lists
#
# DENY_TOOLS: optional native-tool hard deny list. Keep this empty by default
# so Claude can ask the user for permission when the model reaches for native
# tools. Set LEMONCROW_ENFORCE_NATIVE_DENY=1 to hide/block native tools at the
# harness layer for locked-down installs.
# --------------------------------------------------------------------------- #
if [[ "${LEMONCROW_ENFORCE_NATIVE_DENY:-0}" == "1" ]]; then
    LEMONCROW_DENY_TOOLS_JSON='["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"]'
else
    LEMONCROW_DENY_TOOLS_JSON='[]'
fi
# Only tools actually registered by the MCP server (@mcp_tool in
# gateway/adapters/mcp_server.py) -- unregistered names in the allowlist are
# dead entries that mask typos.
LEMONCROW_MCP_TOOLS_JSON='["mcp__lc__codemod", "mcp__lc__code_search", "mcp__lc__compact", "mcp__lc__context", "mcp__lc__edit", "mcp__lc__grep", "mcp__lc__memory", "mcp__lc__read", "mcp__lc__rescue", "mcp__lc__search", "mcp__lc__bash", "mcp__lc__sql", "mcp__lc__trace", "mcp__lc__verify"]'
# git: read/commit subset only -- push/reset/rebase still prompt.
LEMONCROW_BASH_ALLOWS_JSON='["Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git add *)", "Bash(git commit *)", "Bash(gh *)", "Bash(uv run pytest *)", "Bash(uv run python *)", "Bash(uv run mypy *)", "Bash(uv run ruff *)", "Bash(uv run lemoncrow *)", "Bash(uv run uvicorn *)", "Bash(uv sync *)", "Bash(uv add *)", "Bash(uv pip *)", "Bash(uv lock *)", "Bash(npm run *)", "Bash(npm install *)", "Bash(npm test *)", "Bash(npx tsc *)", "Bash(make *)", "Bash(docker-compose *)", "Bash(docker compose *)"]'

# --------------------------------------------------------------------------- #
# apply_enforcement_to_settings <path>
#   Merges LemonCrow deny+allow lists into the given Claude settings.json,
#   preserving any existing entries. Idempotent.
# --------------------------------------------------------------------------- #
apply_enforcement_to_settings() {
    local settings_path="$1"
    local settings_dir
    settings_dir="$(dirname "${settings_path}")"

    if $DRY_RUN; then
        echo "  [dry-run] apply_enforcement_to_settings: merge deny+allow → ${settings_path}"
        return
    fi

    mkdir -p "${settings_dir}"
    [[ -f "${settings_path}" ]] || echo "{}" > "${settings_path}"

    DENY_JSON="${LEMONCROW_DENY_TOOLS_JSON}" \
    MCP_JSON="${LEMONCROW_MCP_TOOLS_JSON}" \
    BASH_JSON="${LEMONCROW_BASH_ALLOWS_JSON}" \
    SETTINGS_PATH="${settings_path}" \
    python3 - <<'PYEOF'
import json
import os
from pathlib import Path

DENY_TOOLS = json.loads(os.environ["DENY_JSON"])
LEMONCROW_MCP_TOOLS = json.loads(os.environ["MCP_JSON"])
BASH_ALLOWS = json.loads(os.environ["BASH_JSON"])

path = Path(os.environ["SETTINGS_PATH"])
data = json.loads(path.read_text(encoding="utf-8") or "{}")

perms = data.setdefault("permissions", {})
deny = perms.setdefault("deny", [])
added_deny = []
for t in DENY_TOOLS:
    if t not in deny:
        deny.append(t)
        added_deny.append(t)

allow = perms.setdefault("allow", [])
added_allow = []
for t in LEMONCROW_MCP_TOOLS + BASH_ALLOWS:
    if t not in allow:
        allow.append(t)
        added_allow.append(t)

path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(
    f"[lemoncrow:claude] enforcement merged → {path} "
    f"(deny +{len(added_deny)}, allow +{len(added_allow)})"
)
PYEOF
}

configure_project_enforcement() {
    # Backwards-compatible alias for callers that pass a project DIR; resolves
    # to <DIR>/.claude/settings.json and forwards to the generic merger.
    local dir="$1"
    apply_enforcement_to_settings "${dir}/.claude/settings.json"
    }


uv run python "$MODE_RENDERER" >/dev/null 2>&1 || python3 "$MODE_RENDERER" >/dev/null 2>&1 || true
STAGING_DIR="${HOME}/.lemoncrow/claude-plugin"
# Start fresh — stale symlinks from prior installs (hooks → source dir)
# will cause `cp -r` to error with "same file".
run rm -rf "$STAGING_DIR"
run mkdir -p "$STAGING_DIR/.claude-plugin"
run cp "${SOURCE_PLUGIN_DIR}/.claude-plugin/plugin.json" "$STAGING_DIR/.claude-plugin/"
run cp "${SOURCE_PLUGIN_DIR}/.claude-plugin/marketplace.json" "$STAGING_DIR/.claude-plugin/"
if ! $DRY_RUN; then
    LEMONCROW_VERSION="$(lemoncrow_resolve_version "$LEMONCROW_REPO")"
    PLUGIN_MANIFEST="${STAGING_DIR}/.claude-plugin/plugin.json" LEMONCROW_VERSION="$LEMONCROW_VERSION" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

manifest = Path(os.environ["PLUGIN_MANIFEST"])
data = json.loads(manifest.read_text(encoding="utf-8"))
data["version"] = os.environ["LEMONCROW_VERSION"]
manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
else
    echo "  [dry-run] stamp ${STAGING_DIR}/.claude-plugin/plugin.json with LemonCrow version"
fi
run mkdir -p "$STAGING_DIR/agents"
info "Staging Claude plugin"
for agent in "${ROLES_ARR[@]}"; do
    lemoncrow_write_managed_copy "${SOURCE_PLUGIN_DIR}/agents/${agent}.md" "$STAGING_DIR/agents/${agent}.md" "$DRY_RUN"
done
run cp -r "${SOURCE_PLUGIN_DIR}/hooks" "$STAGING_DIR/"
run cp -r "${SOURCE_PLUGIN_DIR}/scripts" "$STAGING_DIR/"
run cp -r "${SOURCE_PLUGIN_DIR}/workflows" "$STAGING_DIR/"
SKILL_BUILDER_ARGS=(--host claude --dest "$STAGING_DIR/skills")
if [[ -n "$INCLUDE_SKILLS" ]]; then
    SKILL_BUILDER_ARGS+=("--include-skills=${INCLUDE_SKILLS}")
fi
run bash "$SKILL_BUILDER" "${SKILL_BUILDER_ARGS[@]}"
run cp "${SOURCE_PLUGIN_DIR}/settings.json" "$STAGING_DIR/"
# .mcp.json is deliberately NOT staged into the plugin (neither mode). Any
# server Claude Code discovers inside an installed plugin package gets
# namespaced plugin:<plugin-name>:<server-key> -> mcp__plugin_lemoncrow_lc__*,
# doubling tool-name length/token cost. MCP registration happens below via a
# plain, non-plugin-owned path instead (project-root .mcp.json for workspace
# installs, `claude mcp add --scope user` for global), which Claude Code
# registers under the short "lc" name.
lemoncrow_apply_reply_register_level "$STAGING_DIR" "$DRY_RUN"
# Ensure runnable bits on hook + script entrypoints, even if source perms got
# stripped (e.g. via `git stash`, fresh clone on some FS, or restore from tar).
# Claude Code invokes statusline.sh via the `command` type and exec()s the
# path directly; without +x the statusline silently disappears.
run chmod +x "$STAGING_DIR/scripts/"*.sh 2>/dev/null || true
run chmod +x "$STAGING_DIR/hooks/"*.sh "$STAGING_DIR/hooks/"*.py 2>/dev/null || true
PLUGIN_DIR="$STAGING_DIR"
INSTALL_SOURCE_DIR="$STAGING_DIR"

# Write the Python interpreter path so _run_hook.sh can find lc in all
# install modes (binary, dev-venv, uv-tool, pip).  Probe in preference order:
#   1. uv run from the repo (dev / local checkout)
#   2. uv tool venv python (uv tool install lemoncrow)
#   3. system python3/python (pip install lemoncrow)
# Binary-mode installs have no importable Python; the file is left absent and
# hooks degrade gracefully via their try/except guards.
if ! $DRY_RUN; then
    _LEMONCROW_PY=""
    # 1. dev / local checkout
    if [[ -z "${_LEMONCROW_PY}" ]] && command -v uv >/dev/null 2>&1; then
        _LEMONCROW_PY="$(cd "${LEMONCROW_REPO}" && uv run python -c "import sys; print(sys.executable)" 2>/dev/null || true)"
        [[ -n "${_LEMONCROW_PY}" ]] && "${_LEMONCROW_PY}" -c "import lemoncrow" 2>/dev/null || _LEMONCROW_PY=""
    fi
    # 2. uv tool venv
    if [[ -z "${_LEMONCROW_PY}" ]]; then
        for _py in "${HOME}/.local/share/uv/tools/lemoncrow/bin/python" "${HOME}/.local/share/uv/tools/lemoncrow/bin/python3"; do
            if [[ -x "${_py}" ]] && "${_py}" -c "import lemoncrow" 2>/dev/null; then
                _LEMONCROW_PY="${_py}"; break
            fi
        done
    fi
    # 3. system python (pip install)
    if [[ -z "${_LEMONCROW_PY}" ]]; then
        for _py in python3 python; do
            if command -v "${_py}" >/dev/null 2>&1 && "$(command -v "${_py}")" -c "import lemoncrow" 2>/dev/null; then
                _LEMONCROW_PY="$(command -v "${_py}")"; break
            fi
        done
    fi
    if [[ -n "${_LEMONCROW_PY}" && -x "${_LEMONCROW_PY}" ]]; then
        echo "${_LEMONCROW_PY}" > "${STAGING_DIR}/lemoncrow-python"
        info "Stored lemoncrow python: ${_LEMONCROW_PY}"
    fi
fi

if ! command -v claude &>/dev/null; then
    if $STRICT; then
        echo "[lemoncrow:claude] ERROR: 'claude' CLI not found on PATH. Install from https://claude.ai/download" >&2
        exit 1
    fi
    warn "'claude' CLI not found on PATH - SKIPPING Claude install."
    warn "Install Claude Code, then run: make install-claude"
    echo "=== SKIPPED (claude CLI absent) ==="
    exit 0
fi

CLAUDE_VERSION="$(claude --version 2>/dev/null || echo 'unknown')"
info "Found Claude Code: $CLAUDE_VERSION"

# ---- structural validation --------------------------------------------------
# Always validate the original source plugin dir, not the generated staging copy.
info "Running structural validation on plugin package at ${SOURCE_PLUGIN_DIR}"

STRUCT_FAIL=0
struct_pass() { info "PASS: $*"; }
struct_fail() { echo "[lemoncrow:claude] FAIL: $*" >&2; STRUCT_FAIL=1; }

if [ -d "${SOURCE_PLUGIN_DIR}" ]; then
    struct_pass "plugin directory exists: integrations/claude/plugin/"
    else
    struct_fail "plugin directory missing: ${SOURCE_PLUGIN_DIR}"
    fi

    PLUGIN_JSON="${SOURCE_PLUGIN_DIR}/.claude-plugin/plugin.json"
if [ -f "${PLUGIN_JSON}" ]; then
    NAME=$(python3 -c "import json; d=json.load(open('${PLUGIN_JSON}')); print(d.get('name',''))" 2>/dev/null || echo "")
    if [ "$NAME" = "lemoncrow" ]; then
        struct_pass "plugin.json valid (name=lemoncrow)"
    else
        struct_fail "plugin.json name unexpected: '${NAME}'"
    fi
else
    struct_fail "plugin.json missing: ${PLUGIN_JSON}"
fi

if [ -f "${PLUGIN_JSON}" ]; then
    HAS_FORBIDDEN=$(python3 -c "import json; d=json.load(open('${PLUGIN_JSON}')); bad=[k for k in ('agents','skills','hooks','mcp') if k in d]; print(','.join(bad) if bad else 'none')" 2>/dev/null || echo "error")
    AUTHOR_TYPE=$(python3 -c "import json; d=json.load(open('${PLUGIN_JSON}')); print(type(d.get('author')).__name__)" 2>/dev/null || echo "error")
    if [ "$HAS_FORBIDDEN" = "none" ]; then
        struct_pass "plugin.json has no forbidden keys"
    else
        struct_fail "plugin.json declares '${HAS_FORBIDDEN}' - remove these; they cause install validation errors"
    fi
    if [ "$AUTHOR_TYPE" = "dict" ]; then
        struct_pass "plugin.json author is an object"
    else
        struct_fail "plugin.json author must be an object, got type: ${AUTHOR_TYPE}"
    fi
fi

for agent in "${ROLES_ARR[@]}"; do
    AGENT_FILE="${SOURCE_PLUGIN_DIR}/agents/${agent}.md"
    if [ -f "${AGENT_FILE}" ]; then
        struct_pass "agent exists: agents/${agent}.md"
    else
        struct_fail "agent missing: ${AGENT_FILE}"
    fi
done

HOOKS_JSON="${SOURCE_PLUGIN_DIR}/hooks/hooks.json"
if [ -f "${HOOKS_JSON}" ]; then
    struct_pass "hooks/hooks.json exists"
else
    struct_fail "hooks/hooks.json missing: ${HOOKS_JSON}"
fi

# The plugin must NOT bundle its own .mcp.json. A server bundled inside an
# installed plugin gets namespaced plugin:lemoncrow:lc by Claude Code
# (mcp__plugin_lemoncrow_lc__* tool names) IN ADDITION TO the "lc" server
# registered directly below (claude mcp add / a project-root .mcp.json),
# doubling every tool under two names and twice the token cost.
PLUGIN_MCP_JSON="${SOURCE_PLUGIN_DIR}/.mcp.json"
if [ -f "${PLUGIN_MCP_JSON}" ]; then
    struct_fail ".mcp.json must not exist: ${PLUGIN_MCP_JSON} -- bundling it reintroduces the plugin:lemoncrow:lc duplicate-namespace regression; MCP registers via a non-plugin-owned path instead (see \"MCP config\" below)"
else
    struct_pass "plugin does not bundle its own .mcp.json (avoids plugin:lemoncrow:lc duplicate namespace)"
fi

if [ "$STRUCT_FAIL" -ne 0 ]; then
    echo "[lemoncrow:claude] ERROR: Structural validation failed. Fix the above issues before installing." >&2
    exit 1
fi
info "Structural validation passed"

# ---- plugin install ---------------------------------------------------------
if $DRY_RUN; then
    echo "  [dry-run] claude plugin validate ${PLUGIN_DIR}"
    echo "  [dry-run] claude plugin marketplace add '${INSTALL_SOURCE_DIR}'"
    echo "  [dry-run] claude plugin install ${PLUGIN_REF}"
else
    info "Validating plugin package with Claude CLI at ${PLUGIN_DIR}"
    if ! claude plugin validate "${PLUGIN_DIR}" 2>&1 | grep -q "Validation passed"; then
        echo "[lemoncrow:claude] ERROR: Plugin validation failed. Run: claude plugin validate ${PLUGIN_DIR}" >&2
        exit 1
    fi
    info "Plugin package valid (Claude CLI)"

    info "Registering local Claude plugin source at ${INSTALL_SOURCE_DIR}"
    INSTALL_SOURCE_OUT="$(claude plugin marketplace add "${INSTALL_SOURCE_DIR}" 2>&1 || true)"
    if echo "$INSTALL_SOURCE_OUT" | grep -q "already on disk"; then
        info "Claude plugin source 'lemoncrow' already registered"
    elif echo "$INSTALL_SOURCE_OUT" | grep -q "Successfully added"; then
        info "Claude plugin source 'lemoncrow' registered"
    else
        echo "[lemoncrow:claude] ERROR: plugin source add failed: $INSTALL_SOURCE_OUT" >&2
        exit 1
    fi

    info "Installing/updating plugin ${PLUGIN_REF}"
    claude plugin uninstall "${PLUGIN_REF}" 2>/dev/null || true
    INSTALL_OUT="$(claude plugin install "${PLUGIN_REF}" 2>&1 || true)"
    if echo "$INSTALL_OUT" | grep -qiE "Successfully installed|Installed"; then
        info "Plugin ${PLUGIN_REF} installed"
    else
        echo "[lemoncrow:claude] ERROR: plugin install failed: $INSTALL_OUT" >&2
        exit 1
    fi
fi

# ---- MCP config -------------------------------------------------------------
# Global mode registers "lc" directly in Claude's user scope. Workspace mode
# has no such step, so write a plain project-root .mcp.json instead (merged,
# so any pre-existing unrelated servers survive). Both set alwaysLoad only on
# lc, making Claude wait for its schemas before turn 1 while preserving the
# short mcp__lc__* namespace; the plugin remains MCP-free.
if $WORKSPACE_SET; then
    if $DRY_RUN; then
        echo "  [dry-run] merge lc MCP server into ${MCP_JSON}"
    else
        run mkdir -p "$(dirname "${MCP_JSON}")"
        [[ -f "${MCP_JSON}" ]] || echo "{}" > "${MCP_JSON}"
        LEMONCROW_MCP_JSON_PATH="${MCP_JSON}" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

path = Path(os.environ["LEMONCROW_MCP_JSON_PATH"])
data = json.loads(path.read_text(encoding="utf-8") or "{}")
data.setdefault("mcpServers", {})["lc"] = {
    "type": "stdio",
    "command": "lemoncrow",
    "args": ["mcp", "--host", "claude"],
    "alwaysLoad": True,
}
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"[lemoncrow:claude] lc MCP server merged → {path}")
PYEOF
    fi
else
    if $DRY_RUN; then
        echo "  [dry-run] claude mcp add-json --scope user lc '{\"type\":\"stdio\",\"command\":\"lc\",\"args\":[\"mcp\",\"--host\",\"claude\"],\"alwaysLoad\":true}'"
    else
        info "Registering always-loaded lc MCP server in Claude user scope"
        claude mcp remove --scope user lc 2>/dev/null || true
        claude mcp add-json --scope user lc '{"type":"stdio","command":"lc","args":["mcp","--host","claude"],"alwaysLoad":true}'
    fi
fi

# ---- workspace-local Claude env --------------------------------------------
if $WORKSPACE_SET; then
    run mkdir -p "$CLAUDE_SETTINGS_DIR"
    if $DRY_RUN; then
        echo "  [dry-run] merge CLAUDE_WORKSPACE_ROOT into ${CLAUDE_LOCAL_SETTINGS}"
    else
        if [ ! -f "${CLAUDE_LOCAL_SETTINGS}" ]; then
            info "Creating ${CLAUDE_LOCAL_SETTINGS} with env.CLAUDE_WORKSPACE_ROOT"
            echo "{}" > "${CLAUDE_LOCAL_SETTINGS}"
        fi
        LEMONCROW_CLAUDE_LOCAL_SETTINGS="${CLAUDE_LOCAL_SETTINGS}" LEMONCROW_WORKSPACE="${WORKSPACE}" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

path = Path(os.environ['LEMONCROW_CLAUDE_LOCAL_SETTINGS'])
data = json.loads(path.read_text(encoding='utf-8') or '{}')
data.setdefault('env', {})['CLAUDE_WORKSPACE_ROOT'] = os.environ['LEMONCROW_WORKSPACE']
path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
print(f"[lemoncrow:claude] CLAUDE_WORKSPACE_ROOT written to {path}")
PYEOF
    fi

    if $DRY_RUN; then
        echo "  [dry-run] project workspace-local Claude agents into ${WORKSPACE}/.claude/agents"
    else
        # Use an interpreter that can import lemoncrow (pydantic et al). The bare
        # system python3 usually can't; _LEMONCROW_PY was resolved above to the
        # dev-venv / uv-tool / pip interpreter. Projection is best-effort — a
        # failure must NOT abort the remaining workspace setup (hooks,
        # statusLine, enforcement), so swallow non-zero exits with a warning.
        PYTHONPATH="${LEMONCROW_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "${_LEMONCROW_PY:-python3}" - <<PYEOF || warn "workspace-local Claude agent projection failed (non-fatal); skipping"
from pathlib import Path
from lemoncrow.core.capabilities.workspace_host_overrides import write_workspace_claude_overrides

written = write_workspace_claude_overrides(Path("${WORKSPACE}"), repo_root=Path("${LEMONCROW_REPO}"), role_ids=tuple(r for r in "${ROLES}".split(",") if r))
print(f"[lemoncrow:claude] projected {len(written)} workspace-local Claude files into ${WORKSPACE}/.claude")
PYEOF
    fi
fi


# ---- permissions: allow LemonCrow MCP (and optionally deny native tools) ------
# By default this preserves Claude's normal permission prompt for native tools.
# Set LEMONCROW_ENFORCE_NATIVE_DENY=1 for locked-down installs.
apply_enforcement_to_settings "${CLAUDE_SETTINGS}"

# ---- statusLine setting in ~/.claude/settings.json -------------------------
# LEMONCROW_STATUSLINE_COMPACT=1 installs the compact layout (model · ctx % ·
# cost · savings · background tasks) instead of the full token breakdown.
STATUSLINE_SCRIPT="${INSTALL_SOURCE_DIR}/scripts/statusline.sh"
STATUSLINE_CMD="${STATUSLINE_SCRIPT}"
if [[ -n "${LEMONCROW_STATUSLINE_COMPACT:-}" ]]; then
    STATUSLINE_CMD="LEMONCROW_STATUS_COMPACT=1 ${STATUSLINE_SCRIPT}"
fi
if $DRY_RUN; then
    echo "  [dry-run] set statusLine in ${CLAUDE_SETTINGS} → ${STATUSLINE_SCRIPT}"
elif [ -f "${STATUSLINE_SCRIPT}" ]; then
    python3 - <<PYEOF2
import json
import shutil
import time
from pathlib import Path

path = Path("${CLAUDE_SETTINGS}")
if not path.exists():
    path.write_text("{}\n")
data = json.loads(path.read_text(encoding="utf-8") or "{}")

def lemoncrow_owned(value):
    """True when the setting is unset or was written by an LemonCrow install."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.startswith("lemoncrow:")
    if isinstance(value, dict):
        return "lc" in str(value.get("command", ""))
    return False

desired = {
    "statusLine": {"type": "command", "command": "${STATUSLINE_CMD}", "padding": 1},
    "subagentStatusLine": {"type": "command", "command": "${STATUSLINE_SCRIPT}", "padding": 1},
    "agent": "lemoncrow:code",
}
skipped = [k for k in desired if not lemoncrow_owned(data.get(k))]
changed = {k: v for k, v in desired.items() if k not in skipped and data.get(k) != v}
if changed:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + ".bak." + stamp)
    shutil.copy2(path, backup)
    print(f"[lemoncrow:claude] backed up settings → {backup}")
    data.update(changed)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
for key in desired:
    if key not in skipped:
        print(f"[lemoncrow:claude] {key} set → " + (desired[key] if isinstance(desired[key], str) else desired[key]["command"]))
if skipped:
    print("[lemoncrow:claude] NOTICE: kept your existing settings for: " + ", ".join(skipped))
    print("[lemoncrow:claude] To switch them to LemonCrow, merge this into ${CLAUDE_SETTINGS}:")
    print(json.dumps({k: desired[k] for k in skipped}, indent=2))
PYEOF2
else
    warn "statusline.sh not found at ${STATUSLINE_SCRIPT} — skipping statusLine"
fi

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- per-project enforcement -----------------------------------------------
# Only apply enforcement if explicitly requested via --project.
if [[ -n "${PROJECT_ENFORCE:-}" ]]; then
    if [[ -d "$PROJECT_ENFORCE" ]]; then
        PROJECT_ENFORCE="$(cd "$PROJECT_ENFORCE" && pwd)"
    fi
    info "Configuring enforcement for project: ${PROJECT_ENFORCE}"
    configure_project_enforcement "${PROJECT_ENFORCE}"
    lemoncrow_install_attribution_hook "$PROJECT_ENFORCE" "$DRY_RUN"
elif $WORKSPACE_SET; then
    lemoncrow_install_attribution_hook "$WORKSPACE" "$DRY_RUN"
fi

info "Done. Start Claude Code in your workspace. The lemoncrow:code agent is available."
info "  Agent: lemoncrow:code (other roles are installable on demand)"
info "  Project enforcement: bash scripts/install_claude.sh --project [DIR]"
