#!/usr/bin/env bash
# install_hosts.sh — Install LemonCrow into all available agent CLIs
#
# When run without flags in an interactive terminal, prompts the user to
# select which AI coding agents to install and whether to install globally
# (user config) or project-locally (.mcp.json + AGENTS.md + per-host config).
#
# When run without flags in a non-interactive terminal (CI/scripted), or
# with flags, behaves as before: installs to all detected CLIs globally.
#
# Options:
#   --all          Force attempt all hosts (same as default in non-TTY)
#   --claude       Only install Claude Code
#   --codex        Only install Codex
#   --opencode     Only install opencode
#   --copilot      Only install Copilot
#   --antigravity  Only install Antigravity / agy
#   --cursor       Only install Cursor
#   --hermes       Only install Hermes Agent
#   --dry-run      Pass through to all install scripts
#   --print-only   Pass through to all install scripts
#   --strict       Pass through; scripts exit nonzero if CLI absent
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --roles LIST           Comma-separated agent role ids (claude/codex/opencode only)
#   --include-skills LIST  Comma-separated public skill names (claude/codex only; opencode has no skills concept)

set -euo pipefail

if [[ -t 1 ]]; then
    C_RESET="$(printf '\033[0m')"
    C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[38;2;155;117;217m')"
    C_PURPLE="$(printf '\033[38;2;155;117;217m')"
else
    C_RESET=""
    C_DIM=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
    C_CYAN=""
    C_PURPLE=""
fi
if [[ -n "${FORCE_COLOR:-}${CLICOLOR_FORCE:-}" && -z "${NO_COLOR:-}" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[38;2;155;117;217m')"
    C_PURPLE="$(printf '\033[38;2;155;117;217m')"
fi
# Frame/gutter color, mirrors common.sh. Defined after all color blocks so it
# tracks C_DIM in every mode (TTY, no-TTY, FORCE_COLOR). Must exist before any
# run_installer failure path references it, or `set -u` aborts the whole step.
C_FRAME="$C_DIM"
ACTIVE_BAR="┃"
if [[ "${LC_ALL:-${LANG:-}}" != *"UTF-8"* && "${LC_ALL:-${LANG:-}}" != *"utf8"* ]]; then
    ACTIVE_BAR="|"
fi

LEMONCROW_VERBOSE="${LEMONCROW_VERBOSE:-0}"
LEMONCROW_HOST_INSTALL_TIMEOUT_SECONDS="${LEMONCROW_HOST_INSTALL_TIMEOUT_SECONDS:-180}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_BUILDER="${SCRIPT_DIR}/build_host_skills.sh"
print_message() {
    local color="$1"
    shift
    printf "%b%s%b\n" "$color" "$*" "$C_RESET"
}

verbose() { [[ "$LEMONCROW_VERBOSE" == "1" ]] && printf "%s\n" "$*" || true; }

has_interactive_input() {
    [[ -t 0 ]] || { [[ -e /dev/tty ]] && : </dev/tty; } 2>/dev/null
}

has_passthrough() {
    local needle="$1"
    local item
    for item in "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"; do
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

_in_array() {
    local needle="$1"
    shift
    local item
    for item in "$@"; do
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

host_is_detected() {
    local host="$1"
    case "$host" in
        claude) command -v claude >/dev/null 2>&1 ;;
        codex) command -v codex >/dev/null 2>&1 ;;
        opencode) command -v opencode >/dev/null 2>&1 ;;
        copilot) command -v code >/dev/null 2>&1 ;;
        antigravity) command -v antigravity >/dev/null 2>&1 || command -v agy >/dev/null 2>&1 ;;
        cursor) command -v cursor >/dev/null 2>&1 || [ -d "${HOME}/.cursor" ] ;;
        hermes) command -v hermes >/dev/null 2>&1 || [ -f "${HERMES_HOME:-${HOME}/.hermes}/config.yaml" ] ;;
        *) return 1 ;;
    esac
}

