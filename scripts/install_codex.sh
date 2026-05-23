#!/usr/bin/env bash
# install_codex.sh — Install Atelier into Codex CLI
#
# What it does:
#   Global mode: installs a personal Codex marketplace plus a local Atelier plugin source.
#   Workspace mode (--workspace DIR): installs a repo-local Codex marketplace plus a local Atelier plugin source under DIR.
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippets for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if 'codex' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"
PLUGIN_TEMPLATE="${ATELIER_REPO}/integrations/codex/plugin"

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
fi

if $WORKSPACE_SET; then
    INSTALL_SCOPE="workspace"
    CODEX_HOME="${WORKSPACE}/.codex"
    PLUGIN_DIR="${WORKSPACE}/.codex/plugins/atelier"
    AGENTS_FILE="${WORKSPACE}/AGENTS.md"
    TASKS_DEST_DIR="${WORKSPACE}/.codex/tasks"
else
    INSTALL_SCOPE="global"
    CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
    PLUGIN_DIR="${CODEX_HOME}/plugins/atelier"
    AGENTS_FILE="${CODEX_HOME}/AGENTS.md"
    TASKS_DEST_DIR=""
fi

PLUGIN_MCP_JSON="${PLUGIN_DIR}/.mcp.json"
SKILL_BUILDER="${SCRIPT_DIR}/build_host_skills.sh"

