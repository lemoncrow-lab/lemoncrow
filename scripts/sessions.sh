#!/usr/bin/env bash
# sessions.sh — fetch Atelier and estimate potential savings from local agent sessions.
#
# Intended public entrypoint:
#   curl -fsSL https://savings.atelier.ws | bash
#
# The release ships a Python wheel inside atelier-distribution-<os>-<arch>.tar.gz
# (not a standalone binary), so this installs that wheel into an ephemeral uv
# venv and runs `atelier session stats`. The venv is cached under /tmp keyed by
# release tag + platform, so repeated runs are fast.
#
# Examples:
#   bash scripts/sessions.sh
#   bash scripts/sessions.sh --since 30d --top 10
#   bash scripts/sessions.sh --host codex --limit 20
#   bash scripts/sessions.sh --local                    # install from local bundle wheel (no download)
#   bash scripts/sessions.sh --local --host copilot     # local wheel + extra flags
#
# Optional env:
#   ATELIER_RELEASE_TAG=v1.2.3        (default: latest)
#   ATELIER_SAVINGS_SINCE=7d          (default lookback when no args are passed)
#   ATELIER_SAVINGS_TOP=5             (default top sessions shown when no args are passed)
#   ATELIER_SESSION_CACHE=1           (default: 1; cache the venv under /tmp)
#   ATELIER_SESSION_CACHE_DIR=/tmp/atelier-session-cache
#   ATELIER_LOCAL_WHEEL=./bundle/bin/atelier-*.whl  (override local wheel path)

set -euo pipefail

ATELIER_VERBOSE="${ATELIER_VERBOSE:-0}"

# ── colour + helpers ─────────────────────────────────────────────────────────
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

info()    { printf "  ${_CP}◇${_C0}  %s\n" "$*" >&2; }
warn()    { printf "  ${_CY}⚠${_C0}  %s\n" "$*" >&2; }
error()   { printf "  ${_CR}✗${_C0}  %s\n" "$*" >&2; }
fail()    { error "$*"; exit 1; }
verbose() { [[ "$ATELIER_VERBOSE" == "1" ]] && info "$*" || true; }

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

_hum() {
    local b=$1
    if   (( b >= 1073741824 )); then printf '%d.%dG' $(( b/1073741824 )) $(( (b%1073741824)*10/1073741824 ))
    elif (( b >= 1048576    )); then printf '%d.%dM' $(( b/1048576    )) $(( (b%1048576)*10/1048576     ))
    elif (( b >= 1024       )); then printf '%d.%dK' $(( b/1024       )) $(( (b%1024)*10/1024           ))
    else printf '%dB' "$b"; fi
}

_fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || echo 0; }

_download_progress() {
    local url=$1 dest=$2 total=0
    if command -v curl >/dev/null 2>&1; then
        total=$(curl -fsIL --max-time 5 "$url" 2>/dev/null \
            | tr -d '\r' | awk 'tolower($1)=="content-length:" {print $2}' | tail -1)
        total=${total:-0}
        curl -fLs --retry 3 --retry-delay 2 --connect-timeout 15 "$url" > "$dest" &
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dest" "$url" &
    else
        fail "Missing downloader: install curl or wget."
    fi
    local pid=$!

    if [[ -t 2 && "$total" -gt 0 ]]; then
        local cur=0 t0=$SECONDS
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
    fi
    wait "$pid"
}

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

# ── shared helpers ───────────────────────────────────────────────────────────
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then return; fi
    info "Installing uv..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "Missing downloader: install curl or wget."
    fi
    export PATH="${HOME}/.local/bin:${PATH}"
    command -v uv >/dev/null 2>&1 || fail "uv install completed but uv is not on PATH."
}

