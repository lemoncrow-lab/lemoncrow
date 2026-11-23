#!/usr/bin/env bash
# hosted.sh — Install the auditable LemonCrow Review client.
#
# Review is an independent product. This installer installs only the Review/Core
# artifact, exposes lc + lcr, and never installs or configures Runtime/MCP hosts.
set -euo pipefail

# Keep this formatter self-contained: release helpers also run without a checkout.
lc_ui_printf() (
    lc_ui_style=$1
    shift
    # Nested installers leave the summary to their caller; warnings stay visible.
    if [ "${LEMONCROW_INSTALL_NESTED:-0}" = 1 ] &&
        [ "${LEMONCROW_VERBOSE:-0}" != 1 ] && [ "${DRY_RUN:-0}" != 1 ]; then
        case "$lc_ui_style" in
            '1;31'|'1;33'|progress) ;;
            *) return 0 ;;
        esac
    fi
    lc_ui_force=${FORCE_COLOR:-${CLICOLOR_FORCE:-0}}
    if [ -z "${NO_COLOR:-}" ] && [ "${FORCE_COLOR:-}" != 0 ] &&
        { [ "$lc_ui_force" != 0 ] || { [ -t 1 ] && [ "${TERM:-}" != dumb ]; }; }; then
        lc_ui_format=$1
        shift
        case "$lc_ui_format" in
            '\n'*) printf '\033[2m│\033[0m\n'; lc_ui_format=${lc_ui_format#??} ;;
        esac
        # Rail, marker and text occupy the same columns in every message.
        case "$lc_ui_format" in '  '*) lc_ui_format=${lc_ui_format#  } ;; esac
        # Section diamonds replace the rail; child rows keep the rail.
        if [ "$lc_ui_style" != '1;36' ]; then
            printf '\033[2m│\033[0m  '
        fi
        case "$lc_ui_style" in
            '1;36')
                case "$lc_ui_format" in '◆ '*) lc_ui_format=${lc_ui_format#◆ } ;; esac
                printf '\033[38;2;155;117;217m◆  '
                printf "$lc_ui_format" "$@"
                lc_ui_status=$?
                printf '\033[0m'
                exit "$lc_ui_status"
                ;;
            '1;31')
                printf '\033[1;31m✗\033[0m  ' ;;
            '1;33')
                printf '\033[33m⚠\033[0m  ' ;;
            '1;32')
                case "$lc_ui_format" in '✓ '*) lc_ui_format=${lc_ui_format#✓ } ;; esac
                printf '\033[32m✓\033[0m  ' ;;
            progress) printf '\033[2m◇\033[0m  ' ;;
            command)
                printf '   %s \033[38;2;155;117;217m%s\033[0m%s\n' "$lc_ui_format" "$1" "${2:-}"
                exit $?
                ;;
            *) printf '   ' ;;
        esac
        printf "$lc_ui_format" "$@"
    else
        if [ "$lc_ui_style" = command ]; then
            printf '%s %s%s\n' "$1" "$2" "${3:-}"
        else
            printf "$@"
        fi
    fi
)


# Siblings are trusted only beside a real script file; a piped copy has none and
# never falls back to the current directory.
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    SCRIPT_DIR=""
fi
HOSTED_URL="${LEMONCROW_HOSTED_URL:-${LEMONCROW_URL:-https://api.lemoncrow.com}}"
RELEASE_TAG="${LEMONCROW_RELEASE_TAG:-latest}"
STATE_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
BIN_DIR="${LEMONCROW_BIN_DIR:-${STATE_DIR}/bin}"
INSTALL_ROOT="${LEMONCROW_REVIEW_INSTALL_ROOT:-${LEMONCROW_HOSTED_INSTALL_ROOT:-${STATE_DIR}/review/client}}"
ALLOW_UNVERIFIED="${LEMONCROW_ALLOW_UNVERIFIED:-0}"
FROM_BUILD=""
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"

