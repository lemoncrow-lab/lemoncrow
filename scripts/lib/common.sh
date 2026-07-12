#!/usr/bin/env bash
# common.sh — Shared configuration and functions for the LemonCrow installers.
#
# This file is a LIBRARY. It must be SOURCED, not executed directly. It is
# sourced by:
#   * scripts/local.sh  — SOURCE install (uv tool install from a repo checkout)
#   * scripts/bundle.sh — BINARY install (post-extract setup for a pre-built binary)
#
# Both callers run `set -euo pipefail` before sourcing this file and own their
# own argument parsing. This library provides the shared configuration,
# logging/spinner UI, interactive selectors, host wizard, optional tool
# installers, and the run_setup() post-install orchestrator.
#
# Optional environment variables:
#   LEMONCROW_REPO_URL   Git URL (default: https://github.com/lemoncrowhq/lemoncrow.git)
#   LEMONCROW_REF        Git ref to install (default: main)
#   LEMONCROW_INSTALL_DIR Install location (default: current directory)
#   LEMONCROW_BIN_DIR    Global bin dir for console scripts (default: ~/.local/bin)
#   LEMONCROW_TOOL_DIR   uv tool environment dir (default: ~/.local/share/uv/tools)
#   LEMONCROW_NO_HOSTS   If set to 1, skip agent-host integration install scripts
#   LEMONCROW_NO_SERVICECTL If set to 1, skip starting the background service controller
#   LEMONCROW_SERVICECTL_INTERVAL_SECONDS Poll interval for servicectl (default: 60)
#   LEMONCROW_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS Periodic maintenance interval (default: 86400)
#   LEMONCROW_DRY_RUN    If set to 1, print planned actions and exit
#   LEMONCROW_NO_STACK   If set to 1, skip starting the visualization stack (service + frontend)
#   LEMONCROW_ADVANCED   If set to 1, enable Docker sidecar install (requires --memory)
#   LEMONCROW_MEMORY_BACKEND  Memory sidecar to install: letta | openmemory (default: none)
#   LEMONCROW_ZOEKT      Install the persistent Zoekt code-search sidecar (default: 0; set 1 to opt in)
#   LEMONCROW_INSTALL_RTK 1 = install rtk (command compactor) without prompting; 0 = never offer
#                      (default: interactive y/N prompt when cargo exists and rtk is absent)
#   LEMONCROW_VERBOSE    If set to 1, show verbose installation logs (default: 0)
#   LEMONCROW_STRICT     If set to 1, treat selected post-install degradations as errors
#   LEMONCROW_NON_INTERACTIVE If set to 1, disable all interactive prompts
#   LEMONCROW_INSTALL_CLEAN_PROCESSES If set to 0, do not stop old LemonCrow processes before reinstall
#   LEMONCROW_ZOEKT_AUTO_INSTALL If set to 1, non-interactive runs install local zoekt binaries when missing (default: 1)
#   LEMONCROW_INSTALL_LOG_FILE Optional install log path (default: /tmp/lemoncrow-install.<ts>.<pid>.log)

if [[ -t 1 ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[38;2;155;117;217m')"
    C_PURPLE="$(printf '\033[38;2;155;117;217m')"
else
    C_RESET=""
    C_BOLD=""
    C_DIM=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
    C_CYAN=""
    C_PURPLE=""
fi
if [[ -n "${FORCE_COLOR:-}${CLICOLOR_FORCE:-}" && -z "${NO_COLOR:-}" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[38;2;155;117;217m')"
    C_PURPLE="$(printf '\033[38;2;155;117;217m')"
fi
C_FRAME="$C_DIM"
ACTIVE_BAR="┃"
if [[ "${LC_ALL:-${LANG:-}}" != *"UTF-8"* && "${LC_ALL:-${LANG:-}}" != *"utf8"* ]]; then
    ACTIVE_BAR="|"
fi

LEMONCROW_INSTALL_DIR="${LEMONCROW_INSTALL_DIR:-$(pwd)}"
LEMONCROW_BIN_DIR="${LEMONCROW_BIN_DIR:-${HOME}/.lemoncrow/bin}"
LEMONCROW_NODE_DIR="${LEMONCROW_NODE_DIR:-${HOME}/.lemoncrow/node}"
LEMONCROW_TOOL_DIR="${LEMONCROW_TOOL_DIR:-${HOME}/.lemoncrow/uv-tools}"
LEMONCROW_INSTALL_RECORD="${LEMONCROW_INSTALL_RECORD:-${HOME}/.lemoncrow/install_dir}"
LEMONCROW_NO_HOSTS="${LEMONCROW_NO_HOSTS:-0}"
LEMONCROW_NO_SERVICECTL="${LEMONCROW_NO_SERVICECTL:-0}"
LEMONCROW_DRY_RUN="${LEMONCROW_DRY_RUN:-0}"

persist_install_record() {
    local record_dir
    record_dir="$(dirname "$LEMONCROW_INSTALL_RECORD")"

    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] mkdir -p $record_dir"
        echo "[dry-run] printf '%s\\n' '$LEMONCROW_INSTALL_DIR' > '$LEMONCROW_INSTALL_RECORD'"
        return
    fi

    mkdir -p "$record_dir"
    printf '%s\n' "$LEMONCROW_INSTALL_DIR" > "$LEMONCROW_INSTALL_RECORD"
}

LEMONCROW_SERVICECTL_INTERVAL_SECONDS="${LEMONCROW_SERVICECTL_INTERVAL_SECONDS:-60}"
LEMONCROW_SERVICECTL_INTERVAL_SECONDS="${LEMONCROW_SERVICECTL_INTERVAL_SECONDS:-60}"
LEMONCROW_SERVICECTL_INTERVAL_SECONDS="${LEMONCROW_SERVICECTL_INTERVAL_SECONDS:-60}"
LEMONCROW_NO_STACK="${LEMONCROW_NO_STACK:-0}"
LEMONCROW_ADVANCED="${LEMONCROW_ADVANCED:-0}"
LEMONCROW_MEMORY_BACKEND="${LEMONCROW_MEMORY_BACKEND:-}"   # letta | openmemory | (empty = none)
LEMONCROW_TELEGRAPHIC="${LEMONCROW_TELEGRAPHIC:-}"         # ultra | lite | off (empty = prompt, default ultra)
LEMONCROW_AUTO_OPTIMIZE="${LEMONCROW_AUTO_OPTIMIZE:-1}"   # 1 = enable periodic optimize automation
# Local knowledge extraction (opt-in; off by default to bound spend). Distils
# review rules from .lessons into the reviewer overlay.
[[ -n "${LEMONCROW_KB_EXTRACT+x}" ]] && LEMONCROW_KB_EXTRACT_PRESET=1 || LEMONCROW_KB_EXTRACT_PRESET=0
LEMONCROW_KB_EXTRACT="${LEMONCROW_KB_EXTRACT:-0}"      # 1 = run knowledge extraction during setup
LEMONCROW_KB_HOST="${LEMONCROW_KB_HOST:-auto}"        # auto | claude | codex | ollama
LEMONCROW_KB_MODEL="${LEMONCROW_KB_MODEL:-}"          # model id (required for ollama)
LEMONCROW_KB_MAX_SPEND="${LEMONCROW_KB_MAX_SPEND:-0.50}"  # hard USD cap per run (auto/claude)
# All-sessions Recall. Background-index past transcripts so recall spans every
# session. On by default (the local embedder is free; set to 0 to disable).
# Claude has no embeddings API, so it is not an embedder choice.
[[ -n "${LEMONCROW_RECALL_INDEX+x}" ]] && LEMONCROW_RECALL_PRESET=1 || LEMONCROW_RECALL_PRESET=0
LEMONCROW_RECALL_INDEX="${LEMONCROW_RECALL_INDEX:-1}"            # 1 = enable SessionStart background indexer (default)
LEMONCROW_RECALL_EMBEDDER="${LEMONCROW_RECALL_EMBEDDER:-local}"  # local | openai (codex) | ollama
LEMONCROW_RECALL_EMBED_MODEL="${LEMONCROW_RECALL_EMBED_MODEL:-}" # embed model (e.g. an ollama model name)
LEMONCROW_ZOEKT="${LEMONCROW_ZOEKT:-0}"                    # default off; 1 = install the persistent Zoekt sidecar
# Pinned rtk release (external command compactor). Reproducible installs: bump
# deliberately at LemonCrow release time. Explicitly-empty LEMONCROW_RTK_TAG=""
# means unpinned default-branch HEAD (":-" would swallow the empty override).
LEMONCROW_RTK_TAG="${LEMONCROW_RTK_TAG-v0.43.0}"
OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
BINARY_SUFFIX="${OS_NAME}-${ARCH}"
LEMONCROW_REPO_URL="${LEMONCROW_REPO_URL:-https://github.com/lemoncrowhq/lemoncrow.git}"
LEMONCROW_REF="${LEMONCROW_REF:-main}"
LEMONCROW_STRICT="${LEMONCROW_STRICT:-0}"
LEMONCROW_VERBOSE="${LEMONCROW_VERBOSE:-0}"
LEMONCROW_NON_INTERACTIVE="${LEMONCROW_NON_INTERACTIVE:-0}"
LEMONCROW_INSTALL_CLEAN_PROCESSES="${LEMONCROW_INSTALL_CLEAN_PROCESSES:-1}"
export LEMONCROW_VERBOSE
LEMONCROW_ZOEKT_AUTO_INSTALL="${LEMONCROW_ZOEKT_AUTO_INSTALL:-1}"
LEMONCROW_INSTALL_LOG_FILE="${LEMONCROW_INSTALL_LOG_FILE:-}"
INSTALL_ZOEKT_LOCAL=0
INSTALL_RTK=0
STACK_STARTED=0

# Companion-binary version pins for this release (Node/Go/Zoekt). Kept in a
# sibling file so a release bump is a single edit; the reconcile in
# prompt_local_zoekt_selection / install_node_if_needed acts only on pin changes.
# Falls back to in-usage defaults if the file is absent.
# shellcheck source=versions.sh
[[ -r "${BASH_SOURCE[0]%/*}/versions.sh" ]] && source "${BASH_SOURCE[0]%/*}/versions.sh"
PASSTHROUGH=()
WARNINGS=()
ERRORS=()
FINAL_EXIT_CODE=0
HOST_FLAGS=()
HOST_SCOPE_ARGS=()
HOST_EXTRA_ARGS=()
HOST_CHOICES=()
HOST_DEFAULT_SELECTION=()
HOST_SUMMARY=()
_SPINNER_PID=""
_SPINNER_MSG=""
_SPINNER_ACTIVE=0
LEMONCROW_SPINNER_PID_FILE="${TMPDIR:-/tmp}/lemoncrow-spinner.$$.pid"
touch "$LEMONCROW_SPINNER_PID_FILE"

ORIGINAL_STDOUT_IS_TTY=0
if [[ -t 1 ]]; then
    ORIGINAL_STDOUT_IS_TTY=1
fi
# Save real terminal FD before tee redirect so spinner output never goes through the pipe buffer.
exec 7>&1

if [[ -z "$LEMONCROW_INSTALL_LOG_FILE" ]]; then
    LEMONCROW_INSTALL_LOG_FILE="${TMPDIR:-/tmp}/lemoncrow-install.$(date +%Y%m%dT%H%M%S).$$.log"
fi

mkdir -p "$(dirname "$LEMONCROW_INSTALL_LOG_FILE")" 2>/dev/null || true
: >"$LEMONCROW_INSTALL_LOG_FILE" 2>/dev/null || true
exec > >(tee -a "$LEMONCROW_INSTALL_LOG_FILE") 2>&1

trap '[[ -f "$LEMONCROW_SPINNER_PID_FILE" ]] && { _SPINNER_PID=$(cat "$LEMONCROW_SPINNER_PID_FILE" 2>/dev/null); [[ -n "$_SPINNER_PID" ]] && kill "$_SPINNER_PID" 2>/dev/null; rm -f "$LEMONCROW_SPINNER_PID_FILE"; } || true' EXIT INT TERM

log_raw() {
    [[ "$LEMONCROW_VERBOSE" == "1" ]] && return 0
    [[ -n "$1" ]] && printf "%s\n" "$1" >>"$LEMONCROW_INSTALL_LOG_FILE" || true
}
info()    { _spinner_pause; printf "%b│%b  ◇  %s\n" "$C_FRAME" "$C_RESET" "$*"; _spinner_resume; }
verbose() { [[ "$LEMONCROW_VERBOSE" == "1" ]] && info "$@" || true; }
warn()  {
    WARNINGS+=("$*")
    _spinner_pause
    printf "%b│%b  %b⚠%b  %s\n" "$C_FRAME" "$C_RESET" "$C_YELLOW" "$C_RESET" "$*"
    _spinner_resume
}
error() {
    ERRORS+=("$*")
    _spinner_pause
    printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED" "$C_RESET" "$*" >&2
    _spinner_resume
}
fail()  { error "$*"; exit 1; }
degrade() {
    if [[ "$LEMONCROW_STRICT" == "1" ]]; then
        ERRORS+=("$*")
        FINAL_EXIT_CODE=1
        _spinner_pause
        printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED" "$C_RESET" "$*" >&2
        _spinner_resume
    else
        warn "$*"
    fi
}

_spinner_run() {
    [[ "$ORIGINAL_STDOUT_IS_TTY" == "1" && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]] || return 0
    local _frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    (
        local _i=0
        while true; do
            printf "\r\033[2K%b%s%b  %b%s%b  %b%s%b " \
                "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "${_frames[$((_i % 10))]}" "$C_RESET" "$C_PURPLE" "$_SPINNER_MSG" "$C_RESET" >&7
            sleep 0.08
            _i=$((_i + 1))
        done
    ) &
    _SPINNER_PID=$!
    echo "$_SPINNER_PID" > "$LEMONCROW_SPINNER_PID_FILE"
}
_spinner_pause() {
    _SPINNER_PID=$(cat "$LEMONCROW_SPINNER_PID_FILE" 2>/dev/null)
    [[ -n "${_SPINNER_PID:-}" ]] || return 0
    kill "$_SPINNER_PID" 2>/dev/null || true
    wait "$_SPINNER_PID" 2>/dev/null || true
    _SPINNER_PID=""
    echo "" > "$LEMONCROW_SPINNER_PID_FILE"
    printf "\r\033[2K" >&7
}
_spinner_resume() { if [[ "${_SPINNER_ACTIVE:-0}" == "1" ]]; then _spinner_run; fi; }
_spinner_stop() {
    local _st="${1:-ok}"
    _spinner_pause; _SPINNER_ACTIVE=0
    case "$_st" in
        ok)   printf "%b│%b  %b✓%b  %s\n" "$C_FRAME" "$C_RESET" "$C_GREEN"  "$C_RESET" "$_SPINNER_MSG" ;;
        warn) printf "%b│%b  %b⚠%b  %s\n" "$C_FRAME" "$C_RESET" "$C_YELLOW" "$C_RESET" "$_SPINNER_MSG" ;;
        skip) printf "%b│%b  ○  %s\n"     "$C_FRAME" "$C_RESET"                            "$_SPINNER_MSG" ;;
        err)  printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED"    "$C_RESET" "$_SPINNER_MSG" >&2 ;;
    esac
}
step_start() {
    _SPINNER_ACTIVE=0; _SPINNER_MSG="$*"
    printf "%b│%b\n%b◆%b  %b%s%b\n" "$C_FRAME" "$C_RESET" "$C_FRAME" "$C_RESET" "$C_PURPLE" "$*" "$C_RESET"
}
step_done() { printf "%b│%b\n" "$C_FRAME" "$C_RESET"; }
spin() {
    # spin "message" cmd [args...]  — runs cmd with animated spinner; ✓ or ✗ on finish
    _SPINNER_MSG="$1"; shift; _SPINNER_ACTIVE=1; _spinner_run
    local _ret=0
    local _out
    _out="$("$@" 2>&1)" || _ret=$?
    log_raw "$_out"
    if [[ $_ret -eq 0 ]]; then
        _spinner_stop ok
        if [[ "$LEMONCROW_VERBOSE" == "1" && -n "$_out" ]]; then
            printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_out"
        fi
    else
        _spinner_stop err
        [[ -n "$_out" ]] && printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_out"
    fi
    _SPINNER_ACTIVE=0; return $_ret
}

spin_tail() {
    # spin_tail "message" cmd [args...] — runs cmd and renders transient tail lines.
    local _msg="$1"; shift
    local _ret=0
    local _out_file
    _out_file="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-spin-tail.XXXXXX")"

    "$@" >"$_out_file" 2>&1 &
    local _pid=$!

    if [[ "$ORIGINAL_STDOUT_IS_TTY" == "1" && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]]; then
        local _frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
        local _fi=0
        local _printed_lines=0
        while kill -0 "$_pid" 2>/dev/null; do
            if [[ $_printed_lines -gt 0 ]]; then
                local _j
                for ((_j = 0; _j < _printed_lines; _j++)); do
                    printf "\033[1A\033[2K" >&7
                done
                printf "\r" >&7
            fi

            printf "%b%s%b  %b%s%b  %b%s%b\n" \
                "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "${_frames[$((_fi % 10))]}" "$C_RESET" "$C_PURPLE" "$_msg" "$C_RESET" >&7
            _printed_lines=1
            _fi=$((_fi + 1))

            local _tail_line
            while IFS= read -r _tail_line; do
                [[ -z "${_tail_line// }" ]] && continue
                _tail_line="$(printf "%s" "$_tail_line" | sed $'s/\x1b\\[[0-9;]*m//g')"
                if ((${#_tail_line} > 140)); then
                    _tail_line="${_tail_line:0:137}..."
                fi
                printf "%b│%b    %b%s%b\n" "$C_FRAME" "$C_RESET" "$C_PURPLE" "$_tail_line" "$C_RESET" >&7
                _printed_lines=$((_printed_lines + 1))
            done < <(tail -n 2 "$_out_file")

            sleep 0.12
        done

        wait "$_pid" || _ret=$?

        if [[ $_printed_lines -gt 0 ]]; then
            local _j
            for ((_j = 0; _j < _printed_lines; _j++)); do
                printf "\033[1A\033[2K" >&7
            done
            printf "\r" >&7
        fi

        if [[ $_ret -eq 0 ]]; then
            printf "%b│%b  %b✓%b  %s\n" "$C_FRAME" "$C_RESET" "$C_GREEN" "$C_RESET" "$_msg"
        else
            printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED" "$C_RESET" "$_msg" >&2
        fi
    else
        wait "$_pid" || _ret=$?
        if [[ $_ret -eq 0 ]]; then
            printf "%b│%b  %b✓%b  %s\n" "$C_FRAME" "$C_RESET" "$C_GREEN" "$C_RESET" "$_msg"
        else
            printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED" "$C_RESET" "$_msg" >&2
        fi
    fi

    local _out=""
    _out="$(cat "$_out_file" 2>/dev/null || true)"
    log_raw "$_out"
    rm -f "$_out_file"
    if [[ $_ret -ne 0 && -n "$_out" ]]; then
        printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_out"
    fi
    return $_ret
}

spin_progress() {
    # spin_progress "message" cmd [args...] — runs cmd with a progress bar line.
    local _msg="$1"; shift
    local _ret=0
    local _out_file
    _out_file="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-progress.XXXXXX")"

    if [[ "$ORIGINAL_STDOUT_IS_TTY" == "1" && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]]; then
        "$@" >"$_out_file" 2>&1 &
        local _pid=$!
        local _pct=0
        local _width=24
        local _frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
        local _fi=0
        local _fill_char="█"
        local _empty_char="░"
        if [[ "${LC_ALL:-${LANG:-}}" != *"UTF-8"* && "${LC_ALL:-${LANG:-}}" != *"utf8"* ]]; then
            _frames=(/ - \\ \|)
            _fill_char="="
            _empty_char="-"
        fi

        while kill -0 "$_pid" 2>/dev/null; do
            if [[ "$_pct" -lt 95 ]]; then
                _pct=$((_pct + 1))
            fi
            local _filled=$((_pct * _width / 100))
            local _empty=$((_width - _filled))
            local _bar_fill _bar_empty
            _bar_fill=""
            _bar_empty=""
            local _spin="${_frames[$((_fi % ${#_frames[@]}))]}"
            _fi=$((_fi + 1))
            local _i
            for ((_i = 0; _i < _filled; _i++)); do _bar_fill+="${_fill_char}"; done
            for ((_i = 0; _i < _empty; _i++)); do _bar_empty+="${_empty_char}"; done
            printf "\r\033[2K%b%s%b  %b%s%b  %s  %b▕%b%b%b%b%b▏%b  %b%3d%%%b" \
                "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "$_spin" "$C_RESET" "$_msg" \
                "$C_DIM" "$C_RESET" "$C_CYAN" "$_bar_fill" "$C_DIM" "$_bar_empty" "$C_RESET" \
                "$C_CYAN" "$_pct" "$C_RESET" >&7
            sleep 0.12
        done

        wait "$_pid" || _ret=$?
        printf "\r\033[2K" >&7
        if [[ $_ret -eq 0 ]]; then
            local _bar_done
            _bar_done=""
            local _i
            for ((_i = 0; _i < _width; _i++)); do _bar_done+="${_fill_char}"; done
            printf "%b│%b  %b✓%b  %s  %b▕%b%b%b%b▏%b  %b100%%%b\n" \
                "$C_FRAME" "$C_RESET" "$C_GREEN" "$C_RESET" "$_msg" \
                "$C_DIM" "$C_RESET" "$C_GREEN" "$_bar_done" "$C_DIM" "$C_RESET" \
                "$C_GREEN" "$C_RESET"
        else
            printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED" "$C_RESET" "$_msg" >&2
        fi
    else
        "$@" >"$_out_file" 2>&1 || _ret=$?
        if [[ $_ret -eq 0 ]]; then
            printf "%b│%b  %b✓%b  %s\n" "$C_FRAME" "$C_RESET" "$C_GREEN" "$C_RESET" "$_msg"
        else
            printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED" "$C_RESET" "$_msg" >&2
        fi
    fi

    local _out=""
    _out="$(cat "$_out_file" 2>/dev/null || true)"
    log_raw "$_out"
    rm -f "$_out_file"

    if [[ $_ret -eq 0 ]]; then
        if [[ "$LEMONCROW_VERBOSE" == "1" && -n "$_out" ]]; then
            printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_out"
        fi
    else
        [[ -n "$_out" ]] && printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_out"
    fi
    return $_ret
}

print_installer_header() {
    local display_version=""

    # Fast path: running from a local checkout — read pyproject.toml directly.
    # BASH_SOURCE[0] here resolves to this file (scripts/lib/common.sh), not
    # the caller, so climb two levels (lib -> scripts -> repo root).
    if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
        local script_root
        script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
        if [[ -f "$script_root/pyproject.toml" ]]; then
            display_version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$script_root/pyproject.toml" | head -n 1)"
        fi
    fi

    # Network path: fetch pyproject.toml from the same ref being installed.
    if [[ -z "$display_version" ]] && command -v curl >/dev/null 2>&1; then
        local owner_repo
        owner_repo="$(printf "%s" "$LEMONCROW_REPO_URL" | sed -n 's#.*github\.com/\([^/]*/[^/]*\)\.git#\1#p')"
        if [[ -n "$owner_repo" ]]; then
            display_version="$(
                curl -sSL "https://raw.githubusercontent.com/${owner_repo}/${LEMONCROW_REF}/pyproject.toml" \
                    2>/dev/null | sed -n 's/^version = "\(.*\)"/\1/p' | head -n 1
            )"
        fi
    fi

    # Last-resort fallback.
    display_version="${display_version:-unknown}"

    echo ""
    printf "%b┌%b  LemonCrow v%s\n" "$C_FRAME" "$C_RESET" "$display_version"
    printf "%b│%b\n" "$C_FRAME" "$C_RESET"
}

print_installer_footer() {
    printf "%b│%b\n" "$C_FRAME" "$C_RESET"
}

collect_issues_from_output() {
    local output="$1"
    local line
    while IFS= read -r line; do
        line="$(printf "%s\n" "$line" | sed $'s/\x1b\\[[0-9;]*m//g')"
        case "$line" in
            *"] WARN:"*)
                WARNINGS+=("${line#*WARN: }")
                ;;
            *"] ERROR:"*)
                ERRORS+=("${line#*ERROR: }")
                ;;
        esac
    done <<<"$output"
}

