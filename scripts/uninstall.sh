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

# Read the memory sidecar that was selected at install time (if any).
_MEMORY_BACKEND_FILE="${HOME}/.atelier/memory_backend"
ATELIER_MEMORY_BACKEND=""
if [[ -f "$_MEMORY_BACKEND_FILE" ]]; then
    ATELIER_MEMORY_BACKEND="$(head -n 1 "$_MEMORY_BACKEND_FILE" 2>/dev/null | tr -d '[:space:]')"
fi

# Read the Zoekt selection that was persisted at install time (if any).
_ZOEKT_ENABLED_FILE="${HOME}/.atelier/zoekt_enabled"
ATELIER_ZOEKT=""
if [[ -f "$_ZOEKT_ENABLED_FILE" ]]; then
    ATELIER_ZOEKT="$(head -n 1 "$_ZOEKT_ENABLED_FILE" 2>/dev/null | tr -d '[:space:]')"
fi

# Read persisted Zoekt sidecar selection (if any).
_ZOEKT_ENABLED_FILE="${HOME}/.atelier/zoekt_enabled"
ATELIER_ZOEKT=""
if [[ -f "$_ZOEKT_ENABLED_FILE" ]]; then
    ATELIER_ZOEKT="$(head -n 1 "$_ZOEKT_ENABLED_FILE" 2>/dev/null | tr -d '[:space:]')"
fi

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

    # ---- memory sidecar Docker cleanup --------------------------------------
    case "$ATELIER_MEMORY_BACKEND" in
        letta)
            info "Removing Letta Docker container and volumes..."
            local letta_compose="${install_dir}/deploy/letta/docker-compose.yml"
            if [[ -f "$letta_compose" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose -f '$letta_compose' down -v --remove-orphans 2>/dev/null || true"
            else
                warn "Letta compose file not found or docker unavailable — Docker volumes may need manual removal"
                warn "  docker volume ls | grep letta"
            fi
            ;;
        openmemory)
            info "Removing OpenMemory Docker state and checkout..."
            local om_workdir="${HOME}/.atelier/openmemory/mem0/openmemory"
            if [[ -d "$om_workdir" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose -C '$om_workdir' down -v --remove-orphans 2>/dev/null || true"
            fi
            remove_path "${HOME}/.atelier/openmemory"
            ;;
    esac

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

# ---- stop memory sidecar (before background controller) --------------------
if command -v atelier &>/dev/null && [[ -n "$ATELIER_MEMORY_BACKEND" ]]; then
    case "$ATELIER_MEMORY_BACKEND" in
        letta)
            info "Stopping Letta memory sidecar..."
            run "atelier letta down 2>/dev/null || true"
            ;;
        openmemory)
            info "Stopping OpenMemory memory sidecar..."
            run "atelier openmemory down 2>/dev/null || true"
            ;;
        *)
            warn "Unknown memory backend '$ATELIER_MEMORY_BACKEND' in $_MEMORY_BACKEND_FILE - skipping sidecar teardown"
            ;;
    esac
fi

# ---- stop Zoekt sidecar (before background controller) ---------------------
if command -v atelier &>/dev/null && [[ "$ATELIER_ZOEKT" == "1" ]]; then
    info "Stopping Zoekt code-search sidecar..."
    run "atelier zoekt down 2>/dev/null || true"
fi

# ---- stop running services --------------------------------------------------
if command -v atelier &>/dev/null; then
    case "$ATELIER_MEMORY_BACKEND" in
        letta)
            info "Stopping Letta memory sidecar..."
            run "atelier letta down 2>/dev/null || true"
            ;;
        openmemory)
            info "Stopping OpenMemory memory sidecar..."
            run "atelier openmemory down 2>/dev/null || true"
            ;;
        "")
            ;;
        *)
            warn "Unknown memory backend '$ATELIER_MEMORY_BACKEND' in $_MEMORY_BACKEND_FILE — skipping sidecar teardown"
            ;;
    esac

    info "Stopping Atelier background service controller..."
    run "atelier servicectl stop 2>/dev/null || true"
    info "Stopping Atelier visualization stack..."
    run "atelier stack stop 2>/dev/null || true"
else
    warn "atelier CLI not found on PATH — skipping service shutdown"
fi

# ---- per-host uninstallers --------------------------------------------------
if [[ "$ATELIER_NO_HOSTS" != "1" ]]; then
    for host in claude codex opencode copilot antigravity; do
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
for cmd in atelier atelier-mc; do
    target="${ATELIER_BIN_DIR}/${cmd}"
    if [ -f "$target" ] || [ -L "$target" ]; then
        run "rm -f '$target'"
        info "Removed ${target}"
    fi
done

if [[ "$PURGE" == "1" ]]; then
    local_install_dir="${ATELIER_INSTALL_DIR:-$(install_dir_from_record)}"
    local_install_dir="${local_install_dir:-$ATELIER_DEFAULT_INSTALL_DIR}"

    # ---- memory sidecar Docker cleanup --------------------------------------
    case "$ATELIER_MEMORY_BACKEND" in
        letta)
            info "Removing Letta Docker container and volumes..."
            letta_compose="${local_install_dir}/deploy/letta/docker-compose.yml"
            if [[ -f "$letta_compose" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose -f '$letta_compose' down -v --remove-orphans 2>/dev/null || true"
            else
                warn "Letta compose file not found or docker unavailable - Docker volumes may need manual removal"
            fi
            ;;
        openmemory)
            info "Removing OpenMemory Docker state and checkout..."
            om_workdir="${HOME}/.atelier/openmemory/mem0/openmemory"
            if [[ -d "$om_workdir" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose --project-directory '$om_workdir' down -v --remove-orphans 2>/dev/null || true"
            fi
            remove_path "${HOME}/.atelier/openmemory"
            ;;
    esac

    # ---- Zoekt code-search Docker cleanup -----------------------------------
    if [[ "$ATELIER_ZOEKT" == "1" ]] && command -v docker >/dev/null 2>&1; then
        info "Removing Zoekt Docker containers and volumes..."
        zoekt_containers="$(docker ps -aq --filter "name=atelier-zoekt-" 2>/dev/null || true)"
        if [[ -n "$zoekt_containers" ]]; then
            run "docker stop $zoekt_containers 2>/dev/null || true"
            run "docker rm $zoekt_containers 2>/dev/null || true"
        fi
        zoekt_volumes="$(docker volume ls -q --filter "name=atelier-zoekt-" 2>/dev/null || true)"
        if [[ -n "$zoekt_volumes" ]]; then
            run "docker volume rm $zoekt_volumes 2>/dev/null || true"
        fi
    fi

    purge_leftovers
fi

info "Uninstall complete."
