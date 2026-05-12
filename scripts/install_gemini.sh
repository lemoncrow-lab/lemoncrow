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
EXTENSION_DIR="${ATELIER_REPO}/integrations/gemini/extension"
EXTENSION_MANIFEST="${EXTENSION_DIR}/gemini-extension.json"

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

# ---- check dev mode ---------------------------------------------------------
DEV_MODE="${ATELIER_DEV_MODE:-0}"
STAGING_DIR="${HOME}/.atelier/gemini-extension-slim"
run "mkdir -p '$STAGING_DIR'"
run "cp '${EXTENSION_DIR}/gemini-extension.json' '$STAGING_DIR/'"
run "cp -r '${EXTENSION_DIR}/commands' '$STAGING_DIR/'"
GEMINI_SRC="${ATELIER_REPO}/integrations/gemini/GEMINI.atelier.md"
if [[ "$DEV_MODE" == "1" ]]; then
    info "Dev mode enabled; installing full GEMINI.md with reasoning loop"
    run "cp '${GEMINI_SRC/.md/.dev.md}' '$STAGING_DIR/GEMINI.md'"
else
    info "Dev mode disabled; installing slim GEMINI.md (no skills/reasoning context)"
    run "cp '${GEMINI_SRC}' '$STAGING_DIR/GEMINI.md'"
fi
EXTENSION_DIR="$STAGING_DIR"
EXTENSION_MANIFEST="${EXTENSION_DIR}/gemini-extension.json"
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

if ! command -v atelier-mcp &>/dev/null; then
    if $STRICT; then
        echo "[atelier:gemini] ERROR: 'atelier-mcp' not found on PATH. Install Atelier so the console script is available before enabling the Gemini extension." >&2
        exit 1
    fi
    warn "'atelier-mcp' not found on PATH - SKIPPING. Install Atelier so the console script is available, then rerun this installer."
    exit 0
fi
info "Found atelier-mcp: $(command -v atelier-mcp)"

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
if [ "$COMMAND" = "atelier-mcp" ]; then
    vpass "extension manifest points at atelier-mcp"
else
    vfail "extension manifest does not point at atelier-mcp"
fi

if [ -d "${EXTENSION_DIR}/commands/atelier" ] && [ -f "${EXTENSION_DIR}/commands/atelier/status.toml" ] && [ -f "${EXTENSION_DIR}/commands/atelier/context.toml" ]; then
    vpass "extension command bundle present: ${EXTENSION_DIR}/commands/atelier"
else
    vfail "extension command bundle missing in ${EXTENSION_DIR}/commands/atelier"
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
