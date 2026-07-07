#!/usr/bin/env bash
# statusline.sh -- Codex command-backed statusline for Atelier.
#
# Match the Claude statusline shape:
#   ❯ atelier | gpt-5.5 xhigh ctx 1.1M $0.012(I:30k C:10k O:5k) ↓ $0.045(R:12k C:2k)
#
# Codex statusline input is version-dependent. Newer command-backed builds may
# pass JSON; some builds expose the native footer text. Both are parsed into the
# same fields and rendered through `atelier savings --segment`, the same backend
# used by the Claude script.
set -uo pipefail

input="$(cat 2>/dev/null || true)"
PLUGIN_LABEL="❯ atelier"

MODEL=""
EFFORT=""
SESSION_ID=""
COST="0"
CTX_PCT=""
USED_TOK=""
IN_TOK=""
OUT_TOK=""
CACHE_R="0"
CACHE_W="0"
HOST_LINE=""

tok_to_int() {
  local raw="${1:-0}"
  awk -v raw="$raw" 'BEGIN {
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", raw)
    if (raw == "") { print 0; exit }
    suffix = substr(raw, length(raw), 1)
    mult = 1
    if (suffix ~ /[kK]/) { mult = 1000; raw = substr(raw, 1, length(raw) - 1) }
    else if (suffix ~ /[mM]/) { mult = 1000000; raw = substr(raw, 1, length(raw) - 1) }
    else if (suffix ~ /[bB]/) { mult = 1000000000; raw = substr(raw, 1, length(raw) - 1) }
    if (raw !~ /^[0-9]+(\.[0-9]+)?$/) { print 0; exit }
    printf "%d", raw * mult
  }'
}

fmt_ctx() {
  local n="${1:-0}"
  if [ "$n" -ge 1000000 ] 2>/dev/null; then
    local s=$(( n * 10 / 1000000 ))
    printf '%d.%dM' $(( s / 10 )) $(( s % 10 ))
  elif [ "$n" -ge 1000 ] 2>/dev/null; then
    printf '%dk' $(( n / 1000 ))
  else
    printf '%d' "$n"
  fi
}

