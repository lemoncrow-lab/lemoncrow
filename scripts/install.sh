#!/usr/bin/env bash
# install.sh — bootstrap Atelier from GitHub using a curl|bash-friendly flow.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
#
# By default only the core service and frontend are installed natively.
# Pass --advanced --memory letta|openmemory to install one Docker sidecar.
#
# Optional environment variables:
#   ATELIER_REPO_URL   Git URL (default: https://github.com/pankaj4u4m/atelier.git)
#   ATELIER_REF        Git ref to install (default: main)
#   ATELIER_INSTALL_DIR Install location (default: ~/.local/share/atelier)
#   ATELIER_BIN_DIR    Global bin dir for console scripts (default: ~/.local/bin)
#   ATELIER_TOOL_DIR   uv tool environment dir (default: ~/.local/share/uv/tools)
#   ATELIER_NO_HOSTS   If set to 1, skip agent-host integration install scripts
#   ATELIER_NO_SERVICECTL If set to 1, skip starting the background service controller
#   ATELIER_SERVICECTL_INTERVAL_SECONDS Poll interval for servicectl (default: 60)
#   ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS Periodic maintenance interval (default: 21600)
#   ATELIER_DRY_RUN    If set to 1, print planned actions and exit
#   ATELIER_NO_STACK   If set to 1, skip starting the visualization stack (service + frontend)
#   ATELIER_ADVANCED   If set to 1, enable Docker sidecar install (requires --memory)
#   ATELIER_MEMORY_BACKEND  Memory sidecar to install: letta | openmemory (default: none)
#   ATELIER_ZOEKT      Install the persistent Zoekt code-search sidecar (Docker) (default: 1)
#   ATELIER_LOCAL      If set to 1, install from the current checkout in editable mode
#   ATELIER_VERBOSE   If set to 1, show verbose installation logs (default: 0)
#   ATELIER_STRICT     If set to 1, treat selected post-install degradations as errors
#   ATELIER_NON_INTERACTIVE If set to 1, disable all interactive prompts
#   ATELIER_ZOEKT_AUTO_INSTALL If set to 1, non-interactive runs install local zoekt binaries when missing (default: 1)
#   ATELIER_INSTALL_LOG_FILE Optional install log path (default: /tmp/atelier-install.<ts>.<pid>.log)
#   --workspace DIR    Install host/project MCP artifacts into DIR instead of user-global config
#
# Notes:
#   Exactly one memory sidecar can be active at a time; the selection is
#   persisted to ~/.atelier/memory_backend for uninstall cleanup.
#
#   Codex host install manages its Atelier AGENTS block with explicit START/END
#   sentinels so re-install can replace that block without overwriting user content.

set -euo pipefail

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

ATELIER_REPO_URL="${ATELIER_REPO_URL:-https://github.com/pankaj4u4m/atelier.git}"
ATELIER_REF="${ATELIER_REF:-main}"
ATELIER_INSTALL_DIR="${ATELIER_INSTALL_DIR:-${HOME}/.local/share/atelier}"
ATELIER_BIN_DIR="${ATELIER_BIN_DIR:-${HOME}/.local/bin}"
ATELIER_TOOL_DIR="${ATELIER_TOOL_DIR:-${HOME}/.local/share/uv/tools}"
ATELIER_INSTALL_RECORD="${ATELIER_INSTALL_RECORD:-${HOME}/.atelier/install_dir}"
ATELIER_NO_HOSTS="${ATELIER_NO_HOSTS:-0}"
ATELIER_NO_SERVICECTL="${ATELIER_NO_SERVICECTL:-0}"
ATELIER_SERVICECTL_INTERVAL_SECONDS="${ATELIER_SERVICECTL_INTERVAL_SECONDS:-60}"
ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS="${ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS:-21600}"
ATELIER_DRY_RUN="${ATELIER_DRY_RUN:-0}"
ATELIER_NO_STACK="${ATELIER_NO_STACK:-0}"
ATELIER_ADVANCED="${ATELIER_ADVANCED:-0}"
ATELIER_MEMORY_BACKEND="${ATELIER_MEMORY_BACKEND:-}"   # letta | openmemory | (empty = none)
ATELIER_ZOEKT="${ATELIER_ZOEKT:-1}"                    # 1 = install persistent Zoekt sidecar
ATELIER_LOCAL="${ATELIER_LOCAL:-0}"
ATELIER_STRICT="${ATELIER_STRICT:-0}"
ATELIER_VERBOSE="${ATELIER_VERBOSE:-0}"
ATELIER_NON_INTERACTIVE="${ATELIER_NON_INTERACTIVE:-0}"
export ATELIER_VERBOSE
ATELIER_ZOEKT_AUTO_INSTALL="${ATELIER_ZOEKT_AUTO_INSTALL:-1}"
ATELIER_INSTALL_LOG_FILE="${ATELIER_INSTALL_LOG_FILE:-}"
INSTALL_ZOEKT_LOCAL=0
STACK_STARTED=0
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
ORIGINAL_STDOUT_IS_TTY=0
if [[ -t 1 ]]; then
    ORIGINAL_STDOUT_IS_TTY=1
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all|--claude|--codex|--cursor|--opencode|--copilot|--hermes|--antigravity)
            HOST_FLAGS+=("$1")
            ;;
        --local) ATELIER_LOCAL=1 ;;
        --remote|--no-local) ATELIER_LOCAL=0 ;;
        --dry-run) ATELIER_DRY_RUN=1; PASSTHROUGH+=("$1") ;;
        --no-hosts) ATELIER_NO_HOSTS=1; PASSTHROUGH+=("$1") ;;
        --no-stack) ATELIER_NO_STACK=1; PASSTHROUGH+=("$1") ;;
        --advanced) ATELIER_ADVANCED=1 ;;
        --memory)
            if [[ $# -lt 2 ]]; then fail "--memory requires a value: letta or openmemory"; fi
            shift; ATELIER_MEMORY_BACKEND="$1" ;;
        --memory=*) ATELIER_MEMORY_BACKEND="${1#--memory=}" ;;
        --zoekt) ATELIER_ZOEKT=1; ATELIER_ADVANCED=1 ;;
        --strict) ATELIER_STRICT=1; PASSTHROUGH+=("$1") ;;
        --workspace)
            if [[ $# -lt 2 ]]; then fail "--workspace requires a directory"; fi
            HOST_SCOPE_ARGS=(--workspace "$2")
            shift
            ;;
        --workspace=*)
            HOST_SCOPE_ARGS=(--workspace "${1#--workspace=}")
            ;;
        --verbose) ATELIER_VERBOSE=1 ;;
        --non-interactive) ATELIER_NON_INTERACTIVE=1 ;;
        *) PASSTHROUGH+=("$1") ;;
    esac
    shift
