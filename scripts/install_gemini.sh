#!/usr/bin/env bash
# install_gemini.sh - Install Atelier into Gemini CLI
#
# What it does:
#   Global mode: links the packaged Atelier Gemini extension and enables it for the user.
#   Workspace mode (--workspace DIR): links the packaged extension and enables it only for DIR.
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippet for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if 'gemini' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"
ATELIER_WRAPPER="${ATELIER_REPO}/scripts/atelier_mcp_stdio.sh"
EXTENSION_DIR="${ATELIER_REPO}/integrations/gemini/extension"
EXTENSION_MANIFEST="${EXTENSION_DIR}/gemini-extension.json"
SKILL_BUILDER="${SCRIPT_DIR}/build_host_skills.sh"

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
else
    INSTALL_SCOPE="global"
fi


info()  { echo "[atelier:gemini] $*"; }
warn()  { echo "[atelier:gemini] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

patch_extension_manifest() {
    local workspace_root='${workspacePath}'
    if $WORKSPACE_SET; then
        workspace_root="$WORKSPACE"
    fi

    if $DRY_RUN; then
        echo "  [dry-run] patch $EXTENSION_MANIFEST to use $ATELIER_WRAPPER"
        return
    fi

    python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$EXTENSION_MANIFEST")
data = json.loads(path.read_text(encoding="utf-8"))
server = data.setdefault("mcpServers", {}).setdefault("atelier", {})
server["command"] = "$ATELIER_WRAPPER"
server["args"] = server.get("args", [])
server.setdefault("env", {})["ATELIER_WORKSPACE_ROOT"] = "$workspace_root"
server["env"]["ATELIER_SERVICE_URL"] = "http://127.0.0.1:8787"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF

    info "patched linked extension manifest to use ${ATELIER_WRAPPER}"
}

# ---- resolve install profile ------------------------------------------------
atelier_resolve_install_profile "atelier:gemini"
if [[ -n "${ATELIER_INSTALL_PROFILE_WARNING:-}" ]]; then
    warn "$ATELIER_INSTALL_PROFILE_WARNING"
fi
STAGING_DIR="${HOME}/.atelier/gemini-extension-${INSTALL_PROFILE}"
run "mkdir -p '$STAGING_DIR'"
run "cp '${EXTENSION_DIR}/gemini-extension.json' '$STAGING_DIR/'"
run "cp -r '${EXTENSION_DIR}/commands' '$STAGING_DIR/'"
GEMINI_SRC="${ATELIER_REPO}/integrations/gemini/GEMINI.atelier.md"
if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    info "Install profile: dev; staging full GEMINI.md with task loop"
    atelier_write_managed_copy "${GEMINI_SRC/.md/.dev.md}" "$STAGING_DIR/GEMINI.md" "$DRY_RUN"
else
    info "Install profile: stable; staging stable GEMINI.md without dev-only task guidance"
    atelier_write_managed_copy "${GEMINI_SRC}" "$STAGING_DIR/GEMINI.md" "$DRY_RUN"
fi
if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    run "bash '$SKILL_BUILDER' --host gemini --dest '$STAGING_DIR/skills' --include-dev"
else
    run "bash '$SKILL_BUILDER' --host gemini --dest '$STAGING_DIR/skills'"
fi
EXTENSION_DIR="$STAGING_DIR"
EXTENSION_MANIFEST="${EXTENSION_DIR}/gemini-extension.json"
patch_extension_manifest

backup_file() {
    local f="$1"
    if [ -f "$f" ]; then
        local bk="${f}.atelier-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp '$f' '$bk'"
        info "backed up $f -> $bk"
    fi
}

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
        echo "=== Atelier Gemini CLI - Manual Install ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
        echo "Extension source: ${EXTENSION_DIR}"
        echo "Manifest: ${EXTENSION_MANIFEST}"
    echo ""
        echo "1. Validate the extension:"
        echo "   gemini extensions validate '${EXTENSION_DIR}'"
    echo ""
        echo "2. Link the local extension source:"
        echo "   gemini extensions link '${EXTENSION_DIR}'"
        if $WORKSPACE_SET; then
                echo ""
                echo "3. Enable the extension only for ${WORKSPACE}:"
                echo "   (cd '${WORKSPACE}' && gemini extensions disable atelier --scope user || true)"
                echo "   (cd '${WORKSPACE}' && gemini extensions enable atelier --scope workspace)"
        else
                echo ""
                echo "3. Ensure the extension is enabled for the user:"
                echo "   gemini extensions enable atelier --scope user"
        fi
        echo ""
    exit 0
fi

# ---- check CLI --------------------------------------------------------------
if ! command -v gemini &>/dev/null; then
    if $STRICT; then
        echo "[atelier:gemini] ERROR: 'gemini' CLI not found. Install from https://ai.google.dev/gemini-api/docs/gemini-cli" >&2
        exit 1
    fi
    warn "'gemini' CLI not found - SKIPPING."
    warn "Install Gemini CLI, then run: make install-gemini"
    echo "=== SKIPPED (gemini CLI absent) ==="
    exit 0
fi
info "Found Gemini CLI: $(gemini --version 2>/dev/null || echo 'version unknown')"

if [ ! -x "$ATELIER_WRAPPER" ]; then
    echo "[atelier:gemini] ERROR: Atelier MCP wrapper missing or not executable: $ATELIER_WRAPPER" >&2
    exit 1
fi
info "Using Atelier MCP wrapper: $ATELIER_WRAPPER"

# ---- validate + link packaged extension ------------------------------------
info "Validating extension manifest"
run "gemini extensions validate '$EXTENSION_DIR'"

if $DRY_RUN; then
    info "Dry run complete; skipped install and scope changes because no files were written."
    exit 0
fi

gemini extensions uninstall atelier >/dev/null 2>&1 || true
gemini extensions link "$EXTENSION_DIR" --consent

if $WORKSPACE_SET; then
    (cd "$WORKSPACE" && gemini extensions disable atelier --scope user >/dev/null 2>&1) || true
    (cd "$WORKSPACE" && gemini extensions enable atelier --scope workspace)
    info "enabled atelier for workspace scope: $WORKSPACE"
else
    gemini extensions enable atelier --scope user >/dev/null 2>&1 || true
    info "enabled atelier for user scope"
fi


# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[atelier:gemini] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$EXTENSION_MANIFEST" ]; then
    vpass "extension manifest present: $EXTENSION_MANIFEST"
else
    vfail "missing extension manifest: $EXTENSION_MANIFEST"
fi

COMMAND=$(python3 - <<PYEOF
import json
from pathlib import Path
data = json.loads(Path("$EXTENSION_MANIFEST").read_text(encoding="utf-8"))
print(data.get("mcpServers", {}).get("atelier", {}).get("command", ""))
PYEOF
)
if [ "$COMMAND" = "$ATELIER_WRAPPER" ]; then
    vpass "extension manifest points at repo MCP wrapper"
else
    vfail "extension manifest does not point at repo MCP wrapper"
fi

if [ -x "$ATELIER_WRAPPER" ]; then
    vpass "Atelier MCP wrapper exists: $ATELIER_WRAPPER"
else
    vfail "Atelier MCP wrapper missing or not executable: $ATELIER_WRAPPER"
fi

if [ -d "${EXTENSION_DIR}/commands/atelier" ] && [ -f "${EXTENSION_DIR}/commands/atelier/status.toml" ] && [ -f "${EXTENSION_DIR}/commands/atelier/context.toml" ]; then
    vpass "extension command bundle present: ${EXTENSION_DIR}/commands/atelier"
else
    vfail "extension command bundle missing in ${EXTENSION_DIR}/commands/atelier"
fi

if [[ "$INSTALL_PROFILE" == "dev" ]]; then
    if [ -d "${EXTENSION_DIR}/skills" ] && [ -f "${EXTENSION_DIR}/skills/status/SKILL.md" ] && [ -f "${EXTENSION_DIR}/skills/task/SKILL.md" ]; then
        vpass "extension skill bundle installed with dev skills: ${EXTENSION_DIR}/skills"
    else
        vfail "extension dev skill bundle missing task or status skill: ${EXTENSION_DIR}/skills"
    fi
else
    if [ ! -f "${EXTENSION_DIR}/skills/task/SKILL.md" ] && [ ! -f "${EXTENSION_DIR}/skills/status/SKILL.md" ]; then
        vpass "extension stable skill bundle installed without dev-only skills: ${EXTENSION_DIR}/skills"
    else
        vfail "extension stable skill bundle unexpectedly contains dev-only skills: ${EXTENSION_DIR}/skills"
    fi
fi

if [ -f "${EXTENSION_DIR}/GEMINI.md" ] && grep -q "atelier:code" "${EXTENSION_DIR}/GEMINI.md" 2>/dev/null; then
    vpass "extension context installed: ${EXTENSION_DIR}/GEMINI.md"
else
    vfail "extension context missing or no atelier:code persona: ${EXTENSION_DIR}/GEMINI.md"
fi

if gemini extensions list 2>&1 | grep -qi "atelier"; then
    vpass "Gemini lists the atelier extension"
else
    vfail "Gemini extension list does not include atelier"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[atelier:gemini] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Restart Gemini CLI - the Atelier extension contributes context, commands, skills, and MCP wiring."
info "Note: the linked extension source is ${EXTENSION_DIR}; re-run install if the repo path changes."
info "Tip: run 'atelier-status' in any shell to see current run state."
