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
    MARKETPLACE_JSON="${WORKSPACE}/.agents/plugins/marketplace.json"
    AGENTS_FILE="${WORKSPACE}/AGENTS.md"
    WRAPPER_DEST_DIR="${WORKSPACE}/bin"
    TASKS_DEST_DIR="${WORKSPACE}/.codex/tasks"
else
    INSTALL_SCOPE="global"
    CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
    PLUGIN_DIR="${CODEX_HOME}/plugins/atelier"
    MARKETPLACE_JSON="${HOME}/.agents/plugins/marketplace.json"
    AGENTS_FILE="${CODEX_HOME}/AGENTS.md"
    WRAPPER_DEST_DIR="${HOME}/.local/bin"
    TASKS_DEST_DIR=""
fi

PLUGIN_MCP_JSON="${PLUGIN_DIR}/.mcp.json"
PLUGIN_WRAPPER="${PLUGIN_DIR}/servers/atelier-mcp-wrapper.sh"
MARKETPLACE_PLUGIN_PATH="./.codex/plugins/atelier"

info()  { echo "[atelier:codex] $*"; }
warn()  { echo "[atelier:codex] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
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

write_generated_wrapper() {
    local dest="$1"
    if $DRY_RUN; then
        echo "  [dry-run] write generated MCP wrapper to $dest"
        return
    fi

    mkdir -p "$(dirname "$dest")"
    cat > "$dest" <<EOF
#!/usr/bin/env bash
set -euo pipefail

ATELIER_REPO="${ATELIER_REPO}"

if [ -z "\${ATELIER_WORKSPACE_ROOT:-}" ]; then
    export ATELIER_WORKSPACE_ROOT="\${PWD}"
fi

if [ -z "\${ATELIER_ROOT:-}" ]; then
    if [ -n "\${ATELIER_STORE_ROOT:-}" ]; then
        export ATELIER_ROOT="\${ATELIER_STORE_ROOT}"
    else
        export ATELIER_ROOT="\${HOME}/.atelier"
    fi
fi

if [ -z "\${ATELIER_KNOWLEDGE_ROOT:-}" ]; then
    export ATELIER_KNOWLEDGE_ROOT="\${ATELIER_WORKSPACE_ROOT}/.knowledge"
fi

>&2 echo "[atelier-mcp] repo=\$ATELIER_REPO workspace=\${ATELIER_WORKSPACE_ROOT} root=\${ATELIER_ROOT:-\${ATELIER_STORE_ROOT:-unset}}"

cd "\$ATELIER_REPO"
exec uv run python -m atelier.gateway.adapters.mcp_server "\$@"
EOF
    chmod +x "$dest"
}

install_plugin_bundle() {
    if [ -e "$PLUGIN_DIR" ]; then
        backup_path "$PLUGIN_DIR"
        run "rm -rf '$PLUGIN_DIR'"
    fi
    run "mkdir -p '$PLUGIN_DIR'"
    run "cp -R '$PLUGIN_TEMPLATE/.' '$PLUGIN_DIR/'"
}

patch_plugin_mcp() {
    local wrapper_path="$1"
    local workspace_mode="0"
    if $WORKSPACE_SET; then
        workspace_mode="1"
    fi
    if $DRY_RUN; then
        echo "  [dry-run] patch $PLUGIN_MCP_JSON to use $wrapper_path"
        return
    fi

    python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$PLUGIN_MCP_JSON")
data = json.loads(path.read_text(encoding="utf-8"))
server = data.setdefault("atelier", {})
server["command"] = "$wrapper_path"
server["args"] = server.get("args", [])
if $workspace_mode:
    server["env"] = {
        "ATELIER_WORKSPACE_ROOT": "$WORKSPACE",
        "ATELIER_ROOT": "$WORKSPACE/.atelier",
    }
else:
    server.pop("env", None)
server.pop("cwd", None)
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
}

merge_marketplace() {
    if $DRY_RUN; then
        echo "  [dry-run] merge Atelier plugin entry into $MARKETPLACE_JSON"
        return
    fi

    mkdir -p "$(dirname "$MARKETPLACE_JSON")"
    python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$MARKETPLACE_JSON")
if path.exists():
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
else:
    data = {}

data.setdefault("name", "atelier")
data.setdefault("interface", {}).setdefault("displayName", "Atelier")
plugins = data.setdefault("plugins", [])

entry = {
    "name": "atelier",
    "source": {
        "source": "local",
        "path": "$MARKETPLACE_PLUGIN_PATH",
    },
    "policy": {
        "installation": "INSTALLED_BY_DEFAULT",
        "authentication": "ON_INSTALL",
    },
    "category": "Productivity",
}

for index, plugin in enumerate(plugins):
    if plugin.get("name") == "atelier":
        plugins[index] = entry
        break
else:
    plugins.append(entry)

path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
    info "updated marketplace: $MARKETPLACE_JSON"
}

# ---- check CLI --------------------------------------------------------------
if ! command -v codex &>/dev/null; then
    if $STRICT; then
        echo "[atelier:codex] ERROR: 'codex' CLI not found. Install from https://github.com/openai/codex" >&2
        exit 1
    fi
    warn "'codex' CLI not found — SKIPPING. Install from https://github.com/openai/codex"
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
        echo "2. Generate a repo-pinned MCP wrapper inside the plugin source:"
        echo "   mkdir -p '$(dirname "$PLUGIN_WRAPPER")'"
        echo "   # write ${PLUGIN_WRAPPER} with ATELIER_REPO=${ATELIER_REPO} baked in"
        echo ""
        echo "3. Patch ${PLUGIN_MCP_JSON} so the Atelier MCP server command points at ${PLUGIN_WRAPPER}."
        echo ""
        echo "4. Merge the Atelier marketplace entry into ${MARKETPLACE_JSON}:"
        cat <<JSON
{
    "name": "atelier",
    "interface": {
        "displayName": "Atelier"
    },
    "plugins": [
        {
            "name": "atelier",
            "source": {
                "source": "local",
                "path": "${MARKETPLACE_PLUGIN_PATH}"
            },
            "policy": {
                "installation": "INSTALLED_BY_DEFAULT",
                "authentication": "ON_INSTALL"
            },
            "category": "Productivity"
        }
    ]
}
JSON
        echo ""
        echo "5. Install Codex instructions:"
    echo "   cp '${ATELIER_REPO}/integrations/codex/AGENTS.atelier.md' '${AGENTS_FILE}'"
    echo ""
        echo "6. Install wrapper:"
    echo "   mkdir -p '${WRAPPER_DEST_DIR}'"
    echo "   cp '${ATELIER_REPO}/bin/atelier-codex' '${WRAPPER_DEST_DIR}/atelier-codex'"
    echo "   chmod +x '${WRAPPER_DEST_DIR}/atelier-codex'"
    if $WORKSPACE_SET; then
        echo ""
                echo "7. Install task templates:"
        echo "   mkdir -p '${TASKS_DEST_DIR}'"
        echo "   cp '${ATELIER_REPO}/integrations/codex/tasks/'*.md '${TASKS_DEST_DIR}/'"
    fi
    exit 0
fi

# ---- install plugin bundle + marketplace ------------------------------------
info "Installing Codex plugin source → $PLUGIN_DIR"
install_plugin_bundle
write_generated_wrapper "$PLUGIN_WRAPPER"
patch_plugin_mcp "$PLUGIN_WRAPPER"
merge_marketplace

# ---- AGENTS.md --------------------------------------------------------------
if [ ! -f "$AGENTS_FILE" ]; then
    run "mkdir -p '$(dirname "$AGENTS_FILE")'"
    run "cp '${ATELIER_REPO}/integrations/codex/AGENTS.atelier.md' '$AGENTS_FILE'"
    info "created $AGENTS_FILE"
else
    info "$AGENTS_FILE already exists — not overwriting"
    info "manually copy if needed: cp '${ATELIER_REPO}/integrations/codex/AGENTS.atelier.md' '$AGENTS_FILE'"
fi

# ---- wrapper + task templates ---------------------------------------------
WRAPPER_SRC="${ATELIER_REPO}/bin/atelier-codex"
WRAPPER_DEST="${WRAPPER_DEST_DIR}/atelier-codex"
if [ -f "$WRAPPER_SRC" ]; then
    if [ -e "$WRAPPER_DEST" ] && [ "$(realpath "$WRAPPER_SRC")" = "$(realpath "$WRAPPER_DEST")" ]; then
        info "wrapper already in place: $WRAPPER_DEST"
    else
        run "mkdir -p '$WRAPPER_DEST_DIR'"
        run "cp '$WRAPPER_SRC' '$WRAPPER_DEST'"
        run "chmod +x '$WRAPPER_DEST'"
        info "installed wrapper: $WRAPPER_DEST"
    fi
else
    warn "wrapper source missing: $WRAPPER_SRC"
fi

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

if [ -f "${PLUGIN_DIR}/.codex-plugin/plugin.json" ]; then
    vpass "Codex plugin manifest installed: ${PLUGIN_DIR}/.codex-plugin/plugin.json"
else
    vfail "Codex plugin manifest missing: ${PLUGIN_DIR}/.codex-plugin/plugin.json"
fi

if [ -f "$PLUGIN_MCP_JSON" ]; then
    MCP_COMMAND=$(python3 - <<PYEOF
import json
from pathlib import Path
data = json.loads(Path("$PLUGIN_MCP_JSON").read_text(encoding="utf-8"))
print(data.get("atelier", {}).get("command", ""))
PYEOF
)
    if [ "$MCP_COMMAND" = "$PLUGIN_WRAPPER" ]; then
        vpass "plugin MCP config points at generated wrapper"
    else
        vfail "plugin MCP config does not point at generated wrapper"
    fi
else
    vfail "plugin MCP config missing: $PLUGIN_MCP_JSON"
fi

if [ -f "$MARKETPLACE_JSON" ]; then
    MARKETPLACE_OK=$(python3 - <<PYEOF
import json
from pathlib import Path
data = json.loads(Path("$MARKETPLACE_JSON").read_text(encoding="utf-8"))
plugins = data.get("plugins", [])
print("yes" if any(plugin.get("name") == "atelier" for plugin in plugins) else "no")
PYEOF
)
    if [ "$MARKETPLACE_OK" = "yes" ]; then
        vpass "marketplace contains atelier plugin entry: $MARKETPLACE_JSON"
    else
        vfail "marketplace missing atelier entry: $MARKETPLACE_JSON"
    fi
else
    vfail "marketplace file missing: $MARKETPLACE_JSON"
fi

if [ -x "$PLUGIN_WRAPPER" ]; then
    vpass "generated plugin MCP wrapper installed: $PLUGIN_WRAPPER"
else
    vfail "generated plugin MCP wrapper missing or not executable: $PLUGIN_WRAPPER"
fi

if [ -f "$AGENTS_FILE" ] && grep -q "atelier:code" "$AGENTS_FILE" 2>/dev/null; then
    vpass "AGENTS.md present with atelier:code persona: $AGENTS_FILE"
else
    vfail "AGENTS.md missing or has no atelier:code persona: $AGENTS_FILE"
fi

if [ -x "$WRAPPER_DEST" ]; then
    vpass "Codex preflight wrapper installed: $WRAPPER_DEST"
else
    vfail "Codex preflight wrapper missing or not executable: $WRAPPER_DEST"
fi

if $WORKSPACE_SET; then
    if [ -d "$TASKS_DEST_DIR" ] && [ -f "$TASKS_DEST_DIR/preflight.md" ]; then
        vpass "Codex task templates installed: $TASKS_DEST_DIR"
    else
        vfail "Codex task templates missing in $TASKS_DEST_DIR"
    fi
fi

if [ -x "${ATELIER_REPO}/bin/atelier-status" ]; then
    vpass "bin/atelier-status helper exists"
else
    vfail "bin/atelier-status missing or not executable"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[atelier:codex] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Restart Codex — the Atelier marketplace and plugin source are ready."
