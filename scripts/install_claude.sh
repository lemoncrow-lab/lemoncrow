#!/usr/bin/env bash
# install_claude.sh - Install Atelier into Claude Code
#
# What it does:
#   1. Validates the Claude plugin package at integrations/claude/plugin/.
#   2. Installs/updates atelier@atelier.
#   3. Global mode: registers MCP with Claude's user scope.
#   4. Workspace mode (--workspace DIR): writes project-local .mcp.json and settings.
#   5. Project enforcement (--project DIR): writes permissions.deny + allow into DIR/.claude/settings.json
#      so Claude Code hard-blocks native Read/Grep in favour of atelier MCP equivalents.
#      In global mode without --project, asks interactively when running in a git repo.
# Options:
#   --dry-run        Print what would happen, touch nothing
#   --print-only     Print config snippets for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --project [DIR]  Configure per-project enforcement (default DIR: current directory)
#   --strict         Exit nonzero if 'claude' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"
PLUGIN_DIR="${ATELIER_REPO}/integrations/claude/plugin"
SOURCE_PLUGIN_DIR="${PLUGIN_DIR}"
INSTALL_SOURCE_DIR="${PLUGIN_DIR}"
SKILL_BUILDER="${SCRIPT_DIR}/build_host_skills.sh"
MODE_RENDERER="${SCRIPT_DIR}/render_mode_surfaces.py"