done

if [[ -z "$ATELIER_INSTALL_LOG_FILE" ]]; then
    ATELIER_INSTALL_LOG_FILE="${TMPDIR:-/tmp}/atelier-install.$(date +%Y%m%dT%H%M%S).$$.log"
fi

mkdir -p "$(dirname "$ATELIER_INSTALL_LOG_FILE")" 2>/dev/null || true
: >"$ATELIER_INSTALL_LOG_FILE" 2>/dev/null || true
exec > >(tee -a "$ATELIER_INSTALL_LOG_FILE") 2>&1

trap '[[ -n "${_SPINNER_PID:-}" ]] && { kill "${_SPINNER_PID}" 2>/dev/null; printf "\n"; } || true' EXIT INT TERM

log_raw() {
    [[ "$ATELIER_VERBOSE" == "1" ]] && return 0
    [[ -n "$1" ]] && printf "%s\n" "$1" >>"$ATELIER_INSTALL_LOG_FILE" || true
}
info()    { _spinner_pause; printf "%b│%b  ◇  %s\n" "$C_FRAME" "$C_RESET" "$*"; _spinner_resume; }
verbose() { [[ "$ATELIER_VERBOSE" == "1" ]] && info "$@" || true; }
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
    if [[ "$ATELIER_STRICT" == "1" ]]; then
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
    [[ -t 1 && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]] || return 0
    local _frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    (
        local _i=0
        while true; do
            printf "\r%b%s%b  %b%s%b  %b%s%b " \
                "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "${_frames[$((_i % 10))]}" "$C_RESET" "$C_PURPLE" "$_SPINNER_MSG" "$C_RESET"
            sleep 0.08
            _i=$((_i + 1))
        done
    ) &
    _SPINNER_PID=$!
}
_spinner_pause() {
    [[ -n "${_SPINNER_PID:-}" ]] || return 0
    kill "$_SPINNER_PID" 2>/dev/null || true
    wait "$_SPINNER_PID" 2>/dev/null || true
    _SPINNER_PID=""
    printf "\r\033[2K"
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
        if [[ "$ATELIER_VERBOSE" == "1" && -n "$_out" ]]; then
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
    _out_file="$(mktemp "${TMPDIR:-/tmp}/atelier-spin-tail.XXXXXX")"

    "$@" >"$_out_file" 2>&1 &
    local _pid=$!

    if [[ -t 1 && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]]; then
        local _frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
        local _fi=0
        local _printed_lines=0
        while kill -0 "$_pid" 2>/dev/null; do
            if [[ $_printed_lines -gt 0 ]]; then
                local _j
                for ((_j = 0; _j < _printed_lines; _j++)); do
                    printf "\033[1A\033[2K"
                done
                printf "\r"
            fi

            printf "%b%s%b  %b%s%b  %b%s%b\n" \
                "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "${_frames[$((_fi % 10))]}" "$C_RESET" "$C_PURPLE" "$_msg" "$C_RESET"
            _printed_lines=1
            _fi=$((_fi + 1))

            local _tail_line
            while IFS= read -r _tail_line; do
                [[ -z "${_tail_line// }" ]] && continue
                _tail_line="$(printf "%s" "$_tail_line" | sed $'s/\x1b\\[[0-9;]*m//g')"
                if ((${#_tail_line} > 140)); then
                    _tail_line="${_tail_line:0:137}..."
                fi
                printf "%b│%b    %b%s%b\n" "$C_FRAME" "$C_RESET" "$C_PURPLE" "$_tail_line" "$C_RESET"
                _printed_lines=$((_printed_lines + 1))
            done < <(tail -n 2 "$_out_file")

            sleep 0.12
        done

        wait "$_pid" || _ret=$?

        if [[ $_printed_lines -gt 0 ]]; then
            local _j
            for ((_j = 0; _j < _printed_lines; _j++)); do
                printf "\033[1A\033[2K"
            done
            printf "\r"
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
    _out_file="$(mktemp "${TMPDIR:-/tmp}/atelier-progress.XXXXXX")"

    if [[ -t 1 && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]]; then
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
            printf "\r%b%s%b  %b%s%b  %s  %b▕%b%b%b%b%b▏%b  %b%3d%%%b" \
                "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "$_spin" "$C_RESET" "$_msg" \
                "$C_DIM" "$C_RESET" "$C_CYAN" "$_bar_fill" "$C_DIM" "$_bar_empty" "$C_RESET" \
                "$C_CYAN" "$_pct" "$C_RESET"
            sleep 0.12
        done

        wait "$_pid" || _ret=$?
        printf "\r\033[2K"
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
        if [[ "$ATELIER_VERBOSE" == "1" && -n "$_out" ]]; then
            printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_out"
        fi
    else
        [[ -n "$_out" ]] && printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_out"
    fi
    return $_ret
}

print_installer_header() {
    local script_root
    local display_version="0.1.0"
    script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [[ -f "$script_root/pyproject.toml" ]]; then
        local parsed
        parsed="$(sed -n 's/^version = "\(.*\)"/\1/p' "$script_root/pyproject.toml" | head -n 1)"
        if [[ -n "$parsed" ]]; then
            display_version="$parsed"
        fi
    fi
    echo ""
    printf "%b┌%b  Atelier v%s\n" "$C_FRAME" "$C_RESET" "$display_version"
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

print_issue_group() {
    local title="$1"
    local color="$2"
    shift 2
    local entries=("$@")
    local -A counted=()
    local -A printed=()
    local entry
    local count=0

    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" && -z "${counted[$entry]+x}" ]] || continue
        counted["$entry"]=1
        count=$((count + 1))
    done

    [[ $count -gt 0 ]] || return 0
    printf "%b│%b  %b%s (%d)%b\n" "$C_FRAME" "$C_RESET" "$color" "$title" "$count" "$C_RESET"
    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" && -z "${printed[$entry]+x}" ]] || continue
        printed["$entry"]=1
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
    [[ "$ATELIER_NON_INTERACTIVE" == "1" ]] && return 1
    if [[ "$ORIGINAL_STDOUT_IS_TTY" == "1" ]]; then
        [[ -t 0 ]] || return 1
    else
        [[ -t 0 && -t 1 ]] || return 1
    fi
    [[ -n "${TERM:-}" && "${TERM:-}" != "dumb" ]] || return 1
    return 0
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
            if [[ "$is_selected" == "1" ]]; then
                marker="${C_PURPLE}◼${C_RESET}"
                prefix="${C_PURPLE}❯${C_RESET}"
            else
                marker="${C_DIM}◻${C_RESET}"
                prefix="${C_PURPLE}❯${C_RESET}"
            fi
        else
            if [[ "$is_selected" == "1" ]]; then
                marker="${C_PURPLE}◼${C_RESET}"
            else
                marker="${C_DIM}◻${C_RESET}"
            fi
            prefix=" "
        fi
        _menu_line "  ${prefix} ${marker}  ${name}${badge}"
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
                if [[ "${SELECTED_ITEMS[$cursor]:-0}" == "1" ]]; then
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
    [[ "$ATELIER_NON_INTERACTIVE" == "1" ]] && return 0
    [[ -t 0 ]] || return 0
    [[ -n "$ATELIER_MEMORY_BACKEND" || "$ATELIER_ADVANCED" == "1" ]] && return 0

    local choice_index=0
    if supports_interactive_selector; then
        interactive_single_select \
            "Choose memory backend:" \
            choice_index \
            0 \
            "SQLite (default, local)" \
            "letta (Docker)" \
            "openmemory (Docker)"
    else
        echo ""
        printf "◇  Choose memory backend:\n"
        printf "│  0) SQLite      - local, no Docker needed (default)\n"
        printf "│  1) letta       - Letta memory server (Docker)\n"
        printf "│  2) openmemory  - OpenMemory MCP server (Docker + OpenAI key or ollama)\n"
        printf "Choice [0/1/2, default: 0]: "
        local choice
        read -r choice </dev/tty
        echo ""
        case "$choice" in
            1) choice_index=1 ;;
            2) choice_index=2 ;;
            *) choice_index=0 ;;
        esac
    fi

    case "$choice_index" in
        1) ATELIER_MEMORY_BACKEND="letta"; ATELIER_ADVANCED=1 ;;
        2) ATELIER_MEMORY_BACKEND="openmemory"; ATELIER_ADVANCED=1 ;;
        *) ATELIER_MEMORY_BACKEND="" ;;
    esac
}

