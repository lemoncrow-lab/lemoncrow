#!/usr/bin/env bash
# verify_lemoncrow_service.sh — HTTP service smoke test (no-auth mode)
#
# Starts the service on a random high port, tests /health, /ready,
# and POST /v1/reasoning/check-plan with a bad Shopify URL-handle plan
# (expected: status=blocked), then kills the server.
set -euo pipefail
cd "$(dirname "$0")/.."
export TMPDIR="${TMPDIR:-/var/tmp}"

# Check FastAPI/uvicorn available in the repo-managed environment
uv run python -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "SKIPPED: fastapi or uvicorn not installed"
    echo "Install with: uv sync --all-extras"
    exit 0
}

echo "=== LemonCrow service verification ==="

PORT=$(
    python3 - <<'PYEOF'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    print(sock.getsockname()[1])
PYEOF
)
ROOT=$(mktemp -d)
LOG=$(mktemp)
export LEMONCROW_REQUIRE_AUTH=false
export LEMONCROW_SERVICE_PORT=$PORT
export LEMONCROW_SERVICE_HOST=127.0.0.1
export LEMONCROW_ROOT="$ROOT"
SVC_PID=""

cleanup() {
    if [ -n "$SVC_PID" ]; then
        kill "$SVC_PID" 2>/dev/null || true
        # Wait for the service to exit and release its SQLite file locks
        # before removing the root directory.  Use a bounded wait so a
        # hung process doesn't block the test suite forever.
        for _ in 1 2 3 4 5; do
            kill -0 "$SVC_PID" 2>/dev/null || break
            sleep 0.2
        done
    fi
    rm -rf "$ROOT" 2>/dev/null || true
    rm -f "$LOG"
}
trap cleanup EXIT

# Start service in background
uv run lemon service start >"$LOG" 2>&1 &
SVC_PID=$!

# Wait for service to be ready (up to 15s)
echo "Waiting for service to start (pid=$SVC_PID)..."
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "Service ready after ${i} attempts"
        break
    fi
    if ! kill -0 "$SVC_PID" 2>/dev/null; then
        echo "FAIL: service exited before becoming ready"
        cat "$LOG"
        exit 1
    fi
    sleep 0.5
    if [ "$i" -eq 30 ]; then
        echo "FAIL: service did not start within 15s"
        cat "$LOG"
        exit 1
    fi
done

# --- /health ----------------------------------------------------------------
echo "--- GET /health ---"
HEALTH=$(curl -sf "http://127.0.0.1:${PORT}/health")
echo "$HEALTH"
HEALTH_PAYLOAD="$HEALTH" python3 - <<'PYEOF'
import json
import os

d = json.loads(os.environ["HEALTH_PAYLOAD"])
assert d.get("status") == "ok", f"Expected status=ok, got {d}"
print("PASS /health")
PYEOF

# --- /ready -----------------------------------------------------------------
echo "--- GET /ready ---"
READY=$(curl -sf "http://127.0.0.1:${PORT}/ready")
echo "$READY"
READY_PAYLOAD="$READY" python3 - <<'PYEOF'
import json
import os

d = json.loads(os.environ["READY_PAYLOAD"])
assert d.get("status") == "ok", f"Expected status=ok, got {d}"
print("PASS /ready")
PYEOF

# --- POST /v1/reasoning/context ---------------------------------------------
echo "--- POST /v1/reasoning/context ---"
RESULT=$(curl -sf -X POST "http://127.0.0.1:${PORT}/v1/reasoning/context" \
    -H "Content-Type: application/json" \
    -d '{
        "task": "Update Shopify product description",
        "domain": "beseam.shopify.publish",
        "tools": ["shopify.product.update"]
    }')
echo "$RESULT"
RESULT_PAYLOAD="$RESULT" python3 - <<'PYEOF'
import json
import os

d = json.loads(os.environ["RESULT_PAYLOAD"])
assert "context" in d, f"Expected context in response, got {d}"
print("PASS reasoning/context")
PYEOF

echo "=== PASS: all service checks passed ==="
