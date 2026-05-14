#!/usr/bin/env bash
# uninstall.sh — Remove Atelier and all agent-host integrations
#
# Usage:
#   bash scripts/uninstall.sh [--dry-run] [--no-hosts] [--purge] [--workspace DIR]
#
# Optional environment variables:
#   ATELIER_BIN_DIR    Global bin dir for console scripts (default: ~/.local/bin)
#   ATELIER_TOOL_DIR   uv tool environment dir (default: ~/.local/share/uv/tools)
#   ATELIER_DRY_RUN    If set to 1, print planned actions and exit
#
# Notes:
#   Codex host uninstall removes only the managed Atelier AGENTS block when the
#   destination file uses explicit Atelier START/END sentinels.

set -euo pipefail

ATELIER_BIN_DIR="${ATELIER_BIN_DIR:-${HOME}/.local/bin}"
ATELIER_TOOL_DIR="${ATELIER_TOOL_DIR:-${HOME}/.local/share/uv/tools}"
ATELIER_DRY_RUN="${ATELIER_DRY_RUN:-0}"
ATELIER_NO_HOSTS="${ATELIER_NO_HOSTS:-0}"
ATELIER_INSTALL_RECORD="${HOME}/.atelier/install_dir"
ATELIER_DEFAULT_INSTALL_DIR="${HOME}/.local/share/atelier"
PASSTHROUGH=()
WORKSPACE_EXPLICIT=0
PURGE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  ATELIER_DRY_RUN=1; PASSTHROUGH+=("$1") ;;
        --no-hosts) ATELIER_NO_HOSTS=1 ;;
        --purge)    PURGE=1 ;;
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

remove_path() {
    local path="$1"
    if [ -e "$path" ] || [ -L "$path" ]; then
        run "rm -rf '$path'"
        info "Removed ${path}"
    fi
}

remove_file_if_atelier() {
    local path="$1"
    [ -f "$path" ] || return 0
    grep -qi "atelier" "$path" 2>/dev/null || return 0
    remove_path "$path"
}

remove_glob() {
    local pattern="$1"
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] rm -rf ${pattern}"
    else
        local matches=()
        local match
        while IFS= read -r match; do
            matches+=("$match")
        done < <(compgen -G "$pattern" || true)
        if [[ ${#matches[@]} -gt 0 ]]; then
            rm -rf "${matches[@]}"
        fi
    fi
}

install_dir_from_record() {
    if [ -f "$ATELIER_INSTALL_RECORD" ]; then
        head -n 1 "$ATELIER_INSTALL_RECORD" 2>/dev/null || true
    fi
}

purge_leftovers() {
    local repo_root install_dir
    repo_root="$(cd "${SCRIPT_DIR}/.." && pwd)"
    install_dir="${ATELIER_INSTALL_DIR:-$(install_dir_from_record)}"
    install_dir="${install_dir:-$ATELIER_DEFAULT_INSTALL_DIR}"

    echo ""
    info "Purging Atelier runtime state, install environments, and known host residue..."

    remove_path "${ATELIER_TOOL_DIR}/atelier"
    remove_path "${HOME}/.local/share/uv/tools/atelier"

    remove_file_if_atelier "${HOME}/.codex/AGENTS.md"
    remove_glob "${HOME}/.codex/AGENTS.md.atelier-backup.*"
    remove_glob "${HOME}/.codex/plugins/atelier*"
    remove_path "${HOME}/.codex/plugins/cache/atelier"

    if command -v npm >/dev/null 2>&1; then
        run "npm uninstall -g codeburn tokscale >/dev/null 2>&1 || true"
        info "Removed global npm helper packages installed by Atelier when present"
    fi

    remove_glob "${HOME}/.copilot/instructions/*atelier*"
    remove_glob "${HOME}/.config/Code/User/*.atelier-backup.*"

    remove_path "${HOME}/.atelier"

    if [ -n "$install_dir" ]; then
        case "$install_dir" in
            "$repo_root"|"$PWD")
                warn "Skipping install source removal because it is the current source checkout: $install_dir"
                ;;
            "$HOME"/*)
                remove_path "$install_dir"
                ;;
            *)
                warn "Skipping install source outside HOME: $install_dir"
                ;;
        esac
    fi
}

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
for cmd in atelier atelier-mc atelier-status; do
    target="${ATELIER_BIN_DIR}/${cmd}"
    if [ -f "$target" ] || [ -L "$target" ]; then
        run "rm -f '$target'"
        info "Removed ${target}"
    fi
done

if [[ "$PURGE" == "1" ]]; then
    purge_leftovers
fi

info "Uninstall complete."
