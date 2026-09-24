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
# Source/dev installs use the same thin-client topology as production local mode:
# one explicit loopback server on 7420, never the retired controller + 8787 stack.
LEMONCROW_INSTALL_MODE=local
export LEMONCROW_INSTALL_MODE

# ---- source-only: Python package install ------------------------------------
# (install_uv_if_needed lives in lib/common.sh, shared with bundle.sh.)
install_console_scripts() {
    # litellm is NOT optional in practice: the owned runtime's completion path
    # (gateway/cli/runtime.py) imports it for every model turn, so `lc code`
    local extras="mcp,memory,smart,cloud,postgres,vector,parsers,rename,litellm"
    local package_spec="${LEMONCROW_INSTALL_DIR}[${extras}]"
    local client_source="${LEMONCROW_INSTALL_DIR}/client"
    local client_wheel_dir=""
    local client_wheel=""
    local client_with_args=()
    local server_source="${LEMONCROW_INSTALL_DIR}/server"
    local server_wheel_dir=""
    local server_wheel=""
    local server_with_args=()
    local headroom_with_args=()
    if [[ "${LEMONCROW_DEV_HEADROOM_APPLY:-0}" == "1" ]]; then
        local headroom_version="${LEMONCROW_DEV_HEADROOM_VERSION:-0.37.0}"
        headroom_with_args=(--with "headroom-ai==${headroom_version}")
    fi
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        stop_existing_lemoncrow_processes
        printf '[dry-run] uv sync --frozen (prime cache from uv.lock)\n'
        printf '[dry-run] uv tool uninstall lemoncrow (if present)\n'
        printf '[dry-run] stop local Review server before replacing shared uv-tool environment\n'
        if [[ -f "${client_source}/pyproject.toml" ]]; then
            printf '[dry-run] uv build --wheel %q --out-dir <temp>\n' "$client_source"
        fi
        if [[ -f "${server_source}/pyproject.toml" ]]; then
            printf '[dry-run] uv build --wheel %q --out-dir <temp>\n' "$server_source"
        fi
        printf '[dry-run] UV_TOOL_BIN_DIR=%q UV_TOOL_DIR=%q uv tool install --force' "$LEMONCROW_BIN_DIR" "$LEMONCROW_TOOL_DIR"
        printf ' %q' "$package_spec"
        [[ -f "${client_source}/pyproject.toml" ]] && printf ' --with <built-client-wheel>'
        [[ -f "${server_source}/pyproject.toml" ]] && printf ' --with <built-server-wheel>'
        [[ "${LEMONCROW_DEV_HEADROOM_APPLY:-0}" == "1" ]] && printf ' --with %q' "headroom-ai==${LEMONCROW_DEV_HEADROOM_VERSION:-0.37.0}"
        printf '\n'
        return
    fi

    # Use the project "as is": resolve from the committed uv.lock instead of
    # re-resolving from PyPI. `uv tool install` ignores uv.lock and always
    # resolves from scratch against the index, which is what causes the
    # Use the project "as is": resolve from the committed uv.lock instead of
    # re-resolving from PyPI. uv tool install ignores uv.lock and always
    # resolves from scratch against the index, which is what causes the
    # "stuck resolving packages" hang on a cold/stale cache. Priming the
    # project environment with --frozen (no resolution, no network when the
    # cache is warm) pulls every locked wheel into ~/.cache/uv first so the
    # subsequent uv tool install resolves entirely from cache.
    # NOTE: never pass --no-cache/--refresh/--upgrade here.
    if [[ -f "${LEMONCROW_INSTALL_DIR}/uv.lock" ]]; then
        verbose "Priming uv cache from uv.lock (uv sync --frozen)"
        ( cd "$LEMONCROW_INSTALL_DIR" && uv sync --frozen ) || \
            verbose "uv sync --frozen failed; falling back to fresh resolve"
    fi

    # uv tool install resolves the main project's published dependencies rather
    # than honoring this checkout's workspace source overrides. Snapshot the thin
    # client explicitly or a dev install can combine new LemonCrow code with an
    # older registry client and fail at runtime on newly added client methods.
    if [[ -f "${client_source}/pyproject.toml" ]]; then
        client_wheel_dir="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-client-wheel.XXXXXX")"
        uv build --wheel "$client_source" --out-dir "$client_wheel_dir" >/dev/null
        client_wheel="$(find "$client_wheel_dir" -maxdepth 1 -name 'lemoncrow_client-*.whl' -print -quit)"
        [[ -n "$client_wheel" ]] || fail "Could not build the local LemonCrow client wheel."
        client_with_args=(--with "$client_wheel")
    fi

    if [[ -f "${server_source}/pyproject.toml" ]]; then
        # Build the public server to a wheel before installing it into the
        # shared tool environment. Passing its source directory directly to
        # uv tool install --with would honor its workspace source override and
        # turn the supposedly stable LemonCrow snapshot back into an editable
        # install. The wheel carries only normal package metadata, matching prod.
        server_wheel_dir="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-server-wheel.XXXXXX")"
        uv build --wheel "$server_source" --out-dir "$server_wheel_dir" >/dev/null
        server_wheel="$(find "$server_wheel_dir" -maxdepth 1 -name 'lemoncrow_server-*.whl' -print -quit)"
        [[ -n "$server_wheel" ]] || fail "Could not build the local Review server wheel."
        server_with_args=(--with "$server_wheel")
    fi

    mkdir -p "$LEMONCROW_BIN_DIR" "$LEMONCROW_TOOL_DIR"
    stop_existing_lemoncrow_processes

    # Forcefully remove any existing manual wrappers to prevent uv collision
    rm -f "${LEMONCROW_BIN_DIR}/lemoncrow"

    # Gracefully remove old installation first
    UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" \
        UV_TOOL_DIR="$LEMONCROW_TOOL_DIR" \
        uv tool uninstall lemoncrow >/dev/null 2>&1 || true
    
    # --no-sources is load-bearing here: the root checkout maps
    # lemoncrow-client to ./client in tool.uv.sources, while we deliberately
    # inject the built client wheel below. Letting both URLs reach uv makes the
    # dev installer fail with "conflicting URLs for lemoncrow-client".
    # Snapshot (NON-editable) tool install: the on-PATH `lc` (the MCP server
    # Claude Code launches) is a COPY of this checkout in its own venv, not a
    # live link into the repo. A source edit does NOT affect the running tool
    # Prod (bundle.sh) installs a built wheel; dev installs a source copy.
    # ENABLE_MYPYC=0 keeps the dev install fast and source-live: dev never builds a
    # compiled wheel, so a half-finished edit is never baked into a .so and no C
    # toolchain is needed here (pro/ ships as source, not compiled).
    UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" \
        UV_TOOL_DIR="$LEMONCROW_TOOL_DIR" \
        LEMONCROW_ENABLE_MYPYC=0 uv tool install --force --no-sources \
        "$package_spec" "${client_with_args[@]}" "${server_with_args[@]}" "${headroom_with_args[@]}"
    [[ -n "$client_wheel_dir" ]] && rm -rf "$client_wheel_dir"
    [[ -n "$server_wheel_dir" ]] && rm -rf "$server_wheel_dir"


}

