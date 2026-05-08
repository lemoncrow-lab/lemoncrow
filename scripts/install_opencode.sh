#!/usr/bin/env bash
# install_opencode.sh - Install Atelier into opencode
#
# What it does:
#   Global mode: installs opencode user config and user agent under ~/.config/opencode.
#   Workspace mode (--workspace DIR): installs project-local opencode artifacts under DIR.
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippet for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if 'opencode' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
ATELIER_WRAPPER="${ATELIER_REPO}/scripts/atelier_mcp_stdio.sh"

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

OPENCODE_CONFIG_HOME="${OPENCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-${HOME}/.config}/opencode}"
if $WORKSPACE_SET; then
    INSTALL_SCOPE="workspace"
    OC_FILE="${WORKSPACE}/opencode.json"
    AGENT_DEST_DIR="${WORKSPACE}/.opencode/agents"
else
    INSTALL_SCOPE="global"
    OC_FILE="${OPENCODE_CONFIG_HOME}/opencode.json"
    AGENT_DEST_DIR="${OPENCODE_CONFIG_HOME}/agents"
fi

info()  { echo "[atelier:opencode] $*"; }
warn()  { echo "[atelier:opencode] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
backup_file() {
    local f="$1"
    if [ -f "$f" ]; then
        local bk="${f}.atelier-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp '$f' '$bk'"
        info "backed up $f -> $bk"
    fi
}

if $WORKSPACE_SET; then
    NEW_ENTRY=$(cat <<JSON
{
  "default_agent": "atelier",
  "mcp": {
    "atelier": {
      "type": "local",
      "command": ["${ATELIER_WRAPPER}"],
      "environment": {
        "ATELIER_WORKSPACE_ROOT": "${WORKSPACE}",
        "ATELIER_ROOT": "${WORKSPACE}/.atelier"
      }
    }
  }
}
JSON
)
else
    NEW_ENTRY=$(cat <<JSON
{
  "default_agent": "atelier",
  "mcp": {
    "atelier": {
      "type": "local",
      "command": ["${ATELIER_WRAPPER}"]
    }
  }
}
JSON
)
fi

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== Atelier opencode - Manual Install ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo "Config target: ${OC_FILE}"
    echo "Agent target: ${AGENT_DEST_DIR}/atelier.md"
    echo ""
    echo "Merge/create config:"
    echo "$NEW_ENTRY"
    exit 0
fi

# ---- check CLI --------------------------------------------------------------
if ! command -v opencode &>/dev/null; then
    if $STRICT; then
        echo "[atelier:opencode] ERROR: 'opencode' not found. Install from https://opencode.ai" >&2
        exit 1
    fi
    warn "'opencode' not found - SKIPPING. Install from https://opencode.ai"
    echo "=== SKIPPED (opencode CLI absent) ==="
    exit 0
fi
info "Found opencode: $(opencode --version 2>/dev/null || echo 'version unknown')"

# ---- merge opencode config --------------------------------------------------
run "mkdir -p '$(dirname "$OC_FILE")'"

if [ -f "$OC_FILE" ]; then
    backup_file "$OC_FILE"
    if $DRY_RUN; then
        echo "  [dry-run] merge atelier into $OC_FILE"
    else
        python3 - <<PYEOF
import json
import re
from pathlib import Path

path = Path('$OC_FILE')
content = path.read_text(encoding='utf-8').strip()
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
existing = json.loads(stripped) if stripped.strip() else {}
new_entry = json.loads('''$NEW_ENTRY''')
existing.setdefault('mcp', {}).update(new_entry['mcp'])
existing.setdefault('default_agent', new_entry['default_agent'])
path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
print("[atelier:opencode] merged atelier entry into $OC_FILE")
PYEOF
    fi
else
    if $DRY_RUN; then
        echo "  [dry-run] create $OC_FILE"
    else
        echo "$NEW_ENTRY" > "$OC_FILE"
        info "created $OC_FILE"
    fi
fi

# ---- install opencode atelier agent ----------------------------------------
AGENT_SRC="${ATELIER_REPO}/integrations/opencode/agents/atelier.md"
if [ -f "$AGENT_SRC" ]; then
    run "mkdir -p '$AGENT_DEST_DIR'"
    run "cp -f '$AGENT_SRC' '$AGENT_DEST_DIR/atelier.md'"
    info "atelier agent installed -> $AGENT_DEST_DIR/atelier.md"
else
    warn "agent source missing: $AGENT_SRC"
fi

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[atelier:opencode] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$OC_FILE" ]; then
    HAS=$(python3 - <<PYEOF
import json
import re
from pathlib import Path

content = Path('$OC_FILE').read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    print('yes' if 'atelier' in d.get('mcp', {}) else 'no')
except Exception:
    print('parse-error')
PYEOF
)
    if [ "$HAS" = "yes" ]; then
        vpass "opencode config contains atelier MCP entry ($OC_FILE)"
    elif [ "$HAS" = "parse-error" ]; then
        vfail "opencode config parse error: $OC_FILE"
    else
        vfail "opencode config missing atelier entry"
    fi

    DEFAULT_AGENT=$(python3 - <<PYEOF
import json
import re
from pathlib import Path

content = Path('$OC_FILE').read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    print(d.get('default_agent', ''))
except Exception:
    print('')
PYEOF
)
    if [ "$DEFAULT_AGENT" = "atelier" ]; then
        vpass "opencode default_agent = atelier"
    else
        vfail "opencode default_agent is '$DEFAULT_AGENT' (expected 'atelier')"
    fi
else
    vfail "opencode config not found: $OC_FILE"
fi

AGENT_FILE="${AGENT_DEST_DIR}/atelier.md"
if [ -f "$AGENT_FILE" ]; then
    vpass "opencode atelier agent installed: $AGENT_FILE"
else
    vfail "opencode atelier agent missing: $AGENT_FILE"
fi

if [ -x "${ATELIER_WRAPPER}" ]; then
    vpass "atelier_mcp_stdio.sh exists and is executable"
else
    vfail "atelier_mcp_stdio.sh missing or not executable: ${ATELIER_WRAPPER}"
fi

if [ -x "${ATELIER_REPO}/bin/atelier-status" ]; then
    vpass "bin/atelier-status helper exists"
else
    vfail "bin/atelier-status missing or not executable"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[atelier:opencode] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Restart opencode - Atelier agent and MCP are available."
info "Tip: run 'atelier-status' in any shell to see current run state."