enable_detected_hosts_by_default() {
    host_is_detected claude && DO_CLAUDE=true
    host_is_detected codex && DO_CODEX=true
    host_is_detected opencode && DO_OPENCODE=true
    host_is_detected copilot && DO_COPILOT=true
    host_is_detected antigravity && DO_ANTIGRAVITY=true
    host_is_detected cursor && DO_CURSOR=true
    host_is_detected hermes && DO_HERMES=true
    return 0
}

run_host_installer() {
    local script="$1"
    shift

    if [[ ! "${LEMONCROW_HOST_INSTALL_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] || [[ "${LEMONCROW_HOST_INSTALL_TIMEOUT_SECONDS}" -le 0 ]]; then
        bash "$script" "$@"
        return
    fi

    if ! command -v perl >/dev/null 2>&1; then
        bash "$script" "$@"
        return
    fi

    perl -e '
        use strict;
        use warnings;

        my $timeout = shift @ARGV;
        my @cmd = @ARGV;
        my $pid = fork();
        die "fork failed: $!" unless defined $pid;

        if ($pid == 0) {
            exec @cmd or die "exec failed: $!";
        }

        local $SIG{ALRM} = sub {
            print STDERR "[lemon:install] ERROR: host installer timed out after ${timeout}s\n";
            kill "TERM", $pid;
            sleep 2;
            kill "KILL", $pid;
            waitpid($pid, 0);
            exit 124;
        };

        alarm($timeout);
        waitpid($pid, 0);
        my $status = $?;
        alarm(0);

        if ($status & 127) {
            exit(128 + ($status & 127));
        }
        exit($status >> 8);
    ' "${LEMONCROW_HOST_INSTALL_TIMEOUT_SECONDS}" bash "$script" "$@"
}

print_active_line() {
    printf "%b%s%b  %b%s%b\n" "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "$1" "$C_RESET"
}

print_frame_line() {
    printf "%b│%b  %s\n" "$C_DIM" "$C_RESET" "$1"
}

_SPINNER_PID=""
LEMONCROW_SPINNER_PID_FILE="${TMPDIR:-/tmp}/lemoncrow-spinner-agent.$$.pid"
touch "$LEMONCROW_SPINNER_PID_FILE"
trap '[[ -f "$LEMONCROW_SPINNER_PID_FILE" ]] && { _SPINNER_PID=$(cat "$LEMONCROW_SPINNER_PID_FILE" 2>/dev/null); [[ -n "$_SPINNER_PID" ]] && kill "$_SPINNER_PID" 2>/dev/null; rm -f "$LEMONCROW_SPINNER_PID_FILE"; } || true' EXIT INT TERM

spinner_start() {
    local msg="$1"
    [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]] || return 0
    [[ -t 1 && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]] || return 0
    [[ "${LEMONCROW_VERBOSE:-0}" != "1" ]] || return 0
    local _frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    (
        local _i=0
        while true; do
            printf "\r%b%s%b  %b%s%b  %s " \
                "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "${_frames[$((_i % 10))]}" "$C_RESET" "$msg"
            sleep 0.08
            _i=$((_i + 1))
        done
    ) &
    _SPINNER_PID=$!
    echo "$_SPINNER_PID" > "$LEMONCROW_SPINNER_PID_FILE"
}

spinner_finish() {
    local state="$1"
    local msg="$2"
    _SPINNER_PID=$(cat "$LEMONCROW_SPINNER_PID_FILE" 2>/dev/null)
    [[ -n "${_SPINNER_PID:-}" ]] || return 0
    kill "$_SPINNER_PID" 2>/dev/null || true
    wait "$_SPINNER_PID" 2>/dev/null || true
    _SPINNER_PID=""
    echo "" > "$LEMONCROW_SPINNER_PID_FILE"
    printf "\r\033[2K"
    case "$state" in
        ok)   printf "%b│%b  %b✓%b  %s\n" "$C_DIM" "$C_RESET" "$C_GREEN" "$C_RESET" "$msg" ;;
        warn) printf "%b│%b  %b⚠%b  %s\n" "$C_DIM" "$C_RESET" "$C_YELLOW" "$C_RESET" "$msg" ;;
        skip) printf "%b│%b  %b—%b  %s\n" "$C_DIM" "$C_RESET" "$C_DIM" "$C_RESET" "$msg" ;;
        err)  printf "%b│%b  %b✗%b  %s\n" "$C_DIM" "$C_RESET" "$C_RED" "$C_RESET" "$msg" ;;
    esac
}