# bash 3.2 compatible linear array membership check (no associative arrays)
_in_array() {
    local needle="$1"
    shift
    local item
    for item in "$@"; do
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

print_issue_group() {
    local title="$1"
    local color="$2"
    shift 2
    local entries=("$@")
    local -a unique_entries=()
    local entry
    local count=0

    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" ]] || continue
        if ! _in_array "$entry" "${unique_entries[@]+"${unique_entries[@]}"}"; then
            unique_entries+=("$entry")
            count=$((count + 1))
        fi
    done

    [[ $count -gt 0 ]] || return 0
    printf "%b│%b  %b%s (%d)%b\n" "$C_FRAME" "$C_RESET" "$color" "$title" "$count" "$C_RESET"
    for entry in "${unique_entries[@]+"${unique_entries[@]}"}"; do
        printf "%b│%b    %b-%b %s\n" "$C_FRAME" "$C_RESET" "$color" "$C_RESET" "$entry"
    done
}

print_final_report() {
    if [[ ${#ERRORS[@]} -eq 0 && ${#WARNINGS[@]} -eq 0 ]]; then
        return
    fi
    verbose ""
    print_issue_group "Errors"   "$C_RED"    "${ERRORS[@]+"${ERRORS[@]}"}"
    print_issue_group "Warnings" "$C_YELLOW" "${WARNINGS[@]+"${WARNINGS[@]}"}"
}

supports_interactive_selector() {
    [[ "$LEMONCROW_NON_INTERACTIVE" == "1" ]] && return 1
    [[ "$ORIGINAL_STDOUT_IS_TTY" == "1" || -t 1 ]] || return 1
    has_interactive_input || return 1
    [[ -n "${TERM:-}" && "${TERM:-}" != "dumb" ]] || return 1
    return 0
}

has_interactive_input() {
    [[ -t 0 ]] || { [[ -e /dev/tty ]] && : </dev/tty; } 2>/dev/null
}

_frame_line() {
    printf "\033[2K\r%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$1"
}

_prompt_line() {
    local glyph="$1"
    local text="$2"
    printf "\033[2K\r%b%s%b  %b%s%b\n" "$C_PURPLE" "$glyph" "$C_RESET" "$C_PURPLE" "$text" "$C_RESET"
}

_menu_save_cursor() {
    : # no-op — replaced by line-count redraw
}

_menu_restore_cursor() {
    : # no-op — replaced by line-count redraw
}

_read_menu_byte() {
    local __out_var="${1-}"
    local timeout="${2-}"
    local byte=""
    [[ -n "$__out_var" ]] || return 1
    if [[ -n "$timeout" ]]; then
        IFS= read -rsn1 -t "$timeout" byte </dev/tty 2>/dev/null || IFS= read -rsn1 -t "$timeout" byte 2>/dev/null || return 1
    else
        IFS= read -rsn1 byte </dev/tty 2>/dev/null || IFS= read -rsn1 byte 2>/dev/null || return 1
    fi
    printf -v "$__out_var" '%s' "$byte"
    return 0
}

_read_menu_key() {
    local key ch i
    _read_menu_byte key || key=""
    if [[ "$key" == $'\e' ]]; then
        for i in {1..16}; do
            if ! _read_menu_byte ch 1; then
                break
            fi
            key+="$ch"
            case "$ch" in
                [A-Za-z~]) break ;;
                *) ;;
            esac
        done
        while _read_menu_byte ch 0; do
            key+="$ch"
            case "$ch" in
                [A-Za-z~]) break ;;
                *) ;;
            esac
        done
    fi
    printf "%s" "$key"
}

_term_key_up() {
    tput kcuu1 2>/dev/null || true
}

_term_key_down() {
    tput kcud1 2>/dev/null || true
}

_menu_key_kind() {
    local key="$1"
    local term_up
    local term_down
    term_up="$(_term_key_up)"
    term_down="$(_term_key_down)"
    case "$key" in
        ""|$'\n'|$'\r') printf "enter" ;;
        " ") printf "space" ;;
        a|A) printf "all" ;;
        k|K|A|$'\e[A'|$'\eOA'|$'\e['*A|$'\eO'*A|$'\e'*A) printf "up" ;;
        j|J|B|$'\e[B'|$'\eOB'|$'\e['*B|$'\eO'*B|$'\e'*B) printf "down" ;;
        *)
            if [[ -n "$term_up" && "$key" == "$term_up" ]]; then
                printf "up"
            elif [[ -n "$term_down" && "$key" == "$term_down" ]]; then
                printf "down"
            else
                printf "other"
            fi
            ;;
    esac
}

_MENU_RENDER_LINES=0

_menu_line() {
    # Print one framed line and track it for redraw erase.
    printf "%b%s%b  %s\n" "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$1"
    _MENU_RENDER_LINES=$((_MENU_RENDER_LINES + 1))
}

_menu_erase() {
    # Move cursor up by the number of lines rendered, then clear to end of screen.
    if [[ $_MENU_RENDER_LINES -gt 0 ]]; then
        printf "\033[%dA\033[J" "$_MENU_RENDER_LINES"
    fi
    _MENU_RENDER_LINES=0
}

render_single_select() {
    local selected_index="$1"
    shift 1
    local options=("$@")
    local i

    _MENU_RENDER_LINES=0
    for i in "${!options[@]}"; do
        if [[ "$i" -eq "$selected_index" ]]; then
            _menu_line "  ${C_PURPLE}❯ ●${C_RESET}  ${options[$i]}"
        else
            _menu_line "    ○  ${options[$i]}"
        fi
    done
    _menu_line ""
    _menu_line "  ${C_PURPLE}↑↓${C_RESET} ${C_DIM}navigate  ·  ${C_RESET}${C_PURPLE}enter${C_RESET} ${C_DIM}select${C_RESET}"
}

