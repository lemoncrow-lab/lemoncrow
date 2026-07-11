#!/usr/bin/env bash
# uninstall.sh — Remove LemonCrow and all agent-host integrations
#
# Usage:
#   bash scripts/uninstall.sh [--dry-run] [--no-hosts] [--purge] [--workspace DIR]
#
# Optional environment variables:
#   LEMONCROW_BIN_DIR    Global bin dir for console scripts (default: ~/.local/bin)
#   LEMONCROW_TOOL_DIR   uv tool environment dir (default: ~/.local/share/uv/tools)
#   LEMONCROW_DRY_RUN    If set to 1, print planned actions and exit
#
# Notes:
#   Codex host uninstall removes only the managed LemonCrow AGENTS block when the
#   destination file uses explicit LemonCrow START/END sentinels.

set -euo pipefail

# ANSI Colors
if [[ -t 1 ]]; then
    C_RESET="$(printf '\033[0m')"
    C_DIM="$(printf '\033[2m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_PURPLE="$(printf '\033[38;2;155;117;217m')"
else
    C_RESET=""
    C_DIM=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
    C_PURPLE=""
fi
C_FRAME="$C_DIM"
LEMONCROW_BIN_DIR="${LEMONCROW_BIN_DIR:-${HOME}/.lemoncrow/bin}"
LEMONCROW_TOOL_DIR="${LEMONCROW_TOOL_DIR:-${HOME}/.lemoncrow/uv-tools}"
LEMONCROW_DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
LEMONCROW_NO_HOSTS="${LEMONCROW_NO_HOSTS:-0}"
LEMONCROW_INSTALL_RECORD="${HOME}/.lemoncrow/install_dir"
LEMONCROW_DEFAULT_INSTALL_DIR="${HOME}/.lemoncrow/install"
LEMONCROW_PROTECTED_SOURCE_ROOTS="${LEMONCROW_PROTECTED_SOURCE_ROOTS:-${HOME}/Projects}"
PASSTHROUGH=()
WORKSPACE_EXPLICIT=0
PURGE=0
DEFERRED_REMOVE_INSTALL_DIR=""

# Read the memory sidecar that was selected at install time (if any).
_MEMORY_BACKEND_FILE="${HOME}/.lemoncrow/memory_backend"
LEMONCROW_MEMORY_BACKEND=""
if [[ -f "$_MEMORY_BACKEND_FILE" ]]; then
    LEMONCROW_MEMORY_BACKEND="$(head -n 1 "$_MEMORY_BACKEND_FILE" 2>/dev/null | tr -d '[:space:]')"
fi

# Read the Zoekt selection that was persisted at install time (if any).
_ZOEKT_ENABLED_FILE="${HOME}/.lemoncrow/zoekt_enabled"
LEMONCROW_ZOEKT=""
if [[ -f "$_ZOEKT_ENABLED_FILE" ]]; then
    LEMONCROW_ZOEKT="$(head -n 1 "$_ZOEKT_ENABLED_FILE" 2>/dev/null | tr -d '[:space:]')"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  LEMONCROW_DRY_RUN=1; PASSTHROUGH+=("$1") ;;
        --no-hosts) LEMONCROW_NO_HOSTS=1 ;;
        --purge)    PURGE=1 ;;
        --workspace)
            if [ $# -lt 2 ]; then echo "Missing value for --workspace" >&2; exit 1; fi
            PASSTHROUGH+=("$1" "$2"); WORKSPACE_EXPLICIT=1; shift ;;
        *) PASSTHROUGH+=("$1") ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf "%b│%b  ◇  %s\n" "$C_FRAME" "$C_RESET" "$*"; }
warn() { printf "%b│%b  %b⚠%b  %s\n" "$C_FRAME" "$C_RESET" "$C_YELLOW" "$C_RESET" "$*"; }
run()  { [[ "$LEMONCROW_DRY_RUN" == "1" ]] && echo "[dry-run] $*" || eval "$*"; }