prompt_local_zoekt_selection() {
    local zoekt_all_present=1
    local _z
    for _z in zoekt-git-index zoekt-index zoekt zoekt-webserver; do
        command -v "$_z" >/dev/null 2>&1 || zoekt_all_present=0
    done

    # Auto-install if missing and ATELIER_ZOEKT_AUTO_INSTALL is on (default).
    if [[ "$zoekt_all_present" == "0" && "$ATELIER_ZOEKT_AUTO_INSTALL" == "1" ]]; then
        INSTALL_ZOEKT_LOCAL=1
    else
        INSTALL_ZOEKT_LOCAL=0
    fi
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
    has_flag "--cursor" && return 0
    has_flag "--opencode" && return 0
    has_flag "--copilot" && return 0
    has_flag "--hermes" && return 0
    has_flag "--antigravity" && return 0
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
        HOST_SUMMARY+=("opencode (detected)")
        HOST_CHOICES+=("opencode|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("opencode (not found)")
        HOST_CHOICES+=("opencode|not found")
        HOST_DEFAULT_SELECTION+=(0)
    fi

    if command -v code >/dev/null 2>&1; then
        HOST_SUMMARY+=("Copilot/VS Code (detected)")
        HOST_CHOICES+=("Copilot/VS Code|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("Copilot/VS Code (not found)")
        HOST_CHOICES+=("Copilot/VS Code|not found")
        HOST_DEFAULT_SELECTION+=(0)
    fi

    if command -v antigravity >/dev/null 2>&1 || command -v agy >/dev/null 2>&1; then
        HOST_SUMMARY+=("Antigravity (detected)")
        HOST_CHOICES+=("Antigravity|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("Antigravity (not found)")
        HOST_CHOICES+=("Antigravity|not found")
        HOST_DEFAULT_SELECTION+=(0)
    fi

    if [[ -d "${HOME}/.cursor" ]]; then
        HOST_SUMMARY+=("Cursor IDE (detected)")
        HOST_CHOICES+=("Cursor IDE|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("Cursor IDE (not found — ~/.cursor/ missing)")
        HOST_CHOICES+=("Cursor IDE|not found")
        HOST_DEFAULT_SELECTION+=(0)
    fi

    if [[ -n "${HERMES_HOME:-}" ]] || [[ -n "${HERMES_SESSION_ID:-}" ]] || command -v hermes >/dev/null 2>&1; then
        HOST_SUMMARY+=("Hermes Agent (global-only, detected)")
        HOST_CHOICES+=("Hermes Agent (global-only)|detected")
        HOST_DEFAULT_SELECTION+=(1)
    else
        HOST_SUMMARY+=("Hermes Agent (global-only, not found)")
        HOST_CHOICES+=("Hermes Agent (global-only)|not found")
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
    [[ "$ATELIER_NON_INTERACTIVE" == "1" ]] && return 0
    [[ -t 0 ]] || return 0
    [[ "$ATELIER_NO_HOSTS" == "1" ]] && return 0
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
            "Which agents should Atelier configure?" \
            selected_host_indices \
            "preset" \
            "${HOST_CHOICES[@]}"
        if [[ -z "${selected_host_indices// }" ]]; then
            ATELIER_NO_HOSTS=1
        else
            local idx
            for idx in $selected_host_indices; do
                case "$idx" in
                    0) HOST_FLAGS+=(--claude) ;;
                    1) HOST_FLAGS+=(--codex) ;;
                    2) HOST_FLAGS+=(--opencode) ;;
                    3) HOST_FLAGS+=(--copilot) ;;
                    4) HOST_FLAGS+=(--antigravity) ;;
                    5) HOST_FLAGS+=(--cursor) ;;
                    6) HOST_FLAGS+=(--hermes) ;;
                esac
            done
            [[ ${#HOST_FLAGS[@]} -gt 0 ]] || ATELIER_NO_HOSTS=1
        fi
    else
        echo "◇  Which agents should Atelier configure?"
        printf "│  1) %s\n" "${HOST_SUMMARY[0]}"
        printf "│  2) %s\n" "${HOST_SUMMARY[1]}"
        printf "│  3) %s\n" "${HOST_SUMMARY[2]}"
        printf "│  4) %s\n" "${HOST_SUMMARY[3]}"
        printf "│  5) %s\n" "${HOST_SUMMARY[4]}"
        printf "│  6) %s\n" "${HOST_SUMMARY[5]}"
        printf "│  7) %s\n" "${HOST_SUMMARY[6]}"
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
                        4) HOST_FLAGS+=(--copilot) ;;
                        5) HOST_FLAGS+=(--antigravity) ;;
                        6) HOST_FLAGS+=(--cursor) ;;
                        7) HOST_FLAGS+=(--hermes) ;;
                    esac
                done
                [[ ${#HOST_FLAGS[@]} -gt 0 ]] || ATELIER_NO_HOSTS=1
                ;;
        esac
    fi

    [[ "$ATELIER_NO_HOSTS" == "1" ]] && return 0

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
        HOST_EXTRA_ARGS=(--claude-project "$(pwd)")
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
        cursor)      printf "%s" "~/.cursor" ;;
        opencode)    printf "%s" "~/.config/opencode" ;;
        copilot)     printf "%s" "~/.config/Code" ;;
        hermes)      printf "%s" "~/.hermes" ;;
        antigravity) printf "%s" "~/.config/Antigravity" ;;
        *)           printf "%s" "~/.config" ;;
    esac
}

