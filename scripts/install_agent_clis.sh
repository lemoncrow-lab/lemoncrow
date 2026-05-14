#!/usr/bin/env bash
# install_agent_clis.sh — Install Atelier into all available agent CLIs
#
# By default installs into all CLIs that are on PATH.
# Use flags to target specific hosts or change behavior.
#
# Options:
#   --all          Force attempt all hosts (same as default)
#   --claude       Only install Claude Code
#   --codex        Only install Codex
#   --opencode     Only install opencode
#   --copilot      Only install Copilot
#   --gemini       Only install Gemini CLI
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
else
    C_RESET=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
fi
if [[ -n "${FORCE_COLOR:-}${CLICOLOR_FORCE:-}" && -z "${NO_COLOR:-}" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DO_CLAUDE=false
DO_CODEX=false
DO_OPENCODE=false
DO_COPILOT=false
DO_GEMINI=false
EXPLICIT=false
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)       EXPLICIT=true; DO_CLAUDE=true; DO_CODEX=true; DO_OPENCODE=true; DO_COPILOT=true; DO_GEMINI=true ;;
        --claude)    EXPLICIT=true; DO_CLAUDE=true ;;
        --codex)     EXPLICIT=true; DO_CODEX=true ;;
        --opencode)  EXPLICIT=true; DO_OPENCODE=true ;;
        --copilot)   EXPLICIT=true; DO_COPILOT=true ;;
        --gemini)    EXPLICIT=true; DO_GEMINI=true ;;
        --dry-run|--print-only|--strict) PASSTHROUGH+=("$1") ;;
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            PASSTHROUGH+=("$1" "$2")
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# Default: all hosts
if ! $EXPLICIT; then
    DO_CLAUDE=true; DO_CODEX=true; DO_OPENCODE=true; DO_COPILOT=true; DO_GEMINI=true
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
        print_colored_line "$line"
    done
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
    echo "──────────────────────────────────────────"
    echo " Installing Atelier → ${host}"
    echo "──────────────────────────────────────────"
    output_file="$(mktemp "${TMPDIR:-/tmp}/atelier-${host}.XXXXXX")"
    set +e
    bash "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" 2>&1 | stream_colored_output "$output_file"
    ret=${PIPESTATUS[0]}
    set -e
    output="$(cat "$output_file")"
    rm -f "$output_file"
    collect_issues_from_output "$output"

    if echo "$output" | grep -q "=== SKIPPED"; then
        SKIP+=("$host")
    elif [ $ret -ne 0 ]; then
        FAIL+=("$host")
    elif echo "$output" | grep -q "] WARN:"; then
        WARN+=("$host")
    else
        PASS+=("$host")
    fi
}

$DO_CLAUDE    && run_installer claude
$DO_CODEX     && run_installer codex
$DO_OPENCODE  && run_installer opencode
$DO_COPILOT   && run_installer copilot
$DO_GEMINI    && run_installer gemini

echo ""
echo "══════════════════════════════════════════════"
echo " Atelier Install Summary"
echo "══════════════════════════════════════════════"
for h in "${PASS[@]+"${PASS[@]}"}"; do printf "  %bOK%b       %s\n" "$C_GREEN" "$C_RESET" "$h"; done
for h in "${WARN[@]+"${WARN[@]}"}"; do printf "  %bWARN%b     %s\n" "$C_YELLOW" "$C_RESET" "$h"; done
for h in "${SKIP[@]+"${SKIP[@]}"}"; do printf "  %bSKIPPED%b  %s (CLI not found)\n" "$C_YELLOW" "$C_RESET" "$h"; done
for h in "${FAIL[@]+"${FAIL[@]}"}"; do printf "  %bFAILED%b   %s\n" "$C_RED" "$C_RESET" "$h"; done
echo ""
print_issue_group "Host install errors" "$C_RED" "${ERRORS[@]+"${ERRORS[@]}"}"
print_issue_group "Host install warnings" "$C_YELLOW" "${WARNINGS[@]+"${WARNINGS[@]}"}"

if [ ${#FAIL[@]} -gt 0 ]; then
    echo "Some installs failed. Scroll up for the error output from each failed host."
    echo "Next: fix the errors above, then re-run: make install"
elif [ ${#WARN[@]} -gt 0 ]; then
    echo "Host installs completed with warnings. Review the warnings above before continuing."
    echo "Next: make verify"
else
    echo "Next: make verify"
fi

# Persist host detection results for the Docker service (write-only, no terminal output)
STATUS_SCRIPT="${SCRIPT_DIR}/status.sh"
if [ -f "$STATUS_SCRIPT" ]; then
    bash "$STATUS_SCRIPT" --write 2>/dev/null || true
fi

if [ ${#FAIL[@]} -gt 0 ]; then
    exit 1
fi
exit 0
