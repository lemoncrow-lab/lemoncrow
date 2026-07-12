#!/usr/bin/env bash
# statusline.sh -- Codex command-backed statusline for LemonCrow.
#
# Match the Claude statusline shape:
#   ❯ lc | gpt-5.5 xhigh ctx 1.1M $0.012(I:30k C:10k O:5k) ↓ $0.045(R:12k C:2k)
#
# Codex statusline input is version-dependent. Newer command-backed builds may
# pass JSON; some builds expose the native footer text. Both are parsed into the
# same fields and rendered through `lc savings --segment`, the same backend
# used by the Claude script.
set -uo pipefail

input="$(cat 2>/dev/null || true)"
PLUGIN_LABEL="❯ lc"

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
WORKSPACE=""

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
  json_fields="$(printf '%s' "$input" | jq -r '
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
    ][]' 2>/dev/null || true)"
  field_index=0
  while IFS= read -r field; do
    case "$field_index" in
      0) MODEL="$field" ;;
      1) EFFORT="$field" ;;
      2) WORKSPACE="$field" ;;
      3) SESSION_ID="$field" ;;
      4) COST="${field:-0}" ;;
      5) CTX_PCT="$field" ;;
      6) USED_TOK="$field" ;;
      7) IN_TOK="$field" ;;
      8) OUT_TOK="$field" ;;
      9) CACHE_R="${field:-0}" ;;
      10) CACHE_W="${field:-0}" ;;
    esac
    field_index=$((field_index + 1))
  done <<EOF
$json_fields
EOF
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

LEMONCROW_STATUS_ROOT="${LEMONCROW_ROOT:-${LEMONCROW_STORE_ROOT:-${HOME}/.lemoncrow}}"
export LEMONCROW_STATUS_ROOT
export LEMONCROW_ROOT="${LEMONCROW_STATUS_ROOT}"
export LEMONCROW_STATUS_HOST="codex"
export LEMONCROW_STATUS_SESSION_ID="${SESSION_ID:-${CODEX_SESSION_ID:-}}"
export LEMONCROW_STATUS_WORKSPACE_ROOT="${WORKSPACE:-${CODEX_WORKSPACE_ROOT:-$PWD}}"
export LEMONCROW_STATUS_MODEL="${MODEL:-$MODEL_DISPLAY}"
export LEMONCROW_STATUS_MODEL_DISPLAY="${MODEL_DISPLAY}"
export LEMONCROW_STATUSLINE_COST_USD="${COST:-0}"
export LEMONCROW_STATUSLINE_LIVE_IN_TOK="$(( IN_INT + CACHE_W_INT ))"
export LEMONCROW_STATUSLINE_LIVE_CACHE_TOK="${CACHE_R_INT:-0}"
export LEMONCROW_STATUSLINE_LIVE_OUT_TOK="${OUT_INT:-0}"
[ -n "${LEMONCROW_NO_COLOR:-}" ] && export LEMONCROW_STATUSLINE_NO_COLOR=1
if [ -n "${CODEX_WORKSPACE_ROOT:-}" ]; then
  export CLAUDE_WORKSPACE_ROOT="${CODEX_WORKSPACE_ROOT}"
fi

DYNAMIC_SEG=""
# Short-TTL per-session cache (mirrors the Claude statusline's 8s pattern) so
# a render doesn't spawn the CLI every frame. Keyed strictly by the real
# session id with NO fallback key: an unkeyed cache would leak one window's
# savings segment into another, so with no session id we skip caching.
_NOW_S=$(date +%s)
_SEG_SID="${SESSION_ID:-${CODEX_SESSION_ID:-}}"
_SEG_CACHE="${LEMONCROW_STATUS_ROOT}/statusline_segment_cache_codex_${_SEG_SID}"
_SEG_CACHE_TS="${LEMONCROW_STATUS_ROOT}/statusline_segment_ts_codex_${_SEG_SID}"
if [ -n "${_SEG_SID}" ]; then
  _CACHED_TS=$(cat "${_SEG_CACHE_TS}" 2>/dev/null || echo 0)
  _CACHE_AGE=$(( _NOW_S - ${_CACHED_TS:-0} ))
  if [ "${_CACHE_AGE}" -le 8 ] && [ -f "${_SEG_CACHE}" ]; then
    DYNAMIC_SEG=$(cat "${_SEG_CACHE}" 2>/dev/null || true)
  fi
fi
if [ -z "${DYNAMIC_SEG:-}" ]; then
  LEMONCROW_BIN="$(command -v lemoncrow 2>/dev/null || true)"
  if [ -n "$LEMONCROW_BIN" ]; then
    DYNAMIC_SEG="$("$LEMONCROW_BIN" savings --segment 2>/dev/null || true)"
  fi
  if [ -z "${DYNAMIC_SEG:-}" ]; then
    DYNAMIC_SEG="$(uv run --quiet lemoncrow savings --segment 2>/dev/null || true)"
  fi
  # Cache only with a real session id and real live usage -- never a shared,
  # unkeyed slot, never a stale zero-token segment. Atomic renames so a
  # concurrent render never reads a torn file.
  if [ -n "${DYNAMIC_SEG:-}" ] && [ -n "${_SEG_SID}" ] && [ "${USED_INT:-0}" -gt 0 ] 2>/dev/null; then
    printf '%s' "${DYNAMIC_SEG}" > "${_SEG_CACHE}.$$" 2>/dev/null \
      && mv -f "${_SEG_CACHE}.$$" "${_SEG_CACHE}" 2>/dev/null || true
    printf '%s' "${_NOW_S}" > "${_SEG_CACHE_TS}.$$" 2>/dev/null \
      && mv -f "${_SEG_CACHE_TS}.$$" "${_SEG_CACHE_TS}" 2>/dev/null || true
  fi
fi

if [ -n "${LEMONCROW_NO_COLOR:-}" ]; then
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
