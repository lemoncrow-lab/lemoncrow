#!/usr/bin/env bash
# local_review.sh — Compose local LemonCrow Review without installing Runtime.
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
STATE_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
BIN_DIR="${LEMONCROW_BIN_DIR:-${STATE_DIR}/bin}"
PORT="${LEMONCROW_LOCAL_SERVER_PORT:-7420}"
LOCAL_URL="http://127.0.0.1:${PORT}"
RELEASE_TAG="${LEMONCROW_RELEASE_TAG:-latest}"
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
FROM_BUILD=0
ENGINE_ARCHIVE="${LEMONCROW_REVIEW_ENGINE_ARTIFACT:-${SCRIPT_DIR}/../dist/lemoncrow-review-engine.tar.gz}"
REVIEW_ARCHIVE="${LEMONCROW_REVIEW_ARTIFACT:-${SCRIPT_DIR}/../dist/lemoncrow-review-client.tar.gz}"
ENGINE_HELPER="${LEMONCROW_REVIEW_ENGINE_INSTALLER:-}"
REVIEW_HELPER="${LEMONCROW_REVIEW_INSTALLER:-}"
SERVER_HELPER="${LEMONCROW_LOCAL_SERVER_SCRIPT:-}"
TEMP=""

usage() {
    cat <<'USAGE'
usage: local_review.sh [OPTIONS]

Install local LemonCrow Review without installing Workspaces or enabling coding agents.

  --from-build             use local Review engine + client artifacts from dist/
  --engine-artifact PATH   use a local lemoncrow-review-engine.tar.gz
  --review-artifact PATH   use a local lemoncrow-review-client.tar.gz
  --port PORT              loopback server port (default 7420)
  --dry-run                print the install plan without changing anything

Workspaces can be added independently later with local_runtime.sh / runtime.sh.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-build) FROM_BUILD=1; shift ;;
        --engine-artifact)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "local_review.sh: --engine-artifact needs a value" >&2; exit 2; }
            ENGINE_ARCHIVE="$2"; shift 2 ;;
        --engine-artifact=*) ENGINE_ARCHIVE="${1#--engine-artifact=}"; shift ;;
        --review-artifact)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "local_review.sh: --review-artifact needs a value" >&2; exit 2; }
            REVIEW_ARCHIVE="$2"; shift 2 ;;
        --review-artifact=*) REVIEW_ARCHIVE="${1#--review-artifact=}"; shift ;;
        --port)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "local_review.sh: --port needs a value" >&2; exit 2; }
            PORT="$2"; LOCAL_URL="http://127.0.0.1:${PORT}"; shift 2 ;;
        --port=*) PORT="${1#--port=}"; LOCAL_URL="http://127.0.0.1:${PORT}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help|help) usage; exit 0 ;;
        *) lc_ui_printf '1;31' '%s\n' "local_review.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && (( PORT <= 65535 )) \
    || { lc_ui_printf '1;31' '%s\n' "local_review.sh: invalid port: $PORT" >&2; exit 2; }

fetch_to() {
    local url="$1" target="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fLsS --retry 3 --retry-delay 2 --connect-timeout 15 "$url" -o "$target"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$target" "$url"
    else
        lc_ui_printf '1;31' '%s\n' "local_review.sh: curl or wget is required" >&2
        return 1
    fi
}

release_base_url() {
    if [[ -n "${LEMONCROW_RELEASE_BASE_URL:-}" ]]; then
        printf '%s\n' "${LEMONCROW_RELEASE_BASE_URL%/}"
    elif [[ "$RELEASE_TAG" == "latest" ]]; then
        printf '%s\n' "https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
    else
        printf '%s\n' "https://github.com/lemoncrow-lab/lemoncrow/releases/download/${RELEASE_TAG}"
    fi
}

resolve_helper() {
    local explicit="$1" sibling="$2" name="$3" target
    if [[ -n "$explicit" ]]; then
        [[ -f "$explicit" ]] || { lc_ui_printf '1;31' '%s\n' "local_review.sh: helper not found: $explicit" >&2; return 1; }
        printf '%s\n' "$explicit"; return 0
    fi
    if [[ -f "$sibling" ]]; then printf '%s\n' "$sibling"; return 0; fi
    [[ -n "$TEMP" ]] || { lc_ui_printf '1;31' '%s\n' "local_review.sh: internal temp directory was not initialized" >&2; return 1; }
    target="$TEMP/$name"
    fetch_to "$(release_base_url)/$name" "$target" \
        || { lc_ui_printf '1;31' '%s\n' "local_review.sh: could not download $name" >&2; return 1; }
    printf '%s\n' "$target"
}

if { [[ -z "$ENGINE_HELPER" && ! -f "${SCRIPT_DIR:+$SCRIPT_DIR/review_engine.sh}" ]] \
    || [[ -z "$REVIEW_HELPER" && ! -f "${SCRIPT_DIR:+$SCRIPT_DIR/hosted.sh}" ]] \
    || [[ -z "$SERVER_HELPER" && ! -f "${SCRIPT_DIR:+$SCRIPT_DIR/local_server.sh}" ]]; }; then
    TEMP="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-local-review.XXXXXX")"
fi
cleanup() { [[ -z "$TEMP" ]] || rm -rf "$TEMP"; }
trap cleanup EXIT