interactive_single_select() {
    local prompt="$1"
    local out_var="$2"
    local default_index="$3"
    shift 3
    local options=("$@")
    local option_count="${#options[@]}"
    local selected_index="$default_index"
    local first_render=1

    printf "%b◆%b  %b%s%b\n" "$C_PURPLE" "$C_RESET" "$C_PURPLE" "$prompt" "$C_RESET"
    while true; do
        [[ "$first_render" == "0" ]] && _menu_erase
        render_single_select "$selected_index" "${options[@]}"
        first_render=0
        local key kind
        key="$(_read_menu_key)"
        kind="$(_menu_key_kind "$key")"
        case "$kind" in
            up)   selected_index=$(( (selected_index - 1 + option_count) % option_count )) ;;
            down) selected_index=$(( (selected_index + 1) % option_count )) ;;
            enter) break ;;
            *) ;;
        esac
    done
    _menu_erase
    # Print final confirmed selection
    local label="${options[$selected_index]}"
    printf "%b│%b  %b●%b  %b%s%b\n" "$C_FRAME" "$C_RESET" "$C_DIM" "$C_RESET" "$C_DIM" "$label" "$C_RESET"
    printf -v "$out_var" '%s' "$selected_index"
}

render_multi_select() {
    local selected_cursor="$1"
    shift 1
    local options=("$@")
    local i marker prefix selected_count=0

    for i in "${!options[@]}"; do
        [[ "${SELECTED_ITEMS[$i]:-0}" == "1" ]] && selected_count=$((selected_count + 1))
    done

    _MENU_RENDER_LINES=0
    for i in "${!options[@]}"; do
        local is_selected="${SELECTED_ITEMS[$i]:-0}"
        local is_locked="${LOCKED_ITEMS[$i]:-0}"
        local is_cursor=0
        [[ "$i" -eq "$selected_cursor" ]] && is_cursor=1

        local label="${options[$i]}"
        # Split "Name|status" if present (set by detect_hosts)
        local name="$label" badge=""
        if [[ "$label" == *"|"* ]]; then
            name="${label%%|*}"
            local raw_status="${label##*|}"
            if [[ "$raw_status" == "detected" ]]; then
                badge="  ${C_DIM}✓${C_RESET}"
            else
                badge="  ${C_DIM}—${C_RESET}"
                # Dim undetected options slightly
                name="${C_DIM}${name}${C_RESET}"
            fi
        fi

        if [[ "$is_cursor" == "1" ]]; then
            prefix="${C_PURPLE}❯${C_RESET}"
            if [[ "$is_locked" == "1" ]]; then
                marker="${C_DIM}◼${C_RESET}"
            elif [[ "$is_selected" == "1" ]]; then
                marker="${C_PURPLE}◼${C_RESET}"
            else
                marker="${C_DIM}◻${C_RESET}"
            fi
        else
            prefix=" "
            if [[ "$is_locked" == "1" ]]; then
                marker="${C_PURPLE}◼${C_RESET}"
            elif [[ "$is_selected" == "1" ]]; then
                marker="${C_PURPLE}◼${C_RESET}"
            else
                marker="${C_DIM}◻${C_RESET}"
            fi
        fi
        if [[ "$is_locked" == "1" ]]; then
            _menu_line "  ${prefix} ${marker}  ${name}${badge}"
        else
            _menu_line "  ${prefix} ${marker}  ${name}${badge}"
        fi
    done
    _menu_line ""
    local count_badge="${C_DIM}(${selected_count}/${#options[@]})${C_RESET}"
    _menu_line "  ${C_PURPLE}space${C_RESET} ${C_DIM}toggle  ·  ${C_RESET}${C_PURPLE}a${C_RESET} ${C_DIM}all  ·  ${C_RESET}${C_PURPLE}enter${C_RESET} ${C_DIM}confirm${C_RESET}  ${count_badge}"
}

interactive_multi_select() {
    local prompt="$1"
    local out_var="$2"
    local default_state="${3:-all}"
    shift 3
    local options=("$@")
    local option_count="${#options[@]}"
    local cursor=0
    local i
    local first_render=1

    if [[ "$default_state" != "preset" ]]; then
        SELECTED_ITEMS=()
        LOCKED_ITEMS=()
        for i in "${!options[@]}"; do
            if [[ "$default_state" == "none" ]]; then
                SELECTED_ITEMS[$i]=0
            else
                SELECTED_ITEMS[$i]=1
            fi
        done
    fi

    printf "%b◆%b  %b%s%b\n" "$C_PURPLE" "$C_RESET" "$C_PURPLE" "$prompt" "$C_RESET"
    while true; do
        [[ "$first_render" == "0" ]] && _menu_erase
        render_multi_select "$cursor" "${options[@]}"
        first_render=0
        local key kind
        key="$(_read_menu_key)"
        kind="$(_menu_key_kind "$key")"
        case "$kind" in
            up)    cursor=$(( (cursor - 1 + option_count) % option_count )) ;;
            down)  cursor=$(( (cursor + 1) % option_count )) ;;
            space)
                if [[ "${LOCKED_ITEMS[$cursor]:-0}" == "1" ]]; then
                    SELECTED_ITEMS[$cursor]=1
                elif [[ "${SELECTED_ITEMS[$cursor]:-0}" == "1" ]]; then
                    SELECTED_ITEMS[$cursor]=0
                else
                    SELECTED_ITEMS[$cursor]=1
                fi
                ;;
            all)
                for i in "${!options[@]}"; do
                    SELECTED_ITEMS[$i]=1
                done
                ;;
            enter) break ;;
            *) ;;
        esac
    done
    _menu_erase

    # Print confirmed selections
    for i in "${!options[@]}"; do
        if [[ "${SELECTED_ITEMS[$i]:-0}" == "1" ]]; then
            local label="${options[$i]%%|*}"
            printf "%b│%b  %b◼%b  %b%s%b\n" "$C_FRAME" "$C_RESET" "$C_DIM" "$C_RESET" "$C_DIM" "$label" "$C_RESET"
        fi
    done

    local chosen_indices=()
    for i in "${!options[@]}"; do
        if [[ "${SELECTED_ITEMS[$i]:-0}" == "1" ]]; then
            chosen_indices+=("$i")
        fi
    done
    printf -v "$out_var" '%s' "${chosen_indices[*]:-}"
}

prompt_memory_selection() {
    # SQLite is the default memory backend. Docker sidecars are opt-in via
    # --memory letta|openmemory or LEMONCROW_MEMORY_BACKEND.
    return 0
}

prompt_auto_optimize_selection() {
    if [[ "$LEMONCROW_NO_SERVICECTL" == "1" ]]; then
        LEMONCROW_AUTO_OPTIMIZE=0
        return 0
    fi
    case "${LEMONCROW_AUTO_OPTIMIZE}" in
        0|1) ;;
        *) LEMONCROW_AUTO_OPTIMIZE=1 ;;
    esac
}

prompt_telegraphic_selection() {
    # Reply-register level baked into installed agent personas.
    # Flag/env wins; otherwise interactive selector; default ultra.
    # Change later without reinstalling: `lc settings set cli.telegraphic <level>`.
    case "$LEMONCROW_TELEGRAPHIC" in
        ultra|lite|off) return 0 ;;
        "") ;;
        *) fail "--telegraphic must be 'ultra', 'lite' or 'off', got: '$LEMONCROW_TELEGRAPHIC'" ;;
    esac
    LEMONCROW_TELEGRAPHIC="ultra"
    [[ "$LEMONCROW_NON_INTERACTIVE" == "1" ]] && return 0
    has_interactive_input || return 0
    supports_interactive_selector || return 0
    local tg_idx=0
    interactive_single_select \
        "Agent reply style (change later: /lemoncrow set telegraphic <level>)?" \
        tg_idx \
        0 \
        "Ultra – maximal output compression" \
        "Lite – concise, lighter register" \
        "Off – no reply-style instruction"
    case "$tg_idx" in
        1) LEMONCROW_TELEGRAPHIC="lite" ;;
        2) LEMONCROW_TELEGRAPHIC="off" ;;
        *) LEMONCROW_TELEGRAPHIC="ultra" ;;
    esac
}

persist_telegraphic_selection() {
    # Persist as the cli.telegraphic setting (<root>/plugin_settings.json —
    # same store as `lc settings set`) BEFORE host wiring so staged agent
    # personas pick the level up; exported for the install-script hooks too
    # (lemoncrow_apply_reply_register_level in lib/managed_context.sh).
    export LEMONCROW_TELEGRAPHIC
    [[ -n "$LEMONCROW_TELEGRAPHIC" ]] || return 0
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] persist cli.telegraphic='$LEMONCROW_TELEGRAPHIC' → plugin_settings.json"
        return 0
    fi
    LEMONCROW_RR_LEVEL="$LEMONCROW_TELEGRAPHIC" python3 - <<'PYEOF' || true
import json
import os
from pathlib import Path

root = Path(os.environ.get("LEMONCROW_ROOT", "").strip() or (Path.home() / ".lemoncrow"))
path = root / "plugin_settings.json"
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
except Exception:
    data = {}
data["cli.telegraphic"] = os.environ["LEMONCROW_RR_LEVEL"]
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
PYEOF
}

# --- Companion-binary version reconcile -------------------------------------
# Versions are pinned per release in versions.sh; what we last installed is
# recorded in ~/.lemoncrow/companion_versions. A binary is (re)provisioned only
# when its pin changes (or it is missing) — an unchanged pin is a no-op.
_companion_versions_file() { printf '%s' "${HOME}/.lemoncrow/companion_versions"; }

_companion_recorded_version() {  # $1=key -> prints recorded value (empty if none)
    local f; f="$(_companion_versions_file)"
    [[ -r "$f" ]] || return 0
    sed -n "s/^$1=//p" "$f" | head -n1
}

_companion_record_version() {  # $1=key $2=value
    local f tmp
    f="$(_companion_versions_file)"
    mkdir -p "${HOME}/.lemoncrow" 2>/dev/null || true
    tmp="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-cv.XXXXXX")" || return 0
    [[ -r "$f" ]] && grep -v "^$1=" "$f" > "$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    mv "$tmp" "$f" 2>/dev/null || rm -f "$tmp"
}

prompt_local_zoekt_selection() {
    if [[ "$LEMONCROW_ZOEKT" != "1" ]]; then
        INSTALL_ZOEKT_LOCAL=0
        return 0
    fi

    local zoekt_all_present=1
    local _z
    for _z in zoekt-git-index zoekt-index zoekt zoekt-webserver; do
        command -v "$_z" >/dev/null 2>&1 || zoekt_all_present=0
    done

    # Reconcile to the release-pinned ref: (re)build when the binaries are missing
    # OR the pinned ref differs from the one last installed. Unchanged pin = no-op.
    if [[ "$LEMONCROW_ZOEKT_AUTO_INSTALL" == "1" ]] \
        && { [[ "$zoekt_all_present" == "0" ]] \
          || [[ "$(_companion_recorded_version zoekt)" != "${LEMONCROW_PIN_ZOEKT:-latest}" ]]; }; then
        INSTALL_ZOEKT_LOCAL=1
    else
        INSTALL_ZOEKT_LOCAL=0
    fi
}

_zoekt_all_local_binaries_present() {
    local _z
    for _z in zoekt-git-index zoekt-index zoekt zoekt-webserver; do
        command -v "$_z" >/dev/null 2>&1 || return 1
    done
    return 0
}

prompt_rtk_selection() {
    # rtk (external command compactor) — soft integration, same as ast-grep/jj:
    # always attempted, never prompted. Opt out with LEMONCROW_INSTALL_RTK=0
    # (runtime opt-out once installed: LEMONCROW_BASH_EXTERNAL_COMPACTORS=0).
    [[ "${LEMONCROW_INSTALL_RTK:-}" == "0" ]] && return 0
    INSTALL_RTK=1
}

