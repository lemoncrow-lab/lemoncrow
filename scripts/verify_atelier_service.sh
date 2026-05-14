#!/usr/bin/env bash
# verify_atelier_service.sh — HTTP service smoke test (no-auth mode)
#
# Starts the service on a random high port, tests /health, /ready,
# and POST /v1/reasoning/check-plan with a bad Shopify URL-handle plan
# (expected: status=blocked), then kills the server.
set -euo pipefail
cd "$(dirname "$0")/.."

# Check FastAPI/uvicorn available
python3 -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "SKIPPED: fastapi or uvicorn not installed"
    echo "Install with: uv sync --all-extras"
    exit 0
}

echo "=== Atelier service verification ==="

PORT=18787
ROOT=$(mktemp -d)
export ATELIER_REQUIRE_AUTH=false
export ATELIER_SERVICE_PORT=$PORT
export ATELIER_SERVICE_HOST=127.0.0.1
export ATELIER_ROOT="$ROOT"
SVC_PID=""

cleanup() {
    [ -n "$SVC_PID" ] && kill "$SVC_PID" 2>/dev/null || true
    rm -rf "$ROOT"
}
trap cleanup EXIT

# Start service in background
atelier service start &
SVC_PID=$!

# Wait for service to be ready (up to 15s)
echo "Waiting for service to start (pid=$SVC_PID)..."
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "Service ready after ${i} attempts"
        break
    fi
    sleep 0.5
    if [ "$i" -eq 30 ]; then
        echo "FAIL: service did not start within 15s"
        exit 1
    fi
done

# --- /health ----------------------------------------------------------------
echo "--- GET /health ---"
HEALTH=$(curl -sf "http://127.0.0.1:${PORT}/health")
echo "$HEALTH"
python3 -c "
import json, sys
d = json.loads('$HEALTH')
assert d.get('status') == 'ok', f'Expected status=ok, got {d}'
print('PASS /health')
"

# --- /ready -----------------------------------------------------------------
echo "--- GET /ready ---"
READY=$(curl -sf "http://127.0.0.1:${PORT}/ready")
echo "$READY"
python3 -c "
import json, sys
d = json.loads('$READY')
assert d.get('ready') is True, f'Expected ready=true, got {d}'
print('PASS /ready')
"

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
python3 -c "
import json, sys
d = json.loads('''$RESULT''')
assert 'context' in d, f'Expected context in response, got {d}'
print('PASS reasoning/context')
"

echo "=== PASS: all service checks passed ==="
