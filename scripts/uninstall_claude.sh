#!/usr/bin/env bash
# uninstall_claude.sh - Remove Atelier from Claude Code
#
# Options:
#   --workspace DIR  Remove project-local artifacts from DIR instead of global user config
#   --dry-run        Print what would happen, touch nothing

set -euo pipefail

DRY_RUN=false
WORKSPACE=""
WORKSPACE_SET=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
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
    MCP_JSON="${WORKSPACE}/.mcp.json"
    CLAUDE_SETTINGS_DIR="${WORKSPACE}/.claude"
else
    MCP_JSON=""
    CLAUDE_SETTINGS_DIR="${HOME}/.claude"
fi

info()  { echo "[atelier:uninstall:claude] $*"; }
warn()  { echo "[atelier:uninstall:claude] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

if $WORKSPACE_SET; then
    if [ -f "$MCP_JSON" ] && grep -q "atelier" "$MCP_JSON" 2>/dev/null; then
        run "python3 -c '
import json
from pathlib import Path
path = Path(\"$MCP_JSON\")
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
data.get(\"mcpServers\", {}).pop(\"atelier\", None)
path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
'"
        info "Removed atelier MCP entry from $MCP_JSON"
    fi

    CLAUDE_LOCAL_SETTINGS="${CLAUDE_SETTINGS_DIR}/settings.local.json"
    if [ -f "$CLAUDE_LOCAL_SETTINGS" ] && grep -q "CLAUDE_WORKSPACE_ROOT" "$CLAUDE_LOCAL_SETTINGS" 2>/dev/null; then
        run "python3 -c '
import json
from pathlib import Path
path = Path(\"$CLAUDE_LOCAL_SETTINGS\")
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
data.get(\"env\", {}).pop(\"CLAUDE_WORKSPACE_ROOT\", None)
path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
'"
        info "Removed CLAUDE_WORKSPACE_ROOT from $CLAUDE_LOCAL_SETTINGS"
    fi
elif command -v claude &>/dev/null; then
    run "claude mcp remove --scope user atelier 2>/dev/null || true"
    info "Removed atelier MCP server from Claude user scope"
else
    warn "claude CLI not found, skipping user-scope MCP removal"
fi

if ! $WORKSPACE_SET && command -v claude &>/dev/null; then
    if claude plugin list 2>&1 | grep -q "atelier@atelier"; then
        run "claude plugin uninstall atelier@atelier"
        info "Removed Claude plugin atelier@atelier"
    else
        info "No atelier plugin found in Claude Code"
    fi
elif ! $WORKSPACE_SET; then
    warn "claude CLI not found, skipping plugin removal"
fi

info "Done."