format_host_status_label() {
    local raw_name="$1"
    local target
    target="$(host_target_for_name "$raw_name")"
    if [[ -n "$target" ]]; then
        printf "%s -> %s" "$raw_name" "$target"
    else
        printf "%s" "$raw_name"
    fi
}

ensure_local_zoekt_runtime() {    # Kept for legacy --zoekt-auto-install flag path; prefer install_local_zoekt_if_selected
    local atelier_cli="$1"
    local missing=()
    local name
    for name in zoekt-git-index zoekt-index zoekt zoekt-webserver; do
        if ! command -v "$name" >/dev/null 2>&1; then
            missing+=("$name")
        fi
    done
    [[ ${#missing[@]} -eq 0 ]] && return
    warn "Local Zoekt binaries missing — run: atelier zoekt install"
}

# Install Go to ~/.local/go via official tarball (self-contained, no sudo)
_install_go() {
    local go_ver arch os_low tarball
    go_ver="$(curl -sSL 'https://go.dev/VERSION?m=text' 2>/dev/null | head -1)" || return 1
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

install_local_zoekt_if_selected() {
    [[ "$INSTALL_ZOEKT_LOCAL" != "1" ]] && return 0
    local atelier_cli="$1"
    local go_user_bin="${HOME}/.local/go/bin"
    local go_path_bin=""

    # Check/install Go first
    if ! command -v go >/dev/null 2>&1; then
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] install go"
        else
            spin "Installing Go" _install_go || {
                # Tarball may have set PATH in subshell; try the known path
                if [[ -x "${go_user_bin}/go" ]]; then
                    export PATH="${go_user_bin}:${PATH}"
                else
                    warn "Go install failed — skipping Zoekt binary install"
                    return 0
                fi
            }
        fi
    fi

    # spin() runs in a subshell, so always re-apply user-local Go path in parent shell.
    if [[ -x "${go_user_bin}/go" && ":$PATH:" != *":${go_user_bin}:"* ]]; then
        export PATH="${go_user_bin}:${PATH}"
    fi
    if ! command -v go >/dev/null 2>&1; then
        warn "Go is still not on PATH — skipping Zoekt binary install"
        return 0
    fi
    go_path_bin="$(go env GOPATH 2>/dev/null)/bin"
    if [[ -n "$go_path_bin" && ":$PATH:" != *":${go_path_bin}:"* ]]; then
        export PATH="${go_path_bin}:${PATH}"
    fi

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $atelier_cli zoekt install --auto"
    else
        spin "Installing Zoekt" "$atelier_cli" zoekt install --auto \
            || warn "Zoekt install failed. Run: atelier zoekt install"
    fi
}

run() {
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

install_uv_if_needed() {
    if command -v uv >/dev/null 2>&1; then
        verbose "Found uv: $(uv --version 2>/dev/null || echo unknown)"
        return
    fi

    need_cmd curl
    verbose "Installing uv (official installer)..."
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] curl -LsSf https://astral.sh/uv/install.sh | sh"
    else
        # shellcheck disable=SC2016
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi

    if [[ -x "${HOME}/.local/bin/uv" ]]; then
        export PATH="${HOME}/.local/bin:${PATH}"
    fi

    command -v uv >/dev/null 2>&1 || fail "uv install completed but uv is still not on PATH"
    verbose "Installed uv: $(uv --version 2>/dev/null || echo unknown)"
}

prepare_repo() {
    local dir
    dir="$(dirname "$ATELIER_INSTALL_DIR")"
    run mkdir -p "$dir"

    if [[ -d "$ATELIER_INSTALL_DIR/.git" ]]; then
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR fetch -q --tags --prune origin"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR checkout -f -q $ATELIER_REF"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR reset --hard -q origin/$ATELIER_REF"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR clean -fd >/dev/null"
        else
            git -C "$ATELIER_INSTALL_DIR" fetch -q --tags --prune origin
            git -C "$ATELIER_INSTALL_DIR" checkout -f -q "$ATELIER_REF"
            if git -C "$ATELIER_INSTALL_DIR" rev-parse --verify "origin/$ATELIER_REF" >/dev/null 2>&1; then
                git -C "$ATELIER_INSTALL_DIR" reset --hard -q "origin/$ATELIER_REF"
            else
                git -C "$ATELIER_INSTALL_DIR" reset --hard -q "$ATELIER_REF"
            fi
            git -C "$ATELIER_INSTALL_DIR" clean -fd >/dev/null
        fi
    else
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] git clone -q --depth=1 --branch $ATELIER_REF $ATELIER_REPO_URL $ATELIER_INSTALL_DIR"
        else
            git clone -q --depth=1 --branch "$ATELIER_REF" "$ATELIER_REPO_URL" "$ATELIER_INSTALL_DIR"
        fi
    fi
}

install_console_scripts() {
    local extras="mcp,memory,smart,cloud,repo-map,api,postgres,vector,parsers,rename,telemetry"
    local package_spec="${ATELIER_INSTALL_DIR}[${extras}]"
    local install_args=(tool install --force)

    if [[ "$ATELIER_LOCAL" == "1" ]]; then
        install_args+=(--editable)
    fi
    install_args+=("$package_spec")

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        printf '[dry-run] UV_TOOL_BIN_DIR=%q UV_TOOL_DIR=%q uv' "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
        printf ' %q' "${install_args[@]}"
        printf '\n'
        return
    fi

    mkdir -p "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
    UV_TOOL_BIN_DIR="$ATELIER_BIN_DIR" \
        UV_TOOL_DIR="$ATELIER_TOOL_DIR" \
        uv "${install_args[@]}"

    local mcp_path="$ATELIER_BIN_DIR/atelier-mcp"
    local wrapped_path="$ATELIER_BIN_DIR/atelier-mcp.real"
    if [[ -f "$mcp_path" || -L "$mcp_path" ]]; then
        rm -f "$wrapped_path"
        mv "$mcp_path" "$wrapped_path"
        cat >"$mcp_path" <<EOF
#!/usr/bin/env bash
export ATELIER_DEV_MODE="\${ATELIER_DEV_MODE:-0}"
exec "$wrapped_path" "\$@"
EOF
        chmod +x "$mcp_path"
    fi
}

persist_install_record() {
    local record_dir
    record_dir="$(dirname "$ATELIER_INSTALL_RECORD")"

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] mkdir -p $record_dir"
        echo "[dry-run] printf '%s\\n' '$ATELIER_INSTALL_DIR' > '$ATELIER_INSTALL_RECORD'"
        return
    fi

    mkdir -p "$record_dir"
    printf '%s\n' "$ATELIER_INSTALL_DIR" > "$ATELIER_INSTALL_RECORD"
}

install_code_tools() {
    # Install optional code-quality tools used by the post-edit hook pipeline and
    # the rename backend.  All steps are best-effort: missing tools are warned about
    # but do not abort the install.

    local os_type
    os_type="$(uname -s)"


    # prettier + eslint + ts-morph (TypeScript/JavaScript tools, require npm)
    if command -v npm >/dev/null 2>&1; then
        verbose "Installing prettier (JS/TS formatter)..."
        spin "Installing prettier" npm install -g --no-fund prettier
        verbose "Installing eslint, ts-morph, and typescript (JS/TS linter and rename backend)..."
        spin "Installing eslint + ts-morph" npm install -g --no-fund eslint ts-morph typescript
    else
        warn "npm not found — skipping prettier, eslint, and ts-morph (install Node.js 20+ to enable)"
    fi

    # rustfmt + cargo (Rust formatter and lint-fix backend, via rustup)
    if ! command -v cargo >/dev/null 2>&1; then
        verbose "cargo not found — installing Rust toolchain via rustup..."
        if [[ "$os_type" == "Darwin" ]]; then
            if command -v brew >/dev/null 2>&1; then
                run brew install rustup
                if [[ "$ATELIER_DRY_RUN" != "1" ]]; then
                    rustup-init -y --no-modify-path 2>/dev/null || true
                fi
            else
                warn "Homebrew not found — skipping Rust install on macOS (install from https://rustup.rs)"
            fi
        else
            # Linux
            if command -v curl >/dev/null 2>&1; then
                if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                    echo "[dry-run] curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
                else
                    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path 2>/dev/null \
                        || warn "rustup install failed — Rust post-edit hooks will be skipped"
                fi
            else
                warn "curl not found — skipping Rust toolchain install"
            fi
        fi
    else
        verbose "Found cargo: $(cargo --version 2>/dev/null || echo unknown)"
    fi

}

main() {
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) fail "Unsupported OS: $(uname -s). This installer supports Linux/macOS." ;;
    esac

    need_cmd git
    need_cmd bash

    print_installer_header
    host_wizard
    prompt_memory_selection
    prompt_local_zoekt_selection

    if supports_interactive_selector; then
        print_installer_footer
    fi

    case "$ATELIER_MEMORY_BACKEND" in
        letta|openmemory|"") ;;
        *) fail "--memory must be 'letta' or 'openmemory', got: '$ATELIER_MEMORY_BACKEND'" ;;
    esac
    if [[ -n "$ATELIER_MEMORY_BACKEND" ]]; then
        ATELIER_ADVANCED=1
    fi

    install_uv_if_needed

    local stack_available=0
    if [[ "$ATELIER_NO_STACK" != "1" ]] && command -v npm >/dev/null 2>&1; then
        stack_available=1
    elif [[ "$ATELIER_NO_STACK" != "1" ]]; then
        warn "npm is required to run the optional visualization stack; skipping stack setup"
    fi

    local stack_expected=0
    if [[ "$ATELIER_NO_SERVICECTL" != "1" && "$stack_available" == "1" ]] && { command -v systemctl >/dev/null 2>&1 || [[ "$(uname -s)" == "Darwin" ]]; }; then
        stack_expected=1
    fi

    if [[ "$ATELIER_LOCAL" == "1" ]]; then
        verbose "Local mode: using current directory as an editable install source"
        ATELIER_INSTALL_DIR="$(pwd)"
    else
        spin "Preparing repository" prepare_repo
    fi
    export ATELIER_INSTALL_DIR

    step_start "Installing Atelier"
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        install_console_scripts
    else
        spin_tail "Installing packages" install_console_scripts
    fi
    persist_install_record
    step_done

    step_start "Installing code tools"
    install_code_tools
    step_done

    local selected_memory=""
    if [[ "$ATELIER_ADVANCED" == "1" ]]; then
        if [[ -z "$ATELIER_MEMORY_BACKEND" ]]; then
            warn "--advanced set but no --memory selected; no memory sidecar will be installed"
        elif [[ "$ATELIER_MEMORY_BACKEND" == "letta" ]]; then
            if command -v docker >/dev/null 2>&1; then
                selected_memory="letta"
                verbose "Memory sidecar: Letta (Docker)"
            else
                warn "--memory letta requires Docker - skipping Letta sidecar"
            fi
        elif [[ "$ATELIER_MEMORY_BACKEND" == "openmemory" ]]; then
            local _om_missing=()
            command -v docker >/dev/null 2>&1 || _om_missing+=("docker")
            command -v git >/dev/null 2>&1 || _om_missing+=("git")
            command -v make >/dev/null 2>&1 || _om_missing+=("make")
            local _has_llm=0
            [[ -n "${ATELIER_OPENMEMORY_OPENAI_API_KEY:-}${OPENAI_API_KEY:-}" ]] && _has_llm=1
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
    if [[ "$ATELIER_ZOEKT" == "1" ]]; then
        if command -v docker >/dev/null 2>&1; then
            selected_zoekt="1"
            verbose "Zoekt sidecar: enabled by default (Docker)"
        else
            warn "Docker not found — skipping Zoekt sidecar service setup"
        fi
    fi

    local memory_record="${HOME}/.atelier/memory_backend"
    if [[ -n "$selected_memory" ]]; then
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] printf '%s\\n' '$selected_memory' > '$memory_record'"
        else
            mkdir -p "${HOME}/.atelier"
            printf '%s\n' "$selected_memory" > "$memory_record"
        fi
    elif [[ -f "$memory_record" && "$ATELIER_DRY_RUN" != "1" ]]; then
        : >"$memory_record"
    fi

    local zoekt_record="${HOME}/.atelier/zoekt_enabled"
    if [[ "$selected_zoekt" == "1" ]]; then
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] printf '1\\n' > '$zoekt_record'"
        else
            mkdir -p "${HOME}/.atelier"
            printf '1\n' > "$zoekt_record"
        fi
    elif [[ -f "$zoekt_record" && "$ATELIER_DRY_RUN" != "1" ]]; then
        : >"$zoekt_record"
    fi

    if [[ ":$PATH:" != *":$ATELIER_BIN_DIR:"* ]]; then
        warn "$ATELIER_BIN_DIR is not currently on PATH"
        info "Add this to your shell profile, then restart your shell:"
        info "  export PATH=\"$ATELIER_BIN_DIR:\$PATH\""
    fi

    local atelier_cli="$ATELIER_BIN_DIR/atelier"

    if [[ "$INSTALL_ZOEKT_LOCAL" == "1" ]]; then
        step_start "Installing Zoekt"
        install_local_zoekt_if_selected "$atelier_cli"
        step_done
    fi

    if [[ "$ATELIER_NO_HOSTS" != "1" ]]; then
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
        fi
        if [[ ${#HOST_SCOPE_ARGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_SCOPE_ARGS[@]}")
        fi
        if [[ ${#HOST_EXTRA_ARGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_EXTRA_ARGS[@]}")
        fi
        local project_workspace=""
        if [[ "${ATELIER_LOCAL}" == "1" ]]; then
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
            if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                echo "[dry-run] bash $ATELIER_INSTALL_DIR/scripts/install_agents.sh ${agents_install_args[*]}"
            else
                if [[ "$ATELIER_VERBOSE" == "1" ]]; then
                    bash "$ATELIER_INSTALL_DIR/scripts/install_agents.sh" "${agents_install_args[@]}"
                else
                    bash "$ATELIER_INSTALL_DIR/scripts/install_agents.sh" "${agents_install_args[@]}" >>"$ATELIER_INSTALL_LOG_FILE" 2>&1
                fi
            fi
        fi
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] bash $ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh ${host_install_args[*]}"
        else
            local host_output host_output_file host_ret
            host_output_file="${TMPDIR:-/tmp}/atelier-hosts.$(date +%Y%m%dT%H%M%S).$$.log"
            : >"$host_output_file" 2>/dev/null || host_output_file="$(mktemp "${TMPDIR:-/tmp}/atelier-hosts.XXXXXX")"
            set +e
            if [[ "$ATELIER_VERBOSE" == "1" ]]; then
                if [[ -n "$C_RESET" ]]; then
                    FORCE_COLOR=1 bash "$ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh" "${host_install_args[@]}" 2>&1 | tee "$host_output_file"
                else
                    bash "$ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh" "${host_install_args[@]}" 2>&1 | tee "$host_output_file"
                fi
                host_ret=${PIPESTATUS[0]}
            else
                local had_lastpipe=0
                if shopt -q lastpipe; then
                    had_lastpipe=1
                else
                    shopt -s lastpipe
                fi
                _SPINNER_MSG="Installing host integrations"                _SPINNER_ACTIVE=1
                _spinner_run
                ATELIER_HOST_STATUS_STREAM=1 bash "$ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh" "${host_install_args[@]}" 2>&1 | while IFS= read -r line; do
                    printf "%s\n" "$line" >>"$host_output_file"
                    if [[ "$line" =~ ^@@ATELIER_HOST_STATUS@@[[:space:]]+([A-Z]+)[[:space:]]+(.+)$ ]]; then
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
                    shopt -u lastpipe
                fi
                _SPINNER_MSG="Installing host integrations"
                _spinner_pause
                _SPINNER_ACTIVE=0
            fi
            set -e
            host_output="$(cat "$host_output_file")"
            collect_issues_from_output "$host_output"
            if [[ -f "$host_output_file" ]] && [[ ${#WARNINGS[@]} -gt 0 || ${#ERRORS[@]} -gt 0 ]]; then
                info "Host integration log: $host_output_file"
            fi
            if [[ $host_ret -ne 0 ]]; then                ERRORS+=("One or more host integrations failed")
                FINAL_EXIT_CODE=1
            fi
        fi
        # Persist host detection results for the local service/UI surfaces
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write >>"$ATELIER_INSTALL_LOG_FILE" 2>&1 \
                || degrade "Failed to persist host detection status"
        fi
        step_done
    else
        step_start "Installing host integrations"
        info "Skipped (ATELIER_NO_HOSTS=1)"
        # Still persist current detection state even when skipping install
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write >>"$ATELIER_INSTALL_LOG_FILE" 2>&1 \
                || degrade "Failed to persist host detection status"
        fi
        step_done
    fi

    local index_target=""
    local repo_root=""
    local index_skipped=0
    if repo_root="$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null)"; then
        index_target="$repo_root"
    fi

    step_start "Initializing"
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $atelier_cli init"
        if [[ -n "$index_target" ]]; then
            info "Detected project repo: $index_target"
            echo "[dry-run] $atelier_cli code index --repo-root $index_target"
        else
            info "Detected project root: not found (no git repository in current directory)"
            echo "[dry-run] skip code index (run inside a git repo)"
        fi
    else
        spin "Initializing runtime store" "$atelier_cli" init
        if [[ -n "$index_target" ]]; then
            info "Detected project root: $index_target"
            if ! spin_progress "Bootstrapping code index" "$atelier_cli" code index --repo-root "$index_target"; then
                degrade "Initial code indexing failed; Atelier will continue and autosync will retry."
            fi
        else
            index_skipped=1
            info "Index target: not detected (no git repository in current directory)"
            info "Skipped code indexing (no git repository detected)."
        fi
    fi
    step_done
    if [[ "$ATELIER_NO_SERVICECTL" != "1" ]]; then
        if command -v systemctl >/dev/null 2>&1 || [[ "$(uname -s)" == "Darwin" ]]; then
            verbose "Registering Atelier services with background manager..."
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

            if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $ATELIER_BIN_DIR/atelier background install ${background_args[*]}"
            else
                "$ATELIER_BIN_DIR/atelier" background install "${background_args[@]}" >>"$ATELIER_INSTALL_LOG_FILE" 2>&1
            fi
        else
            verbose "Starting Atelier background service controller (loose process)..."
            if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $ATELIER_BIN_DIR/atelier servicectl start --interval-seconds $ATELIER_SERVICECTL_INTERVAL_SECONDS --maintenance-interval-seconds $ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS"
            else
                "$ATELIER_BIN_DIR/atelier" servicectl start \
                    --interval-seconds "$ATELIER_SERVICECTL_INTERVAL_SECONDS" \
                    --maintenance-interval-seconds "$ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS" >>"$ATELIER_INSTALL_LOG_FILE" 2>&1
            fi

            if [[ "$stack_available" == "1" ]]; then
                verbose "Starting Atelier visualization stack (service + frontend)..."
                if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                    echo "[dry-run] $ATELIER_BIN_DIR/atelier stack start"
                else
                    "$ATELIER_BIN_DIR/atelier" stack start \
                        && STACK_STARTED=1 \
                        || degrade "Visualization stack did not start cleanly"
                fi
            fi
        fi
    else
        verbose "Skipping background services because ATELIER_NO_SERVICECTL=1"
    fi

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
    local code_display="$ATELIER_INSTALL_DIR"
    code_display="${code_display/#$HOME/~}"
    printf "%b📁 Your files:%b\n\n" "$C_PURPLE" "$C_RESET"
    printf "   Atelier dir:   %s\n" "~/.atelier"
    printf "   Code:          %s\n\n" "$code_display"    
    printf "%b─────────────────────────────────────────────────────────%b\n\n" "$C_PURPLE" "$C_RESET"
    printf "%b🚀 Commands:%b\n\n" "$C_PURPLE" "$C_RESET"
    printf "   %batelier%b status              View active reasoning run\n" "$C_PURPLE" "$C_RESET"
    printf "   %batelier%b import              Import past agent sessions\n" "$C_PURPLE" "$C_RESET"
    printf "   %batelier%b memory recall       Search memory\n" "$C_PURPLE" "$C_RESET"
    printf "   %batelier%b code index          Index current repository\n" "$C_PURPLE" "$C_RESET"
    printf "   %batelier%b stack status        Check frontend/service status\n\n" "$C_PURPLE" "$C_RESET"
    if [[ ${#WARNINGS[@]} -gt 0 || ${#ERRORS[@]} -gt 0 ]]; then
        printf "   installer log: %s\n\n" "$ATELIER_INSTALL_LOG_FILE"
    fi
    printf "%b─────────────────────────────────────────────────────────%b\n\n" "$C_PURPLE" "$C_RESET"
    if [[ ":$PATH:" != *":$ATELIER_BIN_DIR:"* ]]; then
        printf "⚡ Reload your shell to use 'atelier' command.\n"
    fi

    return "$FINAL_EXIT_CODE"
}

main "$@"
