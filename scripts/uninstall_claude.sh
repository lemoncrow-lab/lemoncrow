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

CLAUDE_SETTINGS="${CLAUDE_SETTINGS_DIR}/settings.json"
CLAUDE_LOCAL_SETTINGS="${CLAUDE_SETTINGS_DIR}/settings.local.json"

info()  { echo "[atelier:uninstall:claude] $*"; }
warn()  { echo "[atelier:uninstall:claude] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

# ---- workspace MCP entry ----------------------------------------------------
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

# ---- PreToolUse hook in settings.json ---------------------------------------
if [ -f "$CLAUDE_SETTINGS" ] && grep -q "Atelier loop required" "$CLAUDE_SETTINGS" 2>/dev/null; then
    if $DRY_RUN; then
        echo "  [dry-run] remove Atelier PreToolUse hook from $CLAUDE_SETTINGS"
    else
        python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$CLAUDE_SETTINGS")
data = json.loads(path.read_text(encoding="utf-8") or "{}")
hooks = data.get("hooks", {})
pre = hooks.get("PreToolUse", [])
pre = [
    entry for entry in pre
    if not (
        entry.get("matcher") == "Edit|Write"
        and any(
            "Atelier loop required" in h.get("command", "")
            for h in entry.get("hooks", [])
        )
    )
]
if pre:
    hooks["PreToolUse"] = pre
else:
    hooks.pop("PreToolUse", None)
if not hooks:
    data.pop("hooks", None)
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print("[atelier:uninstall:claude] Removed Atelier PreToolUse hook from $CLAUDE_SETTINGS")
PYEOF
    fi
fi

# ---- permissions: remove Atelier MCP tools from permissions.allow -----------
if [ -f "$CLAUDE_SETTINGS" ] && grep -q "mcp__atelier__" "$CLAUDE_SETTINGS" 2>/dev/null; then
    if $DRY_RUN; then
        echo "  [dry-run] remove mcp__atelier__* entries from permissions.allow in $CLAUDE_SETTINGS"
    else
        python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$CLAUDE_SETTINGS")
data = json.loads(path.read_text(encoding="utf-8") or "{}")
perms = data.get("permissions", {})
allow = perms.get("allow", [])
filtered = [r for r in allow if not r.startswith("mcp__atelier__")]
if len(filtered) < len(allow):
    if filtered:
        perms["allow"] = filtered
    else:
        perms.pop("allow", None)
    if not perms:
        data.pop("permissions", None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"[atelier:uninstall:claude] Removed {len(allow) - len(filtered)} mcp__atelier__* entries from permissions.allow")
else:
    print("[atelier:uninstall:claude] No mcp__atelier__* entries found in permissions.allow")
PYEOF
    fi
fi

# ---- project-local and user-global agent files ------------------------------
# Claude Code writes plugin agents into the CWD's .claude/agents/ at install time.
# Remove them from the current directory and from ~/.claude/agents/ (user-global).
for agents_dir in ".claude/agents" "${HOME}/.claude/agents"; do
    if [ -d "$agents_dir" ]; then
        for f in "$agents_dir"/atelier-*.md "$agents_dir"/atelier_*.md; do
            [ -f "$f" ] || continue
            run "rm -f '$f'"
            info "Removed agent file: $f"
        done
    fi
done

# ---- statusLine setting in ~/.claude/settings.json --------------------------
if [ -f "${CLAUDE_SETTINGS}" ] && grep -q "atelier" "${CLAUDE_SETTINGS}" 2>/dev/null; then
    if $DRY_RUN; then
        echo "  [dry-run] remove atelier statusLine from ${CLAUDE_SETTINGS}"
    else
        python3 - <<PYEOF2
import json
from pathlib import Path
path = Path("${CLAUDE_SETTINGS}")
data = json.loads(path.read_text(encoding="utf-8") or "{}")
sl = data.get("statusLine", {})
if isinstance(sl, dict) and "atelier" in sl.get("command", ""):
    data.pop("statusLine", None)
    print("[atelier:uninstall:claude] Removed atelier statusLine from ${CLAUDE_SETTINGS}")
if data.get("agent") == "atelier:code":
    data.pop("agent", None)
    print("[atelier:uninstall:claude] Removed atelier-code default agent from ${CLAUDE_SETTINGS}")
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF2
    fi
fi

# ---- plugin uninstall -------------------------------------------------------
CLAUDE_PLUGINS="${HOME}/.claude/plugins"
if ! $WORKSPACE_SET && command -v claude &>/dev/null; then
    if $DRY_RUN; then
        echo "  [dry-run] claude plugin uninstall atelier@atelier"
        echo "  [dry-run] claude plugin marketplace remove atelier"
        echo "  [dry-run] rm -rf ${CLAUDE_PLUGINS}/atelier* ${CLAUDE_PLUGINS}/cache/atelier ${CLAUDE_PLUGINS}/data/atelier-atelier"
    else
        # Remove via CLI (cleans registry entries)
        claude plugin uninstall atelier@atelier 2>/dev/null \
            || claude plugin uninstall atelier 2>/dev/null \
            || true
        claude plugin marketplace remove atelier 2>/dev/null || true
        # CLI removes registry entries but leaves files on disk — remove directly
        rm -rf "${CLAUDE_PLUGINS}/atelier"
        rm -rf "${CLAUDE_PLUGINS}/cache/atelier"
        rm -rf "${CLAUDE_PLUGINS}/data/atelier-atelier"
        # Clean up any timestamped backups the CLI left (e.g. atelier.atelier-backup.*)
        rm -rf "${CLAUDE_PLUGINS}"/atelier.atelier-backup.*
        info "Removed Claude plugin files and cache"
    fi
elif ! $WORKSPACE_SET; then
    warn "claude CLI not found, skipping plugin removal"
fi



info "Done."
