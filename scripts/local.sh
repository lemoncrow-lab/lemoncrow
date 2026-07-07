#!/usr/bin/env bash
# local.sh — Install Atelier from a local repository checkout.
#
# Usage (from repo root):
#   bash scripts/local.sh
#   bash scripts/local.sh --dry-run
#
# This script installs the Python package via uv, then runs the shared
# setup (code tools, host integrations, services). For binary-only
# installs see scripts/bundle.sh.
#
# All shared configuration, logging, prompts, and the run_setup()
# orchestrator live in scripts/lib/common.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# Caller-specific mode flags. ATELIER_BINARY_MODE is retained for
# compatibility but unused in source mode; ATELIER_LOCAL marks this as a
# source-checkout install so run_setup wires host configs into the repo.
ATELIER_BINARY_MODE="${ATELIER_BINARY_MODE:-0}"
ATELIER_LOCAL=1

# ---- source-only: Python package install ------------------------------------
# (install_uv_if_needed lives in lib/common.sh, shared with bundle.sh.)
install_console_scripts() {
    local extras="mcp,memory,smart,cloud,postgres,vector,parsers,rename"
    local package_spec="${ATELIER_INSTALL_DIR}[${extras}]"

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        stop_existing_atelier_processes
        printf '[dry-run] uv sync --frozen (prime cache from uv.lock)\n'
        printf '[dry-run] uv tool uninstall atelier (if present)\n'
        printf '[dry-run] UV_TOOL_BIN_DIR=%q UV_TOOL_DIR=%q uv tool install --force --editable' "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
        printf ' %q' "$package_spec"
        printf '\n'
        return
    fi

    # Use the project "as is": resolve from the committed uv.lock instead of
    # re-resolving from PyPI. `uv tool install` ignores uv.lock and always
    # resolves from scratch against the index, which is what causes the
    # "stuck resolving packages" hang on a cold/stale cache. Priming the
    # project environment with `--frozen` (no resolution, no network when the
    # cache is warm) pulls every locked wheel into ~/.cache/uv first, so the
    # subsequent `uv tool install` resolves entirely from cache.
    # NOTE: never pass --no-cache/--refresh/--upgrade here — those would defeat
    # the cache reuse this step exists to guarantee.
    if [[ -f "${ATELIER_INSTALL_DIR}/uv.lock" ]]; then
        verbose "Priming uv cache from uv.lock (uv sync --frozen)"
        ( cd "$ATELIER_INSTALL_DIR" && uv sync --frozen ) || \
            verbose "uv sync --frozen failed; falling back to fresh resolve"
    fi

    mkdir -p "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
    stop_existing_atelier_processes

    # Forcefully remove any existing manual wrappers to prevent uv collision
    rm -f "${ATELIER_BIN_DIR}/atelier"

    # Gracefully remove old installation first
    UV_TOOL_BIN_DIR="$ATELIER_BIN_DIR" \
        UV_TOOL_DIR="$ATELIER_TOOL_DIR" \
        uv tool uninstall atelier >/dev/null 2>&1 || true
    
    # Editable tool install: the on-PATH `atelier` (the MCP server Claude Code
    # launches) imports straight from this checkout, so a source edit goes live
    # on the next server/session restart -- no `make dev` re-run needed. Prod
    # (bundle.sh) installs a built wheel instead; only dev is editable.
    UV_TOOL_BIN_DIR="$ATELIER_BIN_DIR" \
        UV_TOOL_DIR="$ATELIER_TOOL_DIR" \
        ATELIER_SKIP_MYPYC=1 uv tool install --force --editable "$package_spec"

}

persist_install_record() {
    local record_dir
    record_dir="$(dirname "$ATELIER_INSTALL_RECORD")"

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] mkdir -p $record_dir"
        echo "[dry-run] printf '%s\\n' '$ATELIER_INSTALL_DIR' > '$ATELIER_INSTALL_RECORD'"
        return
    fi

    mkdir -p "$record_dir"
    printf '%s\n' "$ATELIER_INSTALL_DIR" > "$ATELIER_INSTALL_RECORD"
}

# ---- arg parsing (source-specific flags) ------------------------------------
# Parse flags relevant to source install (--local/--remote are no-ops here,
# everything else is forwarded to common vars already declared in common.sh).
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) ATELIER_DRY_RUN=1 ;;
        --no-hosts) ATELIER_NO_HOSTS=1 ;;
        --no-servicectl) ATELIER_NO_SERVICECTL=1 ;;
        --no-stack) ATELIER_NO_STACK=1 ;;
        --verbose|-v) ATELIER_VERBOSE=1 ;;
        --non-interactive) ATELIER_NON_INTERACTIVE=1 ;;
        --advanced) ATELIER_ADVANCED=1 ;;
        --memory) ATELIER_MEMORY_BACKEND="${2:-}"; shift ;;
        --memory=*) ATELIER_MEMORY_BACKEND="${1#--memory=}" ;;
        --telegraphic) ATELIER_TELEGRAPHIC="${2:-}"; shift ;;
        --telegraphic=*) ATELIER_TELEGRAPHIC="${1#--telegraphic=}" ;;
        --zoekt) ATELIER_ZOEKT=1 ;;
        --workspace) HOST_SCOPE_ARGS+=(--workspace "${2:-}"); shift ;;
        --workspace=*) HOST_SCOPE_ARGS+=(--workspace "${1#--workspace=}") ;;
        --all) HOST_FLAGS+=(--all) ;;
        --local|--remote|--no-local) : ;;  # no-op, always source mode
        *) : ;;
    esac
    shift
done

# ---- main -------------------------------------------------------------------
main() {
    need_cmd git
    need_cmd bash

    print_installer_header
    host_wizard
    prompt_memory_selection
    prompt_auto_optimize_selection
    prompt_local_zoekt_selection
    prompt_rtk_selection
    prompt_telegraphic_selection

    if supports_interactive_selector; then
        print_installer_footer
    fi

    case "$ATELIER_MEMORY_BACKEND" in
        letta|openmemory|"") ;;
        *) fail "--memory must be 'letta' or 'openmemory', got: '$ATELIER_MEMORY_BACKEND'" ;;
    esac
    [[ -n "$ATELIER_MEMORY_BACKEND" ]] && ATELIER_ADVANCED=1

    install_uv_if_needed
    install_node_if_needed

    ATELIER_INSTALL_DIR="$(pwd)"
    export ATELIER_INSTALL_DIR

    step_start "Installing Atelier"
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        install_console_scripts
    else
        spin_tail "Installing packages" install_console_scripts
    fi
    persist_install_record
    # Mark as a dev install so the MCP server enables debug logging automatically.
    # Production installs (bundle.sh / install.sh) never create this file.
    if [[ "$ATELIER_DRY_RUN" != "1" ]]; then
        mkdir -p "${HOME}/.atelier" && touch "${HOME}/.atelier/.dev_mode" 2>/dev/null || true
    fi
    step_done

    # run_setup sets FINAL_EXIT_CODE on partial failures and prints a full
    # report via print_final_report before returning. Prevent set -e from
    # killing the script early so the report always reaches the user.
    run_setup || true
    exit "${FINAL_EXIT_CODE:-0}"
}

main "$@"
