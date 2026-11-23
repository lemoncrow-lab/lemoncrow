#!/usr/bin/env bash
# review_engine.sh — Install the local Review-only server engine.
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
RELEASE_TAG="${LEMONCROW_RELEASE_TAG:-latest}"
STATE_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
INSTALL_ROOT="${LEMONCROW_REVIEW_ENGINE_ROOT:-${STATE_DIR}/review/engine}"
ALLOW_UNVERIFIED="${LEMONCROW_ALLOW_UNVERIFIED:-0}"
FROM_BUILD=""
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"

usage() {
    cat <<'USAGE'
usage: review_engine.sh [OPTIONS]

Install only the local LemonCrow Review engine. No Runtime/MCP implementation is included.

  --from-build [ARCHIVE] install local dist/lemoncrow-review-engine.tar.gz
  --dry-run              print the resolved installation plan and exit
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-build)
            if [[ $# -ge 2 && "$2" != --* ]]; then FROM_BUILD="$2"; shift 2; else FROM_BUILD="${SCRIPT_DIR}/../dist/lemoncrow-review-engine.tar.gz"; shift; fi ;;
        --from-build=*) FROM_BUILD="${1#--from-build=}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help|help) usage; exit 0 ;;
        *) lc_ui_printf '1;31' '%s\n' "review_engine.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

find_python() {
    local candidate version
    for candidate in "${LEMONCROW_PYTHON:-}" python3.13 python3.12 python3; do
        [[ -n "$candidate" ]] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        version="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
        case "$version" in 3.12|3.13) command -v "$candidate"; return 0 ;; esac
    done
    # A uv-managed interpreter (e.g. on Debian 12, which ships 3.11).
    if command -v uv >/dev/null 2>&1; then
        for version in 3.13 3.12; do
            candidate="$(uv python find "$version" 2>/dev/null || true)"
            [[ -n "$candidate" && -x "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
        done
    fi
    return 1
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    else lc_ui_printf '1;31' '%s\n' "review_engine.sh: sha256sum or shasum is required" >&2; return 1
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
    else lc_ui_printf '1;31' '%s\n' "review_engine.sh: curl or wget is required" >&2; return 1
    fi
}

verify_archive() {
    local archive="$1" checksum_file="$2" expected actual
    expected="$(grep -oE '[0-9a-fA-F]{64}' "$checksum_file" 2>/dev/null | head -1 | tr 'A-F' 'a-f' || true)"
    if [[ -z "$expected" ]]; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then
            lc_ui_printf '1;33' '%s\n' "review_engine.sh: WARNING: no valid SHA-256 checksum; proceeding because LEMONCROW_ALLOW_UNVERIFIED=1" >&2
            return 0
        fi
        lc_ui_printf '1;31' '%s\n' "review_engine.sh: missing or invalid SHA-256 checksum; refusing unverified Review engine install" >&2
        return 1
    fi
    actual="$(sha256_file "$archive")"
    [[ "$actual" == "$expected" ]] || { lc_ui_printf '1;31' '%s\n' "review_engine.sh: checksum mismatch; refusing Review engine install" >&2; return 1; }
}

PYTHON="$(find_python || true)"
[[ -n "$PYTHON" ]] || { lc_ui_printf '1;31' '%s\n' "review_engine.sh: Python 3.12 or 3.13 is required; install one with 'uv python install 3.13' or set LEMONCROW_PYTHON" >&2; exit 1; }
if [[ -n "${LEMONCROW_RELEASE_BASE_URL:-}" ]]; then
    BASE_URL="${LEMONCROW_RELEASE_BASE_URL%/}"
elif [[ "$RELEASE_TAG" == "latest" ]]; then
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
else
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/download/${RELEASE_TAG}"
fi
ASSET="lemoncrow-review-engine.tar.gz"

if [[ "$DRY_RUN" == "1" ]]; then
    lc_ui_printf '36' 'Product: LemonCrow Review engine\n'
    lc_ui_printf '36' 'Python: %s\n' "$PYTHON"
    lc_ui_printf '36' 'Install root: %s\n' "$INSTALL_ROOT"
    if [[ -n "$FROM_BUILD" ]]; then lc_ui_printf '36' 'Artifact: %s (local build)\n' "$FROM_BUILD"; else lc_ui_printf '36' 'Artifact: %s/%s (SHA-256 required)\n' "$BASE_URL" "$ASSET"; fi
    lc_ui_printf '33' 'Runtime implementation: absent\n'
    exit 0
fi

TEMP="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-review-engine-install.XXXXXX")"
trap 'rm -rf "${TEMP:-}"' EXIT
archive="$TEMP/$ASSET"
checksum="$TEMP/$ASSET.sha256"
if [[ -n "$FROM_BUILD" ]]; then
    [[ -s "$FROM_BUILD" ]] || { lc_ui_printf '1;31' '%s\n' "review_engine.sh: local Review engine artifact not found: $FROM_BUILD" >&2; exit 1; }
    cp "$FROM_BUILD" "$archive"
    if [[ -s "${FROM_BUILD}.sha256" ]]; then cp "${FROM_BUILD}.sha256" "$checksum"; else printf '%s\n' "$(sha256_file "$archive")" >"$checksum"; fi
