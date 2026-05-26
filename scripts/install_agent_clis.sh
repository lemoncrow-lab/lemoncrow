#!/usr/bin/env bash
# install_agent_clis.sh — Install Atelier into all available agent CLIs
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
#   --cursor       Only install Cursor
#   --opencode     Only install opencode
#   --copilot      Only install Copilot
#   --hermes       Only install Hermes Agent
#   --antigravity  Only install Antigravity / agy
#   --dry-run      Pass through to all install scripts
#   --print-only   Pass through to all install scripts
#   --strict       Pass through; scripts exit nonzero if CLI absent
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config

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
ACTIVE_BAR="┃"
if [[ "${LC_ALL:-${LANG:-}}" != *"UTF-8"* && "${LC_ALL:-${LANG:-}}" != *"utf8"* ]]; then
    ACTIVE_BAR="|"
fi

ATELIER_VERBOSE="${ATELIER_VERBOSE:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_BUILDER="${SCRIPT_DIR}/build_host_skills.sh"
print_message() {
    local color="$1"
    shift
    printf "%b%s%b\n" "$color" "$*" "$C_RESET"
}

verbose() { [[ "$ATELIER_VERBOSE" == "1" ]] && printf "%s\n" "$*" || true; }

print_active_line() {
    printf "%b%s%b  %b%s%b\n" "$C_PURPLE" "$ACTIVE_BAR" "$C_RESET" "$C_PURPLE" "$1" "$C_RESET"
}

print_frame_line() {
    printf "%b│%b  %s\n" "$C_DIM" "$C_RESET" "$1"
}

_SPINNER_PID=""
spinner_start() {
    local msg="$1"
    [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]] || return 0
    [[ -t 1 && -n "${TERM:-}" && "${TERM:-}" != "dumb" ]] || return 0
    [[ "${ATELIER_VERBOSE:-0}" != "1" ]] || return 0
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
}

spinner_finish() {
    local state="$1"
    local msg="$2"
    [[ -n "${_SPINNER_PID:-}" ]] || return 0
    kill "$_SPINNER_PID" 2>/dev/null || true
    wait "$_SPINNER_PID" 2>/dev/null || true
    _SPINNER_PID=""
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
DO_CURSOR=false
DO_OPENCODE=false
DO_COPILOT=false
DO_HERMES=false
DO_ANTIGRAVITY=false
EXPLICIT=false
PASSTHROUGH=()
CLAUDE_EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)       EXPLICIT=true; DO_CLAUDE=true; DO_CODEX=true; DO_CURSOR=true; DO_OPENCODE=true; DO_COPILOT=true; DO_HERMES=true; DO_ANTIGRAVITY=true ;;
        --claude)    EXPLICIT=true; DO_CLAUDE=true ;;
        --codex)     EXPLICIT=true; DO_CODEX=true ;;
        --cursor)    EXPLICIT=true; DO_CURSOR=true ;;
        --opencode)  EXPLICIT=true; DO_OPENCODE=true ;;
        --copilot)   EXPLICIT=true; DO_COPILOT=true ;;
        --hermes)    EXPLICIT=true; DO_HERMES=true ;;
        --antigravity) EXPLICIT=true; DO_ANTIGRAVITY=true ;;
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
        *) print_message "$C_RED" "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# ── Interactive prompts (when no flags and TTY) ──────────────────────────────
