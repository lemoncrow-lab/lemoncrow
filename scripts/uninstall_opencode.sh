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
    AGENTS_DIR="${WORKSPACE}/.opencode/agents"
    PLUGIN_DIR="${WORKSPACE}/.opencode/plugins"
else
    OPENCODE_CONFIG_HOME="${OPENCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-${HOME}/.config}/opencode}"
    OC_FILE="${OPENCODE_CONFIG_HOME}/opencode.json"
    LEGACY_OC_FILE="${OPENCODE_CONFIG_HOME}/opencode.jsonc"
    AGENTS_DIR="${OPENCODE_CONFIG_HOME}/agents"
    PLUGIN_DIR="${OPENCODE_CONFIG_HOME}/plugins"
fi
STAGING_DIR="${HOME}/.atelier/opencode"

info()  { echo "[atelier:uninstall:opencode] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

clean_config() {
    local path="$1"
    if [ -f "$path" ] && grep -q "atelier" "$path" 2>/dev/null; then
        run "python3 -c '
import json
import re
import sys
from pathlib import Path
path = Path(sys.argv[1])
content = path.read_text(encoding=\"utf-8\")
stripped = re.sub(r\"^\\s*//.*\", \"\", content, flags=re.M)
data = json.loads(stripped) if stripped.strip() else {}
data.get(\"mcp\", {}).pop(\"atelier\", None)
data.get(\"provider\", {}).pop(\"atelier\", None)
data.get(\"permission\", {}).pop(\"atelier_*\", None)
if data.get(\"default_agent\") == \"atelier\":
    data.pop(\"default_agent\", None)
for key in (\"mcp\", \"provider\", \"permission\"):
    if key in data and not data[key]:
        del data[key]
if not data:
    path.unlink()
else:
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
' $(printf %q "$path")"
        info "Removed atelier from $path"
    fi
}

clean_config "$OC_FILE"
clean_config "$LEGACY_OC_FILE"

if [ -d "$AGENTS_DIR" ]; then
    for f in "$AGENTS_DIR"/atelier.md "$AGENTS_DIR"/atelier.*.md; do
        [ -f "$f" ] || continue
        run "rm -f $(printf %q "$f")"
        info "Removed $f"
    done
fi

if [ -d "$PLUGIN_DIR" ]; then
    for f in "$PLUGIN_DIR"/atelier-nudge.js "$PLUGIN_DIR"/atelier_nudge.py; do
        [ -f "$f" ] || continue
        run "rm -f $(printf %q "$f")"
        info "Removed $f"
    done
    rmdir "$PLUGIN_DIR" 2>/dev/null || true
fi

if [ -d "$STAGING_DIR" ]; then
    run "rm -rf $(printf %q "$STAGING_DIR")"
    info "Removed $STAGING_DIR"
fi

info "Done."
