#!/usr/bin/env bash
# Atelier statusLine script for Claude Code.
# Prints one compact row that fits inside Claude's native agent frame:
#   atelier | Sonnet ... · ctx ... · ...

set -u
input=$(cat)
PLUGIN_LABEL="atelier"

if command -v jq >/dev/null 2>&1; then
  MODEL=$(printf '%s' "$input" | jq -r '.model.display_name // .model.id // "claude"' 2>/dev/null)
  PCT=$(printf '%s' "$input" | jq -r '.context_window.used_percentage // 0' 2>/dev/null)
  COST=$(printf '%s' "$input" | jq -r '.cost.total_cost_usd // 0' 2>/dev/null)
  DUR_MS=$(printf '%s' "$input" | jq -r '.cost.total_duration_ms // 0' 2>/dev/null)
  IN_TOK=$(printf '%s' "$input" | jq -r '.context_window.current_usage.input_tokens // 0' 2>/dev/null)
  OUT_TOK=$(printf '%s' "$input" | jq -r '.context_window.current_usage.output_tokens // 0' 2>/dev/null)
  CACHE_R=$(printf '%s' "$input" | jq -r '.context_window.current_usage.cache_read_input_tokens // 0' 2>/dev/null)
  CACHE_W=$(printf '%s' "$input" | jq -r '.context_window.current_usage.cache_creation_input_tokens // 0' 2>/dev/null)
  SESSION_ID=$(printf '%s' "$input" | jq -r '.session_id // ""' 2>/dev/null)
else
  read_field() {
    python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1] or '{}')
    keys = sys.argv[2].split('.')
    v = d
    for k in keys:
        if isinstance(v, dict):
            v = v.get(k)
        else:
            v = None
            break
    if v is None:
        v = sys.argv[3]
    print(v)
except Exception:
    print(sys.argv[3])
" "$input" "$1" "$2"
  }
  MODEL=$(read_field "model.display_name" "claude")
  PCT=$(read_field "context_window.used_percentage" "0")
  COST=$(read_field "cost.total_cost_usd" "0")
  DUR_MS=$(read_field "cost.total_duration_ms" "0")
  IN_TOK=$(read_field "context_window.current_usage.input_tokens" "0")
  OUT_TOK=$(read_field "context_window.current_usage.output_tokens" "0")
  CACHE_R=$(read_field "context_window.current_usage.cache_read_input_tokens" "0")
  CACHE_W=$(read_field "context_window.current_usage.cache_creation_input_tokens" "0")
  SESSION_ID=$(read_field "session_id" "")
fi

PCT_INT=${PCT%%.*}
[ -z "$PCT_INT" ] && PCT_INT=0
DUR_MS_INT=${DUR_MS%%.*}
[ -z "$DUR_MS_INT" ] && DUR_MS_INT=0
COST_FMT=$(printf '$%.3f' "$COST" 2>/dev/null || echo "\$0.000")
MINS=$(( DUR_MS_INT / 60000 ))
SECS=$(( (DUR_MS_INT % 60000) / 1000 ))

fmt_tok() {
  local n=$1
  if [ "$n" -ge 1000 ] 2>/dev/null; then
    printf '%dk' $(( n / 1000 ))
  else
    printf '%d' "$n"
  fi
}

IN_F=$(fmt_tok "${IN_TOK:-0}")
OUT_F=$(fmt_tok "${OUT_TOK:-0}")
CACHE_F=$(fmt_tok "${CACHE_R:-0}")
CACHE_WF=$(fmt_tok "${CACHE_W:-0}")

ATELIER_ROOT="${ATELIER_ROOT:-${ATELIER_STORE_ROOT:-${HOME}/.atelier}}"
export ATELIER_STATUS_ROOT="$ATELIER_ROOT"
export ATELIER_STATUS_USD_PER_1K="${ATELIER_USD_PER_1K_TOKENS:-0.003}"
export ATELIER_STATUS_SESSION_ID="${SESSION_ID:-}"
SAVED_LINE=$(python3 2>/dev/null <<'PYEOF'
import json
import os
from pathlib import Path

root = Path(os.environ["ATELIER_STATUS_ROOT"])
usd_per_1k = float(os.environ["ATELIER_STATUS_USD_PER_1K"])
saved_usd = 0.0
ctx_saved = 0
smart_calls = 0
session_id = os.environ.get("ATELIER_STATUS_SESSION_ID") or ""
status_text = ""

def read_json(name: str) -> dict:
  path = root / name
  if not path.is_file():
    return {}
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}

