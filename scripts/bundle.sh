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
# Tell the parent installer (install.sh) which bin dir this run really installed
# into. LEMONCROW_BIN_DIR is re-pointed to `uv tool dir --bin` below, and that
# change lives only in this child process, so install.sh cannot verify the
# result without being told.
_record_resolved_bin_dir() {
    local dir="${1:-$LEMONCROW_BIN_DIR}"
    [[ "$LEMONCROW_DRY_RUN" == "1" ]] && return 0
    printf '%s\n' "$dir" >"${LEMONCROW_INSTALL_DIR%/}/.lemoncrow-bin-dir" 2>/dev/null || true
}

# Print an already-installed lemoncrow binary, but ONLY from a location this
# installer owns: $LEMONCROW_BIN_DIR, uv's tool bin dir, or somewhere under
# ~/.lemoncrow. Returns 1 when there is no such binary.
#
# An arbitrary `command -v lemoncrow` hit must NOT count. A pipx/brew/older
# foreign install anywhere on PATH would make a distribution that shipped no
# wheel look healthy, and its directory would then be recorded for install.sh,
# which reports that foreign binary's --version as "ready!" (GH #41 again).
_find_owned_lemoncrow_bin() {
    local candidate uv_bin_dir on_path
    uv_bin_dir="$(uv tool dir --bin 2>/dev/null || true)"
    for candidate in "${LEMONCROW_BIN_DIR:-}" "$uv_bin_dir" "${HOME:-}/.lemoncrow/bin"; do
        candidate="${candidate%/}"
        [[ -n "$candidate" && -x "${candidate}/lemoncrow" ]] || continue
        printf '%s\n' "${candidate}/lemoncrow"
        return 0
    done
    # A PATH hit counts only when it already lives inside the LemonCrow tree.
    on_path="$(command -v lemoncrow 2>/dev/null || true)"
    if [[ -n "$on_path" && -n "${HOME:-}" && "$on_path" == "${HOME%/}/.lemoncrow/"* ]]; then
        printf '%s\n' "$on_path"
        return 0
    fi
    return 1
}

install_lemoncrow_from_wheel() {
    local wheel="" source_dir=""
    # `make prod` must install the wheel it just built. Selecting by highest
    # version instead lets a leftover release wheel in the install tree win
    # (same or higher version, older code), so when this run came from a local
    # bundle, take that bundle's wheel by path and ignore everything else.
    [[ "${_INSTALL_TREE_SOURCE:-}" == local:* ]] && source_dir="${_INSTALL_TREE_SOURCE#local:}"
    if [[ -n "$source_dir" && -d "$source_dir/bin" ]]; then
        wheel="$(find "${source_dir}/bin" -maxdepth 1 -name "lemoncrow-*.whl" 2>/dev/null | sort -V | tail -1 || true)"
        [[ -n "$wheel" ]] && verbose "Installing the wheel built by this run: ${wheel}"
    fi
    # Distribution install (or a bundle without bin/): fall back to the extracted
    # tree. Pick the highest-versioned wheel when several have accumulated;
    # `sort -V` sorts by version so tail -1 wins regardless of directory order.
    # `|| true`: under set -euo pipefail a missing bin/ dir (e.g. a re-run from
    # a repo checkout) would abort the whole install instead of falling through
    # to the "no bundled wheel" path.
    if [[ -z "${wheel}" ]]; then
        wheel="$(find "${LEMONCROW_INSTALL_DIR}/bin" -maxdepth 1 -name "lemoncrow-*.whl" 2>/dev/null | sort -V | tail -1 || true)"
    fi
    if [[ -z "${wheel}" ]]; then
        # No wheel is only legitimate when LemonCrow is ALREADY installed where
        # THIS installer put it: a bundle.sh re-run after a manual binary
        # update, or a source checkout with no bundle/bin. For a distribution
        # install it means the archive shipped nothing to install, and returning
        # 0 here left an empty bin/, a dangling ~/.local/bin/lemoncrow and exit 0
        # (GH #41). A foreign lemoncrow merely on PATH is not an install we did.
        local existing=""
        existing="$(_find_owned_lemoncrow_bin || true)"
        [[ -n "$existing" ]] || fail "No LemonCrow wheel in ${LEMONCROW_INSTALL_DIR}/bin and no LemonCrow-managed lemoncrow binary already installed (looked in ${LEMONCROW_BIN_DIR}, uv's tool bin dir and ${HOME:-~}/.lemoncrow) — the distribution is incomplete. Re-download the release asset and re-run the installer."
        verbose "No bundled wheel found — LemonCrow already installed at ${existing}"
        _record_resolved_bin_dir "$(dirname "$existing")"
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

    # litellm is NOT optional in practice: the owned runtime's completion path
    # (gateway/cli/runtime.py) imports it for every model turn, so `lc code`
    # dies with "No module named 'litellm'" without it.
    local extras="mcp,memory,smart,cloud,postgres,vector,parsers,rename,litellm"
    stop_existing_lemoncrow_processes
    UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" UV_TOOL_DIR="$LEMONCROW_TOOL_DIR" \
        uv tool uninstall lemoncrow >/dev/null 2>&1 || true

    # Warn before we place the lemoncrow/lc console scripts if a foreign one is on PATH.
    warn_on_foreign_cli_collision

    # Install the console script to the configured LemonCrow bin/tool dirs.
    # uv occasionally has a stale record of a Python it thinks is installed but
    # whose executable is gone from disk (interrupted `uv python install`, a
    # system update that removed it, etc). `uv tool install` then aborts with
    # "The environment at `...` is invalid: missing python executable at ..."
    # instead of just fetching a working interpreter. Detect that failure and
    # force-reinstall the interpreter once before giving up.
    local uv_install_cmd=(
        env UV_TOOL_BIN_DIR="$LEMONCROW_BIN_DIR" UV_TOOL_DIR="$LEMONCROW_TOOL_DIR"
        uv tool install --force --python "$LEMONCROW_PYTHON_VERSION" "${wheel}[${extras}]" ${constraints_arg[@]+"${constraints_arg[@]}"} --reinstall-package lemoncrow
    )
    if ! spin_tail "Installing LemonCrow" "${uv_install_cmd[@]}"; then
        warn "LemonCrow install failed — Python ${LEMONCROW_PYTHON_VERSION} looks missing or broken; reinstalling it and retrying..."
        uv python install --reinstall "$LEMONCROW_PYTHON_VERSION" \
            || fail "Could not install Python ${LEMONCROW_PYTHON_VERSION} via uv. Install it manually (uv python install ${LEMONCROW_PYTHON_VERSION}) and re-run this installer."
        spin_tail "Installing LemonCrow (retry)" "${uv_install_cmd[@]}" \
            || fail "LemonCrow install failed even after reinstalling Python ${LEMONCROW_PYTHON_VERSION}."
    fi

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
    _record_resolved_bin_dir

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
    assert_install_tree_consistent

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
    assert_install_tree_unchanged "before-wheel-install"
    install_lemoncrow_from_wheel

    # Prevent set -e from aborting on partial failures (degrade() sets
    # FINAL_EXIT_CODE). Match local.sh pattern so the report always prints.
    run_setup || true
    exit "${FINAL_EXIT_CODE:-0}"
}

main "$@"