info()  { [[ "${ATELIER_VERBOSE:-0}" == "1" ]] && echo "[atelier:codex] $*" || true; }
warn()  { echo "[atelier:codex] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

# ---- resolve install profile ------------------------------------------------
atelier_resolve_install_profile "atelier:codex"
if [[ -n "${ATELIER_INSTALL_PROFILE_WARNING:-}" ]]; then
    warn "$ATELIER_INSTALL_PROFILE_WARNING"
fi
STAGING_DIR="${HOME}/.atelier/codex-plugin-${INSTALL_PROFILE}"
run "mkdir -p '$STAGING_DIR/.codex-plugin'"
run "cp '${PLUGIN_TEMPLATE}/.codex-plugin/plugin.json' '$STAGING_DIR/.codex-plugin/'"
run "cp '${PLUGIN_TEMPLATE}/.mcp.json' '$STAGING_DIR/'"
run "cp -R '${ATELIER_REPO}/integrations/codex/hooks' '$STAGING_DIR/'"
run "mkdir -p '$STAGING_DIR/agents'"
AGENT_SRC="${ATELIER_REPO}/integrations/codex/AGENTS.atelier.md"
if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    info "Install profile: dev; staging full agent instructions"
    run "cp '${AGENT_SRC/.md/.dev.md}' '$STAGING_DIR/agents/atelier.md'"
else
    info "Install profile: stable; staging stable agent instructions"
    run "cp '${AGENT_SRC}' '$STAGING_DIR/agents/atelier.md'"
fi
if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    run "bash '$SKILL_BUILDER' --host codex --dest '$STAGING_DIR/skills' --include-dev"
else
    run "bash '$SKILL_BUILDER' --host codex --dest '$STAGING_DIR/skills'"
fi
PLUGIN_TEMPLATE="$STAGING_DIR"
backup_file() {
    local f="$1"
    if [ -f "$f" ]; then
        local bk="${f}.atelier-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp '$f' '$bk'"
        info "backed up $f → $bk"
    fi
}

backup_path() {
    local path="$1"
    if [ -e "$path" ]; then
        local bk="${path}.atelier-backup.$(date +%Y%m%dT%H%M%S)"
        if [ -d "$path" ]; then
            run "cp -R '$path' '$bk'"
        else
            run "cp '$path' '$bk'"
        fi
        info "backed up $path → $bk"
    fi
}

merge_agents_file() {
    local source_file="$1"
    local dest_file="$2"

    if [ ! -f "$dest_file" ]; then
        if $DRY_RUN; then
            atelier_write_managed_copy "$source_file" "$dest_file" "true"
        else
            atelier_write_managed_copy "$source_file" "$dest_file" "false"
        fi
        info "created $dest_file"
        return
    fi

    backup_file "$dest_file"
    atelier_upsert_managed_block "$source_file" "$dest_file" "$DRY_RUN"
    info "merged Atelier Codex instructions into $dest_file"
}

install_plugin_bundle() {
    if [ -e "$PLUGIN_DIR" ]; then
        backup_path "$PLUGIN_DIR"
        run "rm -rf '$PLUGIN_DIR'"
    fi
    run "mkdir -p '$PLUGIN_DIR'"
    run "cp -R '$PLUGIN_TEMPLATE/.' '$PLUGIN_DIR/'"
}

codex_cmd() {
    if $WORKSPACE_SET; then
        CODEX_HOME="$CODEX_HOME" codex "$@"
    else
        codex "$@"
    fi
}

patch_plugin_mcp() {
    local workspace_mode="0"
    if $WORKSPACE_SET; then
        workspace_mode="1"
    fi
    if $DRY_RUN; then
        echo "  [dry-run] patch $PLUGIN_MCP_JSON to use atelier-mcp with ATELIER_DEV_MODE=1"
        return
    fi

    python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$PLUGIN_MCP_JSON")
data = json.loads(path.read_text(encoding="utf-8"))
server = data.setdefault("atelier", {})
server["command"] = "atelier-mcp"
server["args"] = ["--host", "codex"]
env = dict(server.get("env") or {})
env["ATELIER_DEV_MODE"] = "1"
if $workspace_mode:
    env["ATELIER_WORKSPACE_ROOT"] = "$WORKSPACE"
else:
    env.pop("ATELIER_WORKSPACE_ROOT", None)
server["env"] = env
server.pop("cwd", None)
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
}

ensure_codex_mcp() {
    run "mkdir -p '$CODEX_HOME'"
    if $DRY_RUN; then
        if $WORKSPACE_SET; then
            echo "  [dry-run] CODEX_HOME='$CODEX_HOME' codex mcp add atelier --env ATELIER_DEV_MODE=1 --env ATELIER_WORKSPACE_ROOT='$WORKSPACE' -- atelier-mcp --host codex"
        else
            echo "  [dry-run] codex mcp add atelier --env ATELIER_DEV_MODE=1 -- atelier-mcp --host codex"
        fi
        return
    fi

    codex_cmd mcp remove atelier >/dev/null 2>&1 || true
    if $WORKSPACE_SET; then
        codex_cmd mcp add atelier --env ATELIER_DEV_MODE=1 --env "ATELIER_WORKSPACE_ROOT=$WORKSPACE" -- atelier-mcp --host codex >/dev/null
    else
        codex_cmd mcp add atelier --env ATELIER_DEV_MODE=1 -- atelier-mcp --host codex >/dev/null
    fi
    info "registered Codex MCP server 'atelier' in ${CODEX_HOME}/config.toml"
}

install_codex_plugin() {
    # Installs atelier as a plugin in Codex's built-in openai-curated
    # marketplace (~/.codex/.tmp/plugins/). This is the only marketplace
    # Codex v0.132+ exposes, so we add atelier as an entry within it.
    if $DRY_RUN; then
        echo "  [dry-run] install atelier into Codex's openai-curated marketplace"
        echo "  [dry-run] codex plugin add atelier@openai-curated"
        return
    fi

    local codex_plugins_repo="${CODEX_HOME}/.tmp/plugins"
    if [ ! -d "$codex_plugins_repo/.git" ]; then
        warn "Codex plugins repository not found at ${codex_plugins_repo}; skipping plugin auto-install. MCP is still configured and AGENTS.md remains active."
        return
    fi

    local plugin_dest="$codex_plugins_repo/plugins/atelier"
    local marketplace="$codex_plugins_repo/.agents/plugins/marketplace.json"

    if [ ! -f "$marketplace" ]; then
        warn "Codex marketplace file not found at ${marketplace}; skipping plugin auto-install."
        return
    fi

    # Copy plugin source into the Codex marketplace repo
    mkdir -p "$plugin_dest/.codex-plugin"
    cp "$PLUGIN_DIR/.codex-plugin/plugin.json" "$plugin_dest/.codex-plugin/"
    cp "$PLUGIN_DIR/.mcp.json" "$plugin_dest/"

    # Add atelier entry to the openai-curated marketplace
    python3 -c "
import json
path = '$marketplace'
data = json.loads(open(path).read())
plugins = data.setdefault('plugins', [])
if not any(p.get('name') == 'atelier' for p in plugins):
    plugins.append({
        'name': 'atelier',
        'source': {'source': 'local', 'path': './plugins/atelier'},
        'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
        'category': 'Coding'
    })
    open(path, 'w').write(json.dumps(data, indent=2) + '\n')
" 2>/dev/null || {
        warn "failed to update Codex marketplace; skipping plugin install"
        return
    }

    # Commit so Codex picks up the change
    if ! git -C "$codex_plugins_repo" add -A 2>/dev/null; then
        warn "failed to stage atelier plugin in Codex marketplace; skipping"
        return
    fi
    git -C "$codex_plugins_repo" commit -m "add atelier plugin" --allow-empty 2>/dev/null || true

    if codex_cmd plugin add atelier@openai-curated >/dev/null 2>&1; then
        info "installed Codex plugin atelier@openai-curated"
    else
        warn "Codex plugin auto-install failed; MCP is still configured and Codex will use Atelier through the registered MCP server."
    fi
}

