#!/usr/bin/env bash
# bundle.sh — Post-extract setup for a pre-built LemonCrow binary.
#
# Called by install.sh after the binary tarball has been extracted.
# LEMONCROW_INSTALL_DIR and LEMONCROW_BIN_DIR must already be set, and the
# LemonCrow binary must already exist at "$LEMONCROW_BIN_DIR/lemoncrow".
#
# Can also be called directly to re-run setup after a manual binary update:
#   LEMONCROW_INSTALL_DIR=~/.local LEMONCROW_BIN_DIR=~/.local/bin bash ~/.local/scripts/bundle.sh
#
# All shared configuration, logging, prompts, and the run_setup()
# orchestrator live in scripts/lib/common.sh. For source-checkout installs
# (uv tool install) see scripts/local.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# A distribution install is never a source checkout; keep host configs global
# unless an explicit --workspace is provided.
LEMONCROW_LOCAL=0
LEMONCROW_DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
LEMONCROW_PYTHON_VERSION="${LEMONCROW_PYTHON_VERSION:-3.13}"

# ---- arg parsing ------------------------------------------------------------
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
        *) : ;;
    esac
    shift
done

# ---- install LemonCrow from bundled wheel ------------------------------------
install_lemoncrow_from_wheel() {
    local wheel
    # Pick the highest-versioned wheel when multiple exist (old releases accumulate
    # in the bin dir across installs). `sort -V` sorts by version number so tail -1
    # always picks the newest regardless of filesystem directory order.
    # `|| true`: under set -euo pipefail a missing bin/ dir (e.g. a re-run from
    # a repo checkout) would abort the whole install instead of falling through
    # to the "no bundled wheel" path.
    wheel="$(find "${LEMONCROW_INSTALL_DIR}/bin" -maxdepth 1 -name "lemoncrow-*.whl" 2>/dev/null | sort -V | tail -1 || true)"
    if [[ -z "${wheel}" ]]; then
        verbose "No bundled wheel found — assuming LemonCrow already installed"
        persist_install_record
        return 0
    fi

    # Belt-and-suspenders for direct callers that skip main(): the shared
    # helper is a cheap no-op when uv is already on PATH.
    install_uv_if_needed

    if [[ "$LEMONCROW_DRY_RUN" != "1" ]]; then
        uv python install "$LEMONCROW_PYTHON_VERSION" >/dev/null 2>&1 || true
    fi

    # Pin every transitive dependency to its locked version via the constraints
    # file build.sh ships next to this script (<bundle>/constraints.txt). Without
    # it, `uv tool install` ignores uv.lock and resolves the wheel's unbounded
    # `>=` deps from scratch against PyPI (~293 packages) — the "stuck resolving
    # packages" hang on a cold machine. With `-c`, resolution is deterministic
    # and does no version search. This is the single install step shared by both
    # `make prod` and the distribution installer: install.sh only downloads and
    # extracts the bundle, then runs this exact script the same way.
    local constraints_arg=()
    if [[ -f "${SCRIPT_DIR}/../constraints.txt" ]]; then
        verbose "Using bundled dependency constraints"
        local constraints_file="${SCRIPT_DIR}/../constraints.txt"
        # uv export emits local-path deps (the babel stub) as a bare, unnamed,
        # build-machine-relative path -- `uv tool install -c` rejects unnamed
        # entries outright, and the relative path wouldn't resolve on this
        # machine anyway. Rewrite it to a named, absolute file:// URL pointing
        # at the wheel build.sh ships alongside constraints.txt.
        if grep -q "vendor/babel-" "${constraints_file}"; then
            constraints_file="${SCRIPT_DIR}/../constraints.resolved.txt"
            sed -E "s#^\\./?vendor/(babel-[^[:space:]]+\\.whl)\$#babel @ file://${SCRIPT_DIR}/../vendor/\\1#" \
                "${SCRIPT_DIR}/../constraints.txt" > "${constraints_file}"
        fi
        constraints_arg=(-c "${constraints_file}")
    fi

    local extras="mcp,memory,smart,cloud,postgres,vector,parsers,rename"
    stop_existing_lemoncrow_processes
    UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" UV_TOOL_DIR="$LEMONCROW_TOOL_DIR" \
        uv tool uninstall lemoncrow >/dev/null 2>&1 || true

    # Warn before we place the lemoncrow/lc console scripts if a foreign one is on PATH.
    warn_on_foreign_cli_collision

    # Install the console script to the configured LemonCrow bin/tool dirs.
    spin_tail "Installing LemonCrow" \
        env UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" UV_TOOL_DIR="$LEMONCROW_TOOL_DIR" \
        uv tool install --force --python "$LEMONCROW_PYTHON_VERSION" "${wheel}[${extras}]" ${constraints_arg[@]+"${constraints_arg[@]}"} --reinstall-package lemoncrow

    # Re-derive LEMONCROW_BIN_DIR to the uv tool install location so that
    # run_setup() finds the real lc binary (not the wheel-only staging dir).
    local uv_bin_dir
    uv_bin_dir="$(uv tool dir --bin 2>/dev/null || echo "${LEMONCROW_BIN_DIR}")"
    if [[ -x "${uv_bin_dir}/lemoncrow" ]]; then
        LEMONCROW_BIN_DIR="${uv_bin_dir}"
        export LEMONCROW_BIN_DIR
    else
        verbose "LemonCrow installed (binary not found in uv tool dir; using PATH fallback)"
    fi

    ensure_lc_alias

    persist_install_record

    # Remove stale wheels left over from previous installs so future runs
    # always see exactly one wheel and `sort -V | tail -1` can't pick a stale one.
    find "${LEMONCROW_INSTALL_DIR}/bin" -maxdepth 1 -name "lemoncrow-*.whl" \
        ! -name "$(basename "${wheel}")" -delete 2>/dev/null || true
}

# ---- main -------------------------------------------------------------------
main() {
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
    _capture_install_previous_version
    install_lemoncrow_from_wheel

    # Prevent set -e from aborting on partial failures (degrade() sets
    # FINAL_EXIT_CODE). Match local.sh pattern so the report always prints.
    run_setup || true
    exit "${FINAL_EXIT_CODE:-0}"
}

main "$@"
