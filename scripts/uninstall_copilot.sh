#!/usr/bin/env bash
# uninstall_copilot.sh - Remove LemonCrow from Copilot
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
    MCP_JSON="${WORKSPACE}/.vscode/mcp.json"
    INSTRUCTIONS="${WORKSPACE}/.github/copilot-instructions.md"
    AGENTS_DIR="${WORKSPACE}/.github/agents"
    TASKS_JSON="${WORKSPACE}/.vscode/tasks.json"
else
    VSCODE_USER_DIR="${VSCODE_USER_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/Code/User}"
    MCP_JSON="${VSCODE_USER_DIR}/mcp.json"
    INSTRUCTIONS="${HOME}/.copilot/instructions/lemoncrow.instructions.md"
    AGENTS_DIR=""
    TASKS_JSON="${VSCODE_USER_DIR}/tasks.json"
    COPILOT_CLI_HOOKS_JSON="${HOME}/.copilot/hooks/hooks.json"
fi

info()  { echo "[lc:uninstall:copilot] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

if [ -f "$MCP_JSON" ] && grep -q "lemoncrow" "$MCP_JSON" 2>/dev/null; then
    run "python3 -c '
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
for key in (\"servers\", \"mcpServers\"):
    data.get(key, {}).pop(\"lemoncrow\", None)
for key in (\"servers\", \"mcpServers\"):
    if key in data and not data[key]:
        del data[key]
if not data:
    path.unlink()
else:
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
' $(printf %q "$MCP_JSON")"
    info "Removed LemonCrow MCP entry from $MCP_JSON"
fi

if [ -f "$INSTRUCTIONS" ] && grep -qi "lemoncrow" "$INSTRUCTIONS" 2>/dev/null; then
    if $WORKSPACE_SET; then
        run "cp $(printf %q "$INSTRUCTIONS") $(printf %q "${INSTRUCTIONS}.lemoncrow-backup.$(date +%Y%m%dT%H%M%S)")"
        run "python3 -c '
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
content = path.read_text(encoding=\"utf-8\")
content = re.sub(r\"\\n?##\\s*LemonCrow[^\\n]*\\n[\\s\\S]*?(?=\\n##\\s|\\Z)\", \"\\n\", content).strip()
path.write_text((content + \"\\n\") if content else \"\", encoding=\"utf-8\")
' $(printf %q "$INSTRUCTIONS")"
        info "Removed LemonCrow section from $INSTRUCTIONS"
    else
        run "rm -f $(printf %q "$INSTRUCTIONS")"
        info "Removed $INSTRUCTIONS"
    fi
fi

if [ -n "$AGENTS_DIR" ] && [ -d "$AGENTS_DIR" ]; then
    run "rm -f $(printf %q "$AGENTS_DIR/lemoncrow.agent.md") $(printf %q "$AGENTS_DIR")/lemoncrow.*.agent.md 2>/dev/null || true"
    info "Removed LemonCrow Copilot agents from $AGENTS_DIR"
fi

if [ -f "$TASKS_JSON" ] && grep -q "LemonCrow:" "$TASKS_JSON" 2>/dev/null; then
    run "python3 -c '
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
data[\"tasks\"] = [t for t in data.get(\"tasks\", []) if not str(t.get(\"label\", \"\")).startswith(\"LemonCrow:\")]
data[\"inputs\"] = [i for i in data.get(\"inputs\", []) if not str(i.get(\"id\", \"\")).startswith(\"lemoncrow\")]
path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
' $(printf %q "$TASKS_JSON")"
    info "Removed LemonCrow task presets from $TASKS_JSON"
fi

if ! $WORKSPACE_SET && [ -f "$COPILOT_CLI_HOOKS_JSON" ] && grep -q "lc" "$COPILOT_CLI_HOOKS_JSON" 2>/dev/null; then
    run "python3 -c '
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
hooks = data.get(\"hooks\", {})
for event, entries in list(hooks.items()):
    filtered = [
        entry for entry in entries
        if \"lc\" not in str(entry.get(\"bash\", \"\")).lower()
    ]
    if filtered:
        hooks[event] = filtered
    else:
        hooks.pop(event, None)
if hooks:
    data[\"hooks\"] = hooks
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
else:
    path.unlink()
' $(printf %q "$COPILOT_CLI_HOOKS_JSON")"
    info "Removed LemonCrow Copilot CLI hooks from $COPILOT_CLI_HOOKS_JSON"
fi


info "Done."
