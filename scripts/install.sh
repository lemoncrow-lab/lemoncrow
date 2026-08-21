#!/usr/bin/env bash
# install.sh — Standalone LemonCrow production bootstrap.
# Downloads a pre-compiled LemonCrow binary for your platform from the
# latest GitHub release, installs LemonCrow-managed files under ~/.lemoncrow/,
# and then installs LemonCrow into each detected agent host (Claude, Copilot,
# Cursor, Codex, etc.).
#
# Usage:
#   curl -fsSL https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download/install.sh | bash
#
# For a comprehensive developer install (with uv, git, node, etc.) use
# scripts/local.sh from the repo checkout.
#
#   LEMONCROW_INSTALL_DIR     Target directory (default: ~/.lemoncrow/install)
#   LEMONCROW_BIN_DIR         Binary directory (default: ~/.lemoncrow/bin)
#   LEMONCROW_RELEASE_TAG     Release tag to install (default: latest)
#   LEMONCROW_RELEASE_TAG     Release tag to install (default: latest)
#   LEMONCROW_DRY_RUN         If set to 1, print planned actions and exit
#   LEMONCROW_VERBOSE         If set to 1, show verbose output
#   LEMONCROW_NON_INTERACTIVE If set to 1, skip all prompts (auto-install all hosts)
#   LEMONCROW_NO_PATH         If set to 1, skip adding to PATH
#   LEMONCROW_NO_HOSTS        If set to 1, skip ALL post-extract setup (bundle.sh): host
#                           integrations AND dependency installs (uv/node/jj/rtk) are
#                           skipped — download & extract only
#   LEMONCROW_INSTALL_RTK     1 = install rtk without prompting, 0 = never offer
#                           (default: prompt during interactive setup when cargo exists;
#                           handled by bundle.sh / lib/common.sh, propagated via env)
#   LEMONCROW_RTK_TAG         rtk release tag to install (default: pinned in
#                           lib/common.sh; empty = unpinned default-branch HEAD)
#   LEMONCROW_KB_EXTRACT      If set to 1, run knowledge extraction during setup (opt-in)
#   LEMONCROW_KB_HOST         Extraction backend: auto | claude | codex | ollama
#   LEMONCROW_KB_MODEL        Model id for extraction (required for ollama)
#   LEMONCROW_KB_MAX_SPEND    Hard USD cap per extraction run (auto/claude)
#   LEMONCROW_RECALL_INDEX    SessionStart background recall indexer: on by default (set to 0 to disable)
#   LEMONCROW_RECALL_EMBEDDER Recall embedder: local | openai (codex) | ollama (Claude has no embeddings API)
#   LEMONCROW_RECALL_EMBED_MODEL  Embed model id (e.g. an ollama model name)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# ---- paths & detection ------------------------------------------------------
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
    amd64) ARCH="x86_64" ;;
    arm64) ARCH="arm64" ;;
    aarch64) ARCH="aarch64" ;;
esac
BINARY_SUFFIX="${OS}-${ARCH}"

LEMONCROW_INSTALL_DIR="${LEMONCROW_INSTALL_DIR:-${HOME}/.lemoncrow/install}"
LEMONCROW_BIN_DIR="${LEMONCROW_BIN_DIR:-${HOME}/.lemoncrow/bin}"
LEMONCROW_RELEASE_TAG="${LEMONCROW_RELEASE_TAG:-latest}"
LEMONCROW_DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
LEMONCROW_VERBOSE="${LEMONCROW_VERBOSE:-0}"
LEMONCROW_NON_INTERACTIVE="${LEMONCROW_NON_INTERACTIVE:-0}"
LEMONCROW_NO_PATH="${LEMONCROW_NO_PATH:-0}"
LEMONCROW_NO_HOSTS="${LEMONCROW_NO_HOSTS:-0}"
LEMONCROW_ALLOW_UNVERIFIED="${LEMONCROW_ALLOW_UNVERIFIED:-0}"
LEMONCROW_LOCAL="${LEMONCROW_LOCAL:-0}"
# Default source for --local: the bundle/ directory produced by 'make build',
# which lives one level up from this script (i.e. <repo>/bundle/).
LEMONCROW_LOCAL_SRC="${LEMONCROW_LOCAL_SRC:-${SCRIPT_DIR}/../bundle}"

