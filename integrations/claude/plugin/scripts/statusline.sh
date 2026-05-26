#!/usr/bin/env bash
# Atelier statusLine script for Claude Code.
# Prints one compact row that fits inside Claude's native agent frame:
#   atelier | Sonnet ... · ctx ... · ...

set -u
input=$(cat)
PLUGIN_LABEL="atelier"

if command -v jq >/dev/null 2>&1; then
  # IFS=$'\t' so spaces in fields like model display_name (e.g. "Opus 4.7")
  # don't cause field-shift that corrupts SESSION_ID (the trailing variable
  # otherwise swallows all remaining whitespace + tab + real id).
  IFS=$'\t' read -r MODEL PCT COST DUR_MS IN_TOK OUT_TOK CACHE_R CACHE_W SESSION_ID MODEL_ID <<<"$(printf '%s' "$input" | jq -r '
    [
      # MODEL = display_name for the UI label ("Opus 4.7")
      (.model.display_name // .model.id // "claude"),
      (.context_window.used_percentage // 0),
      (.cost.total_cost_usd // 0),
      (.cost.total_duration_ms // 0),
      (.context_window.current_usage.input_tokens // 0),
      (.context_window.current_usage.output_tokens // 0),
      (.context_window.current_usage.cache_read_input_tokens // 0),
      (.context_window.current_usage.cache_creation_input_tokens // 0),
      (.session_id // ""),
      # MODEL_ID = canonical id ("claude-opus-4-7") for pricing lookups
      (.model.id // .model.display_name // "")
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
  MODEL=$(read_field "model.display_name" "$(read_field "model.id" "claude")")
  MODEL_ID=$(read_field "model.id" "$MODEL")
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

ATELIER_STATUS_ROOT="${ATELIER_ROOT:-${ATELIER_STORE_ROOT:-${HOME}/.atelier}}"
export ATELIER_STATUS_ROOT
# savings_summary.py reads ATELIER_ROOT (not ATELIER_STATUS_ROOT) — keep them in sync
export ATELIER_ROOT="${ATELIER_STATUS_ROOT}"
export ATELIER_STATUS_SESSION_ID="${SESSION_ID:-}"
# Pass canonical model id (preferred) then display name as fallback so
# pricing lookups hit the LiteLLM catalog even when only a display name is
# available from Claude Code's context_window payload.
export ATELIER_STATUS_MODEL="${MODEL_ID:-${MODEL:-}}"
export ATELIER_STATUS_MODEL_DISPLAY="${MODEL:-}"
ATELIER_PY="$(bash "$(dirname "${BASH_SOURCE[0]}")/_atelier_python.sh" 2>/dev/null)"
ATELIER_PY="${ATELIER_PY:-python3}"

# Compute savings using the unified savings_summary module.
# Derive the `atelier` CLI from the same bin dir as ATELIER_PY (avoids -m
# failure when the package lacks __main__.py in some install layouts).
_ATELIER_BIN="$(dirname "${ATELIER_PY}")/atelier"
if [ -x "${_ATELIER_BIN}" ]; then
  SAVED_LINE=$("${_ATELIER_BIN}" savings --line 2>/dev/null)
fi
if [ -z "${SAVED_LINE:-}" ]; then
  SAVED_LINE=$(uv run --quiet atelier savings --line 2>/dev/null)
fi
# Older installed CLIs emit the pre-I/C/O 7-field format. Retry through the
# local project entrypoint so statusline development picks up the new fields
# before the global binary is upgraded.
SAVED_FIELD_COUNT=$(printf '%s' "${SAVED_LINE:-}" | awk -F'|' '{print NF}' 2>/dev/null || echo 0)
if [ "${SAVED_FIELD_COUNT:-0}" -lt 10 ] 2>/dev/null; then
  SAVED_LINE=$(uv run --quiet atelier savings --line 2>/dev/null)
fi
IFS='|' read -r SAVED_USD SAVED_CTX SAVED_CALLS STATUS_TEXT ROUTING_USD SESSION_BASE_COST CUMULATIVE_TOK DISPLAY_IN_TOK DISPLAY_CACHE_TOK DISPLAY_OUT_TOK <<<"${SAVED_LINE:-}"
[ -z "${SAVED_USD:-}" ] && SAVED_USD="\$0.000"
[ -z "${SAVED_CTX:-}" ] && SAVED_CTX="0"
[ -z "${SAVED_CALLS:-}" ] && SAVED_CALLS="0"
[ -z "${ROUTING_USD:-}" ] && ROUTING_USD="\$0.000"
[ -z "${SESSION_BASE_COST:-}" ] && SESSION_BASE_COST="0"
[ -z "${CUMULATIVE_TOK:-}" ] && CUMULATIVE_TOK="0"
[ -z "${DISPLAY_IN_TOK:-}" ] && DISPLAY_IN_TOK="0"
[ -z "${DISPLAY_CACHE_TOK:-}" ] && DISPLAY_CACHE_TOK="0"
[ -z "${DISPLAY_OUT_TOK:-}" ] && DISPLAY_OUT_TOK="0"
# Cost = max(transcript-derived, live Claude cost). Both are cumulative; we
# trust whichever is larger so the very first frame of a resumed session
TOTAL_COST=$(awk "BEGIN { a=${SESSION_BASE_COST:-0}; b=${COST:-0}; printf \"%.3f\", (a>b?a:b) }" 2>/dev/null || echo "0")
COST_FMT=$(printf '$%.3f' "$TOTAL_COST" 2>/dev/null || echo "\$0.000")

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

# Prefer transcript-derived cumulative buckets when available. Claude's live
# context_window snapshot can be turn-local and regularly understates the true
# session totals, especially on cache-heavy sessions.
LIVE_DISPLAY_IN=$(( ${IN_TOK:-0} + ${CACHE_W:-0} ))
LIVE_DISPLAY_CACHE=${CACHE_R:-0}
LIVE_DISPLAY_OUT=${OUT_TOK:-0}

if [ "${DISPLAY_IN_TOK:-0}" -gt 0 ] 2>/dev/null || [ "${DISPLAY_CACHE_TOK:-0}" -gt 0 ] 2>/dev/null || [ "${DISPLAY_OUT_TOK:-0}" -gt 0 ] 2>/dev/null; then
  TOK_IN_F=$(fmt_tok "${DISPLAY_IN_TOK:-0}")
  TOK_CACHE_F=$(fmt_tok "${DISPLAY_CACHE_TOK:-0}")
  TOK_OUT_F=$(fmt_tok "${DISPLAY_OUT_TOK:-0}")
else
  TOK_IN_F=$(fmt_tok "${LIVE_DISPLAY_IN:-0}")
  TOK_CACHE_F=$(fmt_tok "${LIVE_DISPLAY_CACHE:-0}")
  TOK_OUT_F=$(fmt_tok "${LIVE_DISPLAY_OUT:-0}")
fi
TOK_DISPLAY="I: ${TOK_IN_F} C: ${TOK_CACHE_F} O: ${TOK_OUT_F}"
# Calls-saved counter intentionally not shown in the statusline.
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

printf '%s%s%s %s %s%s ctx %s%% %s %s(%s) ↓ %s%s(%s)%s%s %s %dm%02ds\n' \
  "$C_BRAND" "$PLUGIN_LABEL" "$C_RESET" \
  "$PIPE" "$MODEL" "$STATUS_SEG" "$PCT_INT" \
  "$PIPE" "$COST_FMT" "$TOK_DISPLAY" \
  "$C_GREEN" "$SAVED_USD" "$SAVED_CTX" "$C_RESET" \
  "$ROUTING_SEG" \
  "$PIPE" "$MINS" "$SECS"
