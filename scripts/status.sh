#!/usr/bin/env bash
# status.sh - Show Atelier installation status across agent CLIs
#
# Options:
#   --workspace DIR  Workspace root to inspect (default: cwd)
#   --json           Output in JSON format

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKSPACE="${PWD}"
JSON=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) JSON=true ;;
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

WORKSPACE="$(cd "$WORKSPACE" && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
OPENCODE_CONFIG_HOME="${OPENCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-${HOME}/.config}/opencode}"
VSCODE_USER_DIR="${VSCODE_USER_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/Code/User}"

has_cmd() { command -v "$1" &> /dev/null; }
has_atelier() { grep -q "atelier" "$1" 2>/dev/null; }

check_runtime() {
    if [ -f "${WORKSPACE}/.atelier/ledger.json" ] || [ -f "${WORKSPACE}/.atelier/atelier.db" ]; then
        echo "initialized"
    elif [ -d "${WORKSPACE}/.atelier" ]; then
        echo "exists but not initialized"
    else
        echo "not initialized"
    fi
}

check_symlink() {
    if [ -L "${HOME}/.local/bin/atelier-status" ] || [ -x "${HOME}/.local/bin/atelier-status" ]; then
        echo "linked"
    else
        echo "not linked"
    fi
}

check_claude() {
    if ! has_cmd claude; then
        echo "CLI not found"
        return
    fi

    local plugin="no"
    local mcp="no"
    if claude plugin list 2>&1 | grep -q "atelier"; then
        plugin="yes"
    fi
    if has_atelier "${WORKSPACE}/.mcp.json" || claude mcp list 2>&1 | grep -q "atelier"; then
        mcp="yes"
    fi

    if [ "$plugin" = "yes" ] && [ "$mcp" = "yes" ]; then
        echo "installed"
    elif [ "$plugin" = "yes" ]; then
        echo "plugin installed, MCP not configured"
    elif [ "$mcp" = "yes" ]; then
        echo "MCP configured, plugin not installed"
    else
        echo "CLI found but not installed"
    fi
}

check_codex() {
    if ! has_cmd codex; then
        echo "CLI not found"
        return
    fi

    if [ -d "${WORKSPACE}/.codex/skills/atelier" ] || [ -d "${CODEX_HOME}/skills/atelier" ]; then
        echo "installed"
    else
        echo "CLI found but skills not installed"
    fi
}

check_opencode() {
    if ! has_cmd opencode; then
        echo "CLI not found"
        return
    fi

    if has_atelier "${WORKSPACE}/opencode.json" || \
       has_atelier "${WORKSPACE}/opencode.jsonc" || \
       has_atelier "${OPENCODE_CONFIG_HOME}/opencode.json" || \
       has_atelier "${OPENCODE_CONFIG_HOME}/opencode.jsonc"; then
        echo "installed"
    else
        echo "CLI found but MCP not configured"
    fi
}

check_copilot() {
    if ! has_cmd code; then
        echo "CLI not found"
        return
    fi

    if has_atelier "${WORKSPACE}/.vscode/mcp.json" || has_atelier "${VSCODE_USER_DIR}/mcp.json"; then
        echo "installed"
    else
        echo "CLI found but MCP not configured"
    fi
}

check_gemini() {
    if ! has_cmd gemini; then
        echo "CLI not found"
        return
    fi

    if has_atelier "${WORKSPACE}/.gemini/settings.json" || has_atelier "${HOME}/.gemini/settings.json"; then
        echo "installed"
    else
        echo "CLI found but MCP not configured"
    fi
}

get_latest_run() {
    if [ -d "${WORKSPACE}/.atelier/runs" ]; then
        bash "${ATELIER_REPO}/bin/atelier-status" --root "${WORKSPACE}/.atelier" 2>/dev/null || echo "(no runs yet)"
    else
        echo "(no runs yet)"
    fi
}

RUNTIME_STATUS="$(check_runtime)"
SYMLINK_STATUS="$(check_symlink)"
CLAUDE_STATUS="$(check_claude)"
CODEX_STATUS="$(check_codex)"
OPENCODE_STATUS="$(check_opencode)"
COPILOT_STATUS="$(check_copilot)"
GEMINI_STATUS="$(check_gemini)"

if [ "$JSON" = true ]; then
    RUNTIME_STATUS="$RUNTIME_STATUS" \
    SYMLINK_STATUS="$SYMLINK_STATUS" \
    CLAUDE_STATUS="$CLAUDE_STATUS" \
    CODEX_STATUS="$CODEX_STATUS" \
    OPENCODE_STATUS="$OPENCODE_STATUS" \
    COPILOT_STATUS="$COPILOT_STATUS" \
    GEMINI_STATUS="$GEMINI_STATUS" \
    python3 - <<'PYEOF'
import json
import os

print(json.dumps({
    "runtime": os.environ["RUNTIME_STATUS"],
    "symlink": os.environ["SYMLINK_STATUS"],
    "claude": os.environ["CLAUDE_STATUS"],
    "codex": os.environ["CODEX_STATUS"],
    "opencode": os.environ["OPENCODE_STATUS"],
    "copilot": os.environ["COPILOT_STATUS"],
    "gemini": os.environ["GEMINI_STATUS"],
}))
PYEOF
else
    echo "=== Atelier Status ==="
    echo ""
    echo "Workspace:"
    echo "  $WORKSPACE"
    echo ""
    echo "Runtime Store:"
    echo "  .atelier/       $RUNTIME_STATUS"
    echo ""
    echo "CLI Symlink:"
    echo "  $SYMLINK_STATUS"
    echo ""
    echo "Agent CLI Installations:"
    echo "  Claude Code     $CLAUDE_STATUS"
    echo "  Codex           $CODEX_STATUS"
    echo "  opencode        $OPENCODE_STATUS"
    echo "  Copilot         $COPILOT_STATUS"
    echo "  Gemini          $GEMINI_STATUS"
    echo ""
    echo "Latest Run:"
    echo "  $(get_latest_run)"
fi