lemoncrow_install_attribution_hook() {
    local repo_dir="${1:-.}"
    local dry_run="${2:-false}"
    local hooks_dir hook trailer marker end_marker

    trailer="Co-Authored-By: LemonCrow <293447754+lemoncrow@users.noreply.github.com>"
    marker="# >>> lemoncrow attribution >>>"
    end_marker="# <<< lemoncrow attribution <<<"

    if ! git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1; then
        warn "LemonCrow attribution skipped: ${repo_dir} is not a git repository"
        return 0
    fi

    hooks_dir="$(git -C "$repo_dir" rev-parse --git-path hooks 2>/dev/null)" || {
        warn "LemonCrow attribution skipped: cannot resolve git hooks path for ${repo_dir}"
        return 0
    }
    case "$hooks_dir" in
        /*) : ;;
        *) hooks_dir="${repo_dir}/${hooks_dir}" ;;
    esac
    hook="${hooks_dir}/prepare-commit-msg"

    if [[ "$dry_run" == "true" ]]; then
        echo "  [dry-run] install LemonCrow co-author hook at ${hook}"
        return 0
    fi

    mkdir -p "$hooks_dir"
    if [ -f "$hook" ] && grep -qF "$marker" "$hook"; then
        info "LemonCrow co-author hook already installed at ${hook}"
        return 0
    fi

    if [ -f "$hook" ]; then
        warn "existing prepare-commit-msg found; appending LemonCrow co-author block (${hook})"
    else
        printf '#!/usr/bin/env bash\n\n' >"$hook"
    fi

    cat >>"$hook" <<EOF
$marker
# Managed by LemonCrow. Appends the co-author trailer unless already present.
# Skips merge/squash commit messages.
LEMONCROW_TRAILER="$trailer"
case "\$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "\$LEMONCROW_TRAILER" "\$1" 2>/dev/null; then
      printf '\n%s\n' "\$LEMONCROW_TRAILER" >> "\$1"
    fi
    ;;
esac
$end_marker
EOF
    chmod +x "$hook"
    info "installed LemonCrow co-author hook at ${hook}"
}

has_flag() {
    local needle="$1"
    local item
    for item in "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"; do
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

contains_any_host_flag() {
    [[ ${#HOST_FLAGS[@]} -gt 0 ]] && return 0
    has_flag "--all" && return 0
    has_flag "--claude" && return 0
    has_flag "--codex" && return 0
    has_flag "--opencode" && return 0
    return 1
}

detect_hosts() {
    HOST_FLAGS=()
    HOST_SUMMARY=()
    HOST_CHOICES=()
    HOST_DEFAULT_SELECTION=()

    if command -v claude >/dev/null 2>&1; then
        HOST_SUMMARY+=("Claude Code (detected)")
        HOST_CHOICES+=("Claude Code|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("Claude Code (not found)")
        HOST_CHOICES+=("Claude Code|not found")
        HOST_DEFAULT_SELECTION+=(0)
    fi

    if command -v codex >/dev/null 2>&1; then
        HOST_SUMMARY+=("Codex CLI (detected)")
        HOST_CHOICES+=("Codex CLI|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("Codex CLI (not found)")
        HOST_CHOICES+=("Codex CLI|not found")
        HOST_DEFAULT_SELECTION+=(0)
    fi

    if command -v opencode >/dev/null 2>&1; then
        HOST_SUMMARY+=("OpenCode (Ink LemonCrow provider) (detected)")
        HOST_CHOICES+=("OpenCode (Ink LemonCrow provider)|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("OpenCode (Ink LemonCrow provider) (not found)")
        HOST_CHOICES+=("OpenCode (Ink LemonCrow provider)|not found")
        HOST_DEFAULT_SELECTION+=(0)
    fi

}

join_with_comma_space() {
    local joined=""
    local item
    for item in "$@"; do
        if [[ -z "$joined" ]]; then
            joined="$item"
        else
            joined="$joined, $item"
        fi
    done
    printf "%s" "$joined"
}

host_wizard() {
    [[ "$LEMONCROW_NON_INTERACTIVE" == "1" ]] && return 0
    has_interactive_input || return 0
    [[ "$LEMONCROW_NO_HOSTS" == "1" ]] && return 0
    contains_any_host_flag && return 0
    [[ ${#HOST_SCOPE_ARGS[@]} -gt 0 ]] && return 0

    detect_hosts

    if supports_interactive_selector; then
        local selected_host_indices=""
        SELECTED_ITEMS=()
        local i
        for i in "${!HOST_DEFAULT_SELECTION[@]}"; do
            SELECTED_ITEMS[$i]="${HOST_DEFAULT_SELECTION[$i]}"
        done
        interactive_multi_select \
            "Which agents should LemonCrow configure?" \
            selected_host_indices \
            "preset" \
            "${HOST_CHOICES[@]}"
        if [[ -z "${selected_host_indices// }" ]]; then
            LEMONCROW_NO_HOSTS=1
        else
            local idx
            for idx in $selected_host_indices; do
                case "$idx" in
                    0) HOST_FLAGS+=(--claude) ;;
                    1) HOST_FLAGS+=(--codex) ;;
                    2) HOST_FLAGS+=(--opencode) ;;
                esac
            done
            [[ ${#HOST_FLAGS[@]} -gt 0 ]] || LEMONCROW_NO_HOSTS=1
        fi
    else
        echo "◇  Which agents should LemonCrow configure?"
        printf "│  1) %s\n" "${HOST_SUMMARY[0]}"
        printf "│  2) %s\n" "${HOST_SUMMARY[1]}"
        printf "│  3) %s\n" "${HOST_SUMMARY[2]}"
        printf "│  a) All (default)\n"
        printf "│\n"
        printf "Choice [a]: "

        local selection
        read -r selection </dev/tty || selection="a"
        selection="${selection:-a}"
        echo ""

        case "$selection" in
            a|A|all|ALL)
                HOST_FLAGS=(--all)
                ;;
            *)
                local token
                IFS=',' read -ra _choices <<<"$selection"
                for token in "${_choices[@]}"; do
                    token="$(echo "$token" | xargs)"
                    case "$token" in
                        1) HOST_FLAGS+=(--claude) ;;
                        2) HOST_FLAGS+=(--codex) ;;
                        3) HOST_FLAGS+=(--opencode) ;;
                    esac
                done
                [[ ${#HOST_FLAGS[@]} -gt 0 ]] || LEMONCROW_NO_HOSTS=1
                ;;
        esac
    fi

    [[ "$LEMONCROW_NO_HOSTS" == "1" ]] && return 0

    # --- Optional agent roles ------------------------------------------------
    # `code` always ships (DEFAULT_ROLE_IDS); shown as an info line, not a
    # togglable item. The normal optional roles default to selected; high-autonomy
    # `auto` and minimal `bare` are available but start deselected. Read integrations/agents/*.md frontmatter straight off
    # disk instead of shelling to the `lc` CLI: this runs from host_wizard,
    # which fires before install_lemoncrow_from_wheel, so the CLI (and its
    # tiktoken dependency) aren't installed yet. Cost here is a lightweight
    # chars/4 estimate, not the exact tiktoken count `lc agent list`
    # reports later once the CLI exists.
    local role_rows="" role_names=() role_labels=() code_cost="" _rw_name _rw_cost
    role_rows="$(LEMONCROW_INSTALL_DIR="$LEMONCROW_INSTALL_DIR" python3 -c '
import glob, os
root = os.environ["LEMONCROW_INSTALL_DIR"]
for path in sorted(glob.glob(os.path.join(root, "integrations", "agents", "*.md"))):
    text = open(path, encoding="utf-8").read()
    if not text.startswith("---\n"):
        continue
    end = text.find("\n---\n", 4)
    if end < 0:
        continue
    meta = {}
    for line in text[4:end].splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))
    mode = meta.get("mode", "")
    desc = meta.get("agent_description", "")
    if not mode:
        continue
    cost = max(1, len(mode + ": " + desc) // 4)
    print(str(mode) + chr(9) + str(cost))
' 2>/dev/null)" || role_rows=""

    if [[ -n "$role_rows" ]]; then
        while IFS=$'\t' read -r _rw_name _rw_cost; do
            [[ -n "$_rw_name" ]] || continue
            if [[ "$_rw_name" == "code" ]]; then
                code_cost="$_rw_cost"
            else
                role_names+=("$_rw_name")
                role_labels+=("${_rw_name}  ${C_DIM}~${_rw_cost} tok/turn${C_RESET}")
            fi
        done <<< "$role_rows"
    fi
    if [[ -n "$code_cost" ]]; then
        role_names=("code" "${role_names[@]+"${role_names[@]}"}")
        role_labels=("code  ${C_DIM}~${code_cost} tok/turn  always installed${C_RESET}" "${role_labels[@]+"${role_labels[@]}"}")
    fi

    if [[ ${#role_names[@]} -gt 0 ]]; then
        local roles_csv=""
        if supports_interactive_selector; then
            local selected_roles=""
            SELECTED_ITEMS=()
            LOCKED_ITEMS=()
            local _rw_i
            for _rw_i in "${!role_names[@]}"; do
                case "${role_names[$_rw_i]}" in
                    code) SELECTED_ITEMS[$_rw_i]=1; LOCKED_ITEMS[$_rw_i]=1 ;;
                    auto|bare) SELECTED_ITEMS[$_rw_i]=0; LOCKED_ITEMS[$_rw_i]=0 ;;
                    *) SELECTED_ITEMS[$_rw_i]=1; LOCKED_ITEMS[$_rw_i]=0 ;;
                esac
            done
            interactive_multi_select \
                "Agent roles to install" \
                selected_roles \
                "preset" \
                "${role_labels[@]}"
            local _rw_idx
            for _rw_idx in $selected_roles; do
                roles_csv+="${roles_csv:+,}${role_names[$_rw_idx]}"
            done
        else
            _prompt_line "◇" "Agent roles to install (comma-separated names, empty = standard; all = include auto/bare)"
            local _rw_i
            for _rw_i in "${!role_names[@]}"; do
                _frame_line "  ${role_labels[$_rw_i]}"
            done
            local roles_answer=""
            printf "  Agents to add [standard]: "
            IFS= read -r roles_answer </dev/tty 2>/dev/null || roles_answer=""
            if [[ -z "$roles_answer" || "$roles_answer" == "standard" ]]; then
                for _rw_name in "${role_names[@]}"; do
                    case "$_rw_name" in
                        auto|bare) ;;
                        *) roles_csv+="${roles_csv:+,}${_rw_name}" ;;
                    esac
                done
            elif [[ "$roles_answer" == "all" ]]; then
                for _rw_name in "${role_names[@]}"; do
                    roles_csv+="${roles_csv:+,}${_rw_name}"
                done
            else
                roles_csv="$(_filter_csv_against_set "$roles_answer" "${role_names[@]}")"
                if [[ -n "$roles_csv" && ",${roles_csv}," != *,code,* ]]; then
                    roles_csv="code,${roles_csv}"
                fi
            fi
        fi
        [[ -n "$roles_csv" ]] && HOST_EXTRA_ARGS+=(--roles "$roles_csv")
    fi
    # --- end optional agent roles --------------------------------------------

    # --- Optional skills ------------------------------------------------------
    # Same rationale as agent roles above for reading straight off disk
    # instead of shelling to `lc skill list` -- the CLI isn't installed
    # yet at this point in the wizard. The excluded set here mirrors the
    # dev-only HIDDEN_SKILLS list in scripts/build_host_skills.sh /
    # src/lemoncrow/core/environment.py. Skills only apply to claude/codex.
    local offer_skills=0 _sk_flag
    if [[ ${#HOST_FLAGS[@]} -eq 0 ]]; then
        offer_skills=1
    else
        for _sk_flag in "${HOST_FLAGS[@]}"; do
            [[ "$_sk_flag" == "--claude" || "$_sk_flag" == "--codex" || "$_sk_flag" == "--all" ]] && offer_skills=1
        done
    fi

    if [[ "$offer_skills" == "1" ]]; then
        local skill_rows="" skill_names=() skill_labels=() _sk_name _sk_cost lemoncrow_cost=""
        skill_rows="$(LEMONCROW_INSTALL_DIR="$LEMONCROW_INSTALL_DIR" python3 -c '
import glob, os
root = os.environ["LEMONCROW_INSTALL_DIR"]
hidden = {"analyze-failures", "context", "evals", "rescue", "savings", "status", "record"}
for path in sorted(glob.glob(os.path.join(root, "integrations", "skills", "*", "SKILL.md"))):
    name = os.path.basename(os.path.dirname(path))
    if name in hidden:
        continue
    text = open(path, encoding="utf-8").read()
    if not text.startswith("---\n"):
        continue
    end = text.find("\n---\n", 4)
    if end < 0:
        continue
    meta = {}
    for line in text[4:end].splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))
    desc = meta.get("description", "")
    cost = max(1, len(name + ": " + desc) // 4)
    print(name + chr(9) + str(cost))
' 2>/dev/null)" || skill_rows=""

        if [[ -n "$skill_rows" ]]; then
            while IFS=$'\t' read -r _sk_name _sk_cost; do
                [[ -n "$_sk_name" ]] || continue
                if [[ "$_sk_name" == "lemoncrow" ]]; then
                    lemoncrow_cost="$_sk_cost"
                else
                    skill_names+=("$_sk_name")
                    skill_labels+=("${_sk_name}  ${C_DIM}~${_sk_cost} tok/turn${C_RESET}")
                fi
            done <<< "$skill_rows"
        fi
        if [[ -n "$lemoncrow_cost" ]]; then
            skill_names=("lemoncrow" "${skill_names[@]+"${skill_names[@]}"}")
            skill_labels=("lemoncrow  ${C_DIM}~${lemoncrow_cost} tok/turn  always installed${C_RESET}" "${skill_labels[@]+"${skill_labels[@]}"}")
        fi

        if [[ ${#skill_names[@]} -gt 0 ]]; then
            local skills_csv=""
            local skills_prompt="Complimentary Skills (Install later: /lemoncrow install skill <name>)"
            if supports_interactive_selector; then
                local selected_skills=""
                SELECTED_ITEMS=()
                LOCKED_ITEMS=()
                local _sk_i
                for _sk_i in "${!skill_names[@]}"; do
                    if [[ "${skill_names[$_sk_i]}" == "lemoncrow" ]]; then
                        SELECTED_ITEMS[$_sk_i]=1; LOCKED_ITEMS[$_sk_i]=1
                    else
                        SELECTED_ITEMS[$_sk_i]=1; LOCKED_ITEMS[$_sk_i]=0
                    fi
                done
                interactive_multi_select \
                    "$skills_prompt" \
                    selected_skills \
                    "preset" \
                    "${skill_labels[@]}"
                local _sk_idx
                for _sk_idx in $selected_skills; do
                    skills_csv+="${skills_csv:+,}${skill_names[$_sk_idx]}"
                done
            else
                _prompt_line "◇" "$skills_prompt"
                for _sk_name in "${skill_names[@]}"; do
                    _frame_line "  ${_sk_name}"
                done
                local skills_answer=""
                printf "  Skills to add [all]: "
                IFS= read -r skills_answer </dev/tty 2>/dev/null || skills_answer=""
                if [[ -z "$skills_answer" || "$skills_answer" == "all" ]]; then
                    for _sk_name in "${skill_names[@]}"; do
                        skills_csv+="${skills_csv:+,}${_sk_name}"
                    done
                else
                    skills_csv="$(_filter_csv_against_set "$skills_answer" "${skill_names[@]}")"
                    if [[ -n "$skills_csv" && ",${skills_csv}," != *,lemoncrow,* ]]; then
                        skills_csv="lemoncrow,${skills_csv}"
                    fi
                fi
            fi
            [[ -n "$skills_csv" ]] && HOST_EXTRA_ARGS+=(--include-skills "$skills_csv")
        fi
    fi
    # --- end optional skills ----------------------------------------------------

    prompt_telegraphic_selection

    local scope_choice=0
    if supports_interactive_selector; then
        interactive_single_select \
            "Apply configs globally or just here?" \
            scope_choice \
            0 \
            "All projects (global)" \
            "Just this project"
    else
        echo "◇  Apply agent configs to all your projects, or just this one?"
        echo "│  1) All projects (global)"
        echo "│  2) Just this project"
        printf "Choice [1]: "
        local scope_choice_raw
        read -r scope_choice_raw </dev/tty || scope_choice_raw="1"
        scope_choice_raw="${scope_choice_raw:-1}"
        echo ""
        if [[ "$scope_choice_raw" == "2" ]]; then
            scope_choice=1
        fi
    fi

    local scope="global"
    if [[ "$scope_choice" == "1" ]]; then
        HOST_SCOPE_ARGS=(--workspace .)
        scope="local"
    fi

    local wants_claude=0
    local flag
    for flag in "${HOST_FLAGS[@]+"${HOST_FLAGS[@]}"}"; do
        if [[ "$flag" == "--all" || "$flag" == "--claude" ]]; then
            wants_claude=1
            break
        fi
    done

    if [[ "$wants_claude" == "1" && "$scope" == "global" ]]; then
        HOST_EXTRA_ARGS+=(--claude-project "$(pwd)")
    fi
}

host_scope_is_workspace() {
    local idx
    for idx in "${!HOST_SCOPE_ARGS[@]}"; do
        if [[ "${HOST_SCOPE_ARGS[$idx]}" == "--workspace" ]]; then
            return 0
        fi
    done
    return 1
}

host_target_for_name() {
    local raw_name="$1"
    local host_name="${raw_name%% *}"

    if host_scope_is_workspace; then
        printf "%s" "."
        return 0
    fi

    case "$host_name" in
        claude)      printf "%s" "~/.claude" ;;
        codex)       printf "%s" "~/.codex" ;;
        opencode)    printf "%s" "~/.config/opencode" ;;
        *)           printf "%s" "~/.config" ;;
    esac
}

format_host_status_label() {
    local raw_name="$1"
    case "$raw_name" in
        skills) printf "%s" "shared skills bundle" ; return ;;
        agents) printf "%s" "universal agents" ; return ;;
    esac
    local target
    target="$(host_target_for_name "$raw_name")"
    if [[ -n "$target" ]]; then
        printf "%s -> %s" "$raw_name" "$target"
    else
        printf "%s" "$raw_name"
    fi
}

ensure_local_zoekt_runtime() {    # Kept for legacy --zoekt-auto-install flag path; prefer install_local_zoekt_if_selected
    local lemoncrow_cli="$1"
    local missing=()
    local name
    for name in zoekt-git-index zoekt-index zoekt zoekt-webserver; do
        if ! command -v "$name" >/dev/null 2>&1; then
            missing+=("$name")
        fi
    done
    [[ ${#missing[@]} -eq 0 ]] && return
    warn "Local Zoekt binaries missing — rerun the installer with LEMONCROW_ZOEKT_AUTO_INSTALL=1"
}

# Stop stale LemonCrow background/servicectl/stack processes before reinstalling so a
# new install never leaves an old binary serving requests. Does NOT kill the MCP
# server launched by the host agent (lc mcp --host) — that process stays
# alive and gets reloaded via /mcp reconnect on the next agent session.
stop_existing_lemoncrow_processes() {
    [[ "$LEMONCROW_INSTALL_CLEAN_PROCESSES" == "1" ]] || return 0

    local current_pid="$$"
    local parent_pid="${PPID:-}"
    local pids=()
    local pid args

    local ps_out
    ps_out="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-ps.XXXXXX")"
    ps -eo pid=,args= 2>/dev/null > "$ps_out" || true
    while read -r pid args; do
        [[ -n "${pid:-}" && -n "${args:-}" ]] || continue
        [[ "$pid" == "$current_pid" || "$pid" == "$parent_pid" ]] && continue

        # Kill servicectl and stack-run processes only. The MCP server
        # (lc mcp --host) is deliberately left alive so the host agent
        # doesn't lose connectivity mid-install; the user runs /mcp reconnect
        # afterwards if they want a fresh server process.
        case "$args" in
            *"/lemoncrow --root "*servicectl*|\
            *" lc --root "*servicectl*|\
            *"/lemoncrow servicectl "*|\
            *" lc servicectl "*|\
            *"/lemoncrow stack run"*|\
            *" lc stack run"*)
                pids+=("$pid")
                ;;
        esac
    done < "$ps_out"
    rm -f "$ps_out"

    [[ ${#pids[@]} -gt 0 ]] || return 0

    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        printf '[dry-run] stop stale LemonCrow processes: %s\n' "${pids[*]}"
        return 0
    fi

    verbose "Stopping stale LemonCrow processes before reinstall: ${pids[*]}"
    kill -TERM "${pids[@]}" 2>/dev/null || true
    sleep 1
    local alive=()
    for pid in "${pids[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            alive+=("$pid")
        fi
    done
    if [[ ${#alive[@]} -gt 0 ]]; then
        kill -KILL "${alive[@]}" 2>/dev/null || true
    fi
}

# Warn (never fail) when a foreign `lemoncrow`/`lc` executable — one we did not
# install — is already on the user's PATH, so a shadowing surprise is visible
# before we drop the LemonCrow console scripts into place. We never clobber a
# foreign binary: our scripts land in our own bin dir; PATH order decides which
# wins. Idempotent — our own previously-installed lemoncrow/lc resolve to our
# bin dirs and are treated as ours, so re-runs stay quiet.
warn_on_foreign_cli_collision() {
    local our_dirs=()
    [[ -n "${LEMONCROW_BIN_DIR:-}" ]] && our_dirs+=("$LEMONCROW_BIN_DIR")
    local uv_bin
    uv_bin="$(uv tool dir --bin 2>/dev/null || true)"
    [[ -n "$uv_bin" ]] && our_dirs+=("$uv_bin")

    local cli found resolved d dabs ours
    for cli in lemoncrow lc; do
        found="$(command -v "$cli" 2>/dev/null || true)"
        [[ -n "$found" ]] || continue
        resolved="$(cd "$(dirname "$found")" 2>/dev/null && pwd -P)/$(basename "$found")" 2>/dev/null || resolved="$found"
        ours=0
        for d in ${our_dirs[@]+"${our_dirs[@]}"}; do
            [[ -n "$d" ]] || continue
            dabs="$(cd "$d" 2>/dev/null && pwd -P || echo "$d")"
            case "$resolved" in "$dabs"/*) ours=1; break;; esac
            case "$found" in "$d"/*) ours=1; break;; esac
        done
        [[ "$ours" == "1" ]] && continue
        if [[ "$cli" == "lemoncrow" ]]; then
            warn "A different 'lemoncrow' is already on your PATH: ${found}"
            warn "LemonCrow installs its CLI 'lemoncrow' to ${LEMONCROW_BIN_DIR}; whichever comes first on PATH wins. Adjust PATH order if you want the LemonCrow 'lemoncrow' to take precedence."
        else
            warn "A different 'lc' is already on your PATH: ${found}"
            warn "LemonCrow also installs the short alias 'lc'; it may shadow or be shadowed by the existing one. Reorder PATH, or skip the LemonCrow 'lc' alias, to avoid confusion."
        fi
    done
}

# Install uv (Python package/tool manager) via the official installer.
# Shared by ALL entry points: local.sh (source install), bundle.sh (wheel
# install), and install.sh via its bundle.sh delegation.
install_uv_if_needed() {
    if command -v uv >/dev/null 2>&1; then
        verbose "Found uv: $(uv --version 2>/dev/null || echo unknown)"
    else
        need_cmd curl
        verbose "Installing uv (official installer)..."
        if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
            echo "[dry-run] curl -LsSf https://astral.sh/uv/install.sh | sh"
        else
            # shellcheck disable=SC2016
            curl -LsSf https://astral.sh/uv/install.sh | sh

            if [[ -x "${HOME}/.local/bin/uv" ]]; then
                export PATH="${HOME}/.local/bin:${PATH}"
            fi

            command -v uv >/dev/null 2>&1 || fail "uv install completed but uv is still not on PATH"
            verbose "Installed uv: $(uv --version 2>/dev/null || echo unknown)"
        fi
    fi

    # Source installs pin the checkout to the supported runtime so uv never
    # selects a newer ABI. `uv python pin` writes ./.python-version, so only do
    # it for a repo checkout (LEMONCROW_LOCAL=1). Binary installs pin per-install
    # via LEMONCROW_PYTHON_VERSION inside bundle.sh's install_lemoncrow_from_wheel.
    if [[ "${LEMONCROW_LOCAL:-0}" == "1" && "${LEMONCROW_DRY_RUN:-0}" != "1" ]]; then
        uv python install 3.13 >/dev/null 2>&1 || true
        uv python pin 3.13 >/dev/null 2>&1 || true
    fi
}

# Install Node.js to ~/.local/node via official tarball (self-contained, no sudo)
_install_node() {
    local node_ver="${LEMONCROW_PIN_NODE:-v20.12.2}"
    local arch os_low tarball os_name
    case "$(uname -m)" in
        x86_64)        arch="x64" ;;
        aarch64|arm64) arch="arm64" ;;
        *)             arch="x64" ;;
    esac
    os_name="$(uname -s)"
    os_low="$(echo "$os_name" | tr '[:upper:]' '[:lower:]')"
    [[ "$os_low" == "darwin" ]] && os_low="darwin"

    tarball="node-${node_ver}-${os_low}-${arch}.tar.gz"
    mkdir -p "$LEMONCROW_NODE_DIR"
    
    local tmp_tar
    tmp_tar="$(mktemp "${TMPDIR:-/tmp}/node-tarball.XXXXXX.tar.gz")"
    curl -sSL "https://nodejs.org/dist/${node_ver}/${tarball}" -o "$tmp_tar" || return 1
    
    if tar --help 2>&1 | grep -q "strip-components"; then
        tar -xzf "$tmp_tar" -C "$LEMONCROW_NODE_DIR" --strip-components=1 || { rm -f "$tmp_tar"; return 1; }
    else
        local tmp_dir
        tmp_dir="$(mktemp -d)"
        tar -xzf "$tmp_tar" -C "$tmp_dir" || { rm -f "$tmp_tar"; rm -rf "$tmp_dir"; return 1; }
        mv "$tmp_dir"/node-*/* "$LEMONCROW_NODE_DIR/"
        rm -rf "$tmp_dir"
    fi
    rm -f "$tmp_tar"
    
    export PATH="${LEMONCROW_NODE_DIR}/bin:${PATH}"
    command -v node >/dev/null 2>&1
    command -v npm >/dev/null 2>&1
}