DO_CLAUDE=false
DO_CODEX=false
DO_OPENCODE=false
DO_COPILOT=false
DO_ANTIGRAVITY=false
DO_CURSOR=false
DO_HERMES=false
EXPLICIT=false
PASSTHROUGH=()
CLAUDE_EXTRA_ARGS=()
CODEX_EXTRA_ARGS=()
OPENCODE_EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)       EXPLICIT=true; DO_CLAUDE=true; DO_CODEX=true; DO_OPENCODE=true; DO_COPILOT=true; DO_ANTIGRAVITY=true; DO_CURSOR=true; DO_HERMES=true ;;
        --claude)    EXPLICIT=true; DO_CLAUDE=true ;;
        --codex)     EXPLICIT=true; DO_CODEX=true ;;
        --opencode)  EXPLICIT=true; DO_OPENCODE=true ;;
        --copilot)   EXPLICIT=true; DO_COPILOT=true ;;
        --antigravity) EXPLICIT=true; DO_ANTIGRAVITY=true ;;
        --cursor)    EXPLICIT=true; DO_CURSOR=true ;;
        --hermes)    EXPLICIT=true; DO_HERMES=true ;;
        --dry-run|--print-only|--strict) PASSTHROUGH+=("$1") ;;
        --workspace)
            if [ $# -lt 2 ]; then
                print_message "$C_RED" "Missing value for --workspace" >&2
                exit 1
            fi
            PASSTHROUGH+=("$1" "$2")
            shift
            ;;
        --claude-project)
            if [ $# -ge 2 ] && [[ "$2" != --* ]]; then
                CLAUDE_EXTRA_ARGS+=("--project" "$2")
                shift
            else
                CLAUDE_EXTRA_ARGS+=("--project")
            fi
            ;;
        --roles)
            if [ $# -lt 2 ]; then
                print_message "$C_RED" "Missing value for --roles" >&2
                exit 1
            fi
            # Forwarded only to hosts whose install script understands
            # --roles (claude/codex/opencode); copilot/antigravity never see it.
            CLAUDE_EXTRA_ARGS+=("$1" "$2")
            CODEX_EXTRA_ARGS+=("$1" "$2")
            OPENCODE_EXTRA_ARGS+=("$1" "$2")
            shift
            ;;
        --include-skills)
            if [ $# -lt 2 ]; then
                print_message "$C_RED" "Missing value for --include-skills" >&2
                exit 1
            fi
            # claude/codex only -- opencode has no skills concept and its
            # install script does not accept this flag.
            CLAUDE_EXTRA_ARGS+=("$1" "$2")
            CODEX_EXTRA_ARGS+=("$1" "$2")
            shift
            ;;
        *) print_message "$C_RED" "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# ── Interactive prompts (when no flags and a terminal is available) ──────────
