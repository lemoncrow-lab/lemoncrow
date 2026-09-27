#!/usr/bin/env bash
# uninstall_hermes.sh - Remove LemonCrow from Hermes Agent
#
# Options:
#   --dry-run  Print what would happen, touch nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/python_bootstrap.sh"

DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --workspace)
            echo "[lemoncrow:uninstall:hermes] WARN: --workspace not supported (Hermes is global-only)" >&2
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
CONFIG_FILE="${HERMES_HOME}/config.yaml"

info()  { echo "[lemoncrow:uninstall:hermes] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
# PyYAML is not guaranteed in the system python; prefer the project env.
if command -v uv >/dev/null 2>&1; then
    PYTHON_CMD="uv run python"
else
    PYTHON_CMD="python3"
fi

if [ -f "$CONFIG_FILE" ] && grep -q "lemoncrow" "$CONFIG_FILE" 2>/dev/null; then
    run "$PYTHON_CMD -c '
import sys
from pathlib import Path
import yaml
path = Path(sys.argv[1])
config = yaml.safe_load(path.read_text(encoding=\"utf-8\")) or {}
config.get(\"mcp_servers\", {}).pop(\"lemoncrow\", None)
if \"mcp_servers\" in config and not config[\"mcp_servers\"]:
    del config[\"mcp_servers\"]
toolsets = config.get(\"platform_toolsets\", {}).get(\"cli\")
if isinstance(toolsets, list):
    config[\"platform_toolsets\"][\"cli\"] = [t for t in toolsets if t != \"mcp-lemoncrow\"]
with path.open(\"w\", encoding=\"utf-8\") as f:
    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
' $(printf %q "$CONFIG_FILE")"
    info "Removed LemonCrow from $CONFIG_FILE"
else
    info "No LemonCrow entries found at $CONFIG_FILE"
fi

info "Done."