# ---- check CLI --------------------------------------------------------------
if ! command -v codex &>/dev/null; then
    if $STRICT; then
        echo "[atelier:codex] ERROR: 'codex' CLI not found. Install from https://github.com/openai/codex" >&2
        exit 1
    fi
    warn "'codex' CLI not found — SKIPPING. Install from https://github.com/openai/codex"
    echo "=== SKIPPED (codex CLI absent) ==="
    exit 0
fi
info "Found Codex: $(codex --version 2>/dev/null || echo 'version unknown')"

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== Atelier Codex — Manual Install Steps ==="
    echo "Scope: ${INSTALL_SCOPE}"
    echo ""
        echo "1. Copy the Atelier plugin source:"
        echo "   mkdir -p '${PLUGIN_DIR}'"
        echo "   cp -R '${PLUGIN_TEMPLATE}/.' '${PLUGIN_DIR}/'"
        echo ""
        echo "2. Patch ${PLUGIN_MCP_JSON} to use 'atelier-mcp --host codex' and set 'ATELIER_DEV_MODE=1'."
        echo ""
        echo "3. Register Atelier as a Codex MCP server:"
        if $WORKSPACE_SET; then
            echo "   CODEX_HOME='${CODEX_HOME}' codex mcp add atelier --env ATELIER_DEV_MODE=1 --env ATELIER_WORKSPACE_ROOT='${WORKSPACE}' -- atelier-mcp --host codex"
        else
            echo "   codex mcp add atelier --env ATELIER_DEV_MODE=1 -- atelier-mcp --host codex"
        fi
        echo ""
        echo "4. Add atelier to Codex's openai-curated marketplace:"
        echo "   cp -R '${PLUGIN_DIR}/.' '${CODEX_HOME}/.tmp/plugins/plugins/atelier/'"
        echo "   # Then edit ${CODEX_HOME}/.tmp/plugins/.agents/plugins/marketplace.json"
        echo "   # to add atelier to the 'plugins' array, then git commit:"
        echo "   cd '${CODEX_HOME}/.tmp/plugins' && git add -A && git commit -m 'add atelier plugin'"
        echo "   codex plugin add atelier@openai-curated"
        echo ""
    if $WORKSPACE_SET; then
        echo "5. Install universal project agents (run once per project):"
        echo "   bash scripts/install_agents.sh --workspace '${WORKSPACE}'"
        echo ""
        echo "6. Install task templates:"
        echo "   mkdir -p '${TASKS_DEST_DIR}'"
        echo "   cp '${ATELIER_REPO}/integrations/codex/tasks/'*.md '${TASKS_DEST_DIR}/'"
    else
        echo "5. Install Codex instructions:"
        echo "   cp '${ATELIER_REPO}/integrations/codex/AGENTS.atelier.md' '${AGENTS_FILE}'"
    fi
    exit 0
