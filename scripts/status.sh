#!/usr/bin/env bash
# status.sh - Show LemonCrow installation status across agent CLIs
#
# Options:
#   --workspace DIR  Workspace root to inspect (default: cwd)
#   --json           Output in JSON format
#   --write          Persist detection results to .lemoncrow/hosts/status.json
#                    for the local service/UI surfaces to consume

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

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
has_lemoncrow() { grep -q "lemoncrow" "$1" 2>/dev/null; }

check_runtime() {
    if [ -f "${HOME}/.lemoncrow/ledger.json" ] || [ -f "${HOME}/.lemoncrow/lemoncrow.db" ]; then
        echo "initialized"
    elif [ -d "${HOME}/.lemoncrow" ]; then
        echo "exists but not initialized"
    else
        echo "not initialized"
    fi
}

check_cli() {
    if command -v lc >/dev/null 2>&1; then
        echo "installed"
    else
        echo "not installed"
    fi
}

check_claude() {
    if ! has_cmd claude; then
        echo "CLI not found"
        return
    fi

    local plugin="no"
    local mcp="no"
    if claude plugin list 2>&1 | grep -q "lemoncrow"; then
        plugin="yes"
    fi
    if has_lemoncrow "${WORKSPACE}/.mcp.json" || claude mcp list 2>&1 | grep -q "lemoncrow"; then
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

    local effective_codex_home="${CODEX_HOME}"
    if [ -f "${WORKSPACE}/.codex/config.toml" ]; then
        effective_codex_home="${WORKSPACE}/.codex"
    fi

    if [ -f "${effective_codex_home}/config.toml" ] && \
       grep -q '\[mcp_servers\.lemoncrow\]' "${effective_codex_home}/config.toml" 2>/dev/null && \
       grep -Eq '\[plugins\."lemoncrow@(lemoncrow|openai-curated)"\]' "${effective_codex_home}/config.toml" 2>/dev/null; then
        echo "installed"
    elif [ -f "${effective_codex_home}/config.toml" ] && \
         grep -q '\[mcp_servers\.lemoncrow\]' "${effective_codex_home}/config.toml" 2>/dev/null; then
        echo "MCP configured, plugin not installed"
    else
        echo "CLI found but MCP not configured"
    fi
}

check_opencode() {
    if ! has_cmd opencode; then
        echo "CLI not found"
        return
    fi

    if has_lemoncrow "${WORKSPACE}/opencode.json" || \
       has_lemoncrow "${WORKSPACE}/opencode.jsonc" || \
       has_lemoncrow "${OPENCODE_CONFIG_HOME}/opencode.json" || \
       has_lemoncrow "${OPENCODE_CONFIG_HOME}/opencode.jsonc"; then
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

    if has_lemoncrow "${WORKSPACE}/.vscode/mcp.json" || has_lemoncrow "${VSCODE_USER_DIR}/mcp.json"; then
        echo "installed"
    else
        echo "CLI found but MCP not configured"
    fi
}

check_antigravity() {
    if ! has_cmd antigravity && ! has_cmd agy; then
        echo "CLI not found"
        return
    fi

    if has_lemoncrow "${WORKSPACE}/.vscode/mcp.json" || has_lemoncrow "${VSCODE_USER_DIR}/mcp.json" || has_lemoncrow "${XDG_CONFIG_HOME:-${HOME}/.config}/Antigravity/User/mcp.json"; then
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
    if [ -d "${HOME}/.lemoncrow/runs" ]; then
        lc status --line --root "${HOME}/.lemoncrow" 2>/dev/null || echo "(no runs yet)"
    else
        echo "(no runs yet)"
    fi
}

RUNTIME_STATUS="$(check_runtime)"
CLI_STATUS="$(check_cli)"
CLAUDE_STATUS="$(check_claude)"
CODEX_STATUS="$(check_codex)"
OPENCODE_STATUS="$(check_opencode)"
COPILOT_STATUS="$(check_copilot)"
ANTIGRAVITY_STATUS="$(check_antigravity)"
CODEBURN_STATUS="$(check_codeburn)"
TOKSCALE_STATUS="$(check_tokscale)"

if [ "$WRITE" = true ]; then
    : # write-only mode: skip human-readable output, just persist below
elif [ "$JSON" = true ]; then
    RUNTIME_STATUS="$RUNTIME_STATUS" \
    CLI_STATUS="$CLI_STATUS" \
    CLAUDE_STATUS="$CLAUDE_STATUS" \
    CODEX_STATUS="$CODEX_STATUS" \
    OPENCODE_STATUS="$OPENCODE_STATUS" \
    COPILOT_STATUS="$COPILOT_STATUS" \
    ANTIGRAVITY_STATUS="$ANTIGRAVITY_STATUS" \
    CODEBURN_STATUS="$CODEBURN_STATUS" \
    TOKSCALE_STATUS="$TOKSCALE_STATUS" \
    python3 - <<'PYEOF'
import json
import os

print(json.dumps({
    "runtime": os.environ["RUNTIME_STATUS"],
    "cli": os.environ["CLI_STATUS"],
    "claude": os.environ["CLAUDE_STATUS"],
    "codex": os.environ["CODEX_STATUS"],
    "opencode": os.environ["OPENCODE_STATUS"],
    "copilot": os.environ["COPILOT_STATUS"],
    "antigravity": os.environ["ANTIGRAVITY_STATUS"],
    "codeburn": os.environ["CODEBURN_STATUS"],
    "tokscale": os.environ["TOKSCALE_STATUS"],
}))
PYEOF
else
    echo "=== LemonCrow Status ==="
    echo ""
    echo "Workspace:"
    echo "  $WORKSPACE"
    echo ""
    echo "Runtime Store:"
    echo "  ${HOME}/.lemoncrow/       $RUNTIME_STATUS"
    echo ""
    echo "CLI:"
    echo "  $CLI_STATUS"
    echo ""
    echo "Agent CLI Installations:"
    echo "  Claude Code     $CLAUDE_STATUS"
    echo "  Codex           $CODEX_STATUS"
    echo "  opencode        $OPENCODE_STATUS"
    echo "  Copilot         $COPILOT_STATUS"
    echo "  Antigravity     $ANTIGRAVITY_STATUS"
    echo ""
    echo "External Reporting:"
    echo "  codeburn        $CODEBURN_STATUS"
    echo "  tokscale        $TOKSCALE_STATUS"
    echo ""
    echo "Latest Run:"
    echo "  $(get_latest_run)"
fi

# Persist detection results for the local service/UI surfaces (--write flag)
if [ "$WRITE" = true ]; then
    HOSTS_DIR="${HOME}/.lemoncrow/hosts"
    mkdir -p "$HOSTS_DIR"
    HOSTS_DIR="$HOSTS_DIR" \
    CLAUDE_STATUS="$CLAUDE_STATUS" \
    CODEX_STATUS="$CODEX_STATUS" \
    OPENCODE_STATUS="$OPENCODE_STATUS" \
    COPILOT_STATUS="$COPILOT_STATUS" \
    ANTIGRAVITY_STATUS="$ANTIGRAVITY_STATUS" \
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
    "antigravity": installed(os.environ["ANTIGRAVITY_STATUS"]),
    "codeburn": installed(os.environ["CODEBURN_STATUS"]),
    "tokscale": installed(os.environ["TOKSCALE_STATUS"]),
}
path = os.path.join(hosts_dir, "status.json")
with open(path, "w") as f:
    json.dump(status, f, indent=2)
print(f"Wrote host status to {path}")
PYEOF
fi
