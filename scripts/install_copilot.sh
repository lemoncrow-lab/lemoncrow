#!/usr/bin/env bash
# install_copilot.sh — Install Atelier into VS Code Copilot Chat
#
# What it does:
#   Global mode: installs VS Code MCP/user instructions in the user profile.
#   Workspace mode (--workspace DIR): installs project-local Copilot artifacts under DIR.
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print exact manual steps, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if 'code' CLI not on PATH
#
# Note: VS Code Copilot does not have a standalone CLI; 'code' (VS Code) is
# used as the proxy check. If 'code' is absent, gracefully skip.

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

VSCODE_USER_DIR="${VSCODE_USER_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/Code/User}"
if $WORKSPACE_SET; then
    INSTALL_SCOPE="workspace"
    VSCODE_DIR="${WORKSPACE}/.vscode"
    MCP_JSON="${VSCODE_DIR}/mcp.json"
    INSTRUCTIONS="${WORKSPACE}/.github/copilot-instructions.md"
    CHATMODE_DEST="${WORKSPACE}/.github/chatmodes/atelier.chatmode.md"
    TASKS_DEST="${WORKSPACE}/.vscode/tasks.json"
else
    INSTALL_SCOPE="global"
    VSCODE_DIR="${VSCODE_USER_DIR}"
    MCP_JSON="${VSCODE_DIR}/mcp.json"
    INSTRUCTIONS="${HOME}/.copilot/instructions/atelier.instructions.md"
    CHATMODE_DEST=""
    TASKS_DEST="${VSCODE_USER_DIR}/tasks.json"
fi