remove_path() {
    local path="$1"
    if [ -e "$path" ] || [ -L "$path" ]; then
        run "rm -rf $(printf %q "$path")"
        info "Removed ${path}"
    fi
}

remove_file_if_lemoncrow() {
    local path="$1"
    [ -f "$path" ] || return 0
    grep -qi "lemoncrow" "$path" 2>/dev/null || return 0
    remove_path "$path"
}

remove_glob() {
    local pattern="$1"
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
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
    if [ -f "$LEMONCROW_INSTALL_RECORD" ]; then
        head -n 1 "$LEMONCROW_INSTALL_RECORD" 2>/dev/null || true
    fi
}

is_protected_source_path() {
    local path="$1"
    local root root_real path_real
    if [[ -d "$path" ]]; then
        path_real="$(cd "$path" && pwd -P)"
    else
        path_real="$path"
    fi

    IFS=':' read -r -a roots <<< "$LEMONCROW_PROTECTED_SOURCE_ROOTS"
    for root in "${roots[@]}"; do
        [[ -n "$root" ]] || continue
        if [[ -d "$root" ]]; then
            root_real="$(cd "$root" && pwd -P)"
        else
            root_real="$root"
        fi
        if [[ "$path_real" == "$root_real" || "$path_real" == "$root_real/"* ]]; then
            return 0
        fi
    done
    return 1
}

purge_leftovers() {
    local repo_root install_dir
    repo_root="$(cd "${SCRIPT_DIR}/.." && pwd)"
    install_dir="${LEMONCROW_INSTALL_DIR:-$(install_dir_from_record)}"
    install_dir="${install_dir:-$LEMONCROW_DEFAULT_INSTALL_DIR}"

    printf "%b│%b\n" "$C_FRAME" "$C_RESET"
    remove_path "${LEMONCROW_TOOL_DIR}/lemoncrow"
    remove_path "${LEMONCROW_TOOL_DIR}/lemoncrow"
    remove_path "${HOME}/.lemoncrow/uv-tools/lemoncrow"
    remove_path "${HOME}/.local/share/uv/tools/lemoncrow"
    remove_file_if_lemoncrow "${HOME}/.codex/AGENTS.md"
    remove_glob "${HOME}/.codex/AGENTS.md.lemoncrow-backup.*"
    remove_glob "${HOME}/.codex/plugins/lemoncrow*"
    remove_path "${HOME}/.codex/plugins/cache/lemoncrow"
    remove_path "${HOME}/.codex/plugins/cache/openai-curated/lemoncrow"
    if [ -f "${HOME}/.copilot/hooks/hooks.json" ] && grep -q "lemoncrow" "${HOME}/.copilot/hooks/hooks.json" 2>/dev/null; then
        run "rm -f $(printf %q "${HOME}/.copilot/hooks/hooks.json")"
        info "Removed LemonCrow Copilot CLI hooks config"
    fi

    if command -v npm >/dev/null 2>&1; then
        run "npm uninstall -g codeburn tokscale >/dev/null 2>&1 || true"
        info "Removed global npm helper packages installed by LemonCrow when present"
    fi

    remove_glob "${HOME}/.copilot/instructions/*lemoncrow*"
    remove_glob "${HOME}/.config/Code/User/*.lemoncrow-backup.*"

    # ---- memory sidecar Docker cleanup --------------------------------------
    case "$LEMONCROW_MEMORY_BACKEND" in
        letta)
            info "Removing Letta Docker container and volumes..."
            local letta_compose="${install_dir}/deploy/letta/docker-compose.yml"
            if [[ -f "$letta_compose" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose -f $(printf %q "$letta_compose") down -v --remove-orphans 2>/dev/null || true"
            else
                warn "Letta compose file not found or docker unavailable — Docker volumes may need manual removal"
                warn "  docker volume ls | grep letta"
            fi
            ;;
        openmemory)
            info "Removing OpenMemory Docker state and checkout..."
            local om_workdir="${HOME}/.lemoncrow/openmemory/mem0/openmemory"
            if [[ -d "$om_workdir" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose -C $(printf %q "$om_workdir") down -v --remove-orphans 2>/dev/null || true"
            fi
            remove_path "${HOME}/.lemoncrow/openmemory"
            ;;
    esac

    remove_path "${HOME}/.lemoncrow"

    if [ -n "$install_dir" ]; then
        local script_root_real install_dir_real
        script_root_real="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
        if [[ -d "$install_dir" ]]; then
            install_dir_real="$(cd "$install_dir" && pwd -P)"
        else
            install_dir_real="$install_dir"
        fi

        if is_protected_source_path "$install_dir_real"; then
            warn "Skipping install source under protected source root: $install_dir"
        elif [[ "$script_root_real" == "$install_dir_real" || "$script_root_real" == "$install_dir_real/"* || "$install_dir" == "$repo_root" || "$install_dir" == "$PWD" ]]; then
            if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
                warn "Will remove install source after script exits (deferred): $install_dir"
            else
                DEFERRED_REMOVE_INSTALL_DIR="$install_dir_real"
                warn "Deferring install source removal until script exit: $install_dir"
            fi
        elif [[ "$install_dir" == "$HOME/"* ]]; then
            remove_path "$install_dir"
        else
            warn "Skipping install source outside HOME: $install_dir"
        fi
    fi
}

# ---- stop memory sidecar (before background controller) --------------------
if command -v lemon &>/dev/null && [[ -n "$LEMONCROW_MEMORY_BACKEND" ]]; then
    case "$LEMONCROW_MEMORY_BACKEND" in
        letta)
            info "Stopping Letta memory sidecar..."
            run "lemon letta down 2>/dev/null || true"
            ;;
        openmemory)
            info "Stopping OpenMemory memory sidecar..."
            run "lemon openmemory down 2>/dev/null || true"
            ;;
        *)
            warn "Unknown memory backend '$LEMONCROW_MEMORY_BACKEND' in $_MEMORY_BACKEND_FILE - skipping sidecar teardown"
            ;;
    esac