install_node_if_needed() {
    local node_user_bin="${LEMONCROW_NODE_DIR}/bin"
    if [[ -x "${node_user_bin}/node" && ":$PATH:" != *":${node_user_bin}:"* ]]; then
        export PATH="${node_user_bin}:${PATH}"
    fi

    if command -v npm >/dev/null 2>&1; then
        # Reconcile only an LemonCrow-managed Node (under LEMONCROW_NODE_DIR) to the
        # release-pinned version when the pin changed from what we recorded. A
        # user/system Node is left untouched.
        local _node_path
        _node_path="$(command -v node 2>/dev/null || true)"
        if [[ -n "$_node_path" && "$_node_path" == "${LEMONCROW_NODE_DIR}/"* \
              && "$(_companion_recorded_version node)" != "${LEMONCROW_PIN_NODE:-v20.12.2}" \
              && "$LEMONCROW_DRY_RUN" != "1" ]]; then
            spin "Updating Node.js to ${LEMONCROW_PIN_NODE:-v20.12.2}" _install_node \
                && _companion_record_version node "${LEMONCROW_PIN_NODE:-v20.12.2}" || true
        fi
        verbose "Found npm: $(npm --version 2>/dev/null || echo unknown)"
        return
    fi

    if [[ "$LEMONCROW_NO_STACK" == "1" ]]; then
        return
    fi

    need_cmd curl
    verbose "npm not found — attempting local Node.js installation..."
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] install node ${LEMONCROW_PIN_NODE:-v20.12.2}"
    else
        spin "Installing Node.js" _install_node \
            && _companion_record_version node "${LEMONCROW_PIN_NODE:-v20.12.2}" || true
    fi
    
    if [[ -x "${node_user_bin}/node" && ":$PATH:" != *":${node_user_bin}:"* ]]; then
        export PATH="${node_user_bin}:${PATH}"
    fi
}

# Install Go to ~/.local/go via official tarball (self-contained, no sudo)
_install_go() {
    local go_ver arch os_low tarball pin="${LEMONCROW_PIN_GO:-latest}"
    if [[ "$pin" == "latest" || -z "$pin" ]]; then
        go_ver="$(curl -sSL 'https://go.dev/VERSION?m=text' 2>/dev/null | head -1)" || return 1
    else
        go_ver="$pin"
    fi
    [[ -z "$go_ver" ]] && return 1
    case "$(uname -m)" in
        x86_64)        arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *)             arch="amd64" ;;
    esac
    os_low="$(uname -s | tr '[:upper:]' '[:lower:]')"
    tarball="${go_ver}.${os_low}-${arch}.tar.gz"
    mkdir -p "${HOME}/.local"
    curl -sSL "https://go.dev/dl/${tarball}" | tar -xz -C "${HOME}/.local" || return 1
    export PATH="${HOME}/.local/go/bin:${PATH}"
    command -v go >/dev/null 2>&1
}

_install_zoekt_binaries() {
    local ref="${LEMONCROW_PIN_ZOEKT:-latest}"
    go install "github.com/sourcegraph/zoekt/cmd/zoekt-git-index@${ref}" &&
        go install "github.com/sourcegraph/zoekt/cmd/zoekt-index@${ref}" &&
        go install "github.com/sourcegraph/zoekt/cmd/zoekt@${ref}" &&
        go install "github.com/sourcegraph/zoekt/cmd/zoekt-webserver@${ref}"
}

# Provision Go (if needed) and build the four Zoekt binaries. Runs DETACHED from
# the installer (see install_local_zoekt_if_selected) so it never blocks; all
# output goes to a log. No spin()/terminal UI here — it runs in the background.
_zoekt_provision_background() {
    local go_user_bin="${HOME}/.local/go/bin"
    if ! command -v go >/dev/null 2>&1; then
        _install_go || { echo "Go install failed — Zoekt skipped; search stays on ripgrep."; return 0; }
    fi
    if [[ -x "${go_user_bin}/go" && ":$PATH:" != *":${go_user_bin}:"* ]]; then
        export PATH="${go_user_bin}:${PATH}"
    fi
    if ! command -v go >/dev/null 2>&1; then
        echo "Go not on PATH — Zoekt skipped; search stays on ripgrep."
        return 0
    fi
    local go_path_bin
    go_path_bin="$(go env GOPATH 2>/dev/null)/bin"
    if [[ -n "$go_path_bin" && ":$PATH:" != *":${go_path_bin}:"* ]]; then
        export PATH="${go_path_bin}:${PATH}"
    fi
    if _install_zoekt_binaries; then
        _companion_record_version zoekt "${LEMONCROW_PIN_ZOEKT:-latest}"
        echo "Zoekt ${LEMONCROW_PIN_ZOEKT:-latest} installed"
        # Build the trigram index immediately so search is ready without
        # requiring a manual 'lc code index' re-run after installation.
        local lemoncrow_bin="${LEMONCROW_BIN_DIR:-${HOME}/.local/bin}/lc"
        if [[ -x "$lemoncrow_bin" ]]; then
            echo "Building Zoekt trigram index for $(pwd)..."
            "$lemoncrow_bin" code index --no-stats 2>&1 \
                || echo "Zoekt index build failed — will be built on next 'lc code index' run."
        fi
    else
        echo "Zoekt build failed — search stays on ripgrep. Re-run the installer to retry."
    fi
}

install_local_zoekt_if_selected() {
    [[ "$INSTALL_ZOEKT_LOCAL" != "1" ]] && return 0

    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] background: install Go (if needed) + go install zoekt-{git-index,index,zoekt,webserver}@${LEMONCROW_PIN_ZOEKT:-latest}"
        return 0
    fi

    local log="${HOME}/.lemoncrow/zoekt_install.log"
    mkdir -p "${HOME}/.lemoncrow"

    # Fire-and-forget: a first-time build pulls Go (~150MB) and compiles four
    # binaries — minutes of work. Detach it so the installer never blocks. Search
    # uses ripgrep meanwhile. Once the binaries are ready, _zoekt_provision_background
    # calls 'lc code index' to build the trigram index automatically, so the
    # user never needs to re-run indexing manually. Progress and any failure land in $log.
    ( _zoekt_provision_background ) >"$log" 2>&1 </dev/null &
    disown 2>/dev/null || true

    info "Zoekt: building binaries and index in the background"
}

# Install jj (Jujutsu VCS). Best-effort: a failed install only warns — LemonCrow
# works without jj.
install_jj_if_needed() {
    if command -v jj >/dev/null 2>&1; then
        local jj_ver
        jj_ver="$(jj --version 2>/dev/null || echo "present")"
        _SPINNER_MSG="jj already installed (${jj_ver})"
        _spinner_stop ok
        return 0
    fi
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] install jj (Jujutsu) via brew or cargo"
        return 0
    fi
    case "$OS_NAME" in
        darwin)
            if command -v brew >/dev/null 2>&1; then
                spin_tail "Installing jj (Jujutsu)" brew install jj \
                    || warn "jj install failed — continuing without it"
            elif command -v cargo >/dev/null 2>&1; then
                spin_tail "Installing jj (Jujutsu)" cargo install --locked jj-cli \
                    || warn "jj install failed — continuing without it"
            else
                warn "Neither Homebrew nor cargo found. Install jj manually: https://martinvonz.github.io/jj/latest/install-and-setup"
            fi
            ;;
        linux)
            if command -v cargo >/dev/null 2>&1; then
                spin_tail "Installing jj (Jujutsu)" cargo install --locked jj-cli \
                    || warn "jj install failed — continuing without it"
            elif command -v brew >/dev/null 2>&1; then
                spin_tail "Installing jj (Jujutsu)" brew install jj \
                    || warn "jj install failed — continuing without it"
            else
                warn "cargo not found. Install jj manually: https://martinvonz.github.io/jj/latest/install-and-setup"
            fi
            ;;
    esac
}