else
    lc_ui_printf progress 'Fetching Review engine...\n'
    fetch_to "$BASE_URL/$ASSET" "$archive" || { lc_ui_printf '1;31' '%s\n' "review_engine.sh: could not download $BASE_URL/$ASSET" >&2; exit 1; }
    if pin="$(pinned_sha256 "$ASSET")"; then
        printf '%s\n' "$pin" >"$checksum"
    elif ! fetch_to "$BASE_URL/$ASSET.sha256" "$checksum"; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then : >"$checksum"; else lc_ui_printf '1;31' '%s\n' "review_engine.sh: checksum sidecar is required: $BASE_URL/$ASSET.sha256" >&2; exit 1; fi
    fi
fi
verify_archive "$archive" "$checksum"
tar -xzf "$archive" -C "$TEMP"
wheel="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_review_engine-*-py3-none-any.whl' -print -quit 2>/dev/null || true)"
wheel_count="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_review_engine-*-py3-none-any.whl' -print 2>/dev/null | wc -l | tr -d ' ')"
[[ "$wheel_count" == "1" && -n "$wheel" ]] || { lc_ui_printf '1;31' '%s\n' "review_engine.sh: artifact must contain exactly one lemoncrow-review-engine wheel" >&2; exit 1; }

rm -rf "$INSTALL_ROOT"
mkdir -p "$(dirname "$INSTALL_ROOT")"
"$PYTHON" -m venv "$INSTALL_ROOT" || { lc_ui_printf '1;31' '%s\n' "review_engine.sh: Python venv support is required" >&2; exit 1; }
VENV_PY="$INSTALL_ROOT/bin/python"
lc_ui_printf progress 'Installing engine dependencies...\n'
"$VENV_PY" -m pip install --disable-pip-version-check --force-reinstall "$wheel" >/dev/null
HOME="${HOME}" "$VENV_PY" -m lemoncrow_server_core up --directory "$TEMP/check-state" --local-no-auth --product review --ephemeral --check-only
[[ -f "$TEMP/frontend/index.html" ]] || { lc_ui_printf '1;31' '%s\n' "review_engine.sh: artifact is missing the shared frontend bundle" >&2; exit 1; }
FRONTEND_ROOT="${LEMONCROW_FRONTEND_DIR:-${STATE_DIR}/install/frontend}"
rm -rf "$FRONTEND_ROOT"
mkdir -p "$FRONTEND_ROOT"
cp -R "$TEMP/frontend"/. "$FRONTEND_ROOT"/

lc_ui_printf '1;32' '✓ LemonCrow Review engine installed\n'