PLUGIN_REF="atelier@atelier"
DRY_RUN=false
PRINT_ONLY=false
STRICT=false
WORKSPACE=""
WORKSPACE_SET=false
PROJECT_ENFORCE=""      # path to project dir to write enforcement deny list, empty = don't
PROJECT_ENFORCE_SET=false

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
            if [ $# -lt 2 ]; then
                echo "Missing value for --project" >&2
                exit 1
            fi
            PROJECT_ENFORCE="$2"
            PROJECT_ENFORCE_SET=true
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

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

info()  { [[ "${ATELIER_VERBOSE:-0}" == "1" ]] && echo "[atelier:claude] $*" || true; }
warn()  { echo "[atelier:claude] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

# --------------------------------------------------------------------------- #
# Atelier enforcement lists
#
# DENY_TOOLS: native tools that Atelier provides a replacement for. Listing
# them in permissions.deny removes them from the model's effective toolbelt
# (Claude Code blocks invocation at the harness layer). Bash is denied too
# because that's the biggest "leak" path (subshell grep/cat/find); the
# scoped Bash(*) patterns in ATELIER_BASH_ALLOWS_JSON keep legitimate
# git / gh / uv / make / npm usage working.
# --------------------------------------------------------------------------- #
ATELIER_DENY_TOOLS_JSON='["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"]'
ATELIER_MCP_TOOLS_JSON='["mcp__atelier__code", "mcp__atelier__compact", "mcp__atelier__context", "mcp__atelier__edit", "mcp__atelier__grep", "mcp__atelier__memory", "mcp__atelier__read", "mcp__atelier__rescue", "mcp__atelier__route", "mcp__atelier__search", "mcp__atelier__shell", "mcp__atelier__sql", "mcp__atelier__trace", "mcp__atelier__verify"]'
ATELIER_BASH_ALLOWS_JSON='["Bash(git *)", "Bash(gh *)", "Bash(uv run pytest *)", "Bash(uv run python *)", "Bash(uv run mypy *)", "Bash(uv run ruff *)", "Bash(uv run atelier *)", "Bash(uv run uvicorn *)", "Bash(uv sync *)", "Bash(uv add *)", "Bash(uv pip *)", "Bash(uv lock *)", "Bash(npm run *)", "Bash(npm install *)", "Bash(npm test *)", "Bash(npx tsc *)", "Bash(make *)", "Bash(docker-compose *)", "Bash(docker compose *)"]'

# --------------------------------------------------------------------------- #
# apply_enforcement_to_settings <path>
#   Merges Atelier deny+allow lists into the given Claude settings.json,
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

    DENY_JSON="${ATELIER_DENY_TOOLS_JSON}" \
    MCP_JSON="${ATELIER_MCP_TOOLS_JSON}" \
    BASH_JSON="${ATELIER_BASH_ALLOWS_JSON}" \
    SETTINGS_PATH="${settings_path}" \
    python3 - <<'PYEOF'
import json
import os
from pathlib import Path

DENY_TOOLS = json.loads(os.environ["DENY_JSON"])
ATELIER_MCP_TOOLS = json.loads(os.environ["MCP_JSON"])
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
for t in ATELIER_MCP_TOOLS + BASH_ALLOWS:
    if t not in allow:
        allow.append(t)
        added_allow.append(t)

path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(
    f"[atelier:claude] enforcement merged → {path} "
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


# ---- resolve install profile ------------------------------------------------
atelier_resolve_install_profile "atelier:claude"
if [[ -n "${ATELIER_INSTALL_PROFILE_WARNING:-}" ]]; then
    warn "$ATELIER_INSTALL_PROFILE_WARNING"
fi
python3 "$MODE_RENDERER" >/dev/null
STAGING_DIR="${HOME}/.atelier/claude-plugin-${INSTALL_PROFILE}"
# Start fresh — stale symlinks from prior installs (hooks → source dir)
# will cause `cp -r` to error with "same file".
run "rm -rf '$STAGING_DIR'"
run "mkdir -p '$STAGING_DIR/.claude-plugin'"
run "cp '${SOURCE_PLUGIN_DIR}/.claude-plugin/plugin.json' '$STAGING_DIR/.claude-plugin/'"
run "cp '${SOURCE_PLUGIN_DIR}/.claude-plugin/marketplace.json' '$STAGING_DIR/.claude-plugin/'"
run "mkdir -p '$STAGING_DIR/agents'"
if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    info "Install profile: dev; staging full plugin with task loop"
    for agent in code explore review repair research; do
        atelier_write_managed_copy "${SOURCE_PLUGIN_DIR}/agents/${agent}.dev.md" "$STAGING_DIR/agents/${agent}.md" "$DRY_RUN"
    done
else
    info "Install profile: stable; staging stable plugin without dev-only task loop"
    for agent in code explore review repair research; do
        atelier_write_managed_copy "${SOURCE_PLUGIN_DIR}/agents/${agent}.md" "$STAGING_DIR/agents/${agent}.md" "$DRY_RUN"
    done
fi
run "cp -r '${SOURCE_PLUGIN_DIR}/hooks' '$STAGING_DIR/'"
run "cp -r '${SOURCE_PLUGIN_DIR}/scripts' '$STAGING_DIR/'"
if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    run "bash '$SKILL_BUILDER' --host claude --dest '$STAGING_DIR/skills' --include-dev"
else
    run "bash '$SKILL_BUILDER' --host claude --dest '$STAGING_DIR/skills'"
fi
run "cp '${SOURCE_PLUGIN_DIR}/settings.json' '$STAGING_DIR/'"
run "cp '${SOURCE_PLUGIN_DIR}/.mcp.json' '$STAGING_DIR/'"
# Ensure runnable bits on hook + script entrypoints, even if source perms got
# stripped (e.g. via `git stash`, fresh clone on some FS, or restore from tar).
# Claude Code invokes statusline.sh via the `command` type and exec()s the
# path directly; without +x the statusline silently disappears.
run "chmod +x '$STAGING_DIR/scripts/'*.sh 2>/dev/null || true"
run "chmod +x '$STAGING_DIR/hooks/'*.sh '$STAGING_DIR/hooks/'*.py 2>/dev/null || true"
PLUGIN_DIR="$STAGING_DIR"
INSTALL_SOURCE_DIR="$STAGING_DIR"
if $PRINT_ONLY; then
    echo ""
    echo "=== Atelier Claude Code - Install Steps ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo ""
    echo "Step 1 - Register the local Atelier plugin source:"
    echo "  claude plugin marketplace add '${INSTALL_SOURCE_DIR}'"
    echo ""
    echo "Step 2 - Install the plugin:"
    echo "  claude plugin install ${PLUGIN_REF}"
    echo ""
    if $WORKSPACE_SET; then
        echo "Step 3 - Ensure project-level MCP and agent rules (run once per project):"
        echo "  bash scripts/install_agents.sh --workspace '${WORKSPACE}'"
        echo ""
        echo "Step 4 - Optional project setting:"
        echo "  set env.CLAUDE_WORKSPACE_ROOT=${WORKSPACE} in ${CLAUDE_LOCAL_SETTINGS}"
    else
        echo "Step 3 - Register MCP in Claude user scope:"
        echo "  claude mcp add --scope user atelier -- atelier-mcp --host claude"
    fi
    echo ""
    echo "After install, in Claude Code: /atelier:explore"
    exit 0
fi

if ! command -v claude &>/dev/null; then
    if $STRICT; then
        echo "[atelier:claude] ERROR: 'claude' CLI not found on PATH. Install from https://claude.ai/download" >&2
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
struct_fail() { echo "[atelier:claude] FAIL: $*" >&2; STRUCT_FAIL=1; }

if [ -d "${SOURCE_PLUGIN_DIR}" ]; then
    struct_pass "plugin directory exists: integrations/claude/plugin/"
    else
    struct_fail "plugin directory missing: ${SOURCE_PLUGIN_DIR}"
    fi

    PLUGIN_JSON="${SOURCE_PLUGIN_DIR}/.claude-plugin/plugin.json"
if [ -f "${PLUGIN_JSON}" ]; then
    NAME=$(python3 -c "import json; d=json.load(open('${PLUGIN_JSON}')); print(d.get('name',''))" 2>/dev/null || echo "")
    if [ "$NAME" = "atelier" ]; then
        struct_pass "plugin.json valid (name=atelier)"
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

for agent in code explore review repair research; do
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

PLUGIN_MCP_JSON="${SOURCE_PLUGIN_DIR}/.mcp.json"
if [ -f "${PLUGIN_MCP_JSON}" ]; then
    if grep -q 'CLAUDE_PLUGIN_ROOT' "${PLUGIN_MCP_JSON}"; then
        struct_pass ".mcp.json uses \${CLAUDE_PLUGIN_ROOT}"
    else
        struct_fail ".mcp.json does not use \${CLAUDE_PLUGIN_ROOT} - absolute paths will break marketplace install"
    fi
else
    struct_fail ".mcp.json missing: ${PLUGIN_MCP_JSON}"
fi

if [ "$STRUCT_FAIL" -ne 0 ]; then
    echo "[atelier:claude] ERROR: Structural validation failed. Fix the above issues before installing." >&2
    exit 1
fi
info "Structural validation passed"

# ---- refresh atelier-mcp from local source ---------------------------------
# The MCP server runs from `uv tool install`'s isolated site-packages, NOT
# from a live source link. Without this step, any change you make under src/
# (e.g. capability fixes that affect savings emission) won't reach Claude
# until you re-run install.sh. Reinstall here so install_claude.sh is the
# single command that keeps plugin assets AND the MCP runtime in sync.
refresh_atelier_tool() {
    if ! command -v uv >/dev/null 2>&1; then
        warn "uv not on PATH — skipping atelier-mcp refresh"
        return 0
    fi
    local extras="mcp,memory,smart,cloud,repo-map,api,postgres,vector,parsers,rename,telemetry"
    local pkg_spec="${ATELIER_REPO}[${extras}]"
    local bin_dir="${ATELIER_BIN_DIR:-${HOME}/.local/bin}"
    local tool_dir="${ATELIER_TOOL_DIR:-${HOME}/.local/share/uv/tools}"

    if $DRY_RUN; then
        echo "  [dry-run] uv tool install --reinstall ${pkg_spec}"
        echo "  [dry-run] rebuild ${bin_dir}/atelier-mcp wrapper (exports ATELIER_DEV_MODE)"
        return 0
    fi

    info "Refreshing atelier-mcp from ${ATELIER_REPO}"
    UV_TOOL_BIN_DIR="$bin_dir" UV_TOOL_DIR="$tool_dir" \
        uv tool install --reinstall "$pkg_spec" >/dev/null 2>&1 || {
            warn "uv tool install --reinstall failed; MCP may run stale code"
            return 0
        }

    # uv replaces atelier-mcp with the raw Python entry point. Restore the
    # bash wrapper that exports ATELIER_DEV_MODE so the MCP server's
    # dev-mode flag stays the default-off it shipped with.
    local mcp_path="${bin_dir}/atelier-mcp"
    local wrapped_path="${bin_dir}/atelier-mcp.real"
    local real_target="${tool_dir}/atelier/bin/atelier-mcp"
    if [[ -e "$mcp_path" || -L "$mcp_path" ]]; then
        rm -f "$wrapped_path" "$mcp_path"
        ln -s "$real_target" "$wrapped_path"
        cat > "$mcp_path" <<EOF
#!/usr/bin/env bash
export ATELIER_DEV_MODE="\${ATELIER_DEV_MODE:-0}"
exec "$wrapped_path" "\$@"
EOF
        chmod +x "$mcp_path"
        info "atelier-mcp wrapper restored"
    fi
}

refresh_atelier_tool

# ---- plugin install ---------------------------------------------------------
if $DRY_RUN; then
    echo "  [dry-run] claude plugin validate ${PLUGIN_DIR}"
    echo "  [dry-run] claude plugin marketplace add '${INSTALL_SOURCE_DIR}'"
    echo "  [dry-run] reinstall ${PLUGIN_REF}"
else
    info "Validating plugin package with Claude CLI at ${PLUGIN_DIR}"
    if ! claude plugin validate "${PLUGIN_DIR}" 2>&1 | grep -q "Validation passed"; then
        echo "[atelier:claude] ERROR: Plugin validation failed. Run: claude plugin validate ${PLUGIN_DIR}" >&2
        exit 1
    fi
    info "Plugin package valid (Claude CLI)"

    info "Registering local Claude plugin source at ${INSTALL_SOURCE_DIR}"
    INSTALL_SOURCE_OUT="$(claude plugin marketplace add "${INSTALL_SOURCE_DIR}" 2>&1 || true)"
    if echo "$INSTALL_SOURCE_OUT" | grep -q "already on disk"; then
        info "Claude plugin source 'atelier' already registered"
    elif echo "$INSTALL_SOURCE_OUT" | grep -q "Successfully added"; then
        info "Claude plugin source 'atelier' registered"
    else
        echo "[atelier:claude] ERROR: plugin source add failed: $INSTALL_SOURCE_OUT" >&2
        exit 1
    fi

    info "Installing/updating plugin ${PLUGIN_REF}"
    claude plugin uninstall "${PLUGIN_REF}" 2>/dev/null || true
    INSTALL_OUT="$(claude plugin install "${PLUGIN_REF}" 2>&1 || true)"
    if echo "$INSTALL_OUT" | grep -qiE "Successfully installed|Installed"; then
        info "Plugin ${PLUGIN_REF} installed"
    else
        echo "[atelier:claude] ERROR: plugin install failed: $INSTALL_OUT" >&2
        exit 1
    fi
fi

# ---- MCP config -------------------------------------------------------------
# NOTE: Project-level .mcp.json is handled by scripts/install_agents.sh.
# This installer only deals with Claude-specific global/user MCP and settings.
if $WORKSPACE_SET; then
    info "Project-level .mcp.json is managed by scripts/install_agents.sh — skipping"
    info "  Run: scripts/install_agents.sh --workspace '${WORKSPACE}'"
else
    if $DRY_RUN; then
        echo "  [dry-run] claude mcp add --scope user atelier -- atelier-mcp --host claude"
    else
        info "Registering atelier MCP server in Claude user scope"
        claude mcp remove --scope user atelier 2>/dev/null || true
        claude mcp add --scope user atelier -- atelier-mcp --host claude
    fi
fi

# ---- workspace-local Claude env --------------------------------------------
if $WORKSPACE_SET; then
    run "mkdir -p '$CLAUDE_SETTINGS_DIR'"
    if $DRY_RUN; then
        echo "  [dry-run] merge CLAUDE_WORKSPACE_ROOT into ${CLAUDE_LOCAL_SETTINGS}"
    else
        if [ ! -f "${CLAUDE_LOCAL_SETTINGS}" ]; then
            info "Creating ${CLAUDE_LOCAL_SETTINGS} with env.CLAUDE_WORKSPACE_ROOT"
            echo "{}" > "${CLAUDE_LOCAL_SETTINGS}"
        fi
        python3 - <<PYEOF
import json
from pathlib import Path

path = Path('${CLAUDE_LOCAL_SETTINGS}')
data = json.loads(path.read_text(encoding='utf-8') or '{}')
data.setdefault('env', {})['CLAUDE_WORKSPACE_ROOT'] = '${WORKSPACE}'
path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
print("[atelier:claude] CLAUDE_WORKSPACE_ROOT written to ${CLAUDE_LOCAL_SETTINGS}")
PYEOF
    fi

    # Workspace installs are managed separately by install_agents.sh
fi

# ---- Claude hook settings ---------------------------------------------------
run "mkdir -p '$CLAUDE_SETTINGS_DIR'"

if $DRY_RUN; then
    echo "  [dry-run] merge PreToolUse Atelier loop hook into ${CLAUDE_SETTINGS}"
else
    if [ ! -f "${CLAUDE_SETTINGS}" ]; then
        info "Creating ${CLAUDE_SETTINGS}"
        echo "{}" > "${CLAUDE_SETTINGS}"
    fi
    HOOK_SCRIPT=$(mktemp /tmp/atelier_hook_XXXXXX)
    cat > "${HOOK_SCRIPT}" << 'PYEOF'
import json
import sys

path = sys.argv[1]
hook_command = "echo '{\"systemMessage\": \"Atelier loop required: call task before editing and use rescue on repeated failures.\"}'"

with open(path) as f:
    d = json.load(f)

hooks = d.setdefault("hooks", {})
pre_tool_use = hooks.setdefault("PreToolUse", [])

matcher = "Edit|Write"
for entry in pre_tool_use:
    if entry.get("matcher") == matcher:
        for h in entry.get("hooks", []):
            if h.get("type") == "command" and "Atelier loop required" in h.get("command", ""):
                print("[atelier:claude] Atelier loop PreToolUse hook already present")
                sys.exit(0)

pre_tool_use.append({
    "matcher": matcher,
    "hooks": [{"type": "command", "command": hook_command}]
})

with open(path, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
print("[atelier:claude] Atelier loop PreToolUse hook merged into " + path)
PYEOF
    python3 "${HOOK_SCRIPT}" "${CLAUDE_SETTINGS}"
    rm -f "${HOOK_SCRIPT}"
fi

# ---- permissions: deny native + allow Atelier MCP (and scoped Bash) --------
# This is the always-on enforcement layer. Previously gated behind --project;
# now applied to the active Claude settings.json so the model can't reach
# for native Read/Grep/Glob/Edit/Write/Bash when an Atelier equivalent exists.
apply_enforcement_to_settings "${CLAUDE_SETTINGS}"

# ---- statusLine setting in ~/.claude/settings.json -------------------------
STATUSLINE_SCRIPT="${INSTALL_SOURCE_DIR}/scripts/statusline.sh"
if $DRY_RUN; then
    echo "  [dry-run] set statusLine in ${CLAUDE_SETTINGS} → ${STATUSLINE_SCRIPT}"
elif [ -f "${STATUSLINE_SCRIPT}" ]; then
    python3 - <<PYEOF2
import json
from pathlib import Path
path = Path("${CLAUDE_SETTINGS}")
if not path.exists():
    path.write_text("{}\n")
data = json.loads(path.read_text(encoding="utf-8") or "{}")
data["statusLine"] = {"type": "command", "command": "${STATUSLINE_SCRIPT}", "padding": 1}
data["subagentStatusLine"] = {"type": "command", "command": "${STATUSLINE_SCRIPT}", "padding": 1}
data["agent"] = "atelier:code"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print("[atelier:claude] statusLine set → ${STATUSLINE_SCRIPT}")
print("[atelier:claude] subagentStatusLine set → ${STATUSLINE_SCRIPT}")
print("[atelier:claude] default agent set → atelier-code")
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
fi

info "Done. Start Claude Code in your workspace. Mode skills and agents are available."
info "  Skills: /atelier:code, /atelier:explore, /atelier:review, /atelier:repair, /atelier:research"
info "  Agents: atelier:code, atelier:explore, atelier:review, atelier:repair, atelier:research"
info "  Project enforcement: bash scripts/install_claude.sh --project [DIR]"
