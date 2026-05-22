#!/usr/bin/env bash
# uninstall_opencode.sh - Remove Atelier from opencode
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
    OC_FILE="${WORKSPACE}/opencode.json"
    LEGACY_OC_FILE="${WORKSPACE}/opencode.jsonc"
    AGENT_FILE="${WORKSPACE}/.opencode/agents/atelier.md"
else
    OPENCODE_CONFIG_HOME="${OPENCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-${HOME}/.config}/opencode}"
    OC_FILE="${OPENCODE_CONFIG_HOME}/opencode.json"
    LEGACY_OC_FILE="${OPENCODE_CONFIG_HOME}/opencode.jsonc"
    AGENT_FILE="${OPENCODE_CONFIG_HOME}/agents/atelier.md"
fi

info()  { echo "[atelier:uninstall:opencode] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

clean_config() {
    local path="$1"
    if [ -f "$path" ] && grep -q "atelier" "$path" 2>/dev/null; then
        run "python3 -c '
import json
import re
from pathlib import Path
path = Path(\"$path\")
content = path.read_text(encoding=\"utf-8\")
stripped = re.sub(r\"^\\s*//.*\", \"\", content, flags=re.M)
data = json.loads(stripped) if stripped.strip() else {}
data.get(\"mcp\", {}).pop(\"atelier\", None)
if data.get(\"default_agent\") == \"atelier\":
    data.pop(\"default_agent\", None)
if \"mcp\" in data and not data[\"mcp\"]:
    del data[\"mcp\"]
if not data:
    path.unlink()
else:
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
'"
        info "Removed atelier from $path"
    fi
}

clean_config "$OC_FILE"
clean_config "$LEGACY_OC_FILE"

if [ -f "$AGENT_FILE" ]; then
    run "rm -f '$AGENT_FILE'"
    info "Removed $AGENT_FILE"
fi

info "Done."
