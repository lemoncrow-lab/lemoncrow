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
#   --opencode     Only install opencode
#   --copilot      Only install Copilot
#   --antigravity  Only install Antigravity / agy
#   --dry-run      Pass through to all install scripts
#   --print-only   Pass through to all install scripts
#   --strict       Pass through; scripts exit nonzero if CLI absent
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config

set -euo pipefail

if [[ -t 1 ]]; then
    C_RESET="$(printf '\033[0m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[38;2;155;117;217m')"
    C_PURPLE="$(printf '\033[38;2;155;117;217m')"
else
    C_RESET=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
    C_CYAN=""
    C_PURPLE=""
fi
if [[ -n "${FORCE_COLOR:-}${CLICOLOR_FORCE:-}" && -z "${NO_COLOR:-}" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[38;2;155;117;217m')"
    C_PURPLE="$(printf '\033[38;2;155;117;217m')"
fi

ATELIER_VERBOSE="${ATELIER_VERBOSE:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

print_message() {
    local color="$1"
    shift
    printf "%b%s%b\n" "$color" "$*" "$C_RESET"
}

verbose() { [[ "$ATELIER_VERBOSE" == "1" ]] && printf "%s\n" "$*" || true; }

DO_CLAUDE=false
DO_CODEX=false
DO_OPENCODE=false
DO_COPILOT=false
DO_ANTIGRAVITY=false
EXPLICIT=false
PASSTHROUGH=()
CLAUDE_EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)       EXPLICIT=true; DO_CLAUDE=true; DO_CODEX=true; DO_OPENCODE=true; DO_COPILOT=true; DO_ANTIGRAVITY=true ;;
        --claude)    EXPLICIT=true; DO_CLAUDE=true ;;
        --codex)     EXPLICIT=true; DO_CODEX=true ;;
        --opencode)  EXPLICIT=true; DO_OPENCODE=true ;;
        --copilot)   EXPLICIT=true; DO_COPILOT=true ;;
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
    echo "  ${C_PURPLE}4${C_RESET}) GitHub Copilot"
    echo "  ${C_PURPLE}5${C_RESET}) Antigravity / agy"
    echo "  ${C_PURPLE}a${C_RESET}) All"
    echo "  ${C_PURPLE}n${C_RESET}) None (skip agent installs)"
    echo ""

    read -r -p "  Choice [a]: " runtime_answer
    runtime_answer="${runtime_answer:-a}"

    # Reset all to false — user picks explicitly
    DO_CLAUDE=false; DO_CODEX=false; DO_OPENCODE=false; DO_COPILOT=false; DO_ANTIGRAVITY=false

    case "$runtime_answer" in
        a|A|all|ALL)
            DO_CLAUDE=true; DO_CODEX=true; DO_OPENCODE=true; DO_COPILOT=true; DO_ANTIGRAVITY=true
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
                    *) echo "  ${C_YELLOW}Unknown choice: $choice${C_RESET}" ;;
                esac
            done
            selected=""
            $DO_CLAUDE    && selected="$selected claude"
            $DO_OPENCODE  && selected="$selected opencode"
            $DO_CODEX     && selected="$selected codex"
            $DO_COPILOT   && selected="$selected copilot"
            $DO_ANTIGRAVITY && selected="$selected antigravity"
            echo "  → Selected:${selected:- none}"
            ;;
    esac

    echo ""

    # ── Scope selection ────────────────────────────────────────────────────
    # Only prompt for scope if at least one runtime was selected
    if $DO_CLAUDE || $DO_CODEX || $DO_OPENCODE || $DO_COPILOT || $DO_ANTIGRAVITY; then
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
    DO_CLAUDE=true; DO_CODEX=true; DO_OPENCODE=true; DO_COPILOT=true; DO_ANTIGRAVITY=true
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

    case "$host" in
        claude) script="${SCRIPT_DIR}/install_claude.sh" ;;
        *) script="${SCRIPT_DIR}/install_${host}.sh" ;;
    esac

    echo ""
    if [[ "$ATELIER_VERBOSE" == "1" ]]; then
        print_message "$C_PURPLE" "──────────────────────────────────────────"
        print_message "$C_PURPLE" " Installing Atelier -> ${host}"
        print_message "$C_PURPLE" "──────────────────────────────────────────"
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
    elif [ $ret -ne 0 ]; then
        FAIL+=("$host")
        emit_host_status "FAILED" "$host"
    elif echo "$output" | grep -q "] WARN:"; then
        WARN+=("$host")
        emit_host_status "WARN" "$host"
    else
        PASS+=("$host")
        emit_host_status "OK" "$host"
    fi
}

# ── Universal agents (always run first when using --workspace) ──────────────
if [[ " ${PASSTHROUGH[*]} " =~ "--workspace" ]]; then
    echo ""
    if [[ "$ATELIER_VERBOSE" == "1" ]]; then
        print_message "$C_PURPLE" "──────────────────────────────────────────"
        print_message "$C_PURPLE" " Installing universal agents (.mcp.json + AGENTS.md)"
        print_message "$C_PURPLE" "──────────────────────────────────────────"
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
    elif [ $UNIVERSAL_RET -ne 0 ]; then
        FAIL+=("agents")
        emit_host_status "FAILED" "agents"
    else
        PASS+=("agents")
        emit_host_status "OK" "agents"
    fi
fi

$DO_CLAUDE    && run_installer claude
$DO_CODEX     && run_installer codex
$DO_OPENCODE  && run_installer opencode
$DO_COPILOT   && run_installer copilot
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