if ! $EXPLICIT && has_interactive_input && [[ -t 1 ]]; then
    echo ""
    print_message "$C_PURPLE" "══════════════════════════════════════════════"
    print_message "$C_PURPLE" " LemonCrow — Agent Installation"
    print_message "$C_PURPLE" "══════════════════════════════════════════════"
    echo ""

    # ── Runtime selection ──────────────────────────────────────────────────
    echo "  Which AI coding agents would you like to install LemonCrow for?"
    echo ""
    echo "  ${C_PURPLE}1${C_RESET}) Claude Code"
    echo "  ${C_PURPLE}2${C_RESET}) OpenCode"
    echo "  ${C_PURPLE}3${C_RESET}) Codex CLI"
    echo "  ${C_PURPLE}4${C_RESET}) Copilot"
    echo "  ${C_PURPLE}5${C_RESET}) Antigravity"
    echo "  ${C_PURPLE}6${C_RESET}) Cursor"
    echo "  ${C_PURPLE}7${C_RESET}) Hermes"
    echo "  ${C_PURPLE}a${C_RESET}) All"
    echo "  ${C_PURPLE}n${C_RESET}) None (skip agent installs)"
    echo ""

    read -r -p "  Choice [a]: " runtime_answer </dev/tty || runtime_answer="a"
    runtime_answer="${runtime_answer:-a}"

    # Reset all to false — user picks explicitly
    DO_CLAUDE=false; DO_CODEX=false; DO_OPENCODE=false; DO_COPILOT=false; DO_ANTIGRAVITY=false; DO_CURSOR=false; DO_HERMES=false
    case "$runtime_answer" in
        a|A|all|ALL)
            DO_CLAUDE=true; DO_CODEX=true; DO_OPENCODE=true; DO_COPILOT=true; DO_ANTIGRAVITY=true; DO_CURSOR=true; DO_HERMES=true
            echo "  → All agents"
            ;;
        n|N|none|NONE|skip|SKIP|0)
            echo "  → Skipping agent installs"
            ;;
        *)
            IFS=',' read -ra choices <<< "$runtime_answer"
            for choice in "${choices[@]}"; do
                choice="$(echo "$choice" | xargs)"
                case "$choice" in
                    1) DO_CLAUDE=true ;;
                    2) DO_OPENCODE=true ;;
                    3) DO_CODEX=true ;;
                    4) DO_COPILOT=true ;;
                    5) DO_ANTIGRAVITY=true ;;
                    6) DO_CURSOR=true ;;
                    7) DO_HERMES=true ;;
                    *) echo "  ${C_YELLOW}Unknown choice: $choice${C_RESET}" ;;
                esac
            done
            selected=""
            $DO_CLAUDE    && selected="$selected claude"
            $DO_OPENCODE  && selected="$selected opencode"
            $DO_CODEX     && selected="$selected codex"
            $DO_COPILOT   && selected="$selected copilot"
            $DO_ANTIGRAVITY && selected="$selected antigravity"
            $DO_CURSOR    && selected="$selected cursor"
            $DO_HERMES    && selected="$selected hermes"
            echo "  → Selected:${selected:- none}"
            ;;
    esac

    echo ""

    # ── Scope selection ────────────────────────────────────────────────────
    # Only prompt for scope if at least one runtime was selected
    if $DO_CLAUDE || $DO_CODEX || $DO_OPENCODE || $DO_COPILOT || $DO_ANTIGRAVITY || $DO_CURSOR || $DO_HERMES; then
        echo "  ${C_YELLOW}Install scope:${C_RESET}"
        echo ""
        echo "  ${C_PURPLE}1${C_RESET}) Global — available in all projects"
        echo "  ${C_PURPLE}2${C_RESET}) Project — this directory only (via AGENTS.md)"
        echo ""
        read -r -p "  Choice [1]: " scope_answer </dev/tty || scope_answer="1"
        scope_answer="${scope_answer:-1}"

        if [ "$scope_answer" = "2" ]; then
            if ! has_passthrough "--workspace"; then
                PASSTHROUGH+=("--workspace" ".")
            fi
            echo "  → Project-local install"
        else
            echo "  → Global install"
        fi
        echo ""
    fi

    EXPLICIT=true  # prevent default fallback below
fi

# Default: all hosts
if ! $EXPLICIT; then
    enable_detected_hosts_by_default
fi

PASS=()
WARN=()
FAIL=()
SKIP=()
WARNINGS=()
ERRORS=()
collect_issues_from_output() {
    local output="$1"
    local line
    while IFS= read -r line; do
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

print_colored_output() {
    local output="$1"
    local line
    while IFS= read -r line; do
        print_colored_line "$line"
    done <<<"$output"
}

print_colored_line() {
    local line="$1"
    case "$line" in
        *"] PASS:"*)
            printf "%s\n" "${line/PASS:/${C_GREEN}PASS:${C_RESET}}"
            ;;
        *"] WARN:"*)
            printf "%s\n" "${line/WARN:/${C_YELLOW}WARN:${C_RESET}}"
            ;;
        *"] FAIL:"*)
            printf "%s\n" "${line/FAIL:/${C_RED}FAIL:${C_RESET}}"
            ;;
        *"] ERROR:"*)
            printf "%s\n" "${line/ERROR:/${C_RED}ERROR:${C_RESET}}"
            ;;
        "=== SKIPPED"*)
            print_message "$C_YELLOW" "$line"
            ;;
        *)
            printf "%s\n" "$line"
            ;;
    esac
}

