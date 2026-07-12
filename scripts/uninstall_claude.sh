#!/usr/bin/env bash
# uninstall_claude.sh - Remove LemonCrow from Claude Code
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
CLAUDE_STAGING_DIR="${HOME}/.lemoncrow/claude-plugin"

info()  { echo "[lc:uninstall:claude] $*"; }
warn()  { echo "[lc:uninstall:claude] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

# ---- workspace MCP entry ----------------------------------------------------
if $WORKSPACE_SET; then
    if [ -f "$MCP_JSON" ] && grep -qE "lc" "$MCP_JSON" 2>/dev/null; then
        run "python3 -c '
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
data.get(\"mcpServers\", {}).pop(\"lc\", None)
path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
' $(printf %q "$MCP_JSON")"
        info "Removed LemonCrow MCP entry from $MCP_JSON"
    fi

    if [ -f "$CLAUDE_LOCAL_SETTINGS" ] && grep -qE "CLAUDE_WORKSPACE_ROOT|lc:code|lemoncrow:code" "$CLAUDE_LOCAL_SETTINGS" 2>/dev/null; then
        run "python3 -c '
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
env = data.get(\"env\")
if isinstance(env, dict):
    env.pop(\"CLAUDE_WORKSPACE_ROOT\", None)
    if not env:
        data.pop(\"env\", None)
if data.get(\"agent\") in (\"lc:code\", \"lemoncrow:code\"):
    data.pop(\"agent\", None)
if data:
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
else:
    path.unlink()
' $(printf %q "$CLAUDE_LOCAL_SETTINGS")"
        info "Removed LemonCrow workspace settings from $CLAUDE_LOCAL_SETTINGS"
    fi
elif command -v claude &>/dev/null; then
    run "claude mcp remove --scope user lc 2>/dev/null || true"
    info "Removed LemonCrow MCP server from Claude user scope"
else
    warn "claude CLI not found, skipping user-scope MCP removal"
fi

# ---- PreToolUse hook in settings.json ---------------------------------------
if [ -f "$CLAUDE_SETTINGS" ] && grep -q "LemonCrow loop required" "$CLAUDE_SETTINGS" 2>/dev/null; then
    if $DRY_RUN; then
        echo "  [dry-run] remove LemonCrow PreToolUse hook from $CLAUDE_SETTINGS"
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
            "LemonCrow loop required" in h.get("command", "")
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
print("[lc:uninstall:claude] Removed LemonCrow PreToolUse hook from $CLAUDE_SETTINGS")
PYEOF
    fi
fi

# ---- permissions: remove LemonCrow-installed permission entries ---------------
if [ -f "$CLAUDE_SETTINGS" ]; then
    if $DRY_RUN; then
        echo "  [dry-run] remove LemonCrow permission entries from $CLAUDE_SETTINGS"
    else
        python3 - <<PYEOF
import json
from pathlib import Path

path = Path("$CLAUDE_SETTINGS")
data = json.loads(path.read_text(encoding="utf-8") or "{}")
perms = data.get("permissions", {})
allow = perms.get("allow", [])
deny = perms.get("deny", [])
lc_bash_allows = {
    "Bash(git *)", "Bash(gh *)", "Bash(uv run pytest *)", "Bash(uv run python *)",
    "Bash(uv run mypy *)", "Bash(uv run ruff *)", "Bash(uv run lc *)",
    "Bash(uv run uvicorn *)", "Bash(uv sync *)", "Bash(uv add *)", "Bash(uv pip *)",
    "Bash(uv lock *)", "Bash(npm run *)", "Bash(npm install *)", "Bash(npm test *)",
    "Bash(npx tsc *)", "Bash(make *)", "Bash(docker-compose *)", "Bash(docker compose *)",
}
lemoncrow_denies = {"Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}
filtered_allow = [r for r in allow if not (isinstance(r, str) and (r.startswith("mcp__lc__") or r in lc_bash_allows))]
filtered_deny = [r for r in deny if r not in lemoncrow_denies]
removed = (len(allow) - len(filtered_allow)) + (len(deny) - len(filtered_deny))
if removed:
    if filtered_allow:
        perms["allow"] = filtered_allow
    else:
        perms.pop("allow", None)
    if filtered_deny:
        perms["deny"] = filtered_deny
    else:
        perms.pop("deny", None)
    if not perms:
        data.pop("permissions", None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"[lc:uninstall:claude] Removed {removed} LemonCrow permission entries")
PYEOF
    fi
fi

# ---- project-local and user-global agent files ------------------------------
# Claude Code writes plugin agents into project/user .claude/agents directories.
if $WORKSPACE_SET; then
    AGENT_DIRS=("${WORKSPACE}/.claude/agents")
else
    AGENT_DIRS=(".claude/agents" "${HOME}/.claude/agents")
fi
for agents_dir in "${AGENT_DIRS[@]}"; do
    if [ -d "$agents_dir" ]; then
        for f in "$agents_dir"/lemoncrow-*.md "$agents_dir"/lemoncrow_*.md "$agents_dir"/lemoncrow.*.md; do
            [ -f "$f" ] || continue
            run "rm -f $(printf %q "$f")"
            info "Removed agent file: $f"
        done
    fi
done

if $WORKSPACE_SET && [ -d "${WORKSPACE}/.claude/skills" ]; then
    run "rm -rf $(printf %q "${WORKSPACE}/.claude/skills")"
    info "Removed ${WORKSPACE}/.claude/skills"
fi

# ---- statusline settings in ~/.claude/settings.json -------------------------
if [ -f "${CLAUDE_SETTINGS}" ] && grep -qE "lemoncrow|lc" "${CLAUDE_SETTINGS}" 2>/dev/null; then
    if $DRY_RUN; then
        echo "  [dry-run] remove LemonCrow status line settings from ${CLAUDE_SETTINGS}"
    else
        python3 - <<PYEOF2
import json
from pathlib import Path
path = Path("${CLAUDE_SETTINGS}")
data = json.loads(path.read_text(encoding="utf-8") or "{}")
for key in ("statusLine", "subagentStatusLine"):
    sl = data.get(key, {})
    cmd = sl.get("command", "") if isinstance(sl, dict) else ""
    first = cmd.split()[0] if cmd else ""
    if isinstance(sl, dict) and (first in ("lc", "lemoncrow") or "lemoncrow" in cmd):
        data.pop(key, None)
        print(f"[lc:uninstall:claude] Removed LemonCrow {key} from ${CLAUDE_SETTINGS}")
if data.get("agent") in ("lc:code", "lemoncrow:code"):
    data.pop("agent", None)
    print("[lc:uninstall:claude] Removed lemoncrow-code default agent from ${CLAUDE_SETTINGS}")
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF2
    fi
fi

# ---- plugin uninstall -------------------------------------------------------
CLAUDE_PLUGINS="${HOME}/.claude/plugins"
if ! $WORKSPACE_SET; then
    if $DRY_RUN; then
        echo "  [dry-run] claude plugin uninstall lemoncrow@lemoncrow"
        echo "  [dry-run] claude plugin marketplace remove lemoncrow"
        echo "  [dry-run] rm -rf ${CLAUDE_PLUGINS}/lemoncrow* ${CLAUDE_PLUGINS}/cache/lemoncrow ${CLAUDE_PLUGINS}/data/lemoncrow-lemoncrow ${CLAUDE_STAGING_DIR}"
    else
        if command -v claude &>/dev/null; then
            # Remove via CLI (cleans registry entries)
            claude plugin uninstall lemoncrow@lemoncrow 2>/dev/null \
                || claude plugin uninstall lemoncrow 2>/dev/null \
                || true
            claude plugin marketplace remove lemoncrow 2>/dev/null || true
        else
            warn "claude CLI not found, removing on-disk plugin files only"
        fi
        # CLI removes registry entries but leaves files on disk — remove directly
        rm -rf "${CLAUDE_PLUGINS}/lemoncrow"
        rm -rf "${CLAUDE_PLUGINS}/cache/lemoncrow"
        rm -rf "${CLAUDE_PLUGINS}/data/lemoncrow-lemoncrow"
        # Clean up any timestamped backups the CLI left (e.g. lemoncrow.lemoncrow-backup.*)
        rm -rf "${CLAUDE_PLUGINS}"/lemoncrow.lemoncrow-backup.*
        rm -rf "${CLAUDE_STAGING_DIR}"
        info "Removed Claude plugin files, cache, and staging directory"
    fi
fi



info "Done."
