#!/usr/bin/env bash
# Atelier statusLine script for Claude Code.
# Prints one compact row that fits inside Claude's native agent frame:
#   atelier | Sonnet ... · ctx ... · ...

set -u
input=$(cat)
PLUGIN_LABEL="atelier"

if command -v jq >/dev/null 2>&1; then
  read -r MODEL PCT COST DUR_MS IN_TOK OUT_TOK CACHE_R CACHE_W SESSION_ID <<<"$(printf '%s' "$input" | jq -r '
    [
      (.model.display_name // .model.id // "claude"),
      (.context_window.used_percentage // 0),
      (.cost.total_cost_usd // 0),
      (.cost.total_duration_ms // 0),
      (.context_window.current_usage.input_tokens // 0),
      (.context_window.current_usage.output_tokens // 0),
      (.context_window.current_usage.cache_read_input_tokens // 0),
      (.context_window.current_usage.cache_creation_input_tokens // 0),
      (.session_id // "")
    ] | @tsv
  ' 2>/dev/null)"
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
  # Humanize token counts: 999 → 999, 2_164_000 → 2.1M, 695_000 → 695k.
  # The 1M threshold avoids the "2164k" eyesore.
  local n=$1
  if [ "$n" -ge 1000000 ] 2>/dev/null; then
    # one decimal place: integer division on (n*10/1_000_000) then split
    local scaled=$(( n * 10 / 1000000 ))
    printf '%d.%dM' $(( scaled / 10 )) $(( scaled % 10 ))
  elif [ "$n" -ge 1000 ] 2>/dev/null; then
    printf '%dk' $(( n / 1000 ))
  else
    printf '%d' "$n"
  fi
  }

CACHE_F=$(fmt_tok "${CACHE_R:-0}")
CACHE_WF=$(fmt_tok "${CACHE_W:-0}")

ATELIER_STATUS_ROOT="${ATELIER_ROOT:-${ATELIER_STORE_ROOT:-${HOME}/.atelier}}"
export ATELIER_STATUS_ROOT
export ATELIER_STATUS_USD_PER_1K="${ATELIER_USD_PER_1K_TOKENS:-0.003}"
export ATELIER_STATUS_SESSION_ID="${SESSION_ID:-}"
export ATELIER_STATUS_MODEL="${MODEL:-}"
ATELIER_PY="$(bash "$(dirname "${BASH_SOURCE[0]}")/_atelier_python.sh" 2>/dev/null)"
ATELIER_PY="${ATELIER_PY:-python3}"
SAVED_LINE=$("${ATELIER_PY}" 2>/dev/null <<'PYEOF'
import json
import os
from pathlib import Path

from atelier.core.capabilities.plugin_runtime import load_live_savings_summary

root_env = os.environ.get("ATELIER_STATUS_ROOT") or ""
root = Path(root_env) if root_env else None
usd_per_1k = float(os.environ["ATELIER_STATUS_USD_PER_1K"])
saved_usd = 0.0
ctx_saved = 0
smart_calls = 0
routing_saved_usd = 0.0
session_id = os.environ.get("ATELIER_STATUS_SESSION_ID") or ""
status_text = ""

def read_json(name: str) -> dict:
  if root is None:
    return {}
  path = root / name
  if not path.is_file():
    return {}
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}

if session_id:
  if root is None:
    stats = None
  else:
    stats = root / "session_stats" / f"{session_id}.json"
  if stats and stats.is_file():
    try:
      data = json.loads(stats.read_text(encoding="utf-8"))
      savings = data.get("savings") or {}
      smart_calls = int(savings.get("calls_saved", 0) or 0)
      ctx_saved = int(savings.get("tokens_saved", 0) or 0)
    except Exception:
      pass
  if root is not None and smart_calls == 0 and ctx_saved == 0 and saved_usd <= 0 and routing_saved_usd <= 0:
    live = load_live_savings_summary(root, session_id=session_id)
    smart_calls = max(smart_calls, int(live.get("calls_saved", 0) or 0))
    ctx_saved = max(ctx_saved, int(live.get("tokens_saved", 0) or 0))
    saved_usd = max(saved_usd, float(live.get("saved_usd", 0.0) or 0.0))
    routing_saved_usd = max(routing_saved_usd, float(live.get("routing_saved_usd", 0.0) or 0.0))

if saved_usd <= 0 and ctx_saved > 0:
  # Fallback when no live event exists for this session yet.
  # Use LiteLLM + pricing.yaml (the same path live events use) instead of the
  # flat ATELIER_USD_PER_1K_TOKENS rate, so output/cache-read asymmetry is
  # honored. Atelier savings are context-not-loaded, so price as input tokens.
  try:
    from atelier.core.capabilities.pricing import get_model_pricing
    model_id = os.environ.get("ATELIER_STATUS_MODEL") or os.environ.get("ATELIER_MODEL") or "_default"
    saved_usd = float(get_model_pricing(model_id).tokens_to_usd(ctx_saved, "input"))
  except Exception:
    saved_usd = (ctx_saved / 1000.0) * usd_per_1k
  if saved_usd <= 0:
    saved_usd = (ctx_saved / 1000.0) * usd_per_1k

update = read_json("update.json")
auth = read_json("auth.json")
subscription = read_json("subscription.json")

if ((not auth) or auth.get("authenticated") is False) and os.environ.get("ATELIER_HIDE_MISSING_LOGIN") != "1":
  status_text = "login"
elif update.get("toVersion") and update.get("toVersion") != update.get("fromVersion"):
  status_text = f"update {update.get('toVersion')}"
elif subscription.get("warning"):
  status_text = str(subscription.get("message") or "subscription")[:40]

def k(n: int) -> str:
  # Mirror the bash fmt_tok: <1k literal, <1M as Nk, >=1M as N.NM.
  if n >= 1_000_000:
    return f"{n / 1_000_000:.1f}M"
  if n >= 1000:
    return f"{n // 1000}k"
  return str(n)

print(f"${saved_usd:.3f}|{k(ctx_saved)}|{smart_calls}|{status_text}|${routing_saved_usd:.3f}")
PYEOF
)
IFS='|' read -r SAVED_USD SAVED_CTX SAVED_CALLS STATUS_TEXT ROUTING_USD <<EOF
$SAVED_LINE
EOF
[ -z "$SAVED_USD" ] && SAVED_USD="\$0.000"
[ -z "$SAVED_CTX" ] && SAVED_CTX="0"
[ -z "$SAVED_CALLS" ] && SAVED_CALLS="0"
[ -z "$ROUTING_USD" ] && ROUTING_USD="\$0.000"

# Persist real API cost so the Stop hook can use it instead of estimating.
# The Stop hook payload from Claude Code never includes the total cost, so we
# cache it here (written after every assistant turn) and read it there.
if [ -n "${SESSION_ID:-}" ] && [ "${COST:-0}" != "0" ]; then
  _COST_DIR="${ATELIER_STATUS_ROOT}/session_costs"
  mkdir -p "$_COST_DIR" 2>/dev/null
  printf '%s' "$COST" > "${_COST_DIR}/${SESSION_ID}.txt" 2>/dev/null || true
fi

if [ -n "${ATELIER_NO_COLOR:-}" ]; then
  C_BRAND=""; C_PIPE=""; C_DIM=""; C_GREEN=""; C_RESET=""
else
  C_BRAND=$'\033[1;38;2;168;85;247m'
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

# Calls-saved counter intentionally not shown in the statusline.
# Until the calibration store from tests/benchmarks/ feeds equivalent_calls,
# the per-tool "calls saved" number is a guessed multiplier and showing it
# next to a real dollar figure misleads. Tokens-saved (chars-of-context not
# loaded) is measurable today.
SAVED_CALLS_SEG=""
if [ -n "${STATUS_TEXT:-}" ]; then
  STATUS_SEG=" ${SEP} ${STATUS_TEXT}"
else
  STATUS_SEG=""
fi

if [ "$ROUTING_USD" != "\$0.000" ]; then
  ROUTING_SEG=" ${SEP} routing: ${ROUTING_USD}"
else
  ROUTING_SEG=""
fi

printf '%s%s%s %s %s%s ctx %s%% cache %s%s %s %s ↓ %s%s(%s)%s%s %s %dm%02ds\n' \
  "$C_BRAND" "$PLUGIN_LABEL" "$C_RESET" \
  "$PIPE" "$MODEL" "$STATUS_SEG" "$PCT_INT" \
  "$CACHE_F" "$CACHE_NEW_SEG" \
  "$PIPE" "$COST_FMT" \
  "$C_GREEN" "$SAVED_USD" "$SAVED_CTX" "$C_RESET" \
  "$ROUTING_SEG" \
  "$PIPE" "$MINS" "$SECS"