stream_colored_output() {
    local output_file="$1"
    local line
    while IFS= read -r line; do
        printf "%s\n" "$line" >>"$output_file"
        if [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]]; then
            print_colored_line "$line"
        fi
    done
}

emit_host_status() {
    [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" == "1" ]] || return 0
    printf "@@LEMONCROW_HOST_STATUS@@ %s %s\n" "$1" "$2"
}

print_issue_group() {
    local title="$1"
    local color="$2"
    shift 2
    local entries=("$@")
    local unique_entries=()
    local entry
    local count=0

    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" ]] || continue
        if _in_array "$entry" "${unique_entries[@]+"${unique_entries[@]}"}"; then
            continue
        fi
        unique_entries+=("$entry")
        count=$((count + 1))
    done

    [[ $count -gt 0 ]] || return 0
    printf "%b%s (%d)%b\n" "$color" "$title" "$count" "$C_RESET"
    for entry in "${unique_entries[@]+"${unique_entries[@]}"}"; do
        printf "  %b-%b %s\n" "$color" "$C_RESET" "$entry"
    done
}

run_installer() {
    local host="$1"
    local script
    local output_file output ret
    local spinner_started=0

    script="${SCRIPT_DIR}/install_${host}.sh"

    echo ""
    emit_host_status "START" "$host"
    spinner_start "Installing on ${host}"
    if [[ -n "${_SPINNER_PID:-}" ]]; then
        spinner_started=1
    elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
        print_active_line "Installing on ${host}"
    fi
    output_file="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-${host}.XXXXXX")"
    set +e
    if [[ "$host" == "claude" ]]; then
        run_host_installer "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" "${CLAUDE_EXTRA_ARGS[@]+"${CLAUDE_EXTRA_ARGS[@]}"}" 2>&1 | stream_colored_output "$output_file"
    elif [[ "$host" == "codex" ]]; then
        run_host_installer "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" "${CODEX_EXTRA_ARGS[@]+"${CODEX_EXTRA_ARGS[@]}"}" 2>&1 | stream_colored_output "$output_file"
    elif [[ "$host" == "opencode" ]]; then
        run_host_installer "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" "${OPENCODE_EXTRA_ARGS[@]+"${OPENCODE_EXTRA_ARGS[@]}"}" 2>&1 | stream_colored_output "$output_file"
    else
        run_host_installer "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" 2>&1 | stream_colored_output "$output_file"
    fi
    ret=${PIPESTATUS[0]}
    set -e
    output="$(cat "$output_file")"
    rm -f "$output_file"
    collect_issues_from_output "$output"

    if echo "$output" | grep -q "=== SKIPPED"; then
        SKIP+=("$host")
        emit_host_status "SKIPPED" "$host (CLI not found)"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish skip "Skipped ${host} (CLI not found)"
        elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Skipped ${host} (CLI not found)"
        fi
    elif [ $ret -ne 0 ]; then
        FAIL+=("$host")
        emit_host_status "FAILED" "$host"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish err "Failed ${host}"
        elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Failed ${host}"
        fi
        # Always show the captured output on failure so the user can see why.
        if [[ -n "$output" ]]; then
            printf "%b│%b  %bError output from %s installer:%b\n" \
                "$C_FRAME" "$C_RESET" "$C_RED" "$host" "$C_RESET"
            while IFS= read -r _err_line; do
                printf "%b│%b  %s\n" "$C_FRAME" "$C_RESET" "$_err_line"
            done <<< "$output"
        fi
    elif echo "$output" | grep -q "] WARN:"; then
        WARN+=("$host")
        emit_host_status "WARN" "$host"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish warn "Completed ${host} with warnings"
        elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed ${host} with warnings"
        fi
    else
        PASS+=("$host")
        emit_host_status "OK" "$host"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish ok "Completed ${host}"
        elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed ${host}"
        fi
    fi
}

