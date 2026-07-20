#!/usr/bin/env bash
# local.sh — Install LemonCrow from a local repository checkout.
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

# Caller-specific mode flags. LEMONCROW_BINARY_MODE is retained for
# compatibility but unused in source mode; LEMONCROW_LOCAL marks this as a
# source-checkout install so run_setup wires host configs into the repo.
LEMONCROW_BINARY_MODE="${LEMONCROW_BINARY_MODE:-0}"
LEMONCROW_DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
LEMONCROW_LOCAL=1

# ---- source-only: Python package install ------------------------------------
# (install_uv_if_needed lives in lib/common.sh, shared with bundle.sh.)
install_console_scripts() {
    local extras="mcp,memory,smart,cloud,postgres,vector,parsers,rename"
    local package_spec="${LEMONCROW_INSTALL_DIR}[${extras}]"

    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        stop_existing_lemoncrow_processes
        printf '[dry-run] uv sync --frozen (prime cache from uv.lock)\n'
        printf '[dry-run] uv tool uninstall lemoncrow (if present)\n'
        printf '[dry-run] UV_TOOL_BIN_DIR=%q UV_TOOL_DIR=%q uv tool install --force' "$LEMONCROW_BIN_DIR" "$LEMONCROW_TOOL_DIR"
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
    if [[ -f "${LEMONCROW_INSTALL_DIR}/uv.lock" ]]; then
        verbose "Priming uv cache from uv.lock (uv sync --frozen)"
        ( cd "$LEMONCROW_INSTALL_DIR" && uv sync --frozen ) || \
            verbose "uv sync --frozen failed; falling back to fresh resolve"
    fi

    mkdir -p "$LEMONCROW_BIN_DIR" "$LEMONCROW_TOOL_DIR"
    stop_existing_lemoncrow_processes

    # Forcefully remove any existing manual wrappers to prevent uv collision
    rm -f "${LEMONCROW_BIN_DIR}/lemoncrow"

    # Gracefully remove old installation first
    UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" \
        UV_TOOL_DIR="$LEMONCROW_TOOL_DIR" \
        uv tool uninstall lemoncrow >/dev/null 2>&1 || true
    
    # Snapshot (NON-editable) tool install: the on-PATH `lc` (the MCP server
    # Claude Code launches) is a COPY of this checkout in its own venv, not a
    # live link into the repo. A source edit does NOT affect the running tool
    # until you re-run `make dev`. This is deliberate: a half-finished refactor
    # can never break the very tool (and Claude session) you're using to do it,
    # and the dev checkout is never touched by the auto-updater (.dev_mode).
    # Prod (bundle.sh) installs a built wheel; dev installs a source copy.
    # ENABLE_MYPYC=0 keeps the dev install fast and source-live: dev never builds a
    # compiled wheel, so a half-finished edit is never baked into a .so and no C
    # toolchain is needed here (pro/ ships as source, not compiled).
    UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" \
        UV_TOOL_DIR="$LEMONCROW_TOOL_DIR" \
        LEMONCROW_ENABLE_MYPYC=0 uv tool install --force "$package_spec"

}

persist_install_record() {
    local record_dir
    record_dir="$(dirname "$LEMONCROW_INSTALL_RECORD")"

    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "[dry-run] mkdir -p $record_dir"
        echo "[dry-run] printf '%s\\n' '$LEMONCROW_INSTALL_DIR' > '$LEMONCROW_INSTALL_RECORD'"
        return
    fi

    mkdir -p "$record_dir"
    printf '%s\n' "$LEMONCROW_INSTALL_DIR" > "$LEMONCROW_INSTALL_RECORD"
}

# ---- arg parsing (source-specific flags) ------------------------------------
# Parse flags relevant to source install (--local/--remote are no-ops here,
# everything else is forwarded to common vars already declared in common.sh).
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) LEMONCROW_DRY_RUN=1 ;;
        --no-hosts) LEMONCROW_NO_HOSTS=1 ;;
        --no-servicectl) LEMONCROW_NO_SERVICECTL=1 ;;
        --no-stack) LEMONCROW_NO_STACK=1 ;;
        --verbose|-v) LEMONCROW_VERBOSE=1 ;;
        --non-interactive) LEMONCROW_NON_INTERACTIVE=1 ;;
        --advanced) LEMONCROW_ADVANCED=1 ;;
        --memory) LEMONCROW_MEMORY_BACKEND="${2:-}"; shift ;;
        --memory=*) LEMONCROW_MEMORY_BACKEND="${1#--memory=}" ;;
        --telegraphic) LEMONCROW_TELEGRAPHIC="${2:-}"; shift ;;
        --telegraphic=*) LEMONCROW_TELEGRAPHIC="${1#--telegraphic=}" ;;
        --zoekt) LEMONCROW_ZOEKT=1 ;;
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

    if supports_interactive_selector; then
        print_installer_footer
    fi

    case "$LEMONCROW_MEMORY_BACKEND" in
        letta|openmemory|"") ;;
        *) fail "--memory must be 'letta' or 'openmemory', got: '$LEMONCROW_MEMORY_BACKEND'" ;;
    esac
    [[ -n "$LEMONCROW_MEMORY_BACKEND" ]] && LEMONCROW_ADVANCED=1

    install_uv_if_needed
    install_node_if_needed

    LEMONCROW_INSTALL_DIR="$(pwd)"
    export LEMONCROW_INSTALL_DIR
    _capture_install_previous_version

    step_start "Installing LemonCrow"
    warn_on_foreign_cli_collision
    if [[ "${LEMONCROW_DRY_RUN:-0}" == "1" ]]; then
        install_console_scripts
    else
        spin_tail "Installing packages" install_console_scripts
    fi
    ensure_lc_alias
    persist_install_record
    # Mark as a dev install so the MCP server enables debug logging automatically.
    # Production installs (bundle.sh / install.sh) never create this file.
    if [[ "${LEMONCROW_DRY_RUN:-0}" != "1" ]]; then
        mkdir -p "${HOME}/.lemoncrow" && touch "${HOME}/.lemoncrow/.dev_mode" 2>/dev/null || true
    fi
    step_done

    # run_setup sets FINAL_EXIT_CODE on partial failures and prints a full
    # report via print_final_report before returning. Prevent set -e from
    # killing the script early so the report always reaches the user.
    run_setup || true
    exit "${FINAL_EXIT_CODE:-0}"
}

main "$@"