fi

# ---- install plugin bundle + marketplace ------------------------------------
info "Installing Codex plugin source → $PLUGIN_DIR"
install_plugin_bundle
patch_plugin_mcp
ensure_codex_mcp
install_codex_plugin

# ---- AGENTS.md --------------------------------------------------------------
# NOTE: Project-level AGENTS.md is handled by scripts/install_agents.sh.
# Only write AGENTS.md in global mode (Codex user dir).
if $WORKSPACE_SET; then
    info "Project-level AGENTS.md is managed by scripts/install_agents.sh — skipping"
    info "  Run: scripts/install_agents.sh --workspace '${WORKSPACE}'"
else
    merge_agents_file "${ATELIER_REPO}/integrations/codex/AGENTS.atelier.md" "$AGENTS_FILE"
fi

# ---- task templates ----------------------------------------------------------
TASKS_SRC_DIR="${ATELIER_REPO}/integrations/codex/tasks"
if $WORKSPACE_SET && [ -d "$TASKS_SRC_DIR" ]; then
    run "mkdir -p '$TASKS_DEST_DIR'"
    run "cp '$TASKS_SRC_DIR'/*.md '$TASKS_DEST_DIR/'"
    info "installed task templates: $TASKS_DEST_DIR"
elif $WORKSPACE_SET; then
    warn "task template directory missing: $TASKS_SRC_DIR"
fi

if $DRY_RUN; then
    info "Dry run complete; skipping post-install verification."
    exit 0
fi

# ── Post-install verification ------------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[atelier:codex] FAIL: $*" >&2; VFAIL=1; }
vwarn() { warn "$*"; }

if [ -f "${PLUGIN_DIR}/.codex-plugin/plugin.json" ]; then
    vpass "Codex plugin manifest installed: ${PLUGIN_DIR}/.codex-plugin/plugin.json"
else
    vfail "Codex plugin manifest missing: ${PLUGIN_DIR}/.codex-plugin/plugin.json"
fi

if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    if [ -d "${PLUGIN_DIR}/skills" ] && [ -f "${PLUGIN_DIR}/skills/status/SKILL.md" ] && [ -f "${PLUGIN_DIR}/skills/context/SKILL.md" ]; then
        vpass "Codex skill bundle installed with dev skills: ${PLUGIN_DIR}/skills"
    else
        vfail "Codex dev skill bundle missing context or status skill: ${PLUGIN_DIR}/skills"
    fi
else
    if [ ! -f "${PLUGIN_DIR}/skills/context/SKILL.md" ] && [ ! -f "${PLUGIN_DIR}/skills/status/SKILL.md" ]; then
        vpass "Codex stable skill bundle installed without dev-only skills: ${PLUGIN_DIR}/skills"
    else
        vfail "Codex stable skill bundle unexpectedly contains dev-only skills: ${PLUGIN_DIR}/skills"
    fi
fi

if [ -f "$PLUGIN_MCP_JSON" ]; then
    MCP_STATUS=$(python3 - <<PYEOF
import json
from pathlib import Path
data = json.loads(Path("$PLUGIN_MCP_JSON").read_text(encoding="utf-8"))
server = data.get("atelier", {})
print(server.get("command", ""))
print((server.get("env") or {}).get("ATELIER_DEV_MODE", ""))
PYEOF
)
    MCP_COMMAND=$(printf '%s\n' "$MCP_STATUS" | sed -n '1p')
    MCP_DEV_MODE=$(printf '%s\n' "$MCP_STATUS" | sed -n '2p')
    if [ "$MCP_COMMAND" = "atelier-mcp" ]; then
        vpass "plugin MCP config points at atelier-mcp"
    else
        vfail "plugin MCP config does not point at atelier-mcp (got: $MCP_COMMAND)"
    fi
    if [ "$MCP_DEV_MODE" = "1" ]; then
        vpass "plugin MCP config enables ATELIER_DEV_MODE=1"
    else
        vfail "plugin MCP config does not enable ATELIER_DEV_MODE=1 (got: ${MCP_DEV_MODE:-unset})"
    fi
