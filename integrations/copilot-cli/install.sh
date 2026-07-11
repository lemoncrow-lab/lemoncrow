#!/usr/bin/env bash
# install.sh — Install LemonCrow hooks for GitHub Copilot CLI
#
# Copies hook scripts to ~/.lemoncrow/copilot-cli-hooks/ and writes
# ~/.copilot/hooks/hooks.json pointing to them.
#
# Usage:
#   bash integrations/copilot-cli/install.sh [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

run() { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

HOOKS_DEST="${HOME}/.lemoncrow/copilot-cli-hooks"
COPILOT_HOOKS_DIR="${HOME}/.copilot/hooks"

echo "[lemon:copilot-cli] Installing hook scripts → ${HOOKS_DEST}"
run "mkdir -p '${HOOKS_DEST}'"
run "cp '${SCRIPT_DIR}/hooks/session_start.py' '${HOOKS_DEST}/'"
run "cp '${SCRIPT_DIR}/hooks/post_tool_use_failure.py' '${HOOKS_DEST}/'"
run "cp '${SCRIPT_DIR}/hooks/stop.py' '${HOOKS_DEST}/'"

echo "[lemon:copilot-cli] Writing hooks.json → ${COPILOT_HOOKS_DIR}/hooks.json"
run "mkdir -p '${COPILOT_HOOKS_DIR}'"
if ! $DRY_RUN; then
    sed "s|__HOOKS_DIR__|${HOOKS_DEST}|g" \
        "${SCRIPT_DIR}/hooks/hooks.json" \
        > "${COPILOT_HOOKS_DIR}/hooks.json"
else
    echo "  [dry-run] sed __HOOKS_DIR__ → ${HOOKS_DEST} > ${COPILOT_HOOKS_DIR}/hooks.json"
fi

echo "[lemon:copilot-cli] Done."
echo "  Hook scripts: ${HOOKS_DEST}/"
echo "  Hooks config: ${COPILOT_HOOKS_DIR}/hooks.json"
echo ""
echo "Restart the Copilot CLI for hooks to take effect."
