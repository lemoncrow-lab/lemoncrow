#!/usr/bin/env bash
# install_copilot.sh — Install LemonCrow into Copilot Chat
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
# Note: Copilot does not have a standalone CLI; 'code' (VS Code) is
# used as the proxy check. If 'code' is absent, gracefully skip.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
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

VSCODE_USER_DIR="${VSCODE_USER_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/Code/User}"
if $WORKSPACE_SET; then
    INSTALL_SCOPE="workspace"
    VSCODE_DIR="${WORKSPACE}/.vscode"
    MCP_JSON="${VSCODE_DIR}/mcp.json"
    INSTRUCTIONS="${WORKSPACE}/.github/copilot-instructions.md"
    AGENTS_DEST_DIR="${WORKSPACE}/.github/agents"
    AGENT_VERIFY="${AGENTS_DEST_DIR}/lemoncrow.code.agent.md"
    TASKS_DEST="${WORKSPACE}/.vscode/tasks.json"
else
    INSTALL_SCOPE="global"
    VSCODE_DIR="${VSCODE_USER_DIR}"
    MCP_JSON="${VSCODE_DIR}/mcp.json"
    INSTRUCTIONS="${HOME}/.copilot/instructions/lemoncrow.instructions.md"
    AGENTS_DEST_DIR=""
    AGENT_VERIFY=""
    TASKS_DEST="${VSCODE_USER_DIR}/tasks.json"
fi

info()  { [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]] && echo "[lemon:copilot] $*" || true; }
warn()  { echo "[lemon:copilot] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
backup_file() {
    local f="$1"
    if $WORKSPACE_SET; then
        return
    fi
    if [ -f "$f" ]; then
        local bk="${f}.lemoncrow-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp $(printf %q "$f") $(printf %q "$bk")"
        info "backed up $f → $bk"
    fi
}

# ---- check VS Code ----------------------------------------------------------
if ! command -v code &>/dev/null; then
    if $STRICT; then
        echo "[lemon:copilot] ERROR: 'code' (VS Code) not found on PATH." >&2
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
      "lemoncrow": {
        "type": "stdio",
        "command": "lemon",
        "args": ["mcp", "--host", "copilot"],
        "env": {
          "LEMONCROW_WORKSPACE_ROOT": "${WORKSPACE}"
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
    "lemoncrow": {
      "type": "stdio",
      "command": "lemon",
      "args": ["mcp", "--host", "copilot"]
    }
  }
}
JSON
)
fi

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== LemonCrow Copilot - Manual Install Steps ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo ""
    echo "1. Create/merge ${MCP_JSON}:"
    echo "$NEW_ENTRY"
    echo ""
    echo "2. Append LemonCrow instructions to ${INSTRUCTIONS}:"
    echo "   (contents of ${LEMONCROW_REPO}/integrations/copilot/COPILOT_INSTRUCTIONS.lemoncrow.md)"
    if $WORKSPACE_SET; then
        echo ""
        echo "3. Project Copilot role agents into ${AGENTS_DEST_DIR}:"
        echo "   (lemoncrow.code.agent.md, lemoncrow.execute.agent.md, ... from workspace settings)"
    fi
    echo ""
    echo "Tasks target: ${TASKS_DEST}"
    echo "Reload VS Code window: Ctrl+Shift+P -> 'Developer: Reload Window'"
    exit 0
fi

# ---- write VS Code MCP ------------------------------------------------------
run "mkdir -p $(printf %q "$VSCODE_DIR")"

if [ -f "$MCP_JSON" ]; then
    backup_file "$MCP_JSON"
    if $DRY_RUN; then
        echo "  [dry-run] merge LemonCrow into $MCP_JSON"
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
print("[lemon:copilot] merged LemonCrow into $MCP_JSON")
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
LEMONCROW_INSTRUCTIONS="${LEMONCROW_REPO}/integrations/copilot/COPILOT_INSTRUCTIONS.lemoncrow.md"

STAGING_DIR="${HOME}/.lemoncrow/copilot"
run "mkdir -p $(printf %q "$STAGING_DIR")"
COPILOT_SRC="${LEMONCROW_REPO}/integrations/copilot/COPILOT_INSTRUCTIONS.lemoncrow.md"
info "Staging Copilot instructions"
lemoncrow_write_managed_copy "${COPILOT_SRC}" "$STAGING_DIR/instructions.md" "$DRY_RUN"
LEMONCROW_INSTRUCTIONS="$STAGING_DIR/instructions.md"