# Handle arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local) LEMONCROW_LOCAL=1; shift ;;
        *) shift ;;
    esac
done

if [[ "$LEMONCROW_RELEASE_TAG" == "latest" ]]; then
    RELEASE_BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
else
    RELEASE_BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/download/${LEMONCROW_RELEASE_TAG}"
fi
ASSET_NAME="lemoncrow-distribution-${BINARY_SUFFIX}.tar.gz"
RELEASE_URL="${RELEASE_BASE_URL}/${ASSET_NAME}"

# ---- colour + helpers -------------------------------------------------------
# NOTE: info/warn/error/fail/need_cmd intentionally duplicate scripts/lib/common.sh.
# install.sh runs BEFORE the distribution archive is downloaded and extracted, so
# lib/common.sh does not exist on disk yet and cannot be sourced here. Do not
# "deduplicate" these helpers into common.sh.
if [[ -t 2 ]]; then
    _CP=$'\033[38;5;141m'   # brand purple
    _CD=$'\033[2m'          # dim
    _CB=$'\033[1m'          # bold
    _CG=$'\033[32m'         # green
    _CY=$'\033[33m'         # yellow
    _CR=$'\033[31m'         # red
    _C0=$'\033[0m'          # reset
else
    _CP='' _CD='' _CB='' _CG='' _CY='' _CR='' _C0=''
fi

info()    { printf "  ${_CP}◇${_C0}  %s\n" "$*"; }
warn()    { printf "  ${_CY}⚠${_C0}  %s\n" "$*" >&2; }
error()   { printf "  ${_CR}✗${_C0}  %s\n" "$*" >&2; }
fail()    { error "$*"; exit 1; }
verbose() { [[ "$LEMONCROW_VERBOSE" == "1" ]] && info "$*" || true; }

# _bar <current> <total> [width=40]
_bar() {
    local cur=$1 tot=$2 w=${3:-40}
    (( tot <= 0 )) && tot=1
    local f=$(( cur * w / tot ))
    (( f > w )) && f=$w
    local e=$(( w - f ))
    local i s='' b=''
    for (( i=0; i<f; i++ )); do s+='█'; done
    for (( i=0; i<e; i++ )); do b+='░'; done
    printf "${_CP}%s${_CD}%s${_C0}" "$s" "$b"
}

# _hum <bytes>  — human-readable size
_hum() {
    local b=$1
    if   (( b >= 1073741824 )); then printf '%d.%dG' $(( b/1073741824 )) $(( (b%1073741824)*10/1073741824 ))
    elif (( b >= 1048576    )); then printf '%d.%dM' $(( b/1048576    )) $(( (b%1048576)*10/1048576     ))
    elif (( b >= 1024       )); then printf '%d.%dK' $(( b/1024       )) $(( (b%1024)*10/1024           ))
    else printf '%dB' "$b"; fi
}

# _fsize <file>  — portable file size
_fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || echo 0; }

