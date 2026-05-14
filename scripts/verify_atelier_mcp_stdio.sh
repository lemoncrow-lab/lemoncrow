#!/usr/bin/env bash
# verify_atelier_mcp_stdio.sh — MCP stdio protocol smoke test
#
# Sends JSON-RPC messages over stdin/stdout to atelier-mcp and asserts:
#   1. tools/list returns the 10 expected tools
#   2. reasoning returns context without error
#   3. rescue returns a result without error
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Atelier MCP stdio verification ==="

TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
export ATELIER_ROOT="$TMP_ROOT"
atelier init --seed >/dev/null

# Build the JSON-RPC batch
MESSAGES=$(cat <<'JSONRPC'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","clientInfo":{"name":"verify-script","version":"1"},"capabilities":{}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"task","arguments":{"task":"Update Shopify product metafields","domain":"beseam.shopify.publish","tools":["shopify.update_metafield"]}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"rescue","arguments":{"task":"fix test","error":"AssertionError: expected 200 got 500","attempt":1,"context":"pytest run"}}}
JSONRPC
)

PY_SCRIPT=$(mktemp)
cat <<'EOF' > "$PY_SCRIPT"
import sys, json

lines = [l.strip() for l in sys.stdin.read().strip().split("\n") if l.strip()]
responses = {}
for line in lines:
    try:
        msg = json.loads(line)
        if "id" in msg:
            responses[msg["id"]] = msg
    except Exception:
        pass

# 1. tools/list
assert 2 in responses, "No tools/list response"
tools_result = responses[2].get("result", {})
tool_names = {t["name"] for t in tools_result.get("tools", [])}
required = {
    "task",
    "route",
    "rescue",
    "trace",
    "verify",
    "memory",
    "read",
    "edit",
    "search",
    "compact",
}
missing = required - tool_names
assert not missing, f"Missing tools: {missing}"
print(f"PASS tools/list: {sorted(tool_names)}")

# 3. reasoning -> no error
assert 4 in responses, "No reasoning response"
ctx_result = responses[4].get("result", {})
assert "error" not in responses[4], f"Unexpected error: {responses[4].get('error')}"
print("PASS reasoning: no error")

# 4. rescue -> no error
assert 5 in responses, "No rescue response"
assert "error" not in responses[5], f"Unexpected error: {responses[5].get('error')}"
print("PASS rescue: no error")

print("=== PASS: all MCP stdio checks passed ===")
EOF

echo "$MESSAGES" | atelier-mcp | python3 "$PY_SCRIPT"
rm "$PY_SCRIPT"
