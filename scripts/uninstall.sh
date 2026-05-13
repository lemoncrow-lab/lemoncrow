#!/usr/bin/env bash
# uninstall.sh — Remove Atelier and all agent-host integrations
#
# Usage:
#   bash scripts/uninstall.sh [--dry-run] [--no-hosts] [--workspace DIR]
#
# Optional environment variables:
#   ATELIER_BIN_DIR    Global bin dir for console scripts (default: ~/.local/bin)
#   ATELIER_DRY_RUN    If set to 1, print planned actions and exit
#
# Notes:
#   Codex host uninstall removes only the managed Atelier AGENTS block when the
#   destination file uses explicit Atelier START/END sentinels.

set -euo pipefail

ATELIER_BIN_DIR="${ATELIER_BIN_DIR:-${HOME}/.local/bin}"
ATELIER_DRY_RUN="${ATELIER_DRY_RUN:-0}"
ATELIER_NO_HOSTS="${ATELIER_NO_HOSTS:-0}"
PASSTHROUGH=()
WORKSPACE_EXPLICIT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  ATELIER_DRY_RUN=1; PASSTHROUGH+=("$1") ;;
        --no-hosts) ATELIER_NO_HOSTS=1 ;;
        --workspace)
            if [ $# -lt 2 ]; then echo "Missing value for --workspace" >&2; exit 1; fi
            PASSTHROUGH+=("$1" "$2"); WORKSPACE_EXPLICIT=1; shift ;;
        *) PASSTHROUGH+=("$1") ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { echo "[atelier-uninstall] $*"; }
warn() { echo "[atelier-uninstall] WARN: $*" >&2; }
run()  { [[ "$ATELIER_DRY_RUN" == "1" ]] && echo "[dry-run] $*" || eval "$*"; }

# ---- stop running services --------------------------------------------------
if command -v atelier &>/dev/null; then
    info "Stopping Atelier background service controller..."
    run "atelier servicectl stop 2>/dev/null || true"
    info "Stopping Atelier visualization stack..."
    run "atelier stack stop 2>/dev/null || true"
else
    warn "atelier CLI not found on PATH — skipping service shutdown"
fi

# ---- per-host uninstallers --------------------------------------------------
if [[ "$ATELIER_NO_HOSTS" != "1" ]]; then
    for host in claude codex opencode copilot gemini; do
        script="${SCRIPT_DIR}/uninstall_${host}.sh"
        [ -f "$script" ] || continue
        echo ""
        echo "──────────────────────────────────────────"
        echo " Uninstalling Atelier ← ${host}"
        echo "──────────────────────────────────────────"
        bash "$script" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} || true
        # Also clean workspace-local configs in CWD when no explicit --workspace given
        if [[ "$WORKSPACE_EXPLICIT" == "0" && "$PWD" != "$HOME" ]]; then
            local_args=(--workspace "$PWD")
            [[ "$ATELIER_DRY_RUN" == "1" ]] && local_args+=(--dry-run)
            bash "$script" "${local_args[@]}" 2>/dev/null || true
        fi
    done
    echo ""
else
    info "Skipping host integrations because ATELIER_NO_HOSTS=1"
fi

# ---- remove main bin commands ------------------------------------------------
info "Removing Atelier bin commands from ${ATELIER_BIN_DIR}..."
for cmd in atelier atelier-mcp atelier-api atelier-codex atelier-status atelier-bench; do
    target="${ATELIER_BIN_DIR}/${cmd}"
    if [ -f "$target" ] || [ -L "$target" ]; then
        run "rm -f '$target'"
        info "Removed ${target}"
    fi
done

info "Uninstall complete."