# Install rtk when prompt_rtk_selection opted in. Soft integration: a failed
# install only warns — it must never fail the LemonCrow install. Pinned to
# LEMONCROW_RTK_TAG so release-time installs are reproducible.
# Bootstrap a minimal Rust toolchain via rustup when cargo is missing, so the
# optional rtk install (`cargo install --git ...`) below has something to run.
# Runs rustup's own installer, which persists PATH into the shell profile
# itself. Failure here is soft: rtk stays skipped, nothing else in the
# installer depends on cargo.
_install_rustup() {
    command -v curl >/dev/null 2>&1 || return 1
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y -q --default-toolchain stable --profile minimal
}

install_rtk_if_selected() {
    [[ "$INSTALL_RTK" != "1" ]] && return 0
    local rtk_ref=()
    [[ -n "${LEMONCROW_RTK_TAG}" ]] && rtk_ref=(--tag "${LEMONCROW_RTK_TAG}")
    if command -v rtk >/dev/null 2>&1; then
        local ver
        ver="$(rtk --version 2>/dev/null || echo "present")"
        _SPINNER_MSG="rtk already installed (${ver})"
        _spinner_stop ok
        return 0
    fi
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] cargo install --git https://github.com/rtk-ai/rtk${LEMONCROW_RTK_TAG:+ --tag ${LEMONCROW_RTK_TAG}}"
        return 0
    fi
    if ! command -v cargo >/dev/null 2>&1; then
        # spin_tail (not spin): rustup's download+extract takes a while with
        # no output otherwise, which reads as a hung spinner.
        spin_tail "Installing Rust toolchain (cargo, for rtk)" _install_rustup \
            || warn "rustup install failed — install cargo manually from https://rustup.rs to enable rtk."
        [[ -x "${HOME}/.cargo/bin/cargo" ]] && export PATH="${HOME}/.cargo/bin:${PATH}"
    fi
    if ! command -v cargo >/dev/null 2>&1; then
        warn "cargo unavailable — skipping rtk (LemonCrow works without it)."
        return 0
    fi
    # spin_tail: a from-source cargo build can take minutes; stream the
    # "Compiling ..." lines so it doesn't look stuck.
    spin_tail "Installing rtk ${LEMONCROW_RTK_TAG:-HEAD} (command compactor)" \
        cargo install --git https://github.com/rtk-ai/rtk ${rtk_ref[@]+"${rtk_ref[@]}"} \
        || warn "rtk install failed — LemonCrow works without it (soft integration)."
}

run() {
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

# Downloads the pinned ast-grep binary to $1 and symlinks it into LEMONCROW_BIN_DIR.
# Wrapped in `spin` by install_code_tools so a download failure surfaces as a
# visible line instead of a silent verbose-only message.
_install_astgrep_binary() {
    local astgrep_dest="$1"
    python3 - "${astgrep_dest}" <<'PYEOF'
import hashlib, io, platform, stat, sys, urllib.request, zipfile
from pathlib import Path
dest = Path(sys.argv[1])
ARCH = {'amd64': 'x86_64', 'x64': 'x86_64', 'arm64': 'aarch64'}.get(
    platform.machine().lower(), platform.machine().lower())
ASSETS = {
    'x86_64': (
        'https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-x86_64-unknown-linux-gnu.zip',
        '52aef3ed330a5fb1d9f399b83285bfcf47d92401249803f62711573e83cb47ae'),
    'aarch64': (
        'https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-aarch64-unknown-linux-gnu.zip',
        'a68d7645d49dbd97b423cc8a64f7839fe5541eedf0b4bb4ab79f4ba5d53f0376'),
    'Darwin-x86_64': (
        'https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-x86_64-apple-darwin.zip',
        '6652401a9b98f7c8c528f969d34e2a42d2cb60f29fc4dc569209d16c29702d9c'),
    'Darwin-aarch64': (
        'https://github.com/ast-grep/ast-grep/releases/download/0.42.2/app-aarch64-apple-darwin.zip',
        '9f1522db1f7174ab0cba5a6d1df1861f9b92803fac407988177c28f744bd0f94'),
}
os_prefix = 'Darwin-' if platform.system() == 'Darwin' else ''
key = os_prefix + ARCH
if key not in ASSETS:
    sys.exit(f'no pinned ast-grep asset for {key!r}')
url, sha256 = ASSETS[key]
dest.parent.mkdir(parents=True, exist_ok=True)
with urllib.request.urlopen(url, timeout=120) as r:
    data = r.read()
if hashlib.sha256(data).hexdigest() != sha256:
    sys.exit('sha256 mismatch')
with zipfile.ZipFile(io.BytesIO(data)) as z:
    member = next((n for n in z.namelist() if Path(n).name == 'ast-grep'), None)
    if member is None:
        sys.exit('ast-grep binary not found in zip')
    dest.write_bytes(z.read(member))
dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
PYEOF
    local ret=$?
    # Symlink into LEMONCROW_BIN_DIR so shutil.which('ast-grep') finds it without
    # needing an env-var export in the shell profile.
    [[ $ret -eq 0 ]] && ln -sf "${astgrep_dest}" "${LEMONCROW_BIN_DIR}/ast-grep" 2>/dev/null
    return $ret
}

# Downloads the pinned ripgrep binary to $1 and symlinks it into LEMONCROW_BIN_DIR.
# rg backs the grep fallback in search_read/native_search when Zoekt isn't built;
# only called when no system rg is found (install_code_tools checks first), so
# this never shadows a newer system install.
_install_ripgrep_binary() {
    local rg_dest="$1"
    python3 - "${rg_dest}" <<'PYEOF'
import hashlib, io, platform, stat, sys, tarfile, urllib.request
from pathlib import Path
dest = Path(sys.argv[1])
ARCH = {'amd64': 'x86_64', 'x64': 'x86_64', 'arm64': 'aarch64'}.get(
    platform.machine().lower(), platform.machine().lower())
VER = '14.1.1'
ASSETS = {
    'x86_64': (
        f'https://github.com/BurntSushi/ripgrep/releases/download/{VER}/ripgrep-{VER}-x86_64-unknown-linux-musl.tar.gz',
        '4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e'),
    'aarch64': (
        f'https://github.com/BurntSushi/ripgrep/releases/download/{VER}/ripgrep-{VER}-aarch64-unknown-linux-gnu.tar.gz',
        'c827481c4ff4ea10c9dc7a4022c8de5db34a5737cb74484d62eb94a95841ab2f'),
    'Darwin-x86_64': (
        f'https://github.com/BurntSushi/ripgrep/releases/download/{VER}/ripgrep-{VER}-x86_64-apple-darwin.tar.gz',
        'fc87e78f7cb3fea12d69072e7ef3b21509754717b746368fd40d88963630e2b3'),
    'Darwin-aarch64': (
        f'https://github.com/BurntSushi/ripgrep/releases/download/{VER}/ripgrep-{VER}-aarch64-apple-darwin.tar.gz',
        '24ad76777745fbff131c8fbc466742b011f925bfa4fffa2ded6def23b5b937be'),
}
os_prefix = 'Darwin-' if platform.system() == 'Darwin' else ''
key = os_prefix + ARCH
if key not in ASSETS:
    sys.exit(f'no pinned ripgrep asset for {key!r}')
url, sha256 = ASSETS[key]
dest.parent.mkdir(parents=True, exist_ok=True)
with urllib.request.urlopen(url, timeout=120) as r:
    data = r.read()
if hashlib.sha256(data).hexdigest() != sha256:
    sys.exit('sha256 mismatch')
with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tf:
    member = next((m for m in tf.getmembers() if Path(m.name).name == 'rg'), None)
    if member is None:
        sys.exit('rg binary not found in tarball')
    extracted = tf.extractfile(member)
    if extracted is None:
        sys.exit('rg member is not a regular file')
    dest.write_bytes(extracted.read())
dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
PYEOF
    local ret=$?
    [[ $ret -eq 0 ]] && ln -sf "${rg_dest}" "${LEMONCROW_BIN_DIR}/rg" 2>/dev/null
    return $ret
}

install_code_tools() {
    # Install optional code-quality tools used by edit hooks and the rename backend.
    # All steps are best-effort: missing tools are warned about but do not abort the
    # install. All of them -- language tooling, ast-grep, jj, rtk -- are installed
    # here so they land under one "Installing tools" checklist instead of
    # scattering across the installer.
    local os_type
    os_type="$(uname -s)"


    # eslint + ts-morph + typescript (TypeScript/JavaScript lint/type-check/rename tools)
    # require npm.
    if command -v npm >/dev/null 2>&1; then
        mkdir -p "$LEMONCROW_NODE_DIR" "$LEMONCROW_NODE_DIR/bin"
        if [[ -x "${LEMONCROW_NODE_DIR}/bin/eslint" && -x "${LEMONCROW_NODE_DIR}/bin/tsc" ]]; then
            _SPINNER_MSG="JS/TS tooling already installed"
            _spinner_stop ok
        else
            spin_tail "Installing JS/TS tooling" npm install -g --prefix "$LEMONCROW_NODE_DIR" --no-fund eslint ts-morph typescript
        fi
    else
        warn "npm not found - skipping JS/TS tools. Install Node.js 20+ to enable."
    fi

    # Rust toolchain - only used by edit hooks for Rust file lint-fix. Optional.
    if ! command -v cargo >/dev/null 2>&1; then
        verbose "cargo not found - skipping optional Rust edit hooks"
    else
        verbose "Found cargo: $(cargo --version 2>/dev/null || echo unknown)"
    fi

    # ast-grep binary (codemod tool dependency). Compiled Rust CLI; no pip wheel exists.
    # The managed bootstrap in binaries.py lazy-installs at first use, but that fails
    # in network-restricted environments (proxy CA not trusted by Python ssl). Install
    # eagerly here so the binary is always available, and set LEMONCROW_AST_GREP_BIN to
    # the fixed path so discover_astgrep_binary() never needs to download at runtime.
    # Version/URL/SHA must stay in sync with:
    #   src/lemoncrow/infra/code_intel/astgrep/binaries.py (_MANAGED_VERSION + _MANAGED_ASSETS)
    if command -v python3 >/dev/null 2>&1; then
        local astgrep_dest="${LEMONCROW_INSTALL_DIR}/.lemoncrow/ast-grep"
        if [[ ! -x "${astgrep_dest}" ]]; then
            spin "Installing ast-grep" _install_astgrep_binary "${astgrep_dest}" \
                || warn "ast-grep bootstrap failed -- codemod tool will lazy-install on first use"
        else
            _SPINNER_MSG="ast-grep already installed"
            _spinner_stop ok
        fi
    fi

    # ripgrep (grep fallback for search_read/native_search when Zoekt isn't
    # built). Skip entirely when a system rg is already on PATH.
    if ! command -v rg >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
        local rg_dest="${LEMONCROW_INSTALL_DIR}/.lemoncrow/rg"
        if [[ ! -x "${rg_dest}" ]]; then
            spin "Installing ripgrep" _install_ripgrep_binary "${rg_dest}" \
                || warn "ripgrep bootstrap failed -- search falls back to slower python grep"
        else
            _SPINNER_MSG="ripgrep already installed"
            _spinner_stop ok
        fi
    fi

    # jj (optional VCS) and rtk (optional command compactor) are their own
    # soft-fail installs but belong on the same checklist as the tools above.
    install_jj_if_needed
    install_rtk_if_selected
}

# Detect the user's shell profile file
_detect_shell_profile() {
    local shell_name
    shell_name="$(basename "${SHELL:-bash}")"
    case "$shell_name" in
        zsh)  printf "%s" "${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) printf "%s" "$HOME/.bashrc" ;;
        fish) printf "%s" "$HOME/.config/fish/config.fish" ;;
        *)    printf "%s" "$HOME/.profile" ;;
    esac
}

# Write sentinel-guarded PATH exports to the user's shell profile.
# Replaces on re-install instead of duplicating.
_ensure_path_persistence() {
    local profile_file sentinel_start sentinel_end node_user_bin
    local tmp_input tmp_output in_block line

    profile_file="$(_detect_shell_profile)"
    sentinel_start="# >>> lemoncrow path setup >>>"
    sentinel_end="# <<< lemoncrow path setup <<<"
    node_user_bin="${LEMONCROW_NODE_DIR}/bin"

    mkdir -p "$(dirname "$profile_file")" 2>/dev/null || true
    touch "$profile_file"

    tmp_input="$(mktemp)"
    tmp_output="$(mktemp)"

    # Build the new sentinel block
    {
        printf '%s\n' "$sentinel_start"
        printf 'export PATH="%s:$PATH"\n' "$LEMONCROW_BIN_DIR"
        if [[ -d "$node_user_bin" ]]; then
            printf 'export PATH="%s:$PATH"\n' "$node_user_bin"
        fi
        printf '%s\n' "$sentinel_end"
    } > "$tmp_input"

    if grep -qF "$sentinel_start" "$profile_file" 2>/dev/null; then
        # Replace existing sentinel block in-place
        in_block=0
        while IFS= read -r line; do
            if [[ "$line" == "$sentinel_start" ]]; then
                in_block=1
                cat "$tmp_input"
            elif [[ "$line" == "$sentinel_end" ]]; then
                in_block=0
            elif [[ "$in_block" == "0" ]]; then
                printf '%s\n' "$line"
            fi
        done < "$profile_file" > "$tmp_output"
        mv "$tmp_output" "$profile_file"
    else
        # Append new block
        printf '\n' >> "$profile_file"
        cat "$tmp_input" >> "$profile_file"
    fi

    rm -f "$tmp_input" "$tmp_output"

    info "Added LemonCrow directories to PATH in ${profile_file/#$HOME/~}"
}

