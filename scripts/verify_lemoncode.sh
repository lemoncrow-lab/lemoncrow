#!/usr/bin/env bash
# verify_lemoncode.sh — Verify LemonCrow install + provider wiring for lemoncode.
#
# 1. Runs install_lemoncode.sh (post-install checks; handles --workspace + skip).
# 2. Smoke-tests the `lc` MCP provider entry in the resolved lemoncode config.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/python_bootstrap.sh"

# 1) Install-time verification (also handles --workspace passthrough).
bash "${SCRIPT_DIR}/install_lemoncode.sh" "$@"

# 2) LemonCrow-as-provider smoke test (skip if lemoncode absent).
command -v lemoncode >/dev/null 2>&1 || {
    echo "=== SKIPPED (lemoncode CLI absent) ==="
    exit 0
}

# Only the XDG app dir was rebranded by the fork; the config file name is still
# opencode.json. --workspace shifts the config to <workspace>/opencode.json.
LEMONCODE_CONFIG_HOME="${LEMONCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-${HOME}/.config}/lemoncode}"
LC_CONFIG="${LEMONCODE_CONFIG_HOME}/opencode.json"
_prev=""
for _arg in "$@"; do
    if [ "$_prev" = "--workspace" ]; then
        LC_CONFIG="$(cd "$_arg" && pwd)/opencode.json"
    fi
    _prev="$_arg"
done

echo "=== LemonCrow lemoncode provider verification ==="

echo "--- checking ${LC_CONFIG} exists ---"
test -f "$LC_CONFIG" || { echo "FAIL: ${LC_CONFIG} not found"; exit 1; }

echo "--- validating config (JSON syntax) ---"
LEMONCROW_LC_CONFIG="$LC_CONFIG" python3 -c \
    "import json, os; json.load(open(os.environ['LEMONCROW_LC_CONFIG']))" \
    && echo "${LC_CONFIG}: valid JSON"

echo "--- checking MCP server entry ---"
LEMONCROW_LC_CONFIG="$LC_CONFIG" python3 - <<'EOF'
import json, os, sys
data = json.load(open(os.environ["LEMONCROW_LC_CONFIG"]))
mcps = data.get("mcp", {})
if "lc" not in mcps:
    print("FAIL: 'lc' MCP server not found in the lemoncode config")
    sys.exit(1)
entry = mcps["lc"]
command = entry.get("command", "")
if "lemoncode" not in str(command):
    print(f"FAIL: MCP entry is not registered for the lemoncode host: {command}")
    sys.exit(1)
print(f"LemonCrow MCP entry: {entry}")
EOF

echo "--- checking lemoncode can list tools (via lc mcp stdio) ---"
# The response is captured into a variable and handed to python via the
# environment: piping it into `python3 - <<'EOF'` would make the heredoc, not
# the MCP output, the process stdin.
MCP_RESPONSE="$(printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"verify","version":"1"},"capabilities":{}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    | lc mcp 2>/dev/null)"
LEMONCROW_MCP_RESPONSE="$MCP_RESPONSE" python3 - <<'EOF'
import json, os, sys

for line in os.environ["LEMONCROW_MCP_RESPONSE"].strip().split("\n"):
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if isinstance(msg.get("result"), dict) and "tools" in msg["result"]:
        tools = [t["name"] for t in msg["result"]["tools"]]
        print(f"tools found: {tools}")
        missing = {"code_search", "read"} - set(tools)
        if missing:
            print(f"FAIL: required tools missing: {sorted(missing)}")
            sys.exit(1)
        print("PASS: required tools present")
        sys.exit(0)
print("FAIL: tools/list response not found")
sys.exit(1)
EOF

echo "=== PASS: lemoncode provider checks passed ==="