if [ -f "$LEMONCROW_INSTRUCTIONS" ]; then
    run "mkdir -p $(printf %q "$(dirname "$INSTRUCTIONS")")"
    if [ -f "$INSTRUCTIONS" ]; then
        backup_file "$INSTRUCTIONS"
        lemoncrow_upsert_managed_block "$LEMONCROW_INSTRUCTIONS" "$INSTRUCTIONS" "$DRY_RUN"
        info "merged LemonCrow instructions into $INSTRUCTIONS"
    elif $WORKSPACE_SET; then
        if $DRY_RUN; then
            lemoncrow_write_managed_copy "$LEMONCROW_INSTRUCTIONS" "$INSTRUCTIONS" "true"
        else
            run "cp $(printf %q "$LEMONCROW_INSTRUCTIONS") $(printf %q "$INSTRUCTIONS")"
        fi
        info "created $INSTRUCTIONS"
    else
        if $DRY_RUN; then
            echo "  [dry-run] create $INSTRUCTIONS with Copilot instructions frontmatter"
        else
            {
                echo "---"
                echo 'applyTo: "**"'
                echo "---"
            } > "$INSTRUCTIONS"
            lemoncrow_upsert_managed_block "$LEMONCROW_INSTRUCTIONS" "$INSTRUCTIONS" "false"
            info "created $INSTRUCTIONS"
        fi
    fi
else
    warn "instructions source missing: $LEMONCROW_INSTRUCTIONS"
fi

# ---- install workspace Copilot agents --------------------------------------
if $WORKSPACE_SET; then
    if $DRY_RUN; then
        echo "  [dry-run] project Copilot role agents into ${AGENTS_DEST_DIR}"
    else
        PYTHONPATH="${LEMONCROW_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" python3 - <<PYEOF
from pathlib import Path
from lemoncrow.core.capabilities.workspace_host_overrides import write_workspace_copilot_agents

written = write_workspace_copilot_agents(Path("${WORKSPACE}"), repo_root=Path("${LEMONCROW_REPO}"))
print(f"[lemon:copilot] projected {len(written)} Copilot role agents into ${AGENTS_DEST_DIR}")
PYEOF
    fi
else
    info "global agent install skipped; use --workspace DIR for project agents"
fi

# ---- merge VS Code task presets --------------------------------------------
TASKS_SRC="${LEMONCROW_REPO}/integrations/copilot/tasks.json"

if [ -f "$TASKS_SRC" ]; then
    if [ -f "$TASKS_DEST" ]; then
        backup_file "$TASKS_DEST"
        if $DRY_RUN; then
            echo "  [dry-run] merge LemonCrow task presets into $TASKS_DEST"
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
print('[lemon:copilot] merged LemonCrow task presets into ' + str(dest))
PYEOF
        fi
    else
        run "mkdir -p $(printf %q "$(dirname "$TASKS_DEST")")"
        run "cp $(printf %q "$TASKS_SRC") $(printf %q "$TASKS_DEST")"
        info "created VS Code tasks preset: $TASKS_DEST"
    fi
else
    warn "task preset source missing: $TASKS_SRC"
fi


if $WORKSPACE_SET; then
    lemoncrow_install_attribution_hook "$WORKSPACE" "$DRY_RUN"
fi

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[lemon:copilot] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$MCP_JSON" ]; then
    HAS=$(python3 -c "
import json
d = json.load(open('$MCP_JSON'))
servers = d.get('servers', d.get('mcpServers', {}))
print('yes' if 'lemoncrow' in servers else 'no')
" 2>/dev/null || echo "error")
    if [ "$HAS" = "yes" ]; then
        vpass "$MCP_JSON contains LemonCrow server entry"
    else
        vfail "$MCP_JSON missing LemonCrow entry"
    fi
else
    vfail "$MCP_JSON missing"
fi

if [ -f "$INSTRUCTIONS" ] && grep -q -i "lemoncrow" "$INSTRUCTIONS" 2>/dev/null; then
    vpass "$INSTRUCTIONS references LemonCrow"
else
    vfail "$INSTRUCTIONS missing or no LemonCrow reference"
fi

if command -v lemon &>/dev/null; then
    vpass "lemon is available on PATH"
else
    vfail "lemon NOT found on PATH"
fi

if $WORKSPACE_SET; then
    if [ -f "$AGENT_VERIFY" ]; then
        vpass "Copilot agents installed in: $AGENTS_DEST_DIR"
    else
        vfail "Copilot baseline agent missing: $AGENT_VERIFY"
    fi
else
    vpass "global install does not write project agent"
fi

if [ -f "$TASKS_DEST" ] && grep -q "LemonCrow: Copilot Preflight" "$TASKS_DEST" 2>/dev/null; then
    vpass "LemonCrow VS Code task presets installed in $TASKS_DEST"
else
    vfail "$TASKS_DEST missing LemonCrow task presets"
fi

if command -v lemon >/dev/null 2>&1 && lemon status --help >/dev/null 2>&1; then
    vpass "lemon status command is available"
else
    vfail "lemon status command unavailable"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[lemon:copilot] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Reload VS Code window - LemonCrow MCP and tasks are available."
info "Tip: run 'lemon status' in any shell to see the runs dashboard."
