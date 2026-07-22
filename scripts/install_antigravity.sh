#!/usr/bin/env bash
# install_antigravity.sh — Install LemonCrow into Antigravity / agy
#
# What it does:
#   Global mode: installs user-level Antigravity MCP config, plugin, and skills.
#   Workspace mode (--workspace DIR): installs project-local Antigravity MCP config under DIR.
#
# Options:
#   --dry-run        Print what would happen, touch nothing
#   --print-only     Print exact manual steps, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of user config
#   --strict         Exit nonzero if antigravity/agy absent or --add-mcp fails

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

ANTIGRAVITY_USER_DIR="${ANTIGRAVITY_USER_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/Antigravity/User}"
AGY_GLOBAL_DIR="${HOME}/.gemini/antigravity-cli"
AGY_PLUGIN_DIR="${AGY_GLOBAL_DIR}/plugins/lemoncrow"
AGY_SKILLS_DIR="${AGY_GLOBAL_DIR}/skills"

if $WORKSPACE_SET; then
    INSTALL_SCOPE="workspace"
    MCP_JSON="${WORKSPACE}/.vscode/mcp.json"
else
    INSTALL_SCOPE="global"
    MCP_JSON="${ANTIGRAVITY_USER_DIR}/mcp.json"
fi

info()  { [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]] && echo "[lemoncrow:antigravity] $*" || true; }
warn()  { echo "[lemoncrow:antigravity] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
backup_file() {
    local f="$1"
    if $WORKSPACE_SET; then
        return
    fi
    if [ -f "$f" ]; then
        local bk="${f}.lemoncrow-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp $(printf %q "$f") $(printf %q "$bk")"
        info "backed up $f -> $bk"
    fi
}

ANTIGRAVITY_BIN="$(command -v antigravity || true)"
AGY_BIN="$(command -v agy || true)"
if [[ -z "$ANTIGRAVITY_BIN" && -z "$AGY_BIN" ]]; then
    if $STRICT; then
        echo "[lemoncrow:antigravity] ERROR: neither 'antigravity' nor 'agy' is on PATH." >&2
        exit 1
    fi
    warn "Neither 'antigravity' nor 'agy' is on PATH - SKIPPING."
    warn "Install Antigravity or agy, then run: make install"
    echo "=== SKIPPED (antigravity/agy absent) ==="
    exit 0
fi

if [[ -n "$ANTIGRAVITY_BIN" ]]; then
    info "Found Antigravity: $(antigravity --version 2>/dev/null | head -1 || echo 'version unknown')"
fi
if [[ -n "$AGY_BIN" ]]; then
    info "Found agy: $(agy --version 2>/dev/null | head -1 || echo 'version unknown')"
fi

if $WORKSPACE_SET; then
    NEW_ENTRY=$(cat <<JSON
{
  "servers": {
    "lemoncrow": {
      "type": "stdio",
      "command": "lemoncrow",
      "args": ["mcp", "--host", "antigravity"],
      "env": {
        "LEMONCROW_WORKSPACE_ROOT": "${WORKSPACE}"
      }
    }
  }
}
JSON
)
else
    NEW_ENTRY=$(cat <<'JSON'
{
  "servers": {
    "lemoncrow": {
      "type": "stdio",
      "command": "lemoncrow",
      "args": ["mcp", "--host", "antigravity"]
    }
  }
}
JSON
)
fi

ADD_MCP_JSON=$(cat <<'JSON'
{"name":"lemoncrow","command":"lemoncrow","args":["mcp","--host","antigravity"]}
JSON
)

if $PRINT_ONLY; then
    echo ""
    echo "=== LemonCrow Antigravity - Manual Install Steps ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo ""
    if $WORKSPACE_SET; then
        echo "1. Create/merge ${MCP_JSON}:"
        echo "$NEW_ENTRY"
    else
        echo "1. Add LemonCrow MCP to the Antigravity user profile:"
        echo "   antigravity --add-mcp '$ADD_MCP_JSON'"
        echo ""
        echo "2. Create/merge ${MCP_JSON}:"
        echo "$NEW_ENTRY"
    fi
    echo ""
    echo "3. Open the workspace in Antigravity and use agy or the built-in chat with LemonCrow MCP enabled."
    exit 0
fi

run "mkdir -p $(printf %q "$(dirname "$MCP_JSON")")"
if [ -f "$MCP_JSON" ]; then
    backup_file "$MCP_JSON"
    if $DRY_RUN; then
        echo "  [dry-run] merge LemonCrow into $MCP_JSON"
    else
        MCP_JSON="$MCP_JSON" NEW_ENTRY="$NEW_ENTRY" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

path = Path(os.environ["MCP_JSON"])
existing = json.loads(path.read_text(encoding="utf-8") or "{}")
new_entry = json.loads(os.environ["NEW_ENTRY"])
server_key = "servers" if "servers" in existing or "mcpServers" not in existing else "mcpServers"
existing.setdefault(server_key, {}).update(new_entry["servers"])
path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
print(f"[lemoncrow:antigravity] merged LemonCrow into {path}")
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

if ! $WORKSPACE_SET && [[ -n "$ANTIGRAVITY_BIN" ]] && ! $DRY_RUN; then
    if ! ADD_MCP_OUTPUT=$(antigravity --add-mcp "$ADD_MCP_JSON" 2>&1); then
        if $STRICT; then
            echo "[lemoncrow:antigravity] ERROR: antigravity --add-mcp failed: $ADD_MCP_OUTPUT" >&2
            exit 1
        fi
        warn "antigravity --add-mcp failed: $ADD_MCP_OUTPUT (user mcp.json was still written)"
    fi
fi

info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[lemoncrow:antigravity] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$MCP_JSON" ] && grep -q '"lemoncrow"' "$MCP_JSON" 2>/dev/null; then
    vpass "MCP config present: $MCP_JSON"
else
    vfail "missing LemonCrow MCP config: $MCP_JSON"
fi

# Install plugin (global only — not applicable for workspace-scoped installs)
PLUGIN_SRC="${LEMONCROW_REPO}/integrations/antigravity/plugin"
if ! $WORKSPACE_SET && [[ -d "$PLUGIN_SRC" ]]; then
    if $DRY_RUN; then
        echo "  [dry-run] install plugin -> $AGY_PLUGIN_DIR"
    else
        run "mkdir -p $(printf %q "$AGY_PLUGIN_DIR")"
        run "cp -r $(printf %q "${PLUGIN_SRC}/.") $(printf %q "$AGY_PLUGIN_DIR/")"
        LEMONCROW_VERSION="$(lemoncrow_resolve_version "$LEMONCROW_REPO")"
        PLUGIN_MANIFEST="${AGY_PLUGIN_DIR}/plugin.json" LEMONCROW_VERSION="$LEMONCROW_VERSION" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

manifest = Path(os.environ["PLUGIN_MANIFEST"])
data = json.loads(manifest.read_text(encoding="utf-8"))
data["version"] = os.environ["LEMONCROW_VERSION"]
manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
        lemoncrow_apply_reply_register_level "$AGY_PLUGIN_DIR" false
        info "installed plugin -> $AGY_PLUGIN_DIR"
    fi
fi

# Install global skills (global only)
if ! $WORKSPACE_SET; then
    bash "${SCRIPT_DIR}/build_host_skills.sh" --host antigravity 2>/dev/null || true
    SKILLS_STAGING="${LEMONCROW_REPO}/integrations/antigravity/skills"
    if [[ -d "$SKILLS_STAGING" ]] && compgen -G "${SKILLS_STAGING}/*/SKILL.md" > /dev/null 2>&1; then
        if $DRY_RUN; then
            echo "  [dry-run] install skills -> $AGY_SKILLS_DIR"
        else
            run "mkdir -p $(printf %q "$AGY_SKILLS_DIR")"
            for skill_dir in "${SKILLS_STAGING}"/*/; do
                [[ -f "${skill_dir}SKILL.md" ]] || continue
                skill_name="$(basename "$skill_dir")"
                run "mkdir -p $(printf %q "${AGY_SKILLS_DIR}/${skill_name}")"
                run "cp $(printf %q "${skill_dir}SKILL.md") $(printf %q "${AGY_SKILLS_DIR}/${skill_name}/SKILL.md")"
            done
            info "installed skills -> $AGY_SKILLS_DIR"
        fi
    fi
fi



if command -v lc &>/dev/null; then
    vpass "lc is available on PATH"
else
    vfail "lc NOT found on PATH"
fi

if [[ -n "$ANTIGRAVITY_BIN" || -n "$AGY_BIN" ]]; then
    vpass "Antigravity host executable detected"
else
    vfail "neither antigravity nor agy detected after install"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[lemoncrow:antigravity] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"
info "Done. Open the workspace in Antigravity or launch agy with LemonCrow MCP available."
