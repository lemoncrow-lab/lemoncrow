#!/usr/bin/env bash
# verify_claude.sh — Verify LemonCrow Claude plugin registration and install
#
# Checks:
#   1. 'claude' CLI exists on PATH
#   2. Repo-root .claude-plugin/marketplace.json exists and has name=lemoncrow
#   3. Plugin package at integrations/claude/plugin/ validates
#   4. Packaged workflow assets exist and include code-audit.js + README
#   5. Claude plugin source 'lemoncrow' is registered
#   6. Plugin listed as enabled (claude plugin list — lemoncrow@lemoncrow)
#   7. Global mode: Claude user MCP list contains lemoncrow
#   8. Workspace mode: .mcp.json in workspace contains lemoncrow server entry
#   9. MCP wrapper exists and is executable
#
# Options:
#   --workspace DIR  Verify project-local workspace config instead of global user MCP
#
# Exits 0 if all checks pass (or CLI not found — graceful skip)
# Exits 1 if CLI found but checks fail

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PLUGIN_DIR="${LEMONCROW_REPO}/integrations/claude/plugin"
MARKETPLACE_JSON="${PLUGIN_DIR}/.claude-plugin/marketplace.json"
WORKFLOWS_DIR="${PLUGIN_DIR}/workflows"
WORKFLOW_FILE="${WORKFLOWS_DIR}/code-audit.js"
GATE_WORKFLOW_FILE="${WORKFLOWS_DIR}/gate-benchmark.js"
WORKFLOW_README="${WORKFLOWS_DIR}/README.md"
WORKFLOW_MIN_VERSION="v2.1.154"
WORKSPACE=""
WORKSPACE_SET=false
PLUGIN_REF="lemoncrow@lemoncrow"

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

echo "=== LemonCrow Claude Code verification ==="

if ! command -v claude &>/dev/null; then
    skip "'claude' CLI not on PATH — install from https://claude.ai/download"
    echo "=== SKIPPED (claude CLI absent) ==="
    exit 0
fi
pass "claude CLI found: $(claude --version 2>/dev/null || echo 'version unknown')"

if [ -f "${MARKETPLACE_JSON}" ]; then
    MKT_NAME=$(uv run python -c "import json; d=json.load(open('${MARKETPLACE_JSON}')); print(d.get('name',''))" 2>/dev/null || echo "")
    if [ "$MKT_NAME" = "lemoncrow" ]; then
        pass "repo-root marketplace.json valid (name=lemoncrow)"
    else
        fail "repo-root marketplace.json name unexpected: '${MKT_NAME}' (expected 'lemoncrow')"
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

if [ -d "${WORKFLOWS_DIR}" ] && [ -f "${WORKFLOW_FILE}" ] && [ -f "${GATE_WORKFLOW_FILE}" ] && [ -f "${WORKFLOW_README}" ]; then
    pass "workflow assets bundled (workflows/, code-audit.js, gate-benchmark.js, README.md)"
else
    fail "workflow assets missing from ${WORKFLOWS_DIR} — expected code-audit.js, gate-benchmark.js, and README.md"
fi
echo "NOTE: packaged Claude workflows appear in /workflows and require Claude Code ${WORKFLOW_MIN_VERSION}+"

SOURCE_LIST="$(claude plugin marketplace list 2>&1 || true)"
if echo "$SOURCE_LIST" | grep -q "lemoncrow"; then
    pass "Claude plugin source 'lemoncrow' registered"
else
    fail "Claude plugin source 'lemoncrow' not registered — run: make install-claude"
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
        HAS=$(uv run python -c "
import json
d = json.load(open('$MCP_JSON'))
servers = d.get('mcpServers', {})
print('yes' if 'lemoncrow' in servers else 'no')
" 2>/dev/null || echo "error")
        if [ "$HAS" = "yes" ]; then
            pass ".mcp.json contains lemoncrow server entry"
        else
            fail ".mcp.json missing lemoncrow entry - run: scripts/install_claude.sh --workspace $WORKSPACE"
        fi
    else
        fail ".mcp.json missing at $MCP_JSON - run: scripts/install_claude.sh --workspace $WORKSPACE"
    fi
elif claude mcp list 2>&1 | grep -q "lemoncrow"; then
    pass "Claude user MCP list contains lemoncrow"
else
    fail "Claude user MCP missing lemoncrow - run: make install-claude"
fi

if command -v lc &>/dev/null; then
    pass "lc is available on PATH"
else
    fail "lc NOT found on PATH"
fi

if [ "$FAIL" -ne 0 ]; then
    echo "=== FAIL: one or more Claude checks failed ==="
    exit 1
fi
echo "=== PASS: all Claude checks passed ==="
