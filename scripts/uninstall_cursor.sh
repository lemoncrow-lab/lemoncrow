#!/usr/bin/env bash
# uninstall_cursor.sh - Remove LemonCrow from Cursor IDE
#
# Options:
#   --workspace DIR  Remove project-local artifacts from DIR instead of global user config
#   --dry-run        Print what would happen, touch nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/python_bootstrap.sh"

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

# Mirrors install_cursor.sh: the server registration is global in every scope,
# so that is the file to clean. A workspace may still carry a stale project
# registration written by an older install -- removed below, since that
# duplicate key is what disconnects the live client mid-session.
MCP_FILE="${HOME}/.cursor/mcp.json"
RULES_DIR=""
STALE_WS_MCP=""
if $WORKSPACE_SET; then
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
    RULES_DIR="${WORKSPACE}/.cursor/rules"
    STALE_WS_MCP="${WORKSPACE}/.cursor/mcp.json"
fi

info()  { echo "[lemoncrow:uninstall:cursor] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

if [ -f "$MCP_FILE" ] && grep -q "lemoncrow" "$MCP_FILE" 2>/dev/null; then
    run "python3 -c '
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
data.get(\"mcpServers\", {}).pop(\"lemoncrow\", None)
if \"mcpServers\" in data and not data[\"mcpServers\"]:
    del data[\"mcpServers\"]
if not data:
    path.unlink()
else:
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
' $(printf %q "$MCP_FILE")"
    info "Removed LemonCrow from $MCP_FILE"
fi

# A workspace may also carry a project-scoped registration written by an older
# install. Uninstall is exactly when removing it is right -- unlike install,
# which leaves an existing one alone.
if [ -n "$STALE_WS_MCP" ] && [ -f "$STALE_WS_MCP" ] && grep -q "lemoncrow" "$STALE_WS_MCP" 2>/dev/null; then
    run "python3 -c '
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
data.get(\"mcpServers\", {}).pop(\"lemoncrow\", None)
if \"mcpServers\" in data and not data[\"mcpServers\"]:
    del data[\"mcpServers\"]
if not data:
    path.unlink()
else:
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
' $(printf %q "$STALE_WS_MCP")"
    info "Removed LemonCrow from $STALE_WS_MCP"
fi

# Hooks: remove the LemonCrow-installed entries (sessionStart + stop) and the
# staged hook scripts, in whichever mode they were installed. Global staging
# lives at ~/.lemoncrow/cursor-hooks; workspace hooks at <ws>/.cursor/hooks.
# Match our staged paths (never a bare "lc" substring, which also mismatched
# our own commands) so a user's own same-named hooks are left untouched.
if $WORKSPACE_SET; then
    CURSOR_HOOKS_FILE="${WORKSPACE}/.cursor/hooks.json"
    HOOKS_STAGING="${WORKSPACE}/.cursor/hooks"
else
    CURSOR_HOOKS_FILE="${HOME}/.cursor/hooks.json"
    HOOKS_STAGING="${HOME}/.lemoncrow/cursor-hooks"
fi
if [ -f "$CURSOR_HOOKS_FILE" ]; then
    run "python3 -c '
import json
import sys
from pathlib import Path
MARKERS = (\"cursor-hooks/\", \"/hooks/session_start.py\", \"/hooks/stop.py\", \"/hooks/before_shell_execution.py\", \"/hooks/before_read_file.py\", \"/hooks/before_tool_use.py\")
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
hooks = data.get(\"hooks\", {})
for event in list(hooks):
    entries = hooks.get(event)
    if isinstance(entries, list):
        hooks[event] = [e for e in entries if not any(m in str(e.get(\"command\", \"\")) for m in MARKERS)]
        if not hooks[event]:
            del hooks[event]
if not hooks:
    data.pop(\"hooks\", None)
if set(data) <= {\"version\"}:
    path.unlink()
else:
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
' $(printf %q "$CURSOR_HOOKS_FILE")"
    info "Removed LemonCrow hooks from $CURSOR_HOOKS_FILE"
fi
if [ -d "$HOOKS_STAGING" ]; then
    run "rm -f $(printf %q "$HOOKS_STAGING/session_start.py") $(printf %q "$HOOKS_STAGING/stop.py") $(printf %q "$HOOKS_STAGING/before_shell_execution.py") $(printf %q "$HOOKS_STAGING/before_read_file.py") $(printf %q "$HOOKS_STAGING/before_tool_use.py")"
    rmdir "$HOOKS_STAGING" 2>/dev/null || true
    info "Removed staged hooks in $HOOKS_STAGING"
fi

if [ -n "$RULES_DIR" ] && [ -d "$RULES_DIR" ]; then
    for f in "$RULES_DIR"/coding-guidelines.mdc "$RULES_DIR"/tool-selection.mdc "$RULES_DIR"/lemoncrow*.mdc; do
        [ -f "$f" ] || continue
        run "rm -f $(printf %q "$f")"
        info "Removed $f"
    done
    rmdir "$RULES_DIR" 2>/dev/null || true
fi

info "Done."