fi

# ---- stop Zoekt sidecar (before background controller) ---------------------
if command -v lemon &>/dev/null && [[ "$LEMONCROW_ZOEKT" == "1" ]]; then
    info "Skipping Zoekt CLI teardown; Zoekt management commands are no longer exposed."
fi

# ---- stop running services --------------------------------------------------
if command -v lemon &>/dev/null; then
    case "$LEMONCROW_MEMORY_BACKEND" in
        letta)
            info "Stopping Letta memory sidecar..."
            run "lemon letta down 2>/dev/null || true"
            ;;
        openmemory)
            info "Stopping OpenMemory memory sidecar..."
            run "lemon openmemory down 2>/dev/null || true"
            ;;
        "")
            ;;
        *)
            warn "Unknown memory backend '$LEMONCROW_MEMORY_BACKEND' in $_MEMORY_BACKEND_FILE — skipping sidecar teardown"
            ;;
    esac

    info "Stopping LemonCrow background service controller..."
    run "lemon servicectl stop 2>/dev/null || true"
    info "Stopping LemonCrow visualization stack..."
    run "lemon stack stop 2>/dev/null || true"
else
    warn "lemon CLI not found on PATH — skipping service shutdown"
fi

# ---- per-host uninstallers --------------------------------------------------
if [[ "$LEMONCROW_NO_HOSTS" != "1" ]]; then
    for host in claude codex opencode copilot antigravity cursor hermes; do
        script="${SCRIPT_DIR}/uninstall_${host}.sh"
        [ -f "$script" ] || continue
        printf "%b│%b\n" "$C_FRAME" "$C_RESET"
        printf "%b┌%b  Uninstalling LemonCrow ← %s\n" "$C_FRAME" "$C_RESET" "$host"
        printf "%b│%b\n" "$C_FRAME" "$C_RESET"
        bash "$script" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} || true
        # Also clean workspace-local configs in CWD when no explicit --workspace given
        if [[ "$WORKSPACE_EXPLICIT" == "0" && "$PWD" != "$HOME" ]]; then
            local_args=(--workspace "$PWD")
            [[ "$LEMONCROW_DRY_RUN" == "1" ]] && local_args+=(--dry-run)
            bash "$script" "${local_args[@]}" 2>/dev/null || true
        fi
    done
    printf "%b│%b\n" "$C_FRAME" "$C_RESET"