if ! $EXPLICIT && [[ -t 0 ]] && [[ -t 1 ]]; then
    echo ""
    print_message "$C_PURPLE" "══════════════════════════════════════════════"
    print_message "$C_PURPLE" " Atelier — Agent Installation"
    print_message "$C_PURPLE" "══════════════════════════════════════════════"
    echo ""

    # ── Runtime selection ──────────────────────────────────────────────────
    echo "  Which AI coding agents would you like to install Atelier for?"
    echo ""
    echo "  ${C_PURPLE}1${C_RESET}) Claude Code"
    echo "  ${C_PURPLE}2${C_RESET}) OpenCode"
    echo "  ${C_PURPLE}3${C_RESET}) Codex CLI"
    echo "  ${C_PURPLE}4${C_RESET}) Cursor"
    echo "  ${C_PURPLE}5${C_RESET}) GitHub Copilot"
    echo "  ${C_PURPLE}6${C_RESET}) Hermes Agent"
    echo "  ${C_PURPLE}7${C_RESET}) Antigravity / agy"
    echo "  ${C_PURPLE}a${C_RESET}) All"
    echo "  ${C_PURPLE}n${C_RESET}) None (skip agent installs)"
    echo ""

    read -r -p "  Choice [a]: " runtime_answer
    runtime_answer="${runtime_answer:-a}"

    # Reset all to false — user picks explicitly
    DO_CLAUDE=false; DO_CODEX=false; DO_CURSOR=false; DO_OPENCODE=false; DO_COPILOT=false; DO_HERMES=false; DO_ANTIGRAVITY=false

    case "$runtime_answer" in
        a|A|all|ALL)
            DO_CLAUDE=true; DO_CODEX=true; DO_CURSOR=true; DO_OPENCODE=true; DO_COPILOT=true; DO_HERMES=true; DO_ANTIGRAVITY=true
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
                    4) DO_CURSOR=true ;;
                    5) DO_COPILOT=true ;;
                    6) DO_HERMES=true ;;
                    7) DO_ANTIGRAVITY=true ;;
                    *) echo "  ${C_YELLOW}Unknown choice: $choice${C_RESET}" ;;
                esac
            done
            selected=""
            $DO_CLAUDE    && selected="$selected claude"
            $DO_OPENCODE  && selected="$selected opencode"
            $DO_CODEX     && selected="$selected codex"
            $DO_CURSOR    && selected="$selected cursor"
            $DO_COPILOT   && selected="$selected copilot"
            $DO_HERMES    && selected="$selected hermes"
            $DO_ANTIGRAVITY && selected="$selected antigravity"
            echo "  → Selected:${selected:- none}"
            ;;
    esac

    echo ""

    # ── Scope selection ────────────────────────────────────────────────────
    # Only prompt for scope if at least one runtime was selected
    if $DO_CLAUDE || $DO_CODEX || $DO_CURSOR || $DO_OPENCODE || $DO_COPILOT || $DO_HERMES || $DO_ANTIGRAVITY; then
        echo "  ${C_YELLOW}Install scope:${C_RESET}"
        echo ""
        echo "  ${C_PURPLE}1${C_RESET}) Global — available in all projects"
        echo "  ${C_PURPLE}2${C_RESET}) Project — this directory only (via .mcp.json + AGENTS.md)"
        echo ""
        read -r -p "  Choice [1]: " scope_answer
        scope_answer="${scope_answer:-1}"

        if [ "$scope_answer" = "2" ]; then
            if [[ ! " ${PASSTHROUGH[*]} " =~ "--workspace" ]]; then
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
    DO_CLAUDE=true; DO_CODEX=true; DO_CURSOR=true; DO_OPENCODE=true; DO_COPILOT=true; DO_HERMES=true; DO_ANTIGRAVITY=true
fi

prebuild_shared_skill_bundles() {
    local needs_skills=0
    local spinner_started=0

    if $DO_CLAUDE || $DO_CODEX || $DO_ANTIGRAVITY; then
        needs_skills=1
    fi
    [[ "$needs_skills" == "1" ]] || return 0

    if [[ " ${PASSTHROUGH[*]} " =~ "--dry-run" ]]; then
        [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]] && print_frame_line "Dry run: skipping shared skill bundle generation"
        return 0
    fi

    emit_host_status "START" "skills"
    spinner_start "Generating shared host skill bundles"
    if [[ -n "${_SPINNER_PID:-}" ]]; then
        spinner_started=1
    elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
        print_active_line "Generating shared host skill bundles"
    fi

    if bash "$SKILL_BUILDER" --host all >/dev/null; then
        emit_host_status "OK" "skills"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish ok "Generated shared host skill bundles"
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Generated shared host skill bundles"
        fi
        return 0
    fi

    emit_host_status "FAILED" "skills"
    if [[ "$spinner_started" == "1" ]]; then
        spinner_finish err "Failed to generate shared host skill bundles"
    elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
        print_frame_line "Failed to generate shared host skill bundles"
    fi
    exit 1
}

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
        if [[ "${ATELIER_VERBOSE:-0}" == "1" ]]; then
            print_colored_line "$line"
        fi
    done
}