source_server_available() {
    # Repository scripts are intentionally invoked through bash and need not
    # carry an executable bit in a source/archive checkout.
    [[ -f "${LEMONCROW_INSTALL_DIR}/server/pyproject.toml" && -f "${SCRIPT_DIR}/local_server.sh" ]]
}

stop_local_server_for_install() {
    source_server_available || return 0
    LEMONCROW_TOOL_DIR="$LEMONCROW_TOOL_DIR" bash "${SCRIPT_DIR}/local_server.sh" stop || true
}

report_dev_headroom_installation() {
    [[ "${LEMONCROW_DEV_HEADROOM_APPLY:-0}" == "1" ]] || return 0
    local tool_python="${LEMONCROW_TOOL_DIR}/lemoncrow/bin/python"
    local headroom_version
    [[ -x "$tool_python" ]] || fail "Dev Headroom apply: LemonCrow tool Python not found at $tool_python"
    headroom_version="$("$tool_python" -c 'from importlib.metadata import version; print(version("headroom-ai"))')" \
        || fail "Dev Headroom apply: headroom-ai is not importable from the LemonCrow tool environment"
    _SPINNER_MSG="Headroom installed (headroom-ai ${headroom_version})"
    _spinner_stop ok
}

restart_local_server_after_install() {
    source_server_available || return 0
    local frontend_dir="${LEMONCROW_INSTALL_DIR}/frontend/dist"
    if [[ ! -f "${frontend_dir}/index.html" ]]; then
        frontend_dir="${HOME}/.lemoncrow/install/frontend"
    fi
    if [[ -f "${frontend_dir}/index.html" ]]; then
        spin_tail "LemonCrow local server ready at http://127.0.0.1:${LEMONCROW_LOCAL_SERVER_PORT:-7420}" \
            env LEMONCROW_TOOL_DIR="$LEMONCROW_TOOL_DIR" LEMONCROW_FRONTEND_DIR="$frontend_dir" \
            bash "${SCRIPT_DIR}/local_server.sh" restart
    else
        warn "Local Review server was stopped for the dev install, but no frontend bundle is available; run 'npm --prefix frontend run build' and restart scripts/local_server.sh."
    fi
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

    # `make dev` used to reinstall lemoncrow-controller/lemoncrow-stack after
    # migration cleanup. Retire those exact legacy units here too; the cleanup
    # script deliberately preserves supported lemoncrow-mcp-* services.
    if [[ -x "${SCRIPT_DIR}/cleanup_legacy_runtime.sh" ]]; then
        LEMONCROW_DRY_RUN="$LEMONCROW_DRY_RUN" bash "${SCRIPT_DIR}/cleanup_legacy_runtime.sh"
    fi

    print_installer_header
    host_wizard
    prompt_memory_selection
    prompt_local_zoekt_selection
    prompt_rtk_selection
    # No update question here: this is the dev install (it writes ~/.lemoncrow/.dev_mode),
    # which never checks for or applies updates. Release installs (bundle.sh) ask.

    if supports_interactive_selector; then
        print_installer_footer
    fi

    case "$LEMONCROW_MEMORY_BACKEND" in
        letta|openmemory|"") ;;
        *) fail "--memory must be 'letta' or 'openmemory', got: '$LEMONCROW_MEMORY_BACKEND'" ;;
    esac
    [[ -n "$LEMONCROW_MEMORY_BACKEND" ]] && LEMONCROW_ADVANCED=1

    install_uv_if_needed

    LEMONCROW_INSTALL_DIR="$(pwd)"
    export LEMONCROW_INSTALL_DIR

    step_start "Installing tools"
    install_code_tools
    step_done
    LEMONCROW_CODE_TOOLS_INSTALLED=1

    _capture_install_previous_version
    step_start "Installing LemonCrow"
    warn_on_foreign_cli_collision
    if [[ "${LEMONCROW_DRY_RUN:-0}" == "1" ]]; then
        install_console_scripts
    else
        # systemd lifecycle operations run in this installer shell. Running
        # them inside spin_tail's background subshell made a dev install able
        # to replace the uv environment while the old server kept running.
        stop_local_server_for_install
        spin_tail "Installing packages" install_console_scripts
        report_dev_headroom_installation
        restart_local_server_after_install
    fi
    ensure_lc_alias
    _ensure_path_persistence
    LEMONCROW_PATH_PERSISTED=1
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
