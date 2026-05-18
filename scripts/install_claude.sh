#!/usr/bin/env bash
# install_claude.sh - Install Atelier into Claude Code
#
# What it does:
#   1. Validates the Claude plugin package at integrations/claude/plugin/.
#   2. Installs/updates atelier@atelier.
#   3. Global mode: registers MCP with Claude's user scope.
#   4. Workspace mode (--workspace DIR): writes project-local .mcp.json and settings.
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippets for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if 'claude' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"
PLUGIN_DIR="${ATELIER_REPO}/integrations/claude/plugin"
SOURCE_PLUGIN_DIR="${PLUGIN_DIR}"
INSTALL_SOURCE_DIR="${PLUGIN_DIR}"

PLUGIN_REF="atelier@atelier"

DRY_RUN=false
PRINT_ONLY=false
STRICT=false
WORKSPACE=""
WORKSPACE_SET=false

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

info()  { echo "[atelier:claude] $*"; }
warn()  { echo "[atelier:claude] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

# ---- resolve install profile ------------------------------------------------
atelier_resolve_install_profile "atelier:claude"
if [[ -n "${ATELIER_INSTALL_PROFILE_WARNING:-}" ]]; then
    warn "$ATELIER_INSTALL_PROFILE_WARNING"
fi
STAGING_DIR="${HOME}/.atelier/claude-plugin-${INSTALL_PROFILE}"
run "mkdir -p '$STAGING_DIR/.claude-plugin'"
run "cp '${SOURCE_PLUGIN_DIR}/.claude-plugin/plugin.json' '$STAGING_DIR/.claude-plugin/'"
run "cp '${SOURCE_PLUGIN_DIR}/.claude-plugin/marketplace.json' '$STAGING_DIR/.claude-plugin/'"
run "mkdir -p '$STAGING_DIR/agents'"
if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    info "Install profile: dev; staging full plugin with task loop"
    for agent in code explore review repair; do
        atelier_write_managed_copy "${SOURCE_PLUGIN_DIR}/agents/${agent}.dev.md" "$STAGING_DIR/agents/${agent}.md" "$DRY_RUN"
    done
else
    info "Install profile: stable; staging stable plugin without dev-only task loop"
    for agent in code explore review repair; do
        atelier_write_managed_copy "${SOURCE_PLUGIN_DIR}/agents/${agent}.md" "$STAGING_DIR/agents/${agent}.md" "$DRY_RUN"
    done
fi
run "cp -r '${SOURCE_PLUGIN_DIR}/hooks' '$STAGING_DIR/'"
run "cp '${SOURCE_PLUGIN_DIR}/.mcp.json' '$STAGING_DIR/'"
PLUGIN_DIR="$STAGING_DIR"
INSTALL_SOURCE_DIR="$STAGING_DIR"

if $WORKSPACE_SET; then
    NEW_MCP_ENTRY=$(cat <<JSON
{
  "mcpServers": {
    "atelier": {
      "type": "stdio",
      "command": "atelier-mcp",
      "args": ["--host", "claude"],
      "env": {
        "ATELIER_WORKSPACE_ROOT": "${WORKSPACE}"
      }
    }
  }
}
JSON
)
else
    NEW_MCP_ENTRY=$(cat <<JSON
{
  "mcpServers": {
    "atelier": {
      "type": "stdio",
      "command": "atelier-mcp",
      "args": ["--host", "claude"]
    }
  }
}
JSON
)
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
        echo "Step 3 - Create/merge ${MCP_JSON}:"
        echo "$NEW_MCP_ENTRY"
        echo ""
        echo "Step 4 - Optional project setting:"
        echo "  set env.CLAUDE_WORKSPACE_ROOT=${WORKSPACE} in ${CLAUDE_LOCAL_SETTINGS}"
    else
        echo "Step 3 - Register MCP in Claude user scope:"
        echo "  claude mcp add --scope user atelier -- atelier-mcp --host claude"
    fi
    echo ""
    echo "After install, in Claude Code: /atelier:status"
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

for agent in code explore review repair; do
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
if $WORKSPACE_SET; then
    run "mkdir -p '$(dirname "$MCP_JSON")'"
    if $DRY_RUN; then
        echo "  [dry-run] merge atelier entry into ${MCP_JSON}"
    elif [ ! -f "${MCP_JSON}" ]; then
        info "Creating ${MCP_JSON} with atelier entry"
        echo "${NEW_MCP_ENTRY}" > "${MCP_JSON}"
    else
        HAS=$(python3 -c "
import json
d = json.load(open('${MCP_JSON}'))
servers = d.get('mcpServers', {})
print('yes' if 'atelier' in servers else 'no')
" 2>/dev/null || echo "error")
        if [ "$HAS" = "yes" ]; then
            info "atelier entry already in ${MCP_JSON}"
        else
            info "Merging atelier entry into ${MCP_JSON}"
            python3 - <<PYEOF
import json
from pathlib import Path

path = Path('${MCP_JSON}')
existing = json.loads(path.read_text(encoding='utf-8') or '{}')
new_entry = json.loads('''${NEW_MCP_ENTRY}''')
existing.setdefault('mcpServers', {}).update(new_entry['mcpServers'])
path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
PYEOF
            info "atelier entry merged into ${MCP_JSON}"
        fi
    fi
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
    HOOK_SCRIPT=$(mktemp /tmp/atelier_hook_XXXXXX.py)
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

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- statusLine setting in ~/.claude/settings.json -------------------------
STATUSLINE_SCRIPT="${ATELIER_REPO}/integrations/claude/plugin/scripts/statusline.sh"
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
data["agent"] = "atelier:code"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print("[atelier:claude] statusLine set → ${STATUSLINE_SCRIPT}")
print("[atelier:claude] default agent set → atelier-code")
PYEOF2
else
    warn "statusline.sh not found at ${STATUSLINE_SCRIPT} — skipping statusLine"
fi

info "Done. Start Claude Code in your workspace. Skills and agents are available."
info "  /atelier:status  - show run ledger"
info "  /atelier:context - show task context"
info "  Agents: atelier:code, atelier:explore, atelier:review, atelier:repair"