emit_host_status() {
    [[ "${ATELIER_HOST_STATUS_STREAM:-0}" == "1" ]] || return 0
    printf "@@ATELIER_HOST_STATUS@@ %s %s\n" "$1" "$2"
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
    printf "%b%s (%d)%b\n" "$color" "$title" "$count" "$C_RESET"
    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" && -z "${printed[$entry]+x}" ]] || continue
        printed["$entry"]=1
        printf "  %b-%b %s\n" "$color" "$C_RESET" "$entry"
    done
}

run_installer() {
    local host="$1"
    local script
    local output_file output ret
    local spinner_started=0

    case "$host" in
        claude) script="${SCRIPT_DIR}/install_claude.sh" ;;
        *) script="${SCRIPT_DIR}/install_${host}.sh" ;;
    esac

    echo ""
    emit_host_status "START" "$host"
    spinner_start "Installing on ${host}"
    if [[ -n "${_SPINNER_PID:-}" ]]; then
        spinner_started=1
    elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
        print_active_line "Installing on ${host}"
    fi
    output_file="$(mktemp "${TMPDIR:-/tmp}/atelier-${host}.XXXXXX")"
    set +e
    if [[ "$host" == "claude" ]]; then
        bash "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" "${CLAUDE_EXTRA_ARGS[@]+"${CLAUDE_EXTRA_ARGS[@]}"}" 2>&1 | stream_colored_output "$output_file"
    else
        bash "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" 2>&1 | stream_colored_output "$output_file"
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
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Skipped ${host} (CLI not found)"
        fi
    elif [ $ret -ne 0 ]; then
        FAIL+=("$host")
        emit_host_status "FAILED" "$host"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish err "Failed ${host}"
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Failed ${host}"
        fi
    elif echo "$output" | grep -q "] WARN:"; then
        WARN+=("$host")
        emit_host_status "WARN" "$host"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish warn "Completed ${host} with warnings"
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed ${host} with warnings"
        fi
    else
        PASS+=("$host")
        emit_host_status "OK" "$host"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish ok "Completed ${host}"
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed ${host}"
        fi
    fi
}

prebuild_shared_skill_bundles

# ── Universal agents (always run first when using --workspace) ──────────────
if [[ " ${PASSTHROUGH[*]} " =~ "--workspace" ]]; then
    spinner_started=0
    echo ""
    emit_host_status "START" "agents"
    spinner_start "Installing universal agents (.mcp.json + AGENTS.md)"
    if [[ -n "${_SPINNER_PID:-}" ]]; then
        spinner_started=1
    elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
        print_active_line "Installing universal agents (.mcp.json + AGENTS.md)"
    fi
    UNIVERSAL_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/atelier-agents.XXXXXX")"
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
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed universal agents with warnings"
        fi
    elif [ $UNIVERSAL_RET -ne 0 ]; then
        FAIL+=("agents")
        emit_host_status "FAILED" "agents"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish err "Failed universal agents"
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Failed universal agents"
        fi
    else
        PASS+=("agents")
        emit_host_status "OK" "agents"
        if [[ "$spinner_started" == "1" ]]; then
            spinner_finish ok "Completed universal agents"
        elif [[ "${ATELIER_HOST_STATUS_STREAM:-0}" != "1" ]]; then
            print_frame_line "Completed universal agents"
        fi
    fi
fi

$DO_CLAUDE    && run_installer claude
$DO_CODEX     && run_installer codex
$DO_CURSOR    && run_installer cursor
$DO_OPENCODE  && run_installer opencode
$DO_COPILOT   && run_installer copilot
$DO_HERMES    && run_installer hermes
$DO_ANTIGRAVITY && run_installer antigravity

echo ""
print_message "$C_PURPLE" "══════════════════════════════════════════════"
print_message "$C_PURPLE" " Atelier Install Summary"
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
