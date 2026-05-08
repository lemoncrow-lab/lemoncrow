#!/usr/bin/env bash
# verify_claude.sh — Verify Atelier Claude plugin registration and install
#
# Checks:
#   1. 'claude' CLI exists on PATH
#   2. Repo-root .claude-plugin/marketplace.json exists and has name=atelier
#   3. Plugin package at integrations/claude/plugin/ validates
#   4. Claude plugin source 'atelier' is registered
#   5. Plugin listed as enabled (claude plugin list — atelier@atelier)
#   6. Global mode: Claude user MCP list contains atelier
#   7. Workspace mode: .mcp.json in workspace contains atelier server entry
#   7. MCP wrapper exists and is executable
#
# Options:
#   --workspace DIR  Verify project-local workspace config instead of global user MCP
#
# Exits 0 if all checks pass (or CLI not found — graceful skip)
# Exits 1 if CLI found but checks fail

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_DIR="${ATELIER_REPO}/integrations/claude/plugin"
MARKETPLACE_JSON="${PLUGIN_DIR}/.claude-plugin/marketplace.json"
WORKSPACE=""
WORKSPACE_SET=false
PLUGIN_REF="atelier@atelier"

while [[ $# -gt 0 ]]; do
    case "$1" in
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
FAIL=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; FAIL=1; }
skip() { echo "SKIP: $*"; }

echo "=== Atelier Claude Code verification ==="

if ! command -v claude &>/dev/null; then
    skip "'claude' CLI not on PATH — install from https://claude.ai/download"
    echo "=== SKIPPED (claude CLI absent) ==="
    exit 0
fi
pass "claude CLI found: $(claude --version 2>/dev/null || echo 'version unknown')"

if [ -f "${MARKETPLACE_JSON}" ]; then
    MKT_NAME=$(python3 -c "import json; d=json.load(open('${MARKETPLACE_JSON}')); print(d.get('name',''))" 2>/dev/null || echo "")
    if [ "$MKT_NAME" = "atelier" ]; then
        pass "repo-root marketplace.json valid (name=atelier)"
    else
        fail "repo-root marketplace.json name unexpected: '${MKT_NAME}' (expected 'atelier')"
    fi
else
    fail "repo-root .claude-plugin/marketplace.json missing — run: make install-claude"
fi

VALIDATE_OUT="$(claude plugin validate "${PLUGIN_DIR}" 2>&1 || true)"
if echo "$VALIDATE_OUT" | grep -q "Validation passed"; then
    pass "plugin package valid (claude plugin validate)"
else
    fail "plugin validation failed — run: claude plugin validate ${PLUGIN_DIR}"
fi

SOURCE_LIST="$(claude plugin marketplace list 2>&1 || true)"
if echo "$SOURCE_LIST" | grep -q "atelier"; then
    pass "Claude plugin source 'atelier' registered"
else
    fail "Claude plugin source 'atelier' not registered — run: make install-claude"
fi

PLUGIN_LIST="$(claude plugin list 2>&1 || true)"
if echo "$PLUGIN_LIST" | grep -q "${PLUGIN_REF}"; then
    if echo "$PLUGIN_LIST" | grep -A4 "${PLUGIN_REF}" | grep -qi "enabled"; then
        pass "claude plugin list: ${PLUGIN_REF} ✔ enabled"
    else
        fail "${PLUGIN_REF} found but not enabled — run: claude plugin enable ${PLUGIN_REF}"
    fi
else
    fail "${PLUGIN_REF} not in plugin list — run: make install-claude"
fi

if $WORKSPACE_SET; then
    MCP_JSON="${WORKSPACE}/.mcp.json"
    if [ -f "$MCP_JSON" ]; then
        HAS=$(python3 -c "
import json
d = json.load(open('$MCP_JSON'))
servers = d.get('mcpServers', {})
print('yes' if 'atelier' in servers else 'no')
" 2>/dev/null || echo "error")
        if [ "$HAS" = "yes" ]; then
            pass ".mcp.json contains atelier server entry"
        else
            fail ".mcp.json missing atelier entry - run: scripts/install_claude.sh --workspace $WORKSPACE"
        fi
    else
        fail ".mcp.json missing at $MCP_JSON - run: scripts/install_claude.sh --workspace $WORKSPACE"
    fi
elif claude mcp list 2>&1 | grep -q "atelier"; then
    pass "Claude user MCP list contains atelier"
else
    fail "Claude user MCP missing atelier - run: make install-claude"
fi

WRAPPER="${ATELIER_REPO}/scripts/atelier_mcp_stdio.sh"
if [ -x "$WRAPPER" ]; then
    pass "atelier_mcp_stdio.sh exists and is executable"
else
    fail "atelier_mcp_stdio.sh missing or not executable: $WRAPPER"
fi

if [ "$FAIL" -ne 0 ]; then
    echo "=== FAIL: one or more Claude checks failed ==="
    exit 1
fi
echo "=== PASS: all Claude checks passed ==="