if command -v jq >/dev/null 2>&1 && [ -n "$input" ] && printf '%s' "$input" | jq -e . >/dev/null 2>&1; then
  mapfile -t fields < <(printf '%s' "$input" | jq -r '
    [ (.model.name // .model_display_name // .model // .modelName // ""),
      (.effort // .reasoning_effort // .reasoning.effort // .model.effort // ""),
      (.cwd // .workspace.cwd // .workspace_root // .workspaceRoot // ""),
      (.session_id // .sessionId // .thread_id // .threadId // ""),
      (.cost.total_usd // .total_cost_usd // .cost // ""),
      (.context.used_percent // .context_used_percent // .tokens_used_percent // ""),
      (.context.used_tokens // .context.usedTokens // .context.used // .tokens.used // .tokens_used // .used_tokens // ""),
      (.usage.input_tokens // .input_tokens // .tokens.input // .tokens_in // .inputTokens // ""),
      (.usage.output_tokens // .output_tokens // .tokens.output // .tokens_out // .outputTokens // ""),
      (.usage.cache_read_tokens // .usage.cached_input_tokens // .cache_read_tokens // .cached_input_tokens // .tokens.cache_read // .tokens.cache.read // ""),
      (.usage.cache_write_tokens // .usage.cache_creation_input_tokens // .cache_write_tokens // .cache_creation_input_tokens // .tokens.cache_write // .tokens.cache.write // "")
    ][]' 2>/dev/null || true)
  MODEL="${fields[0]:-}"
  EFFORT="${fields[1]:-}"
  SESSION_ID="${fields[3]:-}"
  COST="${fields[4]:-0}"
  CTX_PCT="${fields[5]:-}"
  USED_TOK="${fields[6]:-}"
  IN_TOK="${fields[7]:-}"
  OUT_TOK="${fields[8]:-}"
  CACHE_R="${fields[9]:-0}"
  CACHE_W="${fields[10]:-0}"
else
  HOST_LINE="$(printf '%s' "$input" | sed -n '1s/[[:space:]]*$//p')"
  MODEL="$(printf '%s' "$HOST_LINE" | awk -F ' · ' '{print $1}')"
  case "$MODEL" in
    *' xhigh'|*' high'|*' medium'|*' low'|*' minimal')
      EFFORT="${MODEL##* }"
      MODEL="${MODEL% *}"
      ;;
  esac
  USED_TOK="$(printf '%s' "$HOST_LINE" | sed -nE 's/.*(^| · )([0-9.]+[kKmMbB]?) used( · |$).*/\2/p')"
  IN_TOK="$(printf '%s' "$HOST_LINE" | sed -nE 's/.*(^| · )([0-9.]+[kKmMbB]?) in( · |$).*/\2/p')"
  OUT_TOK="$(printf '%s' "$HOST_LINE" | sed -nE 's/.*(^| · )([0-9.]+[kKmMbB]?) out( · |$).*/\2/p')"
fi

IN_INT="$(tok_to_int "${IN_TOK:-0}")"
OUT_INT="$(tok_to_int "${OUT_TOK:-0}")"
CACHE_R_INT="$(tok_to_int "${CACHE_R:-0}")"
CACHE_W_INT="$(tok_to_int "${CACHE_W:-0}")"
USED_INT="$(tok_to_int "${USED_TOK:-0}")"
if [ "${USED_INT:-0}" -le 0 ] 2>/dev/null; then
  if [ "${CACHE_R_INT:-0}" -gt 0 ] && [ "${CACHE_R_INT:-0}" -le "${IN_INT:-0}" ] 2>/dev/null; then
    USED_INT=$(( IN_INT - CACHE_R_INT + CACHE_W_INT ))
  else
    USED_INT=$(( IN_INT + CACHE_R_INT + CACHE_W_INT ))
  fi
fi

MODEL_DISPLAY="${MODEL:-codex}"
if [ -n "${EFFORT:-}" ] && ! printf '%s' "$MODEL_DISPLAY" | grep -qi "${EFFORT}"; then
  MODEL_DISPLAY="$MODEL_DISPLAY $EFFORT"
fi
PCT_INT="${CTX_PCT%%.*}"
PCT_PART=""
[ -n "${PCT_INT:-}" ] && PCT_PART=" ${PCT_INT}%"
ACTUAL_CTX_F="$(fmt_ctx "${USED_INT:-0}")"

ATELIER_STATUS_ROOT="${ATELIER_ROOT:-${ATELIER_STORE_ROOT:-${HOME}/.atelier}}"
export ATELIER_STATUS_ROOT
export ATELIER_ROOT="${ATELIER_STATUS_ROOT}"
export ATELIER_STATUS_HOST="codex"
export ATELIER_STATUS_SESSION_ID="${SESSION_ID:-${CODEX_SESSION_ID:-}}"
export ATELIER_STATUS_MODEL="${MODEL:-$MODEL_DISPLAY}"
export ATELIER_STATUS_MODEL_DISPLAY="${MODEL_DISPLAY}"
export ATELIER_STATUSLINE_COST_USD="${COST:-0}"
export ATELIER_STATUSLINE_LIVE_IN_TOK="$(( IN_INT + CACHE_W_INT ))"
export ATELIER_STATUSLINE_LIVE_CACHE_TOK="${CACHE_R_INT:-0}"
export ATELIER_STATUSLINE_LIVE_OUT_TOK="${OUT_INT:-0}"
[ -n "${ATELIER_NO_COLOR:-}" ] && export ATELIER_STATUSLINE_NO_COLOR=1
if [ -n "${CODEX_WORKSPACE_ROOT:-}" ]; then
  export CLAUDE_WORKSPACE_ROOT="${CODEX_WORKSPACE_ROOT}"
fi

DYNAMIC_SEG=""
ATELIER_BIN="$(command -v atelier 2>/dev/null || true)"
if [ -n "$ATELIER_BIN" ]; then
  DYNAMIC_SEG="$("$ATELIER_BIN" savings --segment 2>/dev/null || true)"
fi
if [ -z "${DYNAMIC_SEG:-}" ]; then
  DYNAMIC_SEG="$(uv run --quiet atelier savings --segment 2>/dev/null || true)"
fi

if [ -n "${ATELIER_NO_COLOR:-}" ]; then
  C_BRAND=""; C_PIPE=""; C_RESET=""
else
  C_BRAND=$'\033[1;38;2;168;85;247m'
  C_PIPE=$'\033[2;38;2;200;200;200m'
  C_RESET=$'\033[0m'
fi
PIPE="${C_PIPE}|${C_RESET}"

printf '%s%s%s %s %s ctx %s%s%s\n' \
  "$C_BRAND" "$PLUGIN_LABEL" "$C_RESET" \
  "$PIPE" "$MODEL_DISPLAY" "$ACTUAL_CTX_F" "$PCT_PART" "$DYNAMIC_SEG"