else
    vfail "plugin MCP config missing: $PLUGIN_MCP_JSON"
fi

if [ -f "$CODEX_HOME/config.toml" ] && grep -q '\[mcp_servers\.atelier\]' "$CODEX_HOME/config.toml" 2>/dev/null; then
    vpass "Codex config registers atelier MCP server: $CODEX_HOME/config.toml"
else
    vfail "Codex config missing atelier MCP server entry: $CODEX_HOME/config.toml"
fi

if codex_cmd mcp list 2>/dev/null | grep -q '^atelier[[:space:]]'; then
    vpass "codex mcp list exposes atelier server"
else
    vfail "codex mcp list does not expose atelier server"
fi

if command -v atelier-mcp &>/dev/null; then
    vpass "atelier-mcp is available on PATH"
else
    vfail "atelier-mcp NOT found on PATH"
fi

CODEX_MARKETPLACE="$CODEX_HOME/.tmp/plugins/.agents/plugins/marketplace.json"
if [ -f "$CODEX_MARKETPLACE" ]; then
    MARKETPLACE_OK=$(python3 -c "
import json
data = json.loads(open('$CODEX_MARKETPLACE').read())
print('yes' if any(p.get('name') == 'atelier' for p in data.get('plugins', [])) else 'no')
")
    if [ "$MARKETPLACE_OK" = "yes" ]; then
        vpass "openai-curated marketplace contains atelier entry: $CODEX_MARKETPLACE"
    else
        vfail "openai-curated marketplace missing atelier entry: $CODEX_MARKETPLACE"
    fi
else
    vwarn "Codex marketplace file missing; plugin install skipped"
fi

PLUGIN_CONFIG_KEY='[plugins."atelier@openai-curated"]'
if [ -f "$CODEX_HOME/config.toml" ] && grep -qF "$PLUGIN_CONFIG_KEY" "$CODEX_HOME/config.toml" 2>/dev/null; then
    vpass "Codex config enables plugin atelier@openai-curated"
else
    vwarn "Codex config missing plugin entry for atelier@openai-curated; MCP registration is the required surface, plugin install remains best-effort"
fi

if codex_cmd plugin list 2>/dev/null | grep -Eq '^atelier@openai-curated[[:space:]]+installed, enabled([[:space:]]|$)'; then
    vpass "codex plugin list shows atelier plugin installed"
else
    vwarn "codex plugin list does not show atelier plugin installed; Codex will still use Atelier via the registered MCP server"
fi

if [ -f "$AGENTS_FILE" ] && grep -q "atelier:code" "$AGENTS_FILE" 2>/dev/null; then
    vpass "AGENTS.md present with atelier:code persona: $AGENTS_FILE"
elif $WORKSPACE_SET; then
    vwarn "AGENTS.md missing atelier:code persona — run: scripts/install_agents.sh --workspace '${WORKSPACE}'"
else
    vfail "AGENTS.md missing or has no atelier:code persona: $AGENTS_FILE"
fi

if $WORKSPACE_SET; then
    if [ -d "$TASKS_DEST_DIR" ] && [ -f "$TASKS_DEST_DIR/preflight.md" ]; then
        vpass "Codex task templates installed: $TASKS_DEST_DIR"
    else
        vfail "Codex task templates missing in $TASKS_DEST_DIR"
    fi
fi

if command -v atelier >/dev/null 2>&1 && atelier status --help >/dev/null 2>&1; then
    vpass "atelier status command is available"
else
    vfail "atelier status command unavailable"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[atelier:codex] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Restart Codex — the Atelier marketplace and plugin source are ready."