# _dl_progress <url> <dest>  — download with live progress bar
_dl_progress() {
    local url=$1 dest=$2
    # Probe Content-Length (non-fatal)
    local total=0
    if command -v curl >/dev/null 2>&1; then
        total=$(curl -fsIL --max-time 5 "$url" 2>/dev/null \
            | tr -d '\r' | awk 'tolower($1)=="content-length:" {print $2}' | tail -1)
    fi
    total=${total:-0}

    # Always download silently
    curl -fLs --retry 3 --retry-delay 2 --connect-timeout 15 "$url" > "$dest" &
    local pid=$!

    if [[ -t 2 && "$total" -gt 0 ]]; then
        local cur=0 prev=0 t0=$SECONDS
        while kill -0 "$pid" 2>/dev/null; do
            cur=$(_fsize "$dest")
            local pct=$(( cur * 100 / total ))
            (( pct > 100 )) && pct=100
            local speed=''
            local dt=$(( SECONDS - t0 ))
            if (( dt > 0 )); then
                speed="  $(_hum $(( cur / dt )))/s"
            fi
            printf "\r     $(_bar "$cur" "$total")  %3d%%  $(_hum "$cur") / $(_hum "$total")%s" \
                "$pct" "$speed" >&2
            sleep 0.12
        done
        wait "$pid"; local rc=$?
        printf "\r     $(_bar "$total" "$total")  100%%  $(_hum "$total") ${_CG}✓${_C0}\n" >&2
        return $rc
    else
        wait "$pid"
    fi
}

# _extract_progress <archive> <dest>  — extract with live file-count progress bar
_extract_progress() {
    local arc=$1 dest=$2
    if [[ ! -t 2 ]]; then
        tar -xzf "$arc" -C "$dest"
        return $?
    fi
    local total
    total=$(tar -tzf "$arc" 2>/dev/null | wc -l | tr -d ' ')
    (( total <= 0 )) && total=1
    local n=0
    while IFS= read -r _; do
        (( n++ )) || true
        local pct=$(( n * 100 / total ))
        (( pct > 100 )) && pct=100
        printf "\r     $(_bar "$n" "$total")  %3d%%" "$pct" >&2
    done < <(tar -xvzf "$arc" -C "$dest" 2>&1)
    printf "\r     $(_bar "$total" "$total")  100%% ${_CG}✓${_C0}\n" >&2
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

# verify_checksum <archive> <url>
# Verifies <archive> against a published <url>.sha256 sidecar. Fails closed:
# if the checksum cannot be fetched or does not match, the install aborts
# unless LEMONCROW_ALLOW_UNVERIFIED=1 is set to explicitly opt out.
# TODO: publish lemoncrow-distribution-*.tar.gz.sha256 sidecars in
# .github/workflows/release.yml so this verification is enforced by default.
verify_checksum() {
    local archive="$1" url="$2"
    local expected
    if ! expected="$("${DOWNLOAD_CMD[@]}" "${url}.sha256" 2>/dev/null)"; then
        expected=""
    fi
    # Accept both `<hash>  file` and `SHA256 (file) = <hash>` formats.
    expected="$(printf '%s' "$expected" | grep -oE '[0-9a-fA-F]{64}' | head -1 | tr 'A-F' 'a-f')"
    if [[ -z "$expected" ]]; then
        if [[ "$LEMONCROW_ALLOW_UNVERIFIED" == "1" ]]; then
            warn "No published checksum at ${url}.sha256 — proceeding unverified (LEMONCROW_ALLOW_UNVERIFIED=1)."
            return 0
        fi
        warn "No published checksum at ${url}.sha256 — skipping verification and proceeding."
        return 0
    fi
    local actual
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$archive" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        actual="$(shasum -a 256 "$archive" | awk '{print $1}')"
    else
        fail "Cannot verify checksum: neither sha256sum nor shasum is available."
    fi
    if [[ "$actual" != "$expected" ]]; then
        fail "Checksum mismatch for ${archive}: expected ${expected}, got ${actual}. Aborting."
    fi
    verbose "Checksum verified: ${actual}"
}

run() {
    if [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

# ---- platform check ----------------------------------------------------------
case "$OS" in
    linux|darwin) ;;
    *) fail "Unsupported OS: $OS. LemonCrow supports Linux and macOS." ;;
esac

case "$ARCH" in
    x86_64|aarch64|arm64) ;;
    *) fail "Unsupported architecture: $ARCH" ;;
esac

# ---- prerequisites (bash + curl/wget) ----------------------------------------
need_cmd bash
need_cmd tar