# _capture_install_previous_version — preserve the executable version before
# the installer replaces it. The shared writer runs after replacement, when
# `lc --version` can only report the new version.
_capture_install_previous_version() {
    [[ -n "${LEMONCROW_PREVIOUS_VERSION:-}" ]] && return 0
    local lemoncrow_bin="${LEMONCROW_BIN_DIR}/lc"
    [[ -x "$lemoncrow_bin" ]] || lemoncrow_bin="lc"
    command -v "$lemoncrow_bin" >/dev/null 2>&1 || return 0

    local previous_version
    previous_version=$("$lemoncrow_bin" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    if [[ -n "$previous_version" ]]; then
        export LEMONCROW_PREVIOUS_VERSION="$previous_version"
    fi
    return 0
}

# _write_install_update_state — record a version bump so the SessionStart hook
# can show an update notification in Claude Code on the next session start.
# Uses the version captured before replacement, then falls back to the last
# known state for older install paths. Fresh installs and same-version
# reinstalls do not notify. Fail-open: errors are silently swallowed.
_write_install_update_state() {
    [[ "${LEMONCROW_DRY_RUN:-0}" == "1" ]] && return 0
    local lemoncrow_bin="${LEMONCROW_BIN_DIR}/lc"
    command -v "$lemoncrow_bin" >/dev/null 2>&1 || lemoncrow_bin="lc"
    command -v "$lemoncrow_bin" >/dev/null 2>&1 || return 0

    local new_ver
    new_ver=$("$lemoncrow_bin" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    [[ -n "$new_ver" ]] || return 0

    local state_file="${HOME}/.lemoncrow/update_state.json"
    local prev_ver="${LEMONCROW_PREVIOUS_VERSION:-}"
    if [[ -z "$prev_ver" && -f "$state_file" ]]; then
        # current_version in the existing file is the version that was known
        # before this install (possibly already shown/notified to the user).
        prev_ver=$(python3 -c "
import json
try:
    print(json.load(open('${state_file}')).get('current_version',''))
except Exception:
    pass
" 2>/dev/null || true)
    fi

    local method="install"
    [[ "${LEMONCROW_LOCAL:-0}" == "1" ]] && method="source"

    mkdir -p "${HOME}/.lemoncrow"

    if [[ -z "$prev_ver" ]]; then
        # Fresh install: seed the file so the NEXT upgrade can detect the diff.
        # notified=true so no spurious notification fires on this session start.
        python3 -c "
import json, datetime, pathlib, sys
data = {
    'previous_version': '',
    'current_version': sys.argv[1],
    'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'method': sys.argv[2],
    'notified': True,
}
pathlib.Path(sys.argv[3]).write_text(json.dumps(data, indent=2), encoding='utf-8')
" "$new_ver" "$method" "$state_file" 2>/dev/null || true
        return 0
    fi

    # Same-version reinstall: nothing to notify.
    [[ "$prev_ver" != "$new_ver" ]] || return 0

    python3 -c "
import json, datetime, pathlib, sys
data = {
    'previous_version': sys.argv[1],
    'current_version': sys.argv[2],
    'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'method': sys.argv[3],
    'notified': False,
}
pathlib.Path(sys.argv[4]).write_text(json.dumps(data, indent=2), encoding='utf-8')
" "$prev_ver" "$new_ver" "$method" "$state_file" 2>/dev/null || true
}

# run_setup — shared post-install steps invoked by BOTH installers after the
# LemonCrow CLI is available at "$LEMONCROW_BIN_DIR/lc": code tools, memory /
# zoekt selection, host integrations, init, indexing, optimize automation,
# background services, PATH persistence, and the final report.
prompt_knowledge_extraction() {
    # Opt-in. Honor an explicit env preset (CI / scripted installs); only ask
    # when interactive and the user hasn't already decided.
    [[ "$LEMONCROW_KB_EXTRACT_PRESET" == "1" ]] && return 0
    [[ "$LEMONCROW_NON_INTERACTIVE" == "1" ]] && return 0
    has_interactive_input || return 0

    if supports_interactive_selector; then
        local kb_yn=1
        interactive_single_select \
            "Auto-extract review rules from .lessons files?" \
            kb_yn \
            1 \
            "Yes – populate reviewer knowledge base" \
            "No"
        if [[ "$kb_yn" != "0" ]]; then
            LEMONCROW_KB_EXTRACT=0
            return 0
        fi
        LEMONCROW_KB_EXTRACT=1

        local backend_idx=0
        interactive_single_select \
            "Knowledge extraction backend?" \
            backend_idx \
            0 \
            "auto (LemonCrow model)" \
            "claude" \
            "codex" \
            "ollama"
        case "$backend_idx" in
            1) LEMONCROW_KB_HOST=claude ;;
            2) LEMONCROW_KB_HOST=codex ;;
            3) LEMONCROW_KB_HOST=ollama ;;
            *) LEMONCROW_KB_HOST=auto ;;
        esac
    else
        local ans=""
        printf "  ◇  Auto-extract review rules from .lessons files? [y/N] "
        IFS= read -r ans </dev/tty 2>/dev/null || ans=""
        case "$ans" in
            y | Y | yes | YES) LEMONCROW_KB_EXTRACT=1 ;;
            *) LEMONCROW_KB_EXTRACT=0; return 0 ;;
        esac

        local choice=""
        printf "  ◇  Backend  1) auto (LemonCrow model)  2) claude  3) codex  4) ollama  [1] "
        IFS= read -r choice </dev/tty 2>/dev/null || choice=""
        case "$choice" in
            2) LEMONCROW_KB_HOST=claude ;;
            3) LEMONCROW_KB_HOST=codex ;;
            4) LEMONCROW_KB_HOST=ollama ;;
            *) LEMONCROW_KB_HOST=auto ;;
        esac
    fi

    if [[ "$LEMONCROW_KB_HOST" == "ollama" ]]; then
        local model=""
        printf "  ◇  Ollama model name [llama3.1]: "
        IFS= read -r model </dev/tty 2>/dev/null || model=""
        LEMONCROW_KB_MODEL="${model:-llama3.1}"
    fi

    if [[ "$LEMONCROW_KB_HOST" == "auto" || "$LEMONCROW_KB_HOST" == "claude" ]]; then
        local cap=""
        printf "  ◇  Max spend per run in USD [%s]: " "$LEMONCROW_KB_MAX_SPEND"
        IFS= read -r cap </dev/tty 2>/dev/null || cap=""
        [[ -n "$cap" ]] && LEMONCROW_KB_MAX_SPEND="$cap"
    fi
}

run_knowledge_extraction_if_selected() {
    [[ "$LEMONCROW_KB_EXTRACT" == "1" ]] || return 0
    local lemoncrow_bin="$LEMONCROW_BIN_DIR/lc"
    [[ -x "$lemoncrow_bin" ]] || lemoncrow_bin="lc"
    step_start "Extracting knowledge from .lessons (host=$LEMONCROW_KB_HOST)"
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        info "[dry-run] $lemoncrow_bin knowledge extract --host $LEMONCROW_KB_HOST --max-spend $LEMONCROW_KB_MAX_SPEND"
    else
        local kb_args=(knowledge extract --host "$LEMONCROW_KB_HOST" --max-spend "$LEMONCROW_KB_MAX_SPEND")
        [[ -n "$LEMONCROW_KB_MODEL" ]] && kb_args+=(--model "$LEMONCROW_KB_MODEL")
        if ! "$lemoncrow_bin" "${kb_args[@]}"; then
            degrade "knowledge extraction did not complete (continuing install)"
        fi
    fi
    step_done
}

configure_recall_if_selected() {
    # Persist only when an explicit env preset was given, so a re-install never
    # resets a prior recall config. Without a preset, Recall stays on by default
    # (local embedder) via the runtime — no install-time prompt, no persistence.
    [[ "$LEMONCROW_RECALL_PRESET" == "1" ]] || return 0
    local lemoncrow_bin="$LEMONCROW_BIN_DIR/lc"
    [[ -x "$lemoncrow_bin" ]] || lemoncrow_bin="lc"
    local auto_flag="--no-auto-index"
    [[ "$LEMONCROW_RECALL_INDEX" == "1" ]] && auto_flag="--auto-index"
    local rc_args=(recall config "$auto_flag" --embedder "$LEMONCROW_RECALL_EMBEDDER")
    [[ -n "$LEMONCROW_RECALL_EMBED_MODEL" ]] && rc_args+=(--embed-model "$LEMONCROW_RECALL_EMBED_MODEL")
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        info "[dry-run] $lemoncrow_bin ${rc_args[*]}"
        return 0
    fi
    if ! "$lemoncrow_bin" "${rc_args[@]}" >/dev/null 2>&1; then
        degrade "recall config did not complete (continuing install)"
    fi
}

# _lemoncrow_list_rows <plain-text-cli-output>
# Parses "  [available] name   ~N tok/turn standing cost" lines from the
# CLI's own `lc agent|skill list` output into "name<TAB>cost" rows.
# Deliberately parses the plain listing (not --json) so the bootstrap never
# needs a python3/jq dependency just to show token costs; the numbers still
# come straight from the CLI, never hardcoded or recomputed here.
_lemoncrow_list_rows() {
    local line name cost
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*\[(available|installed)\][[:space:]]+([A-Za-z0-9_-]+)[[:space:]]+~([0-9]+)\ tok ]]; then
            name="${BASH_REMATCH[2]}"
            cost="${BASH_REMATCH[3]}"
            printf '%s\t%s\n' "$name" "$cost"
        fi
    done <<< "$1"
}

# _filter_csv_against_set <comma-list> <valid-name>...
# Trims and validates a free-text comma-separated answer against the real
# available-name set, dropping (and warning about) anything unrecognized.
# Only used on the non-menu (dumb-terminal) fallback path -- the interactive
# multi-select path can't produce an invalid name since it picks by index.
_filter_csv_against_set() {
    local input="$1"
    shift
    local valid=("$@")
    [[ -n "$input" ]] || return 0
    local -a tokens
    IFS=',' read -ra tokens <<< "$input"
    local token trimmed v ok out=""
    for token in "${tokens[@]}"; do
        trimmed="$(echo "$token" | xargs)"
        [[ -n "$trimmed" ]] || continue
        ok=0
        for v in "${valid[@]}"; do
            [[ "$trimmed" == "$v" ]] && { ok=1; break; }
        done
        if [[ "$ok" == "1" ]]; then
            out+="${out:+,}${trimmed}"
        else
            # Not the shared warn() helper: this function's stdout is captured
            # via command substitution by its caller, and warn() writes to
            # stdout (by design, for the top-level install report) -- using it
            # here would corrupt the returned CSV. Print straight to stderr.
            printf "  %b\xe2\x9a\xa0%b  ignoring unknown name: %s\n" "$C_YELLOW" "$C_RESET" "$trimmed" >&2
        fi
    done
    printf '%s' "$out"
}

