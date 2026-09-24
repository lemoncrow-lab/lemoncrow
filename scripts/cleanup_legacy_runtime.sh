#!/usr/bin/env bash
# cleanup_legacy_runtime.sh — temporary migration cleanup for pre-thin-client installs.
#
# Keep this script for a few releases while installations migrate. It removes
# only the retired controller/stack/global-MCP runtime and their stale state.
# Per-hostname `lemoncrow-mcp-*.service` / `com.lemoncrow.mcp.*` services are a
# supported product feature and are deliberately never matched or removed.
set -euo pipefail

HOME_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
LAUNCHD_USER_DIR="${HOME}/Library/LaunchAgents"
changed=0

say() { printf '  ◇  %s\n' "$*"; }

remove_path() {
    local path="$1"
    [[ -e "$path" || -L "$path" ]] || return 0
    changed=1
    if [[ "$DRY_RUN" == "1" ]]; then
        say "Would remove legacy LemonCrow state: $path"
    else
        rm -rf -- "$path"
    fi
}

cleanup_systemd() {
    local unit path
    # Exact names only. Never glob lemoncrow-mcp-*; those are current persistent
    # MCP publication services and must survive installs/upgrades.
    for unit in lemoncrow-controller.service lemoncrow-stack.service lemoncrow-mcp.service; do
        path="${SYSTEMD_USER_DIR}/${unit}"
        if [[ -e "$path" || -L "$path" || -L "${SYSTEMD_USER_DIR}/default.target.wants/${unit}" ]]; then
            changed=1
            if [[ "$DRY_RUN" == "1" ]]; then
                say "Would remove legacy user service: $unit"
            else
                if command -v systemctl >/dev/null 2>&1; then
                    systemctl --user disable --now "$unit" >/dev/null 2>&1 || systemctl --user stop "$unit" >/dev/null 2>&1 || true
                fi
                rm -f -- "$path" "${SYSTEMD_USER_DIR}/default.target.wants/${unit}"
            fi
        fi
    done
    if [[ "$DRY_RUN" != "1" ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user reset-failed lemoncrow-controller.service lemoncrow-stack.service lemoncrow-mcp.service >/dev/null 2>&1 || true
    fi
}

cleanup_launchd() {
    local label plist
    for label in com.lemoncrow.controller com.lemoncrow.stack com.lemoncrow.mcp; do
        plist="${LAUNCHD_USER_DIR}/${label}.plist"
        [[ -e "$plist" || -L "$plist" ]] || continue
        changed=1
        if [[ "$DRY_RUN" == "1" ]]; then
            say "Would remove legacy LaunchAgent: $label"
        else
            command -v launchctl >/dev/null 2>&1 && launchctl unload "$plist" >/dev/null 2>&1 || true
            rm -f -- "$plist"
        fi
    done
}

case "$(uname -s)" in
    Linux) cleanup_systemd ;;
    Darwin) cleanup_launchd ;;
esac

remove_path "${HOME_DIR}/servicectl"
remove_path "${HOME_DIR}/mcp_daemons"

if [[ "$changed" == "1" && "$DRY_RUN" != "1" ]]; then
    say "Removed retired LemonCrow runtime state; persistent MCP services were preserved."
fi