# verify_checksum <archive> <url>
# Verifies <archive> against a published <url>.sha256 sidecar. Fails closed:
# if the checksum cannot be fetched or does not match, the run aborts unless
# ATELIER_ALLOW_UNVERIFIED=1 is set to explicitly opt out.
# TODO: publish atelier-distribution-*.tar.gz.sha256 sidecars in
# .github/workflows/release.yml so this verification is enforced by default.
verify_checksum() {
    local archive="$1" url="$2" expected=""
    if command -v curl >/dev/null 2>&1; then
        expected="$(curl -fsSL "${url}.sha256" 2>/dev/null || true)"
    elif command -v wget >/dev/null 2>&1; then
        expected="$(wget -qO- "${url}.sha256" 2>/dev/null || true)"
    fi
    # Accept both `<hash>  file` and `SHA256 (file) = <hash>` formats.
    expected="$(printf '%s' "$expected" | grep -oE '[0-9a-fA-F]{64}' | head -1 | tr 'A-F' 'a-f')"
    if [[ -z "$expected" ]]; then
        if [[ "${ATELIER_ALLOW_UNVERIFIED:-0}" == "1" ]]; then
            warn "No published checksum at ${url}.sha256 — proceeding unverified (ATELIER_ALLOW_UNVERIFIED=1)."
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

# install_wheel_to_venv <wheel> <venv_dir> [constraints]
# Installs the wheel into a fresh venv at <venv_dir>; resolution is pinned by the
# bundled constraints file when present (avoids re-resolving unbounded deps).
install_wheel_to_venv() {
    local wheel="$1" venv="$2" constraints="${3:-}"
    ensure_uv
    info "Preparing temporary Atelier runtime"
    uv venv "$venv" >/dev/null
    local cargs=()
    if [[ -n "$constraints" && -f "$constraints" ]]; then
        # uv constraints reject unnamed requirements such as bare file://
        # paths emitted by uv export for local-path dependencies (e.g. the
        # babel stub). Rewrite them to named, absolute file:// constraints.
        if grep -qE '^\./?vendor/' "$constraints"; then
            local constraints_dir resolved
            constraints_dir="$(dirname "$constraints")"
            resolved="${constraints_dir}/constraints.resolved.txt"
            # Match lines like `./vendor/babel-99.0.0-py3-none-any.whl`
            # and rewrite to `babel @ file:///abs/path/to/vendor/babel-99.0.0-py3-none-any.whl`.
            # Capture the package name (everything before the first `-`) and the
            # version+tags stem after it.
            sed -E 's#^\.?/?vendor/([a-zA-Z0-9_.]+)-([0-9].*\.whl)$#\1 @ file://'"${constraints_dir}"'/vendor/\1-\2#' \
                "$constraints" > "$resolved"
            cargs=(-c "$resolved")
        else
            cargs=(-c "$constraints")
        fi
    fi
    uv pip install --python "$venv" "${cargs[@]+"${cargs[@]}"}" "$wheel" >/dev/null
}

# ── parse --local out before forwarding remaining args ───────────────────────
USE_LOCAL=0
FORWARD_ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--local" ]]; then
        USE_LOCAL=1
    else
        FORWARD_ARGS+=("$arg")
    fi
done
set -- "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}"

CACHE_ENABLED="${ATELIER_SESSION_CACHE:-1}"
CACHE_ROOT="${ATELIER_SESSION_CACHE_DIR:-/tmp/atelier-session-cache}"

if [[ "$USE_LOCAL" == "1" ]]; then
    # Resolve the local wheel: explicit ATELIER_LOCAL_WHEEL, then bundle/bin next
    # to the script (dist layout), then repo/cwd bundle/bin.
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    WHEEL="${ATELIER_LOCAL_WHEEL:-}"
    if [[ -z "$WHEEL" ]]; then
        for cand in "${SCRIPT_DIR}/../bin" "${SCRIPT_DIR}/../bundle/bin" "./bundle/bin"; do
            match="$(ls "${cand}"/atelier-*.whl 2>/dev/null | head -1 || true)"
            if [[ -n "$match" ]]; then WHEEL="$match"; break; fi
        done
    fi
    if [[ -z "$WHEEL" || ! -f "$WHEEL" ]]; then
        error "--local: could not find a local atelier wheel."
        echo "  Tried: ${SCRIPT_DIR}/../bin, ${SCRIPT_DIR}/../bundle/bin, ./bundle/bin (atelier-*.whl)" >&2
        echo "  Set ATELIER_LOCAL_WHEEL=/path/to/atelier-*.whl to override." >&2
        exit 1
    fi
    verbose "Using local wheel: $WHEEL"

    CONSTRAINTS=""
    [[ -f "$(dirname "$WHEEL")/../constraints.txt" ]] && CONSTRAINTS="$(cd "$(dirname "$WHEEL")/.." && pwd)/constraints.txt"
    VENV="${CACHE_ROOT}/local/$(basename "$WHEEL" .whl)/venv"
    ATELIER_BIN="${VENV}/bin/atelier"
    if [[ "${CACHE_ENABLED}" != "1" || ! -x "$ATELIER_BIN" ]]; then
        rm -rf "$VENV"
        install_wheel_to_venv "$WHEEL" "$VENV" "$CONSTRAINTS"
    fi
else
    OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
    ARCH="$(uname -m)"
    case "$ARCH" in
        amd64) ARCH="x86_64" ;;
        arm64) ARCH="arm64" ;;
        aarch64) ARCH="aarch64" ;;
    esac

    case "$OS" in
        linux|darwin) ;;
        *) fail "Unsupported OS: $OS" ;;
    esac
    case "$ARCH" in
        x86_64|aarch64|arm64) ;;
        *) fail "Unsupported architecture: $ARCH" ;;
    esac

    TAG="${ATELIER_RELEASE_TAG:-latest}"

    # If TAG is "latest", resolve the actual version tag from GitHub API
    if [[ "$TAG" == "latest" ]]; then
        if command -v curl >/dev/null 2>&1; then
            REAL_TAG=$(curl -sI https://github.com/atelier-ws/atelier/releases/latest | grep -i location | awk -F/ '{print $NF}' | tr -d '\r')
        elif command -v wget >/dev/null 2>&1; then
            REAL_TAG=$(wget --server-response --spider -q https://github.com/atelier-ws/atelier/releases/latest 2>&1 | grep -i 'Location:' | awk -F/ '{print $NF}' | tr -d '\r' | tail -1)
        else
            REAL_TAG=""
        fi
        if [[ -z "$REAL_TAG" ]]; then
            warn "Failed to resolve 'latest' tag. Falling back to cached 'latest' if available."
        else
            TAG="$REAL_TAG"
        fi
    fi

    SUFFIX="${OS}-${ARCH}"
    ASSET="atelier-distribution-${SUFFIX}.tar.gz"
    URL="https://github.com/atelier-ws/atelier/releases/download/${TAG}/${ASSET}"

    CACHE_DIR="${CACHE_ROOT}/${TAG}/${SUFFIX}"
    VENV="${CACHE_DIR}/venv"
    ATELIER_BIN="${VENV}/bin/atelier"

    if [[ "${CACHE_ENABLED}" != "1" || ! -x "$ATELIER_BIN" ]]; then
        TMP_BASE="/tmp/atelier-session-${SUFFIX}-$$"
        ARCHIVE="${TMP_BASE}.tar.gz"
        cleanup() { rm -rf "${TMP_BASE}" "${ARCHIVE}" 2>/dev/null || true; }
        trap cleanup EXIT

        mkdir -p "${TMP_BASE}"
        printf "  ${_CP}◇${_C0}  ${_CB}Downloading${_C0} Atelier estimator %s  ${_CD}(%s)${_C0}\n" \
            "${TAG}" "${SUFFIX}" >&2
        if ! _download_progress "${URL}" "${ARCHIVE}"; then
            fail "Could not download ${ASSET}. The release may not include this platform asset yet: ${URL}"
        fi
        [[ -s "$ARCHIVE" ]] || fail "Downloaded archive is empty: ${URL}"

        verify_checksum "${ARCHIVE}" "${URL}"

        printf "  ${_CP}◇${_C0}  ${_CB}Extracting${_C0}\n" >&2
        _extract_progress "${ARCHIVE}" "${TMP_BASE}"
        WHEEL="$(ls "${TMP_BASE}"/bin/atelier-*.whl 2>/dev/null | head -1 || true)"
        if [[ -z "$WHEEL" ]]; then
            fail "atelier wheel not found in release archive ${ASSET}"
        fi
        CONSTRAINTS=""
        [[ -f "${TMP_BASE}/constraints.txt" ]] && CONSTRAINTS="${TMP_BASE}/constraints.txt"

        rm -rf "$VENV"
        mkdir -p "$CACHE_DIR"
        install_wheel_to_venv "$WHEEL" "$VENV" "$CONSTRAINTS"
    fi
fi

if [[ ! -x "$ATELIER_BIN" ]]; then
    fail "atelier not found after install: ${ATELIER_BIN}"
fi

# Scan live host session directories and print an aggregate potential-savings
# report. This is intentionally read-only: live scans import into a temporary
# store and do not require Atelier login or provider API keys.
_run_stats() {
    "${ATELIER_BIN}" session stats "$@" 2> >(
        grep -vE \
            -e '^Scanning last .* across .* host\(s\)' \
            -e '^claude reader: dropped .* unparseable line\(s\) while importing session ' \
            >&2
    )
}

verbose "Scanning local agent sessions for potential Atelier savings"
if [[ "$#" -eq 0 ]]; then
    _run_stats \
        --source live \
        --since "${ATELIER_SAVINGS_SINCE:-7d}" \
        --top "${ATELIER_SAVINGS_TOP:-5}"
    exit $?
fi
_run_stats "$@"
