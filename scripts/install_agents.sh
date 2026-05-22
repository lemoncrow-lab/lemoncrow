#!/usr/bin/env bash
# install_agents.sh — Universal project-local Atelier installer
#
# Creates/updates the two universally-respected config files:
#   .mcp.json  — Atelier MCP server (respected by all MCP-compatible hosts)
#   AGENTS.md  — Atelier agent persona (respected by opencode, codex, copilot,
#                gemini, claude, etc.)
#
# This script is host-agnostic. Run it once per project regardless of which
# agent CLI(s) you use. Per-host installers (install_opencode.sh, etc.) each
# add their own host-specific configs but do NOT touch these two files.
#
# Usage:
#   bash scripts/install_agents.sh --workspace /path/to/project
#   bash scripts/install_agents.sh --workspace . --dry-run
#   bash scripts/install_agents.sh --print-only
#
# Options:
#   --workspace DIR  Project root to install into (default: current directory)
#   --dry-run        Print what would happen, touch nothing
#   --print-only     Print manual steps, touch nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

DRY_RUN=false
PRINT_ONLY=false
WORKSPACE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true ;;
        --print-only) PRINT_ONLY=true ;;
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            WORKSPACE="$2"
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

if [ -z "$WORKSPACE" ]; then
    WORKSPACE="$(pwd)"
fi
WORKSPACE="$(cd "$WORKSPACE" && pwd)"

info()  { echo "[atelier:agents] $*"; }
warn()  { echo "[atelier:agents] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

# ── 1. MCP config (.mcp.json) ────────────────────────────────────────────────
# Writes/merges the atelier MCP server entry into the project's .mcp.json.
# This is the universal MCP config that all MCP-compatible hosts respect.

MCP_JSON="${WORKSPACE}/.mcp.json"
NEW_MCP_ENTRY=$(cat <<JSON
{
  "mcpServers": {
    "atelier": {
      "command": "atelier-mcp",
      "args": ["--host", "mcp"]
    }
  }
}
JSON
)

if $PRINT_ONLY; then
    echo ""
    echo "=== Atelier Universal Agents Install ==="
    echo ""
    echo "Project: ${WORKSPACE}"
    echo ""
    echo "1. Create/merge ${MCP_JSON}:"
    echo "${NEW_MCP_ENTRY}"
    echo ""
    echo "2. Ensure ${WORKSPACE}/AGENTS.md has atelier:code persona"
    echo "   Source: ${ATELIER_REPO}/AGENTS.md"
    echo ""
    echo "After install, any MCP-compatible host will pick up:"
    echo "  - atelier MCP server (from .mcp.json)"
    echo "  - atelier:code agent persona (from AGENTS.md)"
    exit 0
fi

info "Installing universal agent configs into ${WORKSPACE}"

run "mkdir -p '$WORKSPACE'"

if [ -f "$MCP_JSON" ]; then
    if $DRY_RUN; then
        echo "  [dry-run] merge atelier MCP server into $MCP_JSON"
    else
        python3 - <<PYEOF
import json
from pathlib import Path
path = Path('$MCP_JSON')
existing = json.loads(path.read_text(encoding='utf-8') or '{}')
new_entry = json.loads('''$NEW_MCP_ENTRY''')
existing.setdefault('mcpServers', {}).update(new_entry['mcpServers'])
path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
PYEOF
        info "merged atelier MCP server into $MCP_JSON"
    fi
else
    if $DRY_RUN; then
        echo "  [dry-run] create $MCP_JSON with atelier MCP server"
    else
        echo "$NEW_MCP_ENTRY" > "$MCP_JSON"
        info "created $MCP_JSON with atelier MCP server"
    fi
fi

# ── 2. AGENTS.md ─────────────────────────────────────────────────────────────
# Ensures the project's AGENTS.md includes the atelier:code persona via
# sentinel markers so re-install updates in place without destroying user content.

AGENTS_FILE="${WORKSPACE}/AGENTS.md"
AGENTS_SOURCE="${ATELIER_REPO}/AGENTS.md"

if [ -f "$AGENTS_SOURCE" ]; then
    if [ -f "$AGENTS_FILE" ]; then
        if $DRY_RUN; then
            atelier_upsert_managed_block "$AGENTS_SOURCE" "$AGENTS_FILE" "true"
            info "[dry-run] would ensure atelier:code persona in $AGENTS_FILE"
        else
            atelier_upsert_managed_block "$AGENTS_SOURCE" "$AGENTS_FILE" "false"
            info "ensured atelier:code persona in $AGENTS_FILE"
        fi
    else
        if $DRY_RUN; then
            atelier_write_managed_copy "$AGENTS_SOURCE" "$AGENTS_FILE" "true"
            info "[dry-run] would create $AGENTS_FILE with atelier:code persona"
        else
            atelier_write_managed_copy "$AGENTS_SOURCE" "$AGENTS_FILE" "false"
            info "created $AGENTS_FILE with atelier:code persona"
        fi
    fi
else
    warn "atelier persona source not found: $AGENTS_SOURCE"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "Universal agents config installed in ${WORKSPACE}"
info "  ${MCP_JSON}  — atelier MCP server (respected by all MCP hosts)"
info "  ${AGENTS_FILE}  — atelier:code persona (respected by all agent CLIs)"
echo ""
info "Next: install per-host configs (if needed)"
info "  bash scripts/install_opencode.sh --workspace '${WORKSPACE}'"
info "  bash scripts/install_codex.sh --workspace '${WORKSPACE}'"
info "  bash scripts/install_copilot.sh --workspace '${WORKSPACE}'"
info "  bash scripts/install_claude.sh --workspace '${WORKSPACE}'"
