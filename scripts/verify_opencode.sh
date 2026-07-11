#!/usr/bin/env bash
# verify_opencode.sh — Verify LemonCrow install + provider wiring for opencode.
#
# 1. Runs install_opencode.sh (post-install checks; handles --workspace + skip).
# 2. Smoke-tests the `lemon` MCP provider entry in opencode.json (tools/list).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 1) Install-time verification (also handles --workspace passthrough).
bash "${SCRIPT_DIR}/install_opencode.sh" "$@"

# 2) LemonCrow-as-provider smoke test in opencode.json (skip if opencode absent).
command -v opencode >/dev/null 2>&1 || {
    echo "=== SKIPPED (opencode CLI absent) ==="
    exit 0
}

cd "${REPO_ROOT}"

echo "=== LemonCrow opencode provider verification ==="

echo "--- checking opencode.json exists ---"
test -f opencode/opencode.json || { echo "FAIL: opencode/opencode.json not found"; exit 1; }

echo "--- validating opencode.json (JSON syntax) ---"
python3 -c "import json, sys; json.load(open('opencode/opencode.json'))" \
    && echo "opencode.json: valid JSON"

echo "--- checking MCP server entry ---"
python3 - <<'EOF'
import json, sys
data = json.load(open("opencode/opencode.json"))
mcps = data.get("mcp", {})
if "lemoncrow" not in mcps:
    print("FAIL: 'lemoncrow' MCP server not found in opencode.json")
    sys.exit(1)
entry = mcps["lemoncrow"]
cmd = entry.get("command", "")
if "lemon" not in str(cmd) and "mcp" not in str(entry.get("args", [])):
    print(f"FAIL: unexpected command: {cmd}")
    sys.exit(1)
print(f"LemonCrow MCP entry: {entry}")
EOF

echo "--- checking opencode can list tools (via lemon mcp stdio) ---"
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"verify","version":"1"},"capabilities":{}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    | lemon mcp 2>/dev/null \
    | python3 - <<'EOF'
import sys, json
lines = sys.stdin.read().strip().split("\n")
for line in lines:
    try:
        msg = json.loads(line)
        if "result" in msg and "tools" in msg.get("result", {}):
            tools = [t["name"] for t in msg["result"]["tools"]]
            print(f"tools found: {tools}")
            assert "check_plan" in tools, "check_plan tool missing"
            print("PASS: required tools present")
            sys.exit(0)
    except Exception:
        pass
print("FAIL: tools/list response not found")
sys.exit(1)
EOF

echo "=== PASS: opencode provider checks passed ==="
