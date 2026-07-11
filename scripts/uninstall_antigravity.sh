#!/usr/bin/env bash
# uninstall_antigravity.sh - Remove LemonCrow from Antigravity / agy
#
# Options:
#   --workspace DIR  Remove project-local artifacts from DIR instead of user config
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
fi

ANTIGRAVITY_USER_DIR="${ANTIGRAVITY_USER_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/Antigravity/User}"
if $WORKSPACE_SET; then
    MCP_JSON="${WORKSPACE}/.vscode/mcp.json"
else
    MCP_JSON="${ANTIGRAVITY_USER_DIR}/mcp.json"
fi

info()  { echo "[lemon:uninstall:antigravity] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || "$@"; }

if [ -f "$MCP_JSON" ]; then
    if $DRY_RUN; then
        echo "  [dry-run] remove LemonCrow server from $MCP_JSON"
    else
        python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$MCP_JSON")
data = json.loads(path.read_text(encoding="utf-8") or "{}")
server_key = "servers" if "servers" in data else "mcpServers"
servers = data.get(server_key, {})
if isinstance(servers, dict):
    servers.pop("lemoncrow", None)
if servers:
    data[server_key] = servers
else:
    data.pop(server_key, None)
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print("[lemon:uninstall:antigravity] updated $MCP_JSON")
PYEOF
    fi
fi

info "Done."