usage() {
    cat <<'USAGE'
usage: hosted.sh [OPTIONS]

Install LemonCrow Review only. Workspaces and coding-agent integration are separate.

  --url URL              LemonCrow server (default https://api.lemoncrow.com)
  --from-build [ARCHIVE] install local dist/lemoncrow-review-client.tar.gz
  --no-hosts             accepted compatibility no-op; Review never configures hosts
  --dry-run              print the resolved installation plan and exit

Workspaces moved to its own installer:
  runtime.sh [--agent claude|codex|opencode|auto]

Environment overrides:
  LEMONCROW_RELEASE_TAG, LEMONCROW_HOME, LEMONCROW_BIN_DIR,
  LEMONCROW_REVIEW_INSTALL_ROOT, LEMONCROW_ALLOW_UNVERIFIED
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "hosted.sh: --url needs a value" >&2; exit 2; }
            HOSTED_URL="$2"; shift 2 ;;
        --url=*) HOSTED_URL="${1#--url=}"; shift ;;
        --from-build)
            if [[ $# -ge 2 && "$2" != --* ]]; then FROM_BUILD="$2"; shift 2; else FROM_BUILD="${SCRIPT_DIR}/../dist/lemoncrow-review-client.tar.gz"; shift; fi ;;
        --from-build=*) FROM_BUILD="${1#--from-build=}"; shift ;;
        --no-hosts) shift ;; # Review never mutates host config; retained for upgrade scripts.
        --agent|--runtime-agent)
            lc_ui_printf '1;31' '%s\n' "hosted.sh: Workspaces is a separate product; install it with runtime.sh, then run 'lc runtime enable <host>'" >&2
            exit 2 ;;
        --agent=*|--runtime-agent=*)
            lc_ui_printf '1;31' '%s\n' "hosted.sh: Workspaces is a separate product; install it with runtime.sh, then run 'lc runtime enable <host>'" >&2
            exit 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help|help) usage; exit 0 ;;
        --local) lc_ui_printf '1;31' '%s\n' "hosted.sh: --local was renamed to --from-build" >&2; exit 2 ;;
        *) lc_ui_printf '1;31' '%s\n' "hosted.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$HOSTED_URL" in
    https://*) ;;
    http://localhost|http://localhost/*|http://localhost:*|http://127.0.0.1|http://127.0.0.1/*|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]/*|http://\[::1\]:*) ;;
    http://*) lc_ui_printf '1;31' '%s\n' "hosted.sh: plaintext HTTP is allowed only for literal loopback; use HTTPS: $HOSTED_URL" >&2; exit 2 ;;
    *) lc_ui_printf '1;31' '%s\n' "hosted.sh: hosted URL must be absolute HTTPS (or loopback HTTP): $HOSTED_URL" >&2; exit 2 ;;
esac

REVIEW_MODE="hosted"
# A loopback URL means the local Review server, unless the caller chose the
# cloud deployment explicitly (install.sh --cloud against a local enterprise server).
case "${LEMONCROW_DEPLOYMENT:-}" in
    cloud|hosted) ;;
    *)
        case "$HOSTED_URL" in
            http://localhost|http://localhost/*|http://localhost:*|http://127.0.0.1|http://127.0.0.1/*|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]/*|http://\[::1\]:*) REVIEW_MODE="local" ;;
        esac ;;
esac

find_python() {
    local candidate version
    for candidate in "${LEMONCROW_PYTHON:-}" python3.13 python3.12 python3; do
        [[ -n "$candidate" ]] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        version="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
        case "$version" in 3.12|3.13) command -v "$candidate"; return 0 ;; esac
    done
    return 1
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    else lc_ui_printf '1;31' '%s\n' "hosted.sh: sha256sum or shasum is required" >&2; return 1
    fi
}

# A release-pinned install.sh passes name=sha256 pins for its own release; a
# pinned hash replaces the checksum sidecar served beside the archive.
RELEASE_PINS="${LEMONCROW_RELEASE_PINS:-}"
pinned_sha256() {
    local entry
    local -a pins=()
    read -r -a pins <<<"$RELEASE_PINS" || true
    for entry in ${pins[@]+"${pins[@]}"}; do
        if [[ "${entry%%=*}" == "$1" ]]; then printf '%s\n' "${entry#*=}"; return 0; fi
    done
    return 1
}

fetch_to() {
    local url="$1" target="$2"
    if command -v curl >/dev/null 2>&1; then curl -fLsS --retry 3 --retry-delay 2 --connect-timeout 15 "$url" -o "$target"
    elif command -v wget >/dev/null 2>&1; then wget -qO "$target" "$url"
    else lc_ui_printf '1;31' '%s\n' "hosted.sh: curl or wget is required" >&2; return 1
    fi
}