info()  { echo "[atelier:copilot] $*"; }
warn()  { echo "[atelier:copilot] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
backup_file() {
    local f="$1"
    if [ -f "$f" ]; then
        local bk="${f}.atelier-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp '$f' '$bk'"
        info "backed up $f → $bk"
    fi
}

# ---- check VS Code ----------------------------------------------------------
if ! command -v code &>/dev/null; then
    if $STRICT; then
        echo "[atelier:copilot] ERROR: 'code' (VS Code) not found on PATH." >&2
        exit 1
    fi
    warn "'code' (VS Code) not found — SKIPPING."
    warn "Install VS Code from https://code.visualstudio.com then run: make install-copilot"
    echo "=== SKIPPED (code CLI absent) ==="
    exit 0
fi
info "Found VS Code: $(code --version 2>/dev/null | head -1 || echo 'version unknown')"

# ---- MCP entry --------------------------------------------------------------
if $WORKSPACE_SET; then
    NEW_ENTRY=$(cat <<JSON
{
  "servers": {
    "atelier": {
      "type": "stdio",
      "command": "${ATELIER_WRAPPER}",
      "args": [],
      "env": {
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
  "servers": {
    "atelier": {
      "type": "stdio",
      "command": "${ATELIER_WRAPPER}",
      "args": []
    }
  }
}
JSON
)
fi

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== Atelier VS Code Copilot - Manual Install Steps ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo ""
    echo "1. Create/merge ${MCP_JSON}:"
    echo "$NEW_ENTRY"
    echo ""
    echo "2. Append Atelier instructions to ${INSTRUCTIONS}:"
    echo "   (contents of ${ATELIER_REPO}/integrations/copilot/COPILOT_INSTRUCTIONS.atelier.md)"
    if $WORKSPACE_SET; then
        echo ""
        echo "3. Copy Copilot chat mode to ${CHATMODE_DEST}:"
        echo "   (contents of ${ATELIER_REPO}/integrations/copilot/chatmodes/atelier.chatmode.md)"
    fi
    echo ""
    echo "Tasks target: ${TASKS_DEST}"
    echo "Reload VS Code window: Ctrl+Shift+P -> 'Developer: Reload Window'"
    exit 0
fi

# ---- write VS Code MCP ------------------------------------------------------
run "mkdir -p '$VSCODE_DIR'"

if [ -f "$MCP_JSON" ]; then
    backup_file "$MCP_JSON"
    if $DRY_RUN; then
        echo "  [dry-run] merge atelier into $MCP_JSON"
    else
        python3 - <<PYEOF
import json
from pathlib import Path

path = Path('$MCP_JSON')
existing = json.loads(path.read_text(encoding='utf-8') or '{}')
new_entry = json.loads('''$NEW_ENTRY''')
server_key = 'servers' if 'servers' in existing or 'mcpServers' not in existing else 'mcpServers'
existing.setdefault(server_key, {}).update(new_entry['servers'])
path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
print("[atelier:copilot] merged atelier into $MCP_JSON")
PYEOF
    fi
else
    if $DRY_RUN; then
        echo "  [dry-run] create $MCP_JSON"
    else
        echo "$NEW_ENTRY" > "$MCP_JSON"
        info "created $MCP_JSON"
    fi
fi

# ---- install Copilot instructions ------------------------------------------
ATELIER_INSTRUCTIONS="${ATELIER_REPO}/integrations/copilot/COPILOT_INSTRUCTIONS.atelier.md"

# ---- check dev mode ---------------------------------------------------------
DEV_MODE="${ATELIER_DEV_MODE:-0}"
if [[ "$DEV_MODE" != "1" ]]; then
    info "Dev mode disabled; using slim instructions"
    STAGING_DIR="${ATELIER_REPO}/.atelier/copilot-slim"
    run "mkdir -p '$STAGING_DIR'"
    # Create neutral instructions
    if ! $DRY_RUN; then
        cat > "$STAGING_DIR/instructions.md" <<EOF
# Atelier

Atelier is currently in **Passive Mode**. Active reasoning features are disabled.
To enable active reasoning, set ATELIER_DEV_MODE=1 and re-run install.
EOF
    fi
    ATELIER_INSTRUCTIONS="$STAGING_DIR/instructions.md"
fi

if [ -f "$ATELIER_INSTRUCTIONS" ]; then
    run "mkdir -p '$(dirname "$INSTRUCTIONS")'"
    if [ -f "$INSTRUCTIONS" ]; then
        if grep -q "Atelier.*Copilot Instructions" "$INSTRUCTIONS" 2>/dev/null; then
            info "$INSTRUCTIONS already contains Atelier section - skipping"
        else
            backup_file "$INSTRUCTIONS"
            if $DRY_RUN; then
                echo "  [dry-run] append Atelier section to $INSTRUCTIONS"
            else
                echo "" >> "$INSTRUCTIONS"
                cat "$ATELIER_INSTRUCTIONS" >> "$INSTRUCTIONS"
                info "appended Atelier instructions to $INSTRUCTIONS"
            fi
        fi
    elif $WORKSPACE_SET; then
        run "cp '$ATELIER_INSTRUCTIONS' '$INSTRUCTIONS'"
        info "created $INSTRUCTIONS"
    else
        if $DRY_RUN; then
            echo "  [dry-run] create $INSTRUCTIONS with Copilot instructions frontmatter"
        else
            {
                echo "---"
                echo 'applyTo: "**"'
                echo "---"
                echo ""
                cat "$ATELIER_INSTRUCTIONS"
            } > "$INSTRUCTIONS"
            info "created $INSTRUCTIONS"
        fi
    fi
else
    warn "instructions source missing: $ATELIER_INSTRUCTIONS"
fi

# ---- install workspace Copilot chat mode -----------------------------------
CHATMODE_SRC="${ATELIER_REPO}/integrations/copilot/chatmodes/atelier.chatmode.md"
if $WORKSPACE_SET; then
    if [ -f "$CHATMODE_SRC" ]; then
        run "mkdir -p '$(dirname "$CHATMODE_DEST")'"
        if [ -f "$CHATMODE_DEST" ]; then
            info "$CHATMODE_DEST already exists - not overwriting"
        else
            run "cp '$CHATMODE_SRC' '$CHATMODE_DEST'"
            info "created chat mode: $CHATMODE_DEST"
        fi
    else
        warn "chat mode source missing: $CHATMODE_SRC"
    fi
else
    info "global chat mode install skipped; use --workspace DIR for project chat modes"
fi

# ---- merge VS Code task presets --------------------------------------------
TASKS_SRC="${ATELIER_REPO}/integrations/copilot/tasks.json"

if [ -f "$TASKS_SRC" ]; then
    if [ -f "$TASKS_DEST" ]; then
        backup_file "$TASKS_DEST"
        if $DRY_RUN; then
            echo "  [dry-run] merge Atelier task presets into $TASKS_DEST"
        else
            python3 - <<PYEOF
import json
from pathlib import Path

dest = Path('$TASKS_DEST')
src = Path('$TASKS_SRC')
existing = json.loads(dest.read_text(encoding='utf-8') or '{}')
incoming = json.loads(src.read_text(encoding='utf-8'))

existing.setdefault('version', '2.0.0')
existing_tasks = existing.setdefault('tasks', [])
existing_inputs = existing.setdefault('inputs', [])

existing_labels = {str(t.get('label')) for t in existing_tasks if isinstance(t, dict)}
for task in incoming.get('tasks', []):
    if task.get('label') not in existing_labels:
        existing_tasks.append(task)

existing_input_ids = {str(i.get('id')) for i in existing_inputs if isinstance(i, dict)}
for item in incoming.get('inputs', []):
    if item.get('id') not in existing_input_ids:
        existing_inputs.append(item)

dest.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
print('[atelier:copilot] merged Atelier task presets into ' + str(dest))
PYEOF
        fi
    else
        run "mkdir -p '$(dirname "$TASKS_DEST")'"
        run "cp '$TASKS_SRC' '$TASKS_DEST'"
        info "created VS Code tasks preset: $TASKS_DEST"
    fi
else
    warn "task preset source missing: $TASKS_SRC"
fi

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[atelier:copilot] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$MCP_JSON" ]; then
    HAS=$(python3 -c "
import json
d = json.load(open('$MCP_JSON'))
servers = d.get('servers', d.get('mcpServers', {}))
print('yes' if 'atelier' in servers else 'no')
" 2>/dev/null || echo "error")
    if [ "$HAS" = "yes" ]; then
        vpass "$MCP_JSON contains atelier server entry"
    else
        vfail "$MCP_JSON missing atelier entry"
    fi
else
    vfail "$MCP_JSON missing"
fi

if [ -f "$INSTRUCTIONS" ] && grep -q -i "atelier" "$INSTRUCTIONS" 2>/dev/null; then
    vpass "$INSTRUCTIONS references Atelier"
else
    vfail "$INSTRUCTIONS missing or no Atelier reference"
fi

if [ -x "${ATELIER_WRAPPER}" ]; then
    vpass "atelier_mcp_stdio.sh exists and is executable"
else
    vfail "atelier_mcp_stdio.sh missing or not executable: ${ATELIER_WRAPPER}"
fi

if $WORKSPACE_SET; then
    if [ -f "$CHATMODE_DEST" ]; then
        vpass "Copilot chat mode installed: $CHATMODE_DEST"
    else
        vfail "Copilot chat mode missing: $CHATMODE_DEST"
    fi
else
    vpass "global install does not write project chat mode"
fi

if [ -f "$TASKS_DEST" ] && grep -q "Atelier: Check Plan" "$TASKS_DEST" 2>/dev/null; then
    vpass "Atelier VS Code task presets installed in $TASKS_DEST"
else
    vfail "$TASKS_DEST missing Atelier task presets"
fi

if [ -x "${ATELIER_REPO}/bin/atelier-status" ]; then
    vpass "bin/atelier-status helper exists"
else
    vfail "bin/atelier-status missing or not executable"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[atelier:copilot] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Reload VS Code window - Atelier MCP and tasks are available."
info "Tip: run 'atelier-status' in any shell to see current run state."
