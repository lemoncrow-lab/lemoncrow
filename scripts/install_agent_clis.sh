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
FAIL=()
SKIP=()

run_installer() {
    local host="$1"
    local script

    case "$host" in
        claude) script="${SCRIPT_DIR}/install_claude.sh" ;;
        *) script="${SCRIPT_DIR}/install_${host}.sh" ;;
    esac

    echo ""
    echo "──────────────────────────────────────────"
    echo " Installing Atelier → ${host}"
    echo "──────────────────────────────────────────"
    set +e
    output=$(bash "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" 2>&1)
    local ret=$?
    set -e
    echo "$output"

    if echo "$output" | grep -q "=== SKIPPED"; then
        SKIP+=("$host")
    elif [ $ret -eq 0 ]; then
        PASS+=("$host")
    else
        FAIL+=("$host")
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
for h in "${PASS[@]+"${PASS[@]}"}"; do echo "  OK       $h"; done
for h in "${SKIP[@]+"${SKIP[@]}"}"; do echo "  SKIPPED  $h (CLI not found)"; done
for h in "${FAIL[@]+"${FAIL[@]}"}"; do echo "  FAILED   $h"; done
echo ""

if [ ${#FAIL[@]} -gt 0 ]; then
    echo "Some installs failed. Scroll up for the error output from each failed host."
    echo "Next: fix the errors above, then re-run: make install"
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
