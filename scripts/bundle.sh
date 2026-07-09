#!/usr/bin/env bash
# bundle.sh — Post-extract setup for a pre-built Atelier binary.
#
# Called by install.sh after the binary tarball has been extracted.
# ATELIER_INSTALL_DIR and ATELIER_BIN_DIR must already be set, and the
# Atelier binary must already exist at "$ATELIER_BIN_DIR/atelier".
#
# Can also be called directly to re-run setup after a manual binary update:
#   ATELIER_INSTALL_DIR=~/.local ATELIER_BIN_DIR=~/.local/bin bash ~/.local/scripts/bundle.sh
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
ATELIER_LOCAL=0
ATELIER_DRY_RUN="${ATELIER_DRY_RUN:-0}"
ATELIER_PYTHON_VERSION="${ATELIER_PYTHON_VERSION:-3.13}"

# ---- arg parsing ------------------------------------------------------------
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
        *) : ;;
    esac
    shift
done

# ---- install atelier from bundled wheel ------------------------------------
install_atelier_from_wheel() {
    local wheel
    # Pick the highest-versioned wheel when multiple exist (old releases accumulate
    # in the bin dir across installs). `sort -V` sorts by version number so tail -1
    # always picks the newest regardless of filesystem directory order.
    # `|| true`: under set -euo pipefail a missing bin/ dir (e.g. a re-run from
    # a repo checkout) would abort the whole install instead of falling through
    # to the "no bundled wheel" path.
    wheel="$(find "${ATELIER_INSTALL_DIR}/bin" -maxdepth 1 -name "atelier-*.whl" 2>/dev/null | sort -V | tail -1 || true)"
    if [[ -z "${wheel}" ]]; then
        verbose "No bundled wheel found — assuming atelier already installed"
        persist_install_record
        return 0
    fi

    # Belt-and-suspenders for direct callers that skip main(): the shared
    # helper is a cheap no-op when uv is already on PATH.
    install_uv_if_needed

    if [[ "$ATELIER_DRY_RUN" != "1" ]]; then
        uv python install "$ATELIER_PYTHON_VERSION" >/dev/null 2>&1 || true
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
    stop_existing_atelier_processes
    UV_TOOL_BIN_DIR="$ATELIER_BIN_DIR" UV_TOOL_DIR="$ATELIER_TOOL_DIR" \
        uv tool uninstall atelier >/dev/null 2>&1 || true

    # Install the console script to the configured Atelier bin/tool dirs.
    spin_tail "Installing Atelier" \
        env UV_TOOL_BIN_DIR="$ATELIER_BIN_DIR" UV_TOOL_DIR="$ATELIER_TOOL_DIR" \
        uv tool install --force --python "$ATELIER_PYTHON_VERSION" "${wheel}[${extras}]" ${constraints_arg[@]+"${constraints_arg[@]}"} --reinstall-package atelier

    # Re-derive ATELIER_BIN_DIR to the uv tool install location so that
    # run_setup() finds the real atelier binary (not the wheel-only staging dir).
    local uv_bin_dir
    uv_bin_dir="$(uv tool dir --bin 2>/dev/null || echo "${ATELIER_BIN_DIR}")"
    if [[ -x "${uv_bin_dir}/atelier" ]]; then
        ATELIER_BIN_DIR="${uv_bin_dir}"
        export ATELIER_BIN_DIR
    else
        verbose "atelier installed (binary not found in uv tool dir; using PATH fallback)"
    fi

    persist_install_record

    # Remove stale wheels left over from previous installs so future runs
    # always see exactly one wheel and `sort -V | tail -1` can't pick a stale one.
    find "${ATELIER_INSTALL_DIR}/bin" -maxdepth 1 -name "atelier-*.whl" \
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

    case "$ATELIER_MEMORY_BACKEND" in
        letta|openmemory|"") ;;
        *) fail "--memory must be 'letta' or 'openmemory', got: '$ATELIER_MEMORY_BACKEND'" ;;
    esac
    [[ -n "$ATELIER_MEMORY_BACKEND" ]] && ATELIER_ADVANCED=1

    install_uv_if_needed
    install_node_if_needed
    install_atelier_from_wheel

    # Prevent set -e from aborting on partial failures (degrade() sets
    # FINAL_EXIT_CODE). Match local.sh pattern so the report always prints.
    run_setup || true
    exit "${FINAL_EXIT_CODE:-0}"
}

main "$@"
