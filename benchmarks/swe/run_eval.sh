#!/usr/bin/env bash
# run_eval.sh — Run SWE-bench Lite eval: baseline vs Atelier compression
#
# Usage:
#   bash benchmarks/swe/run_eval.sh [SLICE] [RUN_ID_SUFFIX]
#
# SLICE  : Python slice, e.g. "0:5" (default: "0:5")
#
# Outputs:
#   benchmarks/swe/outputs/baseline/   — mini-SWE-agent output, no compression
#   benchmarks/swe/outputs/atelier/    — mini-SWE-agent output, Atelier proxy
#   benchmarks/swe/outputs/baseline_preds.json
#   benchmarks/swe/outputs/atelier_preds.json
#   benchmarks/swe/outputs/proxy_savings.jsonl
#   benchmarks/swe/outputs/report.md

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SLICE="${1:-0:5}"
OUTDIR="$REPO_ROOT/benchmarks/swe/outputs"
PROXY_LOG="$OUTDIR/proxy_savings.jsonl"
PROXY_PID_FILE="$OUTDIR/proxy.pid"
PROXY_PID=""

SWE_BENCH_CONFIG_DIR="$REPO_ROOT/benchmarks/swe/configs"
SWEBENCH_YAML="$REPO_ROOT/.venv/lib/python3.11/site-packages/minisweagent/config/benchmarks/swebench.yaml"

rm -rf "$OUTDIR/baseline" "$OUTDIR/atelier"
mkdir -p "$OUTDIR/baseline" "$OUTDIR/atelier"
rm -f "$PROXY_LOG" "$PROXY_PID_FILE"

cleanup() {
    if [[ -n "$PROXY_PID" ]]; then
        kill "$PROXY_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

check_exit_statuses() {
    local run_dir="$1"
    local label="$2"
    local status_file
    status_file="$(ls -1t "$run_dir"/exit_statuses_*.yaml 2>/dev/null | head -1 || true)"
    if [[ -z "$status_file" ]]; then
        echo "ERROR: $label produced no exit_statuses_*.yaml in $run_dir"
        return 1
    fi

    uv run python - "$status_file" "$label" <<'PY'
import sys
from pathlib import Path

import yaml

status_path = Path(sys.argv[1])
label = sys.argv[2]
data = yaml.safe_load(status_path.read_text()) or {}
statuses = (data.get("instances_by_exit_status") or {}).keys()
bad_markers = ("error", "exception", "auth")
bad = [s for s in statuses if any(m in str(s).lower() for m in bad_markers)]
if bad:
    print(f"ERROR: {label} has fatal exit statuses in {status_path}: {', '.join(bad)}")
    sys.exit(1)
print(f"[ok] {label} exit statuses are non-fatal ({status_path.name}): {', '.join(statuses) or 'none'}")
PY
}

echo "====================================================="
echo " Atelier × SWE-bench Lite evaluation"
echo " Slice   : $SLICE"
echo " Output  : $OUTDIR"
echo "====================================================="

# ── Dependency check ────────────────────────────────────────────────
if ! curl -s http://localhost:11434/v1/models >/dev/null 2>&1; then
    echo "ERROR: Ollama not running or not reachable at :11434"
    exit 1
fi
echo "[ok] Ollama reachable"

# ── Install proxy dependencies ───────────────────────────────────────
cd "$REPO_ROOT"
uv pip install fastapi uvicorn httpx --quiet

# ── STEP 1: Baseline run (no proxy) ─────────────────────────────────
echo ""
echo "──────────────────────────────────────────────────────"
echo "STEP 1/3  Baseline run (direct Ollama)"
echo "──────────────────────────────────────────────────────"
uv run python -m minisweagent.run.benchmarks.swebench \
    -c "$SWEBENCH_YAML" \
    -c "$SWE_BENCH_CONFIG_DIR/ollama_baseline.yaml" \
    --subset lite \
    --split dev \
    --slice "$SLICE" \
    --output "$OUTDIR/baseline" \
    --workers 1
check_exit_statuses "$OUTDIR/baseline" "Baseline run"
echo "[ok] Baseline done"

# ── STEP 2: Start Atelier proxy ──────────────────────────────────────
echo ""
echo "──────────────────────────────────────────────────────"
echo "STEP 2/3  Starting Atelier proxy on :11435"
echo "──────────────────────────────────────────────────────"
uv run python benchmarks/swe/atelier_proxy.py \
    --upstream http://localhost:11434/v1 \
    --port 11435 \
    --log "$PROXY_LOG" &
PROXY_PID=$!
echo "$PROXY_PID" > "$PROXY_PID_FILE"
echo "[ok] Proxy PID $PROXY_PID"
sleep 3  # wait for proxy to bind

# Verify proxy is up
if ! curl -s http://localhost:11435/v1/models >/dev/null 2>&1; then
    echo "ERROR: Atelier proxy did not start"
    kill "$PROXY_PID" 2>/dev/null || true
    exit 1
fi
echo "[ok] Proxy responding"

# ── STEP 3: Atelier run (via proxy) ─────────────────────────────────
echo ""
echo "──────────────────────────────────────────────────────"
echo "STEP 3/3  Atelier run (through compression proxy)"
echo "──────────────────────────────────────────────────────"
uv run python -m minisweagent.run.benchmarks.swebench \
    -c "$SWEBENCH_YAML" \
    -c "$SWE_BENCH_CONFIG_DIR/ollama_atelier.yaml" \
    --subset lite \
    --split dev \
    --slice "$SLICE" \
    --output "$OUTDIR/atelier" \
    --workers 1
check_exit_statuses "$OUTDIR/atelier" "Atelier run"
echo "[ok] Atelier run done"

# ── Stop proxy ────────────────────────────────────────────────────────
kill "$PROXY_PID" 2>/dev/null || true
PROXY_PID=""
echo "[ok] Proxy stopped"

# ── Generate preds.json ───────────────────────────────────────────────
echo ""
echo "Generating preds.json files..."
uv run python benchmarks/swe/make_preds.py \
    --input "$OUTDIR/baseline" \
    --output "$OUTDIR/baseline_preds.json" \
    --run-id "atelier-baseline-$(date +%Y%m%d)"

uv run python benchmarks/swe/make_preds.py \
    --input "$OUTDIR/atelier" \
    --output "$OUTDIR/atelier_preds.json" \
    --run-id "atelier-compressed-$(date +%Y%m%d)"

# ── Print summary ─────────────────────────────────────────────────────
echo ""
uv run python benchmarks/swe/report.py \
    --baseline "$OUTDIR/baseline_preds.json" \
    --atelier "$OUTDIR/atelier_preds.json" \
    --savings-log "$PROXY_LOG" \
    --output "$OUTDIR/report.md"

echo ""
echo "====================================================="
echo " Done. Next: submit to sb-cli"
echo "  sb-cli submit swe-bench_lite dev \\"
echo "    --predictions_path $OUTDIR/baseline_preds.json \\"
echo "    --run_id atelier-baseline"
echo ""
echo "  sb-cli submit swe-bench_lite dev \\"
echo "    --predictions_path $OUTDIR/atelier_preds.json \\"
echo "    --run_id atelier-compressed"
echo "====================================================="