run_setup() {
    persist_telegraphic_selection

    local stack_available=0
    if [[ "$LEMONCROW_NO_STACK" != "1" ]] && command -v npm >/dev/null 2>&1; then
        stack_available=1
    elif [[ "$LEMONCROW_NO_STACK" != "1" ]]; then
        warn "npm is required to run the optional visualization stack; skipping stack setup"
    fi

    local stack_expected=0
    if [[ "$LEMONCROW_NO_SERVICECTL" != "1" && "$stack_available" == "1" ]] && { command -v systemctl >/dev/null 2>&1 || [[ "$(uname -s)" == "Darwin" ]]; }; then
        stack_expected=1
    fi

    step_start "Installing tools"
    install_code_tools
    step_done

    local selected_memory=""
    if [[ "$LEMONCROW_ADVANCED" == "1" ]]; then
        if [[ -z "$LEMONCROW_MEMORY_BACKEND" ]]; then
            warn "--advanced set but no --memory selected; no memory sidecar will be installed"
        elif [[ "$LEMONCROW_MEMORY_BACKEND" == "letta" ]]; then
            if command -v docker >/dev/null 2>&1; then
                selected_memory="letta"
                verbose "Memory sidecar: Letta (Docker)"
            else
                warn "--memory letta requires Docker - skipping Letta sidecar"
            fi
        elif [[ "$LEMONCROW_MEMORY_BACKEND" == "openmemory" ]]; then
            local _om_missing=()
            command -v docker >/dev/null 2>&1 || _om_missing+=("docker")
            command -v git >/dev/null 2>&1 || _om_missing+=("git")
            command -v make >/dev/null 2>&1 || _om_missing+=("make")
            local _has_llm=0
            [[ -n "${LEMONCROW_OPENMEMORY_OPENAI_API_KEY:-}${OPENAI_API_KEY:-}" ]] && _has_llm=1
            command -v ollama >/dev/null 2>&1 && _has_llm=1
            [[ -n "${OLLAMA_HOST:-}" ]] && _has_llm=1
            [[ "$_has_llm" == "1" ]] || _om_missing+=("OPENAI_API_KEY or ollama")
            if [[ ${#_om_missing[@]} -gt 0 ]]; then
                warn "OpenMemory prerequisites missing (${_om_missing[*]}) - skipping memory sidecar"
            else
                selected_memory="openmemory"
                verbose "Memory sidecar: OpenMemory (Docker)"
            fi
        fi
    fi

    local selected_zoekt=""
    if [[ "$LEMONCROW_ZOEKT" == "1" ]]; then
        if _zoekt_all_local_binaries_present; then
            selected_zoekt="1"
            verbose "Zoekt runtime: local binaries found on PATH"
        else
            selected_zoekt="1"
            verbose "Zoekt runtime: local binaries will be installed"
        fi
    fi

    local memory_record="${HOME}/.lemoncrow/memory_backend"
    if [[ -n "$selected_memory" ]]; then
        if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
            echo "[dry-run] printf '%s\\n' '$selected_memory' > '$memory_record'"
        else
            mkdir -p "${HOME}/.lemoncrow"
            printf '%s\n' "$selected_memory" > "$memory_record"
        fi
    elif [[ -f "$memory_record" && "$LEMONCROW_DRY_RUN" != "1" ]]; then
        : >"$memory_record"
    fi

    local zoekt_record="${HOME}/.lemoncrow/zoekt_enabled"
    if [[ "$selected_zoekt" == "1" ]]; then
        if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
            echo "[dry-run] printf '1\\n' > '$zoekt_record'"
        else
            mkdir -p "${HOME}/.lemoncrow"
            printf '1\n' > "$zoekt_record"
        fi
    elif [[ -f "$zoekt_record" && "$LEMONCROW_DRY_RUN" != "1" ]]; then
        : >"$zoekt_record"
    fi

    local node_user_bin="${LEMONCROW_NODE_DIR}/bin"
    _ensure_path_persistence
    # Re-export for this session too
    if [[ ":$PATH:" != *":$LEMONCROW_BIN_DIR:"* ]]; then
        export PATH="${LEMONCROW_BIN_DIR}:${PATH}"
    fi
    if [[ -d "$node_user_bin" && ":$PATH:" != *":$node_user_bin:"* ]]; then
        export PATH="${node_user_bin}:${PATH}"
    fi

    local lemoncrow_cli="$LEMONCROW_BIN_DIR/lc"

    if [[ "$INSTALL_ZOEKT_LOCAL" == "1" ]]; then
        install_local_zoekt_if_selected
    fi

    if [[ "$LEMONCROW_NO_HOSTS" != "1" ]]; then
        step_start "Installing host integrations"
        local host_install_args=()
        local passthrough
        for passthrough in "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"; do
            case "$passthrough" in
                --dry-run|--print-only|--strict)
                    host_install_args+=("$passthrough")
                    ;;
            esac
        done
        if [[ ${#HOST_FLAGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_FLAGS[@]}")
        else
            # No explicit selection (e.g. non-interactive run): cap to the
            # supported set so copilot/antigravity are never auto-installed.
            host_install_args+=(--claude --codex --opencode)
        fi
        if [[ ${#HOST_SCOPE_ARGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_SCOPE_ARGS[@]}")
        fi
        if [[ ${#HOST_EXTRA_ARGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_EXTRA_ARGS[@]}")
        fi
        local project_workspace=""
        if [[ "${LEMONCROW_LOCAL}" == "1" ]]; then
            local local_repo_root=""
            if local_repo_root="$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null)"; then
                project_workspace="$local_repo_root"
            fi
        fi
        if [[ -z "$project_workspace" ]] && host_scope_is_workspace; then
            local idx
            for idx in "${!HOST_SCOPE_ARGS[@]}"; do
                if [[ "${HOST_SCOPE_ARGS[$idx]}" == "--workspace" ]]; then
                    if [[ $((idx + 1)) -lt ${#HOST_SCOPE_ARGS[@]} ]]; then
                        project_workspace="${HOST_SCOPE_ARGS[$((idx + 1))]}"
                    fi
                    break
                fi
            done
        fi
        if [[ -n "$project_workspace" ]]; then
            local agents_install_args=(--workspace "$project_workspace")
            has_flag "--dry-run" && agents_install_args+=(--dry-run)
            has_flag "--print-only" && agents_install_args+=(--print-only)
            if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
                echo "[dry-run] bash $LEMONCROW_INSTALL_DIR/scripts/install_agents.sh ${agents_install_args[*]}"
            else
                if [[ "$LEMONCROW_VERBOSE" == "1" ]]; then
                    bash "$LEMONCROW_INSTALL_DIR/scripts/install_agents.sh" "${agents_install_args[@]}"
                else
                    bash "$LEMONCROW_INSTALL_DIR/scripts/install_agents.sh" "${agents_install_args[@]}" >>"$LEMONCROW_INSTALL_LOG_FILE" 2>&1
                fi
            fi
        fi
        if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
            echo "[dry-run] bash $LEMONCROW_INSTALL_DIR/scripts/install_hosts.sh ${host_install_args[*]+${host_install_args[*]}}"
        else
            local host_output host_output_file host_ret
            host_output_file="${TMPDIR:-/tmp}/lemoncrow-hosts.$(date +%Y%m%dT%H%M%S).$$.log"
            : >"$host_output_file" 2>/dev/null || host_output_file="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-hosts.XXXXXX")"
            set +e
            if [[ "$LEMONCROW_VERBOSE" == "1" ]]; then
                if [[ -n "$C_RESET" ]]; then
                    FORCE_COLOR=1 bash "$LEMONCROW_INSTALL_DIR/scripts/install_hosts.sh" "${host_install_args[@]+"${host_install_args[@]}"}" 2>&1 | tee "$host_output_file"
                else
                    bash "$LEMONCROW_INSTALL_DIR/scripts/install_hosts.sh" "${host_install_args[@]+"${host_install_args[@]}"}" 2>&1 | tee "$host_output_file"
                fi
                host_ret=${PIPESTATUS[0]}
            else
                local had_lastpipe=0
                # lastpipe is bash 4.2+; macOS 3.2 doesn't have it.
                if shopt -q lastpipe 2>/dev/null; then
                    had_lastpipe=1
                else
                    shopt -s lastpipe 2>/dev/null || true
                fi
                _SPINNER_MSG="Installing host integrations"                _SPINNER_ACTIVE=1
                _spinner_run
                LEMONCROW_HOST_STATUS_STREAM=1 bash "$LEMONCROW_INSTALL_DIR/scripts/install_hosts.sh" "${host_install_args[@]+"${host_install_args[@]}"}" 2>&1 | while IFS= read -r line; do
                    printf "%s\n" "$line" >>"$host_output_file"
                    if [[ "$line" =~ ^@@LEMONCROW_HOST_STATUS@@[[:space:]]+([A-Z]+)[[:space:]]+(.+)$ ]]; then
                        local status="${BASH_REMATCH[1]}"
                        local hname="${BASH_REMATCH[2]}"
                        case "$status" in
                            START)
                                local status_label
                                status_label="$(format_host_status_label "$hname")"
                                _spinner_pause
                                _SPINNER_MSG="Installing on ${status_label}"
                                _spinner_resume
                                ;;
                            OK)
                                local status_label
                                status_label="$(format_host_status_label "$hname")"
                                _spinner_pause
                                printf "%b│%b  %b✓%b  %s\n" "$C_FRAME" "$C_RESET" "$C_GREEN" "$C_RESET" "$status_label"
                                _SPINNER_MSG="Installing host integrations"
                                _spinner_resume
                                ;;
                            WARN)
                                local status_label
                                status_label="$(format_host_status_label "$hname")"
                                _spinner_pause
                                printf "%b│%b  %b⚠%b  %s\n" "$C_FRAME" "$C_RESET" "$C_YELLOW" "$C_RESET" "$status_label"
                                _SPINNER_MSG="Installing host integrations"
                                _spinner_resume
                                ;;
                            FAILED)
                                local status_label
                                status_label="$(format_host_status_label "$hname")"
                                _spinner_pause
                                printf "%b│%b  %b✗%b  %s\n" "$C_FRAME" "$C_RESET" "$C_RED" "$C_RESET" "$status_label"
                                _SPINNER_MSG="Installing host integrations"
                                _spinner_resume
                                ;;
                            SKIPPED)
                                local status_label
                                status_label="$(format_host_status_label "$hname")"
                                _spinner_pause
                                printf "%b│%b  %b—%b  %s\n" "$C_FRAME" "$C_RESET" "$C_DIM" "$C_RESET" "$status_label"
                                _SPINNER_MSG="Installing host integrations"
                                _spinner_resume
                                ;;
                        esac
                    fi
                done
                host_ret=${PIPESTATUS[0]}
                if [[ "$had_lastpipe" -eq 0 ]]; then
                    shopt -u lastpipe 2>/dev/null || true
                fi
                _SPINNER_MSG="Installing host integrations"
                _spinner_pause
                _SPINNER_ACTIVE=0
            fi
            set -e
            host_output="$(cat "$host_output_file")"
            collect_issues_from_output "$host_output"
            if [[ $host_ret -ne 0 ]]; then
                ERRORS+=("One or more host integrations failed")
                FINAL_EXIT_CODE=1
                # Dump the full host output inline so failures are visible
                # even when sub-scripts don't stream verbose output.
                if [[ -s "$host_output_file" ]]; then
                    # Write host details to log file, not terminal — the
                    # @@LEMONCROW_HOST_STATUS@@ lines are internal markers.
                    {
                        printf -- "Host install details (from %s):\n" "$host_output_file"
                        cat "$host_output_file"
                        printf -- "--- end host output ---\n"
                    } >>"$LEMONCROW_INSTALL_LOG_FILE"
                fi
            fi
            if [[ -f "$host_output_file" ]]; then
                verbose "Host integration log preserved at: $host_output_file"
            fi
        fi
        # Persist host detection results for the local service/UI surfaces
        if [[ "$LEMONCROW_DRY_RUN" != "1" && -f "$LEMONCROW_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$LEMONCROW_INSTALL_DIR/scripts/status.sh" --write >>"$LEMONCROW_INSTALL_LOG_FILE" 2>&1 \
                || degrade "Failed to persist host detection status"
        fi
        step_done
    else
        step_start "Installing host integrations"
        info "Skipped (LEMONCROW_NO_HOSTS=1)"
        # Still persist current detection state even when skipping install
        if [[ "$LEMONCROW_DRY_RUN" != "1" && -f "$LEMONCROW_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$LEMONCROW_INSTALL_DIR/scripts/status.sh" --write >>"$LEMONCROW_INSTALL_LOG_FILE" 2>&1 \
                || degrade "Failed to persist host detection status"
        fi
        step_done
    fi

    local index_target=""
    local repo_root=""
    if repo_root="$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null)"; then
        index_target="$repo_root"
    fi

    step_start "Initializing"
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        if [[ "$LEMONCROW_AUTO_OPTIMIZE" == "1" ]]; then
            echo "[dry-run] $lemoncrow_cli optimize auto enable"
        else
            echo "[dry-run] $lemoncrow_cli optimize auto disable"
        fi
    else
        if [[ "$LEMONCROW_AUTO_OPTIMIZE" == "1" ]]; then
            "$lemoncrow_cli" optimize auto enable >>"$LEMONCROW_INSTALL_LOG_FILE" 2>&1 \
                || degrade "Failed to persist auto optimize settings"
        else
            "$lemoncrow_cli" optimize auto disable >>"$LEMONCROW_INSTALL_LOG_FILE" 2>&1 \
                || degrade "Failed to persist auto optimize settings"
        fi
    fi
    step_done
    if [[ "$LEMONCROW_NO_SERVICECTL" != "1" ]]; then
        if command -v systemctl >/dev/null 2>&1 || [[ "$(uname -s)" == "Darwin" ]]; then
            verbose "Registering LemonCrow services with background manager..."
            local background_args=()
            if [[ "$stack_available" == "1" ]]; then
                background_args+=("--with-stack")
            fi
            case "$selected_memory" in
                letta) background_args+=("--with-letta") ;;
                openmemory) background_args+=("--with-openmemory") ;;
            esac
            if [[ "$selected_zoekt" == "1" ]]; then
                background_args+=("--with-zoekt")
            fi

            if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $LEMONCROW_BIN_DIR/lc background install ${background_args[*]}"
            else
                "$LEMONCROW_BIN_DIR/lc" background install "${background_args[@]}" >>"$LEMONCROW_INSTALL_LOG_FILE" 2>&1
            fi
        else
            verbose "Starting LemonCrow background service controller (loose process)..."
            if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $LEMONCROW_BIN_DIR/lc servicectl start --interval-seconds $LEMONCROW_SERVICECTL_INTERVAL_SECONDS --maintenance-interval-seconds $LEMONCROW_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS"
            else
                "$LEMONCROW_BIN_DIR/lc" servicectl start \
                    --interval-seconds "$LEMONCROW_SERVICECTL_INTERVAL_SECONDS" \
                    --maintenance-interval-seconds "$LEMONCROW_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS" >>"$LEMONCROW_INSTALL_LOG_FILE" 2>&1
            fi

            if [[ "$stack_available" == "1" ]]; then
                verbose "Starting LemonCrow HTTP service..."
                if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
                    echo "[dry-run] $LEMONCROW_BIN_DIR/lcd start"
                else
                    "$LEMONCROW_BIN_DIR/lcd" start &
                    STACK_STARTED=1
                fi
            fi
        fi
    else
        verbose "Skipping background services because LEMONCROW_NO_SERVICECTL=1"
    fi

    run_knowledge_extraction_if_selected
    configure_recall_if_selected
    _write_install_update_state

    print_final_report
    local completion_title_line="✓ Installation Complete!                              "
    if [[ ${#ERRORS[@]} -gt 0 ]]; then
        completion_title_line="✗ Installation Completed with Errors                  "
    elif [[ ${#WARNINGS[@]} -gt 0 ]]; then
        completion_title_line="⚠ Installation Completed with Warnings                "
    fi

    if [[ ${#ERRORS[@]} -gt 0 ]]; then
        info "${C_BOLD}${C_RED}Completed with errors.${C_RESET}"
    elif [[ ${#WARNINGS[@]} -gt 0 ]]; then
        info "${C_BOLD}${C_YELLOW}Completed with warnings.${C_RESET}"
    fi
    printf "%b└%b\n\n" "$C_FRAME" "$C_RESET"

    printf "  %b┌─────────────────────────────────────────────────────────┐%b\n" "$C_PURPLE" "$C_RESET"
    printf "  %b│  %s │%b\n" "$C_PURPLE" "$completion_title_line" "$C_RESET"
    printf "  %b└─────────────────────────────────────────────────────────┘%b\n\n" "$C_PURPLE" "$C_RESET"

    if [[ "$STACK_STARTED" == "1" || "$stack_expected" == "1" ]]; then
        printf "%b📊 Visualization stack:%b\n" "$C_PURPLE" "$C_RESET"
        printf "  frontend: %bhttp://localhost:3125%b\n" "$C_PURPLE" "$C_RESET"
        printf "  service:  %bhttp://localhost:8787%b\n\n" "$C_PURPLE" "$C_RESET"
    fi
    local code_display="${LEMONCROW_BIN_DIR}/lc"
    code_display="${code_display/#$HOME/~}"
    printf "%b📁 Your files:%b\n\n" "$C_PURPLE" "$C_RESET"
    printf "   LemonCrow dir:   %s\n" "~/.lemoncrow"
    printf "   Binary:        %s\n\n" "$code_display"    
    printf "%b─────────────────────────────────────────────────────────%b\n\n" "$C_PURPLE" "$C_RESET"
    printf "%b🚀 Commands:%b\n\n" "$C_PURPLE" "$C_RESET"
    printf "   %blc%b init                Initialize LemonCrow for a new project\n" "$C_PURPLE" "$C_RESET"
    printf "   %blc%b status              View active runs\n" "$C_PURPLE" "$C_RESET"
    printf "   %blc%b import              Import past agent sessions\n" "$C_PURPLE" "$C_RESET"
    printf "   %blc%b memory recall       Search memory\n" "$C_PURPLE" "$C_RESET"
    printf "   %blc%b code index          Index current repository\n" "$C_PURPLE" "$C_RESET"
    printf "   %blcd%b status             Check service status\n\n" "$C_PURPLE" "$C_RESET"
    if [[ ${#WARNINGS[@]} -gt 0 || ${#ERRORS[@]} -gt 0 ]]; then
        printf "   installer log: %s\n\n" "$LEMONCROW_INSTALL_LOG_FILE"
    fi
    printf "%b─────────────────────────────────────────────────────────%b\n\n" "$C_PURPLE" "$C_RESET"

    # Deferred, un-spun on purpose: `lc init` requires a LemonCrow account and
    # opens an interactive browser login when none is found. Running it earlier
    # under `spin` (which captures stdout via command substitution) breaks TTY
    # detection, so login could never actually open — it just failed straight
    # to the "run lc login" error. Installation succeeds independent of this;
    # only project activation needs it, so it only runs when a repo was
    # detected, after the install is already reported complete.
    #
    # Also reconnect stdout/stderr to fd 7 (the real terminal, saved before the
    # `exec > >(tee ...)` redirect above) instead of the tee pipe: the CLI's
    # login-flow gate checks sys.stdout.isatty(), which is false through the
    # pipe even when the script itself is fully interactive. Without this,
    # `lc init` would deterministically hit the non-interactive error path on
    # every fresh install and never actually offer the login flow.
    if [[ -n "$index_target" ]]; then
        printf "%b🔑 Activating this project:%b\n\n" "$C_PURPLE" "$C_RESET"
        if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
            echo "[dry-run] $lemoncrow_cli init"
        elif [[ "$ORIGINAL_STDOUT_IS_TTY" == "1" && "$LEMONCROW_NON_INTERACTIVE" != "1" ]]; then
            "$lemoncrow_cli" init >&7 2>&7 || true
        else
            printf "   Run 'lc login' then 'lc init' to activate this project.\n"
        fi
        printf "\n"
    fi

    return "$FINAL_EXIT_CODE"
}