ENGINE_HELPER="$(resolve_helper "$ENGINE_HELPER" "${SCRIPT_DIR:+$SCRIPT_DIR/review_engine.sh}" review_engine.sh)"
REVIEW_HELPER="$(resolve_helper "$REVIEW_HELPER" "${SCRIPT_DIR:+$SCRIPT_DIR/hosted.sh}" hosted.sh)"
SERVER_HELPER="$(resolve_helper "$SERVER_HELPER" "${SCRIPT_DIR:+$SCRIPT_DIR/local_server.sh}" local_server.sh)"

if [[ "$FROM_BUILD" == "1" ]]; then
    [[ -s "$ENGINE_ARCHIVE" ]] || { lc_ui_printf '1;31' '%s\n' "local_review.sh: Review engine artifact not found: $ENGINE_ARCHIVE" >&2; exit 1; }
    [[ -s "$REVIEW_ARCHIVE" ]] || { lc_ui_printf '1;31' '%s\n' "local_review.sh: Review client artifact not found: $REVIEW_ARCHIVE" >&2; exit 1; }
fi

SERVER_TARGET_PRODUCT=review
if [[ -x "$STATE_DIR/runtime/client/bin/lemoncrow-client" ]] \
    && grep -qx 'LEMONCROW_INSTALL_MODE=local' "$STATE_DIR/runtime/env" 2>/dev/null; then
    SERVER_TARGET_PRODUCT=both
fi

lc_ui_printf progress 'Installing Review engine...\n'
engine_args=()
[[ "$FROM_BUILD" == "1" ]] && engine_args+=(--from-build "$ENGINE_ARCHIVE")
[[ "$DRY_RUN" == "1" ]] && engine_args+=(--dry-run)
# Bash 3.2 treats empty arrays as unset under nounset. Preserve zero arguments.
env LEMONCROW_INSTALL_NESTED=1 LEMONCROW_HOME="$STATE_DIR" LEMONCROW_RELEASE_TAG="$RELEASE_TAG" \
    bash "$ENGINE_HELPER" ${engine_args[@]+"${engine_args[@]}"}

lc_ui_printf progress 'Installing Review client...\n'
review_args=(--url "$LOCAL_URL")
[[ "$FROM_BUILD" == "1" ]] && review_args+=(--from-build "$REVIEW_ARCHIVE")
[[ "$DRY_RUN" == "1" ]] && review_args+=(--dry-run)
env LEMONCROW_INSTALL_NESTED=1 LEMONCROW_HOME="$STATE_DIR" LEMONCROW_BIN_DIR="$BIN_DIR" LEMONCROW_RELEASE_TAG="$RELEASE_TAG" \
    bash "$REVIEW_HELPER" "${review_args[@]}"

if [[ "$DRY_RUN" == "1" ]]; then
    lc_ui_printf '36' '[dry-run] local server target: %s\n' "$SERVER_TARGET_PRODUCT"
    lc_ui_printf '36' '[dry-run] local server lifecycle: %s restart\n' "$SERVER_HELPER"
    exit 0
fi

# Keep the lifecycle helper in shared install state so uninstall/restart paths do
# not depend on the source checkout or a temporary downloaded installer.
INSTALLED_SERVER_HELPER="$STATE_DIR/install/scripts/local_server.sh"
mkdir -p "$(dirname "$INSTALLED_SERVER_HELPER")"
cp -f "$SERVER_HELPER" "$INSTALLED_SERVER_HELPER"
chmod +x "$INSTALLED_SERVER_HELPER"

lc_ui_printf progress 'Starting local Review server...\n'

# Product installation is a synchronous lifecycle boundary: stop any previous
# composition, then start the requested one and return only after health passes.
env \
    LEMONCROW_HOME="$STATE_DIR" \
    LEMONCROW_LOCAL_SERVER_PORT="$PORT" \
    LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT="$SERVER_TARGET_PRODUCT" \
    bash "$INSTALLED_SERVER_HELPER" stop >/dev/null 2>&1 || true
env \
    LEMONCROW_HOME="$STATE_DIR" \
    LEMONCROW_LOCAL_SERVER_PORT="$PORT" \
    LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT="$SERVER_TARGET_PRODUCT" \
    LEMONCROW_INSTALL_NESTED=1 bash "$INSTALLED_SERVER_HELPER" start

[[ -x "$BIN_DIR/lc" && -x "$BIN_DIR/lcr" ]] \
    || { lc_ui_printf '1;31' '%s\n' "local_review.sh: Review client did not install lc/lcr under $BIN_DIR" >&2; exit 1; }
[[ -x "$STATE_DIR/review/engine/bin/python" ]] \
    || { lc_ui_printf '1;31' '%s\n' "local_review.sh: Review engine was not installed" >&2; exit 1; }

if [[ "${LEMONCROW_INSTALL_NESTED:-0}" != 1 ]]; then
    lc_ui_printf '1;36' '\n◆ Verify installation\n'
    lc_ui_printf progress 'Checking lc and lcr...\n'
    LEMONCROW_HOME="$STATE_DIR" LEMONCROW_NO_LAZY_INSTALL=1 "$BIN_DIR/lc" --version >/dev/null
    LEMONCROW_HOME="$STATE_DIR" LEMONCROW_NO_LAZY_INSTALL=1 "$BIN_DIR/lcr" --help >/dev/null
fi
lc_ui_printf '1;32' '\n✓ Local Review ready\n'
lc_ui_printf '36' '  endpoint: %s\n' "$LOCAL_URL"
lc_ui_printf '1;36' '\nNext:\n'
lc_ui_printf command '  Open Review:' 'lcr'
lc_ui_printf '1;36' '\nOptional:\n'
lc_ui_printf command '  add Workspaces:' 'lc runtime enable codex' '   # or claude / opencode / auto'