if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    fail "Either curl or wget is required to download the LemonCrow binary."
fi
# DOWNLOAD_CMD used only for checksum sidecar fetch (small, no progress needed)
if command -v curl >/dev/null 2>&1; then
    DOWNLOAD_CMD=(curl -fLs --retry 3 --retry-delay 2 --connect-timeout 15)
else
    DOWNLOAD_CMD=(wget -qO-)
fi

# ---- single-installer lock ---------------------------------------------------
# Two installers on one machine share $LEMONCROW_INSTALL_DIR as mutable state:
# `make prod` (local bundle), the published curl installer, `lc update`, and the
# controller's auto-update all clean and repopulate that tree. Whichever starts
# second overwrites the first one's tree *while it is running*, so the first
# installs the other's wheel and delegates to the other's host scripts (the
# "Unknown option: --lemoncode" failure). Serialize instead: later installers
# wait rather than clobber. Held for this whole script, bundle.sh included.
_acquire_install_lock() {
    [[ "${LEMONCROW_INSTALL_LOCK_HELD:-0}" == "1" ]] && return 0
    command -v flock >/dev/null 2>&1 || return 0
    mkdir -p "$(dirname "$LEMONCROW_INSTALL_DIR")" 2>/dev/null || true
    exec 9>"${LEMONCROW_INSTALL_DIR%/}.lock" 2>/dev/null || return 0
    if ! flock -n 9 2>/dev/null; then
        info "Another LemonCrow installer is running — waiting for it to finish…"
        flock 9 || return 0
    fi
    export LEMONCROW_INSTALL_LOCK_HELD=1
}
_acquire_install_lock

# ---- install-tree fingerprint ------------------------------------------------
# Record who wrote this tree. bundle.sh re-checks it before installing the wheel
# and before installing hosts, so a tree swapped out mid-run names the process
# that did it instead of surfacing as a confusing error from an older script
# ("Unknown option: --lemoncode").
_write_install_stamp() {
    # Exported so bundle.sh/lib/common.sh can restore this tree if another
    # installer overwrites it mid-run — the on-disk stamp is what gets clobbered.
    export LEMONCROW_INSTALL_SOURCE="${1:-unknown}"
    printf 'pid=%s ppid=%s written=%s source=%s\n' \
        "$$" "${PPID:-0}" "$(date -Iseconds 2>/dev/null || date)" "${1:-unknown}" \
        >"${LEMONCROW_INSTALL_DIR}/.lemoncrow-install-stamp" 2>/dev/null || true
}
_clean_managed_install_tree() {
    # The distribution is extracted as a directory tree, not as a versioned
    # package directory. Remove managed top-level payloads first so files deleted
    # from a release cannot linger across installs.
    mkdir -p "$LEMONCROW_INSTALL_DIR"
    local path
    for path in \
        "$LEMONCROW_INSTALL_DIR/bin" \
        "$LEMONCROW_INSTALL_DIR/constraints.txt" \
        "$LEMONCROW_INSTALL_DIR/constraints.resolved.txt" \
        "$LEMONCROW_INSTALL_DIR/deploy" \
        "$LEMONCROW_INSTALL_DIR/integrations" \
        "$LEMONCROW_INSTALL_DIR/scripts" \
        "$LEMONCROW_INSTALL_DIR/vendor"
    do
        if [[ -e "$path" || -L "$path" ]]; then
            rm -rf -- "$path"
        fi
    done
}

# ---- download & extract ------------------------------------------------------
if [[ "$LEMONCROW_LOCAL" == "1" ]]; then
    LOCAL_SRC_ABS="$(cd "${LEMONCROW_LOCAL_SRC}" 2>/dev/null && pwd)" \
        || fail "Local bundle not found at '${LEMONCROW_LOCAL_SRC}'. Run 'make build' first."
    mkdir -p "${LEMONCROW_INSTALL_DIR}"
    INSTALL_DIR_ABS="$(cd "${LEMONCROW_INSTALL_DIR}" && pwd)"
    if [[ "${LOCAL_SRC_ABS}" != "${INSTALL_DIR_ABS}" ]]; then
        _clean_managed_install_tree
        cp -r "${LOCAL_SRC_ABS}/." "${INSTALL_DIR_ABS}/"
    fi
    _write_install_stamp "local:${LOCAL_SRC_ABS}"
