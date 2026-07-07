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
MODE_RENDERER="${SCRIPT_DIR}/sync_agent_context.py"

PLUGIN_REF="atelier@atelier"
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
            if [ $# -lt 2 ]; then
                echo "Missing value for --project" >&2
                exit 1
            fi
            PROJECT_ENFORCE="$2"
            PROJECT_ENFORCE_SET=true
            shift
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

info()  { [[ "${ATELIER_VERBOSE:-0}" == "1" ]] && echo "[atelier:claude] $*" || true; }
warn()  { echo "[atelier:claude] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

# --------------------------------------------------------------------------- #
# Atelier enforcement lists
#
# DENY_TOOLS: optional native-tool hard deny list. Keep this empty by default
# so Claude can ask the user for permission when the model reaches for native
# tools. Set ATELIER_ENFORCE_NATIVE_DENY=1 to hide/block native tools at the
# harness layer for locked-down installs.
# --------------------------------------------------------------------------- #
if [[ "${ATELIER_ENFORCE_NATIVE_DENY:-0}" == "1" ]]; then
    ATELIER_DENY_TOOLS_JSON='["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"]'
else
    ATELIER_DENY_TOOLS_JSON='[]'
fi
ATELIER_MCP_TOOLS_JSON='["mcp__atelier__symbols", "mcp__atelier__node", "mcp__atelier__callers", "mcp__atelier__callees", "mcp__atelier__usages", "mcp__atelier__codemod", "mcp__atelier__code_search", "mcp__atelier__compact", "mcp__atelier__context", "mcp__atelier__edit", "mcp__atelier__grep", "mcp__atelier__memory", "mcp__atelier__read", "mcp__atelier__rescue", "mcp__atelier__route", "mcp__atelier__search", "mcp__atelier__bash", "mcp__atelier__sql", "mcp__atelier__trace", "mcp__atelier__verify"]'
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


uv run python "$MODE_RENDERER" >/dev/null 2>&1 || python3 "$MODE_RENDERER" >/dev/null 2>&1 || true
STAGING_DIR="${HOME}/.atelier/claude-plugin"
# Start fresh — stale symlinks from prior installs (hooks → source dir)
# will cause `cp -r` to error with "same file".
run "rm -rf '$STAGING_DIR'"
run "mkdir -p '$STAGING_DIR/.claude-plugin'"
run "cp '${SOURCE_PLUGIN_DIR}/.claude-plugin/plugin.json' '$STAGING_DIR/.claude-plugin/'"
run "cp '${SOURCE_PLUGIN_DIR}/.claude-plugin/marketplace.json' '$STAGING_DIR/.claude-plugin/'"
run "mkdir -p '$STAGING_DIR/agents'"
info "Staging Claude plugin"
for agent in "${ROLES_ARR[@]}"; do
    atelier_write_managed_copy "${SOURCE_PLUGIN_DIR}/agents/${agent}.md" "$STAGING_DIR/agents/${agent}.md" "$DRY_RUN"
done
run "cp -r '${SOURCE_PLUGIN_DIR}/hooks' '$STAGING_DIR/'"
run "cp -r '${SOURCE_PLUGIN_DIR}/scripts' '$STAGING_DIR/'"
run "cp -r '${SOURCE_PLUGIN_DIR}/workflows' '$STAGING_DIR/'"
SKILL_BUILDER_INCLUDE=""
if [[ -n "$INCLUDE_SKILLS" ]]; then
    SKILL_BUILDER_INCLUDE=" --include-skills=$(printf %q "$INCLUDE_SKILLS")"
fi
run "bash '$SKILL_BUILDER' --host claude --dest '$STAGING_DIR/skills'${SKILL_BUILDER_INCLUDE}"
run "cp '${SOURCE_PLUGIN_DIR}/settings.json' '$STAGING_DIR/'"
run "cp '${SOURCE_PLUGIN_DIR}/.mcp.json' '$STAGING_DIR/'"
atelier_apply_reply_register_level "$STAGING_DIR" "$DRY_RUN"
# Ensure runnable bits on hook + script entrypoints, even if source perms got
# stripped (e.g. via `git stash`, fresh clone on some FS, or restore from tar).
# Claude Code invokes statusline.sh via the `command` type and exec()s the
# path directly; without +x the statusline silently disappears.
run "chmod +x '$STAGING_DIR/scripts/'*.sh 2>/dev/null || true"
run "chmod +x '$STAGING_DIR/hooks/'*.sh '$STAGING_DIR/hooks/'*.py 2>/dev/null || true"
PLUGIN_DIR="$STAGING_DIR"
INSTALL_SOURCE_DIR="$STAGING_DIR"

# Write the Python interpreter path so _run_hook.sh can find atelier in all
# install modes (binary, dev-venv, uv-tool, pip).  Probe in preference order:
#   1. uv run from the repo (dev / local checkout)
#   2. uv tool venv python (uv tool install atelier)
#   3. system python3/python (pip install atelier)
# Binary-mode installs have no importable Python; the file is left absent and
# hooks degrade gracefully via their try/except guards.
if ! $DRY_RUN; then
    _ATELIER_PY=""
    # 1. dev / local checkout
    if [[ -z "${_ATELIER_PY}" ]] && command -v uv >/dev/null 2>&1; then
        _ATELIER_PY="$(cd "${ATELIER_REPO}" && uv run python -c "import sys; print(sys.executable)" 2>/dev/null || true)"
        [[ -n "${_ATELIER_PY}" ]] && "${_ATELIER_PY}" -c "import atelier" 2>/dev/null || _ATELIER_PY=""
    fi
    # 2. uv tool venv
    if [[ -z "${_ATELIER_PY}" ]]; then
        for _py in "${HOME}/.local/share/uv/tools/atelier/bin/python" "${HOME}/.local/share/uv/tools/atelier/bin/python3"; do
            if [[ -x "${_py}" ]] && "${_py}" -c "import atelier" 2>/dev/null; then
                _ATELIER_PY="${_py}"; break
            fi
        done
    fi
    # 3. system python (pip install)
    if [[ -z "${_ATELIER_PY}" ]]; then
        for _py in python3 python; do
            if command -v "${_py}" >/dev/null 2>&1 && "$(command -v "${_py}")" -c "import atelier" 2>/dev/null; then
                _ATELIER_PY="$(command -v "${_py}")"; break
            fi
        done
    fi
    if [[ -n "${_ATELIER_PY}" && -x "${_ATELIER_PY}" ]]; then
        echo "${_ATELIER_PY}" > "${STAGING_DIR}/atelier-python"
        info "Stored atelier python: ${_ATELIER_PY}"
    fi
fi

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
        echo "Step 4 - Project local Claude agents are projected into ${WORKSPACE}/.claude/agents"
    else
        echo "Step 3 - Register MCP in Claude user scope:"
        echo "  claude mcp add --scope user atelier -- atelier mcp --host claude"
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

# ---- plugin install ---------------------------------------------------------
if $DRY_RUN; then
    echo "  [dry-run] claude plugin validate ${PLUGIN_DIR}"
    echo "  [dry-run] claude plugin marketplace add '${INSTALL_SOURCE_DIR}'"
    echo "  [dry-run] claude plugin install ${PLUGIN_REF}"
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
# NOTE: Project-level .mcp.json is not needed when using the Claude plugin.
# This installer only deals with Claude-specific global/user MCP and settings.
if $WORKSPACE_SET; then
    info "Project-level .mcp.json is not needed with the Claude plugin — skipping"
else
    if $DRY_RUN; then
        echo "  [dry-run] claude mcp add --scope user atelier -- atelier mcp --host claude"
    else
        info "Registering atelier MCP server in Claude user scope"
        claude mcp remove --scope user atelier 2>/dev/null || true
        claude mcp add --scope user atelier -- atelier mcp --host claude
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

    if $DRY_RUN; then
        echo "  [dry-run] project workspace-local Claude agents into ${WORKSPACE}/.claude/agents"
    else
        # Use an interpreter that can import atelier (pydantic et al). The bare
        # system python3 usually can't; _ATELIER_PY was resolved above to the
        # dev-venv / uv-tool / pip interpreter. Projection is best-effort — a
        # failure must NOT abort the remaining workspace setup (hooks,
        # statusLine, enforcement), so swallow non-zero exits with a warning.
        PYTHONPATH="${ATELIER_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "${_ATELIER_PY:-python3}" - <<PYEOF || warn "workspace-local Claude agent projection failed (non-fatal); skipping"
from pathlib import Path
from atelier.core.capabilities.workspace_host_overrides import write_workspace_claude_overrides

written = write_workspace_claude_overrides(Path("${WORKSPACE}"), repo_root=Path("${ATELIER_REPO}"), role_ids=tuple(r for r in "${ROLES}".split(",") if r))
print(f"[atelier:claude] projected {len(written)} workspace-local Claude files into ${WORKSPACE}/.claude")
PYEOF
    fi
fi


# ---- permissions: allow Atelier MCP (and optionally deny native tools) ------
# By default this preserves Claude's normal permission prompt for native tools.
# Set ATELIER_ENFORCE_NATIVE_DENY=1 for locked-down installs.
apply_enforcement_to_settings "${CLAUDE_SETTINGS}"

# ---- statusLine setting in ~/.claude/settings.json -------------------------
# ATELIER_STATUSLINE_COMPACT=1 installs the compact layout (model · ctx % ·
# cost · savings · background tasks) instead of the full token breakdown.
STATUSLINE_SCRIPT="${INSTALL_SOURCE_DIR}/scripts/statusline.sh"
STATUSLINE_CMD="${STATUSLINE_SCRIPT}"
if [[ -n "${ATELIER_STATUSLINE_COMPACT:-}" ]]; then
    STATUSLINE_CMD="ATELIER_STATUS_COMPACT=1 ${STATUSLINE_SCRIPT}"
fi
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
data["statusLine"] = {"type": "command", "command": "${STATUSLINE_CMD}", "padding": 1}
data["subagentStatusLine"] = {"type": "command", "command": "${STATUSLINE_SCRIPT}", "padding": 1}
data["agent"] = "atelier:code"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print("[atelier:claude] statusLine set → ${STATUSLINE_SCRIPT}")
print("[atelier:claude] subagentStatusLine set → ${STATUSLINE_SCRIPT}")
print("[atelier:claude] default agent set → atelier:code")
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
    atelier_install_attribution_hook "$PROJECT_ENFORCE" "$DRY_RUN"
elif $WORKSPACE_SET; then
    atelier_install_attribution_hook "$WORKSPACE" "$DRY_RUN"
fi

info "Done. Start Claude Code in your workspace. The atelier:code agent is available."
info "  Agent: atelier:code (other roles are installable on demand)"
info "  Project enforcement: bash scripts/install_claude.sh --project [DIR]"
