#!/usr/bin/env bash
# install_cursor.sh - Install Atelier into Cursor IDE
#
# What it does:
#   Global mode: adds atelier to ~/.cursor/mcp.json.
#   Workspace mode (--workspace DIR): adds atelier to DIR/.cursor/mcp.json
#   and writes a rules file at DIR/.cursor/rules/atelier.mdc.
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippet for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if cursor CLI not on PATH (heuristic: ~/.cursor exists)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

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
    MCP_FILE="${WORKSPACE}/.cursor/mcp.json"
    RULES_DIR="${WORKSPACE}/.cursor/rules"
else
    INSTALL_SCOPE="global"
    MCP_FILE="${HOME}/.cursor/mcp.json"
    RULES_DIR=""
fi

CURSOR_RULES_SRC_DIR="${ATELIER_REPO}/integrations/cursor/rules"

info()  { [[ "${ATELIER_VERBOSE:-0}" == "1" ]] && echo "[atelier:cursor] $*" || true; }
warn()  { echo "[atelier:cursor] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
backup_file() {
    local f="$1"
    if $WORKSPACE_SET; then
        return
    fi
    if [ -f "$f" ]; then
        local bk="${f}.atelier-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp '$f' '$bk'"
        info "backed up $f -> $bk"
    fi
}

MCP_ENTRY=$(cat <<JSON
{
  "mcpServers": {
    "atelier": {
      "type": "stdio",
      "command": "atelier-mcp",
      "args": ["--host", "cursor"],
      "alwaysAllow": ["code","compact","context","edit","grep","memory","read","rescue","route","search","shell","sql","trace","verify"]
    }
  }
}
JSON
)

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== Atelier Cursor - Manual Install ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo "MCP config target: ${MCP_FILE}"
    echo ""
    echo "Merge/create config:"
    echo "$MCP_ENTRY"
    if $WORKSPACE_SET; then
        echo ""
        echo "Copy workspace rules into ${RULES_DIR}:"
        echo "  - ${CURSOR_RULES_SRC_DIR}/coding-guidelines.mdc"
        echo "  - ${CURSOR_RULES_SRC_DIR}/tool-selection.mdc"
    fi
    exit 0
fi

# ---- check cursor installation ----------------------------------------------
if [ ! -d "${HOME}/.cursor" ] && ! $WORKSPACE_SET && [ ! -f "$MCP_FILE" ]; then
    if $STRICT; then
        echo "[atelier:cursor] ERROR: ~/.cursor not found. Is Cursor installed?" >&2
        exit 1
    fi
    warn "~/.cursor not found - SKIPPING. Install Cursor from https://cursor.com"
    echo "=== SKIPPED (Cursor not detected) ==="
    exit 0
fi
info "Found Cursor config dir"

# ---- merge MCP config -------------------------------------------------------
run "mkdir -p '$(dirname "$MCP_FILE")'"

if [ -f "$MCP_FILE" ]; then
    backup_file "$MCP_FILE"
    if $DRY_RUN; then
        echo "  [dry-run] merge atelier into $MCP_FILE"
    else
        python3 - <<PYEOF
import json
from pathlib import Path

path = Path('$MCP_FILE')
content = path.read_text(encoding='utf-8').strip()
if content:
    existing = json.loads(content)
else:
    existing = {}
existing.setdefault('mcpServers', {}).update({
    'atelier': {
        'type': 'stdio',
        'command': 'atelier-mcp',
        'args': ['--host', 'cursor'],
        'alwaysAllow': ['code','compact','context','edit','grep','memory','read','rescue','route','search','shell','sql','trace','verify'],
    }
})
path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
print("[atelier:cursor] merged atelier entry into $MCP_FILE")
PYEOF
    fi
else
    if $DRY_RUN; then
        echo "  [dry-run] create $MCP_FILE"
    else
        echo "$MCP_ENTRY" > "$MCP_FILE"
        info "created $MCP_FILE"
    fi
fi

# ---- write rules files (workspace only) -------------------------------------
if $WORKSPACE_SET; then
    if $DRY_RUN; then
        echo "  [dry-run] copy Cursor rules into $RULES_DIR"
    else
        run "mkdir -p '$RULES_DIR'"
        if compgen -G "${CURSOR_RULES_SRC_DIR}/*.mdc" > /dev/null; then
            for src in "${CURSOR_RULES_SRC_DIR}"/*.mdc; do
                dest="${RULES_DIR}/$(basename "$src")"
                run "cp '$src' '$dest'"
                info "installed rule -> $dest"
            done
        else
            warn "no Cursor rule sources found in ${CURSOR_RULES_SRC_DIR}"
        fi
    fi
fi

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[atelier:cursor] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$MCP_FILE" ]; then
    HAS=$(python3 - <<PYEOF
import json
from pathlib import Path
try:
    d = json.loads(Path('$MCP_FILE').read_text(encoding='utf-8'))
    print('yes' if 'atelier' in d.get('mcpServers', {}) else 'no')
except Exception:
    print('parse-error')
PYEOF
)
    if [ "$HAS" = "yes" ]; then
        vpass "Cursor MCP config contains atelier entry ($MCP_FILE)"
    elif [ "$HAS" = "parse-error" ]; then
        vfail "Cursor MCP config parse error: $MCP_FILE"
    else
        vfail "Cursor MCP config missing atelier entry"
    fi
else
    vfail "Cursor MCP config not found: $MCP_FILE"
fi

if $WORKSPACE_SET; then
    if compgen -G "${RULES_DIR}/*.mdc" > /dev/null; then
        vpass "Cursor rules installed under $RULES_DIR"
    else
        vfail "Cursor rules missing under $RULES_DIR"
    fi
fi

if command -v atelier-mcp &>/dev/null; then
    vpass "atelier-mcp is available on PATH"
else
    vfail "atelier-mcp NOT found on PATH"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[atelier:cursor] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Restart Cursor for MCP changes to take effect."
info "Tip: run 'atelier status' in any shell to see the runs dashboard."