# ── Universal agents (always run first when using --workspace) ──────────────
if has_passthrough "--workspace"; then
    spinner_started=0
    echo ""
    emit_host_status "START" "agents"
    spinner_start "Installing universal agents (AGENTS.md)"
    if [[ -n "${_SPINNER_PID:-}" ]]; then
        spinner_started=1
    elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
        print_active_line "Installing universal agents (AGENTS.md)"
    fi
    UNIVERSAL_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-agents.XXXXXX")"
    set +e
    bash "${SCRIPT_DIR}/install_agents.sh" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" 2>&1 | stream_colored_output "$UNIVERSAL_OUTPUT_FILE"
    UNIVERSAL_RET=${PIPESTATUS[0]}
    set -e
    UNIVERSAL_OUTPUT="$(cat "$UNIVERSAL_OUTPUT_FILE")"
    rm -f "$UNIVERSAL_OUTPUT_FILE"
    collect_issues_from_output "$UNIVERSAL_OUTPUT"
    if echo "$UNIVERSAL_OUTPUT" | grep -q "] WARN:"; then
        WARN+=("agents")
        emit_host_status "WARN" "agents"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish warn "Completed universal agents with warnings"
        elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed universal agents with warnings"
        fi
    elif [ $UNIVERSAL_RET -ne 0 ]; then
        FAIL+=("agents")
        emit_host_status "FAILED" "agents"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish err "Failed universal agents"
        elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Failed universal agents"
        fi
    else
        PASS+=("agents")
        emit_host_status "OK" "agents"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish ok "Completed universal agents"
        elif [[ "${LEMONCROW_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed universal agents"
        fi
    fi
fi

$DO_CLAUDE    && run_installer claude
$DO_CODEX     && run_installer codex
$DO_OPENCODE  && run_installer opencode
$DO_COPILOT   && run_installer copilot
$DO_ANTIGRAVITY && run_installer antigravity
$DO_CURSOR    && run_installer cursor
$DO_HERMES    && run_installer hermes

echo ""
print_message "$C_PURPLE" "══════════════════════════════════════════════"
print_message "$C_PURPLE" " LemonCrow Install Summary"
print_message "$C_PURPLE" "══════════════════════════════════════════════"
for h in "${PASS[@]+"${PASS[@]}"}"; do printf "  %bOK%b       %s\n" "$C_GREEN" "$C_RESET" "$h"; done
for h in "${WARN[@]+"${WARN[@]}"}"; do printf "  %bWARN%b     %s\n" "$C_YELLOW" "$C_RESET" "$h"; done
for h in "${SKIP[@]+"${SKIP[@]}"}"; do printf "  %bSKIPPED%b  %s (CLI not found)\n" "$C_YELLOW" "$C_RESET" "$h"; done
for h in "${FAIL[@]+"${FAIL[@]}"}"; do printf "  %bFAILED%b   %s\n" "$C_RED" "$C_RESET" "$h"; done
echo ""
print_issue_group "Host install errors" "$C_RED" "${ERRORS[@]+"${ERRORS[@]}"}"
print_issue_group "Host install warnings" "$C_YELLOW" "${WARNINGS[@]+"${WARNINGS[@]}"}"

if [ ${#FAIL[@]} -gt 0 ]; then
    print_message "$C_RED" "Some installs failed. Scroll up for the error output from each failed host."
    print_message "$C_PURPLE" "Next: fix the errors above, then re-run: make install"
elif [ ${#WARN[@]} -gt 0 ]; then
    print_message "$C_YELLOW" "Host installs completed with warnings. Review the warnings above before continuing."
fi

# Persist host detection results for the Docker service (write-only, no terminal output)
STATUS_SCRIPT="${SCRIPT_DIR}/status.sh"
if [ -f "$STATUS_SCRIPT" ]; then
    bash "$STATUS_SCRIPT" --write >/dev/null 2>&1 || true
fi

if [ ${#FAIL[@]} -gt 0 ]; then
    exit 1
fi
exit 0