persist_uninstall_suite() {
    local dest="$STATE_DIR/install/scripts" name source target
    mkdir -p "$dest"
    for name in uninstall_product.sh uninstall_review.sh uninstall_runtime.sh uninstall.sh local_server.sh; do
        source="${SCRIPT_DIR:+$SCRIPT_DIR/$name}"
        target="$dest/$name"
        if [[ -f "$source" ]]; then
            cp -f "$source" "$target"
        else
            fetch_to "$BASE_URL/$name" "$target" || { lc_ui_printf '1;31' '%s\n' "hosted.sh: could not install $name" >&2; return 1; }
        fi
        chmod +x "$target"
    done
}

verify_archive() {
    local archive="$1" checksum_file="$2" expected actual
    expected="$(grep -oE '[0-9a-fA-F]{64}' "$checksum_file" 2>/dev/null | head -1 | tr 'A-F' 'a-f' || true)"
    if [[ -z "$expected" ]]; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then
            lc_ui_printf '1;33' '%s\n' "hosted.sh: WARNING: no valid SHA-256 checksum; proceeding because LEMONCROW_ALLOW_UNVERIFIED=1" >&2
            return 0
        fi
        lc_ui_printf '1;31' '%s\n' "hosted.sh: missing or invalid SHA-256 checksum; refusing unverified Review install" >&2
        return 1
    fi
    actual="$(sha256_file "$archive")"
    [[ "$actual" == "$expected" ]] || { lc_ui_printf '1;31' '%s\n' "hosted.sh: checksum mismatch; refusing Review install" >&2; return 1; }
}

write_env() {
    local target="$STATE_DIR/review/env" tmp
    mkdir -p "$STATE_DIR/review"
    chmod 700 "$STATE_DIR" "$STATE_DIR/review" 2>/dev/null || true
    tmp="$(mktemp "${target}.XXXXXX")"
    if [[ -f "$target" ]]; then
        grep -Ev '^(LEMONCROW_URL|LEMONCROW_HOSTED_URL|LEMONCROW_INSTALL_MODE)=' "$target" >"$tmp" || true
    fi
    printf 'LEMONCROW_URL=%s\nLEMONCROW_INSTALL_MODE=%s\n' "$HOSTED_URL" "$REVIEW_MODE" >>"$tmp"
    if [[ "$REVIEW_MODE" == "hosted" ]]; then
        printf 'LEMONCROW_HOSTED_URL=%s\n' "$HOSTED_URL" >>"$tmp"
    fi
    chmod 600 "$tmp"
    mv -f "$tmp" "$target"
}

PYTHON="$(find_python || true)"
[[ -n "$PYTHON" ]] || { lc_ui_printf '1;31' '%s\n' "hosted.sh: Python 3.12 or 3.13 is required" >&2; exit 1; }

if [[ -n "${LEMONCROW_RELEASE_BASE_URL:-}" ]]; then
    BASE_URL="${LEMONCROW_RELEASE_BASE_URL%/}"
elif [[ "$RELEASE_TAG" == "latest" ]]; then
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
else
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/download/${RELEASE_TAG}"
fi
ASSET="lemoncrow-review-client.tar.gz"

if [[ "$DRY_RUN" == "1" ]]; then
    lc_ui_printf '36' 'Product: LemonCrow Review\n'
    lc_ui_printf '1;34' 'Review endpoint: %s\n' "$HOSTED_URL"
    lc_ui_printf '36' 'Review mode: %s\n' "$REVIEW_MODE"
    lc_ui_printf '36' 'Python: %s\n' "$PYTHON"
    lc_ui_printf '36' 'Install root: %s\n' "$INSTALL_ROOT"
    lc_ui_printf '36' 'CLI bin dir: %s\n' "$BIN_DIR"
    if [[ -n "$FROM_BUILD" ]]; then lc_ui_printf '36' 'Artifact: %s (local build)\n' "$FROM_BUILD"; else lc_ui_printf '36' 'Artifact: %s/%s (SHA-256 required)\n' "$BASE_URL" "$ASSET"; fi
    printf 'Workspaces/agent configuration: none\n'
    exit 0
fi