else
    info "Skipping host integrations because LEMONCROW_NO_HOSTS=1"
fi

# ---- remove main bin commands ------------------------------------------------
info "Removing LemonCrow bin commands from ${LEMONCROW_BIN_DIR}..."
for cmd in lemon lc lemond; do
    target="${LEMONCROW_BIN_DIR}/${cmd}"
    if [ -f "$target" ] || [ -L "$target" ]; then
        run "rm -f $(printf %q "$target")"
        info "Removed ${target}"
    fi
done

# ---- remove PATH sentinel from shell profile --------------------------------
_remove_path_sentinel() {
    local sentinel_start="# >>> lemoncrow path setup >>>"
    local sentinel_end="# <<< lemoncrow path setup <<<"
    local profile_file shell_name

    shell_name="$(basename "${SHELL:-bash}")"
    case "$shell_name" in
        zsh)  profile_file="${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) profile_file="$HOME/.bashrc" ;;
        fish) profile_file="$HOME/.config/fish/config.fish" ;;
        *)    profile_file="$HOME/.profile" ;;
    esac

    [[ -f "$profile_file" ]] || return 0

    if grep -qF "$sentinel_start" "$profile_file" 2>/dev/null; then
        if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
            echo "[dry-run] Remove LemonCrow PATH block from ${profile_file}"
            return 0
        fi
        local tmp_file in_block line
        tmp_file="$(mktemp)"
        in_block=0
        while IFS= read -r line; do
            if [[ "$line" == "$sentinel_start" ]]; then
                in_block=1
            elif [[ "$line" == "$sentinel_end" ]]; then
                in_block=0
            elif [[ "$in_block" == "0" ]]; then
                printf '%s\n' "$line"
            fi
        done < "$profile_file" > "$tmp_file"
        mv "$tmp_file" "$profile_file"
        info "Removed LemonCrow PATH block from ${profile_file/#$HOME/~}"
    fi
}
_remove_path_sentinel

if [[ "$PURGE" == "1" ]]; then
    local_install_dir="${LEMONCROW_INSTALL_DIR:-$(install_dir_from_record)}"
    local_install_dir="${local_install_dir:-$LEMONCROW_DEFAULT_INSTALL_DIR}"

    # ---- memory sidecar Docker cleanup --------------------------------------
    case "$LEMONCROW_MEMORY_BACKEND" in
        letta)
            info "Removing Letta Docker container and volumes..."
            letta_compose="${local_install_dir}/deploy/letta/docker-compose.yml"
            if [[ -f "$letta_compose" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose -f $(printf %q "$letta_compose") down -v --remove-orphans 2>/dev/null || true"
            else
                warn "Letta compose file not found or docker unavailable - Docker volumes may need manual removal"
            fi
            ;;
        openmemory)
            info "Removing OpenMemory Docker state and checkout..."
            om_workdir="${HOME}/.lemoncrow/openmemory/mem0/openmemory"
            if [[ -d "$om_workdir" ]] && command -v docker >/dev/null 2>&1; then
                run "docker compose --project-directory $(printf %q "$om_workdir") down -v --remove-orphans 2>/dev/null || true"
            fi
            remove_path "${HOME}/.lemoncrow/openmemory"
            ;;
    esac

    purge_leftovers
fi

printf "%b│%b\n" "$C_FRAME" "$C_RESET"
info "Uninstall complete."

if [[ -n "$DEFERRED_REMOVE_INSTALL_DIR" ]]; then
    info "Scheduling deferred removal of install source: $DEFERRED_REMOVE_INSTALL_DIR"
    ( sleep 1; rm -rf -- "$DEFERRED_REMOVE_INSTALL_DIR" ) >/dev/null 2>&1 &
fi
