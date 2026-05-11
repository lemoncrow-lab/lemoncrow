#!/usr/bin/env bash
# status.sh - Show Atelier installation status across agent CLIs
#
# Options:
#   --workspace DIR  Workspace root to inspect (default: cwd)
#   --json           Output in JSON format
#   --write          Persist detection results to .atelier/hosts/status.json
#                    for the Docker service to consume (via mounted volume)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKSPACE="${PWD}"
JSON=false
WRITE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json) JSON=true ;;
        --write) WRITE=true ;;
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
    if [ -f "${HOME}/.atelier/ledger.json" ] || [ -f "${HOME}/.atelier/atelier.db" ]; then
        echo "initialized"
    elif [ -d "${HOME}/.atelier" ]; then
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

check_codeburn() {
    if has_cmd codeburn; then
        echo "installed"
    else
        echo "not installed"
    fi
}

check_tokscale() {
    if has_cmd tokscale; then
        echo "installed"
    else
        echo "not installed"
    fi
}

get_latest_run() {
    if [ -d "${HOME}/.atelier/runs" ]; then
        bash "${ATELIER_REPO}/bin/atelier-status" --root "${HOME}/.atelier" 2>/dev/null || echo "(no runs yet)"
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
CODEBURN_STATUS="$(check_codeburn)"
TOKSCALE_STATUS="$(check_tokscale)"

if [ "$WRITE" = true ]; then
    : # write-only mode: skip human-readable output, just persist below
elif [ "$JSON" = true ]; then
    RUNTIME_STATUS="$RUNTIME_STATUS" \
    SYMLINK_STATUS="$SYMLINK_STATUS" \
    CLAUDE_STATUS="$CLAUDE_STATUS" \
    CODEX_STATUS="$CODEX_STATUS" \
    OPENCODE_STATUS="$OPENCODE_STATUS" \
    COPILOT_STATUS="$COPILOT_STATUS" \
    GEMINI_STATUS="$GEMINI_STATUS" \
    CODEBURN_STATUS="$CODEBURN_STATUS" \
    TOKSCALE_STATUS="$TOKSCALE_STATUS" \
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
    "codeburn": os.environ["CODEBURN_STATUS"],
    "tokscale": os.environ["TOKSCALE_STATUS"],
}))
PYEOF
else
    echo "=== Atelier Status ==="
    echo ""
    echo "Workspace:"
    echo "  $WORKSPACE"
    echo ""
    echo "Runtime Store:"
    echo "  ${HOME}/.atelier/       $RUNTIME_STATUS"
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
    echo "External Reporting:"
    echo "  codeburn        $CODEBURN_STATUS"
    echo "  tokscale        $TOKSCALE_STATUS"
    echo ""
    echo "Latest Run:"
    echo "  $(get_latest_run)"
fi

# Persist detection results for the Docker service (--write flag)
if [ "$WRITE" = true ]; then
    HOSTS_DIR="${HOME}/.atelier/hosts"
    mkdir -p "$HOSTS_DIR"
    HOSTS_DIR="$HOSTS_DIR" \
    CLAUDE_STATUS="$CLAUDE_STATUS" \
    CODEX_STATUS="$CODEX_STATUS" \
    OPENCODE_STATUS="$OPENCODE_STATUS" \
    COPILOT_STATUS="$COPILOT_STATUS" \
    GEMINI_STATUS="$GEMINI_STATUS" \
    CODEBURN_STATUS="$CODEBURN_STATUS" \
    TOKSCALE_STATUS="$TOKSCALE_STATUS" \
    python3 - <<'PYEOF'
import json, os

def installed(s: str) -> str:
    return "installed" if s == "installed" else "not_installed"

hosts_dir = os.environ["HOSTS_DIR"]
status = {
    "claude": installed(os.environ["CLAUDE_STATUS"]),
    "codex": installed(os.environ["CODEX_STATUS"]),
    "opencode": installed(os.environ["OPENCODE_STATUS"]),
    "copilot": installed(os.environ["COPILOT_STATUS"]),
    "gemini": installed(os.environ["GEMINI_STATUS"]),
    "codeburn": installed(os.environ["CODEBURN_STATUS"]),
    "tokscale": installed(os.environ["TOKSCALE_STATUS"]),
}
path = os.path.join(hosts_dir, "status.json")
with open(path, "w") as f:
    json.dump(status, f, indent=2)
print(f"Wrote host status to {path}")
PYEOF
fi
