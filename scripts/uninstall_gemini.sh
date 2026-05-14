#!/usr/bin/env bash
# uninstall_gemini.sh - Remove Atelier from Gemini CLI
#
# Options:
#   --workspace DIR  Remove project-local artifacts from DIR instead of global user config
#   --dry-run        Print what would happen, touch nothing

set -euo pipefail

DRY_RUN=false
WORKSPACE=""
WORKSPACE_SET=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            WORKSPACE="$2"
            WORKSPACE_SET=true
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

if $WORKSPACE_SET; then
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
fi

info()  { echo "[atelier:uninstall:gemini] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

if command -v gemini &>/dev/null; then
    if $WORKSPACE_SET; then
        run "cd '$WORKSPACE' && gemini extensions disable atelier --scope workspace || true"
        run "gemini extensions enable atelier --scope user >/dev/null 2>&1 || true"
        info "Disabled atelier for workspace scope: $WORKSPACE"
    else
        run "gemini extensions uninstall atelier >/dev/null 2>&1 || true"
        info "Unlinked atelier Gemini extension"
    fi
fi



info "Done."