TEMP="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-review-install.XXXXXX")"
trap 'rm -rf "${TEMP:-}"' EXIT
archive="$TEMP/$ASSET"
checksum="$TEMP/$ASSET.sha256"

if [[ -n "$FROM_BUILD" ]]; then
    [[ -s "$FROM_BUILD" ]] || { lc_ui_printf '1;31' '%s\n' "hosted.sh: local Review artifact not found: $FROM_BUILD" >&2; exit 1; }
    cp "$FROM_BUILD" "$archive"
    if [[ -s "${FROM_BUILD}.sha256" ]]; then cp "${FROM_BUILD}.sha256" "$checksum"; else printf '%s\n' "$(sha256_file "$archive")" >"$checksum"; fi
else
    lc_ui_printf progress 'Fetching Review client...\n'
    fetch_to "$BASE_URL/$ASSET" "$archive" || { lc_ui_printf '1;31' '%s\n' "hosted.sh: could not download $BASE_URL/$ASSET" >&2; exit 1; }
    if pin="$(pinned_sha256 "$ASSET")"; then
        printf '%s\n' "$pin" >"$checksum"
    elif ! fetch_to "$BASE_URL/$ASSET.sha256" "$checksum"; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then : >"$checksum"; else lc_ui_printf '1;31' '%s\n' "hosted.sh: checksum sidecar is required: $BASE_URL/$ASSET.sha256" >&2; exit 1; fi
    fi
fi
verify_archive "$archive" "$checksum"

tar -xzf "$archive" -C "$TEMP"
wheel="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_review_client-*-py3-none-any.whl' -print -quit 2>/dev/null || true)"
wheel_count="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_review_client-*-py3-none-any.whl' -print 2>/dev/null | wc -l | tr -d ' ')"
[[ "$wheel_count" == "1" && -n "$wheel" ]] || { lc_ui_printf '1;31' '%s\n' "hosted.sh: artifact must contain exactly one lemoncrow-review-client wheel" >&2; exit 1; }

rm -rf "$INSTALL_ROOT"
mkdir -p "$(dirname "$INSTALL_ROOT")" "$BIN_DIR"
"$PYTHON" -m venv "$INSTALL_ROOT" || { lc_ui_printf '1;31' '%s\n' "hosted.sh: Python venv support is required" >&2; exit 1; }
VENV_PY="$INSTALL_ROOT/bin/python"
"$VENV_PY" -m pip install --disable-pip-version-check --no-index --no-deps --force-reinstall "$wheel" >/dev/null
CLIENT="$INSTALL_ROOT/bin/lc"
LCR="$INSTALL_ROOT/bin/lcr"
REVIEW_CLIENT="$INSTALL_ROOT/bin/lemoncrow-review-client"
[[ -x "$CLIENT" && -x "$LCR" && -x "$REVIEW_CLIENT" ]] || { lc_ui_printf '1;31' '%s\n' "hosted.sh: Review wheel did not provide lc/lcr" >&2; exit 1; }
ln -sfn "$CLIENT" "$BIN_DIR/lc"
ln -sfn "$LCR" "$BIN_DIR/lcr"
ln -sfn "$REVIEW_CLIENT" "$BIN_DIR/lemoncrow-review-client"
write_env
persist_uninstall_suite

if [[ "${LEMONCROW_INSTALL_NESTED:-0}" != 1 ]]; then
    lc_ui_printf '1;36' '\n◆ Verify installation\n'
    lc_ui_printf progress 'Checking lc and lcr...\n'
    LEMONCROW_HOME="$STATE_DIR" LEMONCROW_NO_LAZY_INSTALL=1 "$CLIENT" --version >/dev/null
    LEMONCROW_HOME="$STATE_DIR" LEMONCROW_NO_LAZY_INSTALL=1 "$LCR" --help >/dev/null
fi
lc_ui_printf '1;32' '\n✓ Review ready\n'
lc_ui_printf '36' '  endpoint: %s\n' "$HOSTED_URL"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) lc_ui_printf '36' '  Shell setup: add %s to PATH\n' "$BIN_DIR" ;; esac
lc_ui_printf '1;36' '\nNext:\n'
if [[ "$REVIEW_MODE" == hosted ]]; then lc_ui_printf command '  Sign in:' 'lc auth login'; fi
lc_ui_printf command '  Open Review:' 'lcr'