elif [[ "$LEMONCROW_DRY_RUN" == "1" ]]; then
    echo "  [dry-run] ${DOWNLOAD_CMD[*]} $RELEASE_URL > /tmp/${ASSET_NAME}"
    echo "  [dry-run] tar -xzf /tmp/${ASSET_NAME} -C $LEMONCROW_INSTALL_DIR"
    echo "  [dry-run] Binaries would be installed to: $LEMONCROW_BIN_DIR"
    echo ""
    exit 0
else
    mkdir -p "$LEMONCROW_BIN_DIR"
    TMP_ARCHIVE="$(mktemp -t lemoncrow-binaries.XXXXXX.tar.gz)"
    trap 'rm -f "$TMP_ARCHIVE"' EXIT

    verbose "Downloading from: $RELEASE_URL"
    printf "  ${_CP}◇${_C0}  ${_CB}Downloading${_C0} LemonCrow %s  ${_CD}(%s)${_C0}\n" \
        "${LEMONCROW_RELEASE_TAG}" "${BINARY_SUFFIX}" >&2
    if ! _dl_progress "$RELEASE_URL" "$TMP_ARCHIVE"; then
        fail "Could not download ${ASSET_NAME}. The release may not include this platform asset yet: ${RELEASE_URL}"
    fi

    if [[ ! -s "$TMP_ARCHIVE" ]]; then
        fail "Downloaded archive is empty: ${RELEASE_URL}"
    fi

    verify_checksum "$TMP_ARCHIVE" "$RELEASE_URL"
    _clean_managed_install_tree

    printf "  ${_CP}◇${_C0}  ${_CB}Extracting${_C0}\n" >&2
    _extract_progress "$TMP_ARCHIVE" "$LEMONCROW_INSTALL_DIR"
    _write_install_stamp "release:${RELEASE_URL}"

    info "Distribution extracted to: ${LEMONCROW_INSTALL_DIR}"
fi

# ---- run full setup via bundle.sh (installs wheel + host integrations) ------
# ---- run full setup via bundle.sh (installs wheel + optional host integrations) ----
# The wheel installation is mandatory. --no-hosts only suppresses host integration
# setup inside bundle.sh; it must never skip installing LemonCrow itself.
export PATH="${LEMONCROW_BIN_DIR}:${PATH}"
BUNDLE_SH="${LEMONCROW_INSTALL_DIR}/scripts/bundle.sh"
if [[ -f "$BUNDLE_SH" ]]; then
    SETUP_ARGS=()
    [[ "$LEMONCROW_DRY_RUN" == "1" ]] && SETUP_ARGS+=(--dry-run)
    [[ "$LEMONCROW_NON_INTERACTIVE" == "1" ]] && SETUP_ARGS+=(--non-interactive)
    [[ "$LEMONCROW_NO_HOSTS" == "1" ]] && SETUP_ARGS+=(--no-hosts)
    # When piped from curl, bash reads install.sh from stdin (a pipe), so
    # bundle.sh inherits that pipe as fd 0. Give interactive setup a real TTY
    # when one is available; otherwise keep stdin as-is for CI/containers.
    if [[ ! -t 0 ]] && : </dev/tty 2>/dev/null; then
        LEMONCROW_INSTALL_DIR="$LEMONCROW_INSTALL_DIR" \
        LEMONCROW_BIN_DIR="$LEMONCROW_BIN_DIR" \
        bash "$BUNDLE_SH" "${SETUP_ARGS[@]+${SETUP_ARGS[@]}}" </dev/tty \
            || fail "LemonCrow setup failed while installing the bundled wheel."
    else
        LEMONCROW_INSTALL_DIR="$LEMONCROW_INSTALL_DIR" \
        LEMONCROW_BIN_DIR="$LEMONCROW_BIN_DIR" \
        bash "$BUNDLE_SH" "${SETUP_ARGS[@]+${SETUP_ARGS[@]}}" \
            || fail "LemonCrow setup failed while installing the bundled wheel."
    fi