if session_id:
  stats = root / "session_stats" / f"{session_id}.json"
  if stats.is_file():
    try:
      data = json.loads(stats.read_text(encoding="utf-8"))
      savings = data.get("savings") or {}
      smart_calls = int(savings.get("calls_saved", 0) or 0)
      ctx_saved = int(savings.get("tokens_saved", 0) or 0)
    except Exception:
      pass

if ctx_saved > 0:
  saved_usd = (ctx_saved / 1000.0) * usd_per_1k

update = read_json("update.json")
auth = read_json("auth.json")
subscription = read_json("subscription.json")
free_plan = read_json("free_plan.json")

if auth and auth.get("authenticated") is False and os.environ.get("ATELIER_HIDE_MISSING_LOGIN") != "1":
  status_text = "login"
elif update.get("toVersion") and update.get("toVersion") != update.get("fromVersion"):
  status_text = f"update {update.get('toVersion')}"
elif subscription.get("warning"):
  status_text = str(subscription.get("message") or "subscription")[:40]
elif free_plan.get("limit"):
  limit = max(1, int(free_plan.get("limit") or 1))
  remaining = int(free_plan.get("remaining") or 0)
  used_pct = int(round(100 * max(0, limit - remaining) / limit))
  if used_pct >= 90:
    status_text = f"plan {used_pct}%"

def k(n: int) -> str:
  return f"{n//1000}k" if n >= 1000 else str(n)

print(f"${saved_usd:.3f}|{k(ctx_saved)}|{smart_calls}|{status_text}")
PYEOF
)
IFS='|' read -r SAVED_USD SAVED_CTX SAVED_CALLS STATUS_TEXT <<EOF
$SAVED_LINE
EOF
[ -z "$SAVED_USD" ] && SAVED_USD="\$0.000"
[ -z "$SAVED_CTX" ] && SAVED_CTX="0"
[ -z "$SAVED_CALLS" ] && SAVED_CALLS="0"

if [ -n "${ATELIER_NO_COLOR:-}" ]; then
  C_BRAND=""; C_PIPE=""; C_DIM=""; C_GREEN=""; C_RESET=""
else
  C_BRAND=$'\033[1;38;2;230;100;55m'
  C_PIPE=$'\033[2;38;2;200;200;200m'
  C_DIM=$'\033[2;38;2;200;200;200m'
  C_GREEN=$'\033[1;38;2;72;199;116m'
  C_RESET=$'\033[0m'
fi

SEP="${C_DIM}·${C_RESET}"
PIPE="${C_PIPE}|${C_RESET}"

# Build cache write segment only when non-zero (new tokens written to cache)
if [ "${CACHE_W:-0}" -gt 0 ] 2>/dev/null; then
  CACHE_NEW_SEG="+${CACHE_WF}"
else
  CACHE_NEW_SEG=""
fi

if [ "${SAVED_CALLS:-0}" -gt 0 ] 2>/dev/null; then
  SAVED_CALLS_SEG=" / ${SAVED_CALLS}c"
else
  SAVED_CALLS_SEG=""
fi

if [ -n "${STATUS_TEXT:-}" ]; then
  STATUS_SEG=" ${SEP} ${STATUS_TEXT}"
else
  STATUS_SEG=""
fi

printf '%s%s%s %s %s%s ctx %s%% cache %s%s %s %s ↓ %s%s%s(%s%s%s) %s %dm%02ds\n' \
  "$C_BRAND" "$PLUGIN_LABEL" "$C_RESET" \
  "$PIPE" "$MODEL" "$STATUS_SEG" "$PCT_INT" \
  "$CACHE_F" "$CACHE_NEW_SEG" \
  "$PIPE" "$COST_FMT" \
  "$C_GREEN" "$SAVED_USD" "$C_RESET" "$C_GREEN" "${SAVED_CTX}${SAVED_CALLS_SEG}" "$C_RESET" \
  "$PIPE" "$MINS" "$SECS"
