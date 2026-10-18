#!/usr/bin/env bash
# verify_opencode.sh — Verify LemonCrow install + provider wiring for opencode.
#
# 1. Runs install_opencode.sh (post-install checks; handles --workspace + skip).
# 2. Smoke-tests the `lc` MCP entry the installer wrote to opencode.json (tools/list).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/python_bootstrap.sh"

# 1) Install-time verification (also handles --workspace passthrough).
bash "${SCRIPT_DIR}/install_opencode.sh" "$@"

# 2) LemonCrow MCP smoke test against the installed opencode.json (skip if opencode absent).
command -v opencode >/dev/null 2>&1 || {
    echo "=== SKIPPED (opencode CLI absent) ==="
    exit 0
}

# Resolve the same opencode.json install_opencode.sh writes.
OC_FILE="${OPENCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-${HOME}/.config}/opencode}/opencode.json"
while [ $# -gt 0 ]; do
    if [ "$1" = "--workspace" ] && [ $# -gt 1 ]; then
        OC_FILE="$(cd "$2" && pwd)/opencode.json"
        shift
    fi
    shift
done

echo "=== LemonCrow opencode provider verification ==="

echo "--- checking ${OC_FILE} ---"
test -f "$OC_FILE" || { echo "FAIL: $OC_FILE not found"; exit 1; }

echo "--- checking lc MCP entry and listing its tools ---"
python3 - "$OC_FILE" <<'EOF'
import json, os, subprocess, sys

data = json.load(open(sys.argv[1]))
entry = data.get("mcp", {}).get("lc")
if not isinstance(entry, dict):
    print("FAIL: 'lc' MCP server not found in opencode.json")
    sys.exit(1)
command = entry.get("command")
if not isinstance(command, list) or "mcp" not in command:
    print(f"FAIL: unexpected lc MCP command: {command}")
    sys.exit(1)
print(f"lc MCP entry: {command}")

requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "verify", "version": "1"}, "capabilities": {}}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
]
env = {**os.environ, **{k: str(v) for k, v in (entry.get("environment") or {}).items() if "<" not in str(v)}}
try:
    result = subprocess.run(
        command, input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True, text=True, timeout=60, env=env,
    )
except (OSError, subprocess.TimeoutExpired) as exc:
    print(f"FAIL: lc MCP did not answer tools/list: {exc}")
    sys.exit(1)
for line in result.stdout.splitlines():
    try:
        message = json.loads(line)
    except ValueError:
        continue
    tools = message.get("result", {}).get("tools") if isinstance(message.get("result"), dict) else None
    if tools is not None:
        names = sorted(tool["name"] for tool in tools)
        print(f"tools found: {names}")
        missing = {"code_search", "read"} - set(names)
        if missing:
            print(f"FAIL: required tools missing: {sorted(missing)}")
            sys.exit(1)
        print("PASS: required tools present")
        sys.exit(0)
print("FAIL: tools/list response not found")
sys.exit(1)
EOF

echo "=== PASS: opencode provider checks passed ==="