else
    fail "bundle.sh not found at ${BUNDLE_SH} — the distribution archive is incomplete."
fi
# ---- PATH persistence --------------------------------------------------------
if [[ "$LEMONCROW_NO_PATH" != "1" ]]; then
    case "$(basename "${SHELL:-bash}")" in
        zsh)  PROFILE="${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) PROFILE="$HOME/.bashrc" ;;
        fish) PROFILE="$HOME/.config/fish/config.fish" ;;
        *)    PROFILE="$HOME/.profile" ;;
    esac

    if ! echo ":$PATH:" | grep -q ":${LEMONCROW_BIN_DIR}:"; then
        export PATH="${LEMONCROW_BIN_DIR}:${PATH}"
        info "Added ${LEMONCROW_BIN_DIR} to PATH for this session"
    fi

    # Symlink into ~/.local/bin so non-login shells (opencode MCP spawns) find lemoncrow
    LOCAL_BIN="${HOME}/.local/bin"
    if [[ ! -e "$LOCAL_BIN/lemoncrow" ]]; then
        mkdir -p "$LOCAL_BIN" 2>/dev/null || true
        ln -sf "${LEMONCROW_BIN_DIR}/lemoncrow" "$LOCAL_BIN/lemoncrow"
        info "Symlinked lemoncrow -> ${LOCAL_BIN}/lemoncrow"
    fi
    # Same for the short `lc` alias and the `lcd` daemon entrypoint -- MCP
    # host configs (e.g. Claude Code's mcpServers) invoke the bare `lc`
    # command, so it needs the same non-login-shell fallback as `lemoncrow`.
    for short_bin in lc lcd; do
        if [[ -e "${LEMONCROW_BIN_DIR}/${short_bin}" && ! -e "$LOCAL_BIN/${short_bin}" ]]; then
            mkdir -p "$LOCAL_BIN" 2>/dev/null || true
            ln -sf "${LEMONCROW_BIN_DIR}/${short_bin}" "$LOCAL_BIN/${short_bin}"
            info "Symlinked ${short_bin} -> ${LOCAL_BIN}/${short_bin}"
        fi
    done
    if [[ -f "$PROFILE" ]] && ! grep -q "lemoncrow.*PATH" "$PROFILE" 2>/dev/null; then
        {
            echo ""
            echo "# >>> lemoncrow >>>"
            echo "export PATH=\"${LEMONCROW_BIN_DIR}:\$PATH\""
            echo "# <<< lemoncrow <<<"
        } >> "$PROFILE"
        info "Added to PATH in ${PROFILE/#$HOME/~}"
    fi
fi

# ---- done --------------------------------------------------------------------
echo ""
cli="lemoncrow"
[[ "${LC_ALIAS_AVAILABLE:-0}" == "1" ]] && cli="lc"
if [[ -x "${LEMONCROW_BIN_DIR}/lemoncrow" ]] || command -v lc >/dev/null 2>&1 || ( command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^lemoncrow" ); then
    info "LemonCrow $("${LEMONCROW_BIN_DIR}/lemoncrow" --version 2>/dev/null || lc --version 2>/dev/null || echo '') ready!"
    echo ""
    echo "  Quick start:  ${cli} --help"
    echo "  Init runtime: ${cli} init"
    echo "  Docs:         https://docs.lemoncrow.com"
    echo "  Github:       https://github.com/lemoncrow-lab/lemoncrow"
else
    info "LemonCrow installed to ${LEMONCROW_BIN_DIR}"
    echo ""
    echo "  Restart your shell or run:"
    echo "    export PATH=\"${LEMONCROW_BIN_DIR}:\$PATH\""
    echo "    ${cli} --help"
fi
echo ""
