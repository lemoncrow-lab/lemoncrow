#!/usr/bin/env bash
# local_runtime.sh — Install serverless Workspaces Lite independently of Review.
#
# Runtime Lite uses only session-scoped stdio processes. It installs no local API
# server, frontend, daemon, listener, or service.
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
RELEASE_TAG="${LEMONCROW_RELEASE_TAG:-latest}"
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
FROM_BUILD=0
AGENT=""
RUNTIME_ARCHIVE="${LEMONCROW_RUNTIME_ARTIFACT:-${SCRIPT_DIR}/../dist/lemoncrow-runtime-client.tar.gz}"
RUNTIME_HELPER="${LEMONCROW_RUNTIME_INSTALLER:-}"
TEMP=""

usage() {
    cat <<'USAGE'
usage: local_runtime.sh [OPTIONS]

Install LemonCrow Workspaces Lite with no LemonCrow server, frontend, daemon, or listener.

  --from-build [ARCHIVE]    use a local Workspaces client artifact
  --runtime-artifact PATH   compatibility alias for --from-build PATH
  --agent NAME              opt in to claude, codex, opencode, or auto
  --dry-run                 print the install plan without changing anything

Legacy --port is accepted and ignored so old scripted installs migrate safely.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from-build)
            FROM_BUILD=1
            if [[ $# -ge 2 && "$2" != --* ]]; then
                # Older callers passed the historical engine bundle here and the
                # Runtime client separately. Ignore a directory for compatibility.
                if [[ ! -d "$2" ]]; then RUNTIME_ARCHIVE="$2"; fi
                shift 2
            else
                shift
            fi
            ;;
        --from-build=*)
            FROM_BUILD=1
            candidate="${1#--from-build=}"
            [[ -d "$candidate" ]] || RUNTIME_ARCHIVE="$candidate"
            shift
            ;;
        --runtime-artifact)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: --runtime-artifact needs a value" >&2; exit 2; }
            FROM_BUILD=1
            RUNTIME_ARCHIVE="$2"
            shift 2
            ;;
        --runtime-artifact=*)
            FROM_BUILD=1
            RUNTIME_ARCHIVE="${1#--runtime-artifact=}"
            shift
            ;;
        --port)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: --port needs a value" >&2; exit 2; }
            shift 2
            ;;
        --port=*) shift ;;
        --agent)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: --agent needs a value" >&2; exit 2; }
            AGENT="$2"
            shift 2
            ;;
        --agent=*) AGENT="${1#--agent=}"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help|help) usage; exit 0 ;;
        *) lc_ui_printf '1;31' '%s\n' "local_runtime.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -n "$AGENT" ]]; then
    case "$AGENT" in
        auto|claude|codex|opencode) ;;
        *) lc_ui_printf '1;31' '%s\n' "local_runtime.sh: unsupported --agent: $AGENT" >&2; exit 2 ;;
    esac
fi

if [[ -n "${LEMONCROW_RELEASE_BASE_URL:-}" ]]; then
    BASE_URL="${LEMONCROW_RELEASE_BASE_URL%/}"
elif [[ "$RELEASE_TAG" == "latest" ]]; then
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
else
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/download/${RELEASE_TAG}"
fi

fetch_to() {
    local url="$1" target="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fLsS --retry 3 --retry-delay 2 --connect-timeout 15 "$url" -o "$target"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$target" "$url"
    else
        lc_ui_printf '1;31' '%s\n' "local_runtime.sh: curl or wget is required" >&2
        return 1
    fi
}

resolve_runtime_helper() {
    if [[ -n "$RUNTIME_HELPER" ]]; then
        [[ -f "$RUNTIME_HELPER" ]] || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: helper not found: $RUNTIME_HELPER" >&2; return 1; }
        printf '%s\n' "$RUNTIME_HELPER"
        return 0
    fi
    if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/runtime.sh" ]]; then
        printf '%s\n' "$SCRIPT_DIR/runtime.sh"
        return 0
    fi
    local target="$TEMP/runtime.sh"
    fetch_to "$BASE_URL/runtime.sh" "$target" \
        || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: could not download runtime.sh" >&2; return 1; }
    chmod +x "$target"
    printf '%s\n' "$target"
}

legacy_uninstaller() {
    # Prefer the copy belonging to the installed legacy Runtime. It knows the
    # exact server ownership/state layout being migrated.
    if [[ -f "$STATE_DIR/install/scripts/uninstall_runtime.sh" ]]; then
        printf '%s\n' "$STATE_DIR/install/scripts/uninstall_runtime.sh"
        return 0
    fi
    if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/uninstall_runtime.sh" ]]; then
        printf '%s\n' "$SCRIPT_DIR/uninstall_runtime.sh"
        return 0
    fi
    return 1
}

migrate_legacy_local_runtime() {
    local env_file="$STATE_DIR/runtime/env" uninstaller
    grep -qx 'LEMONCROW_INSTALL_MODE=local' "$env_file" 2>/dev/null || return 0

    uninstaller="$(legacy_uninstaller || true)"
    if [[ -z "$uninstaller" ]]; then
        # No persisted or sibling copy (e.g. a piped install of an old engine):
        # use the release's uninstall_runtime.sh. TEMP is set here, in the parent
        # shell, so the EXIT trap removes it.
        if [[ "$DRY_RUN" == "1" ]]; then
            lc_ui_printf '36' '[dry-run] retire the legacy server-backed install with: %s/uninstall_runtime.sh --keep-hosts\n' "$BASE_URL"
            return 0
        fi
        [[ -n "$TEMP" ]] || TEMP="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-local-runtime.XXXXXX")"
        uninstaller="$TEMP/uninstall_runtime.sh"
        fetch_to "$BASE_URL/uninstall_runtime.sh" "$uninstaller" \
            || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: a legacy server-backed install is present, but its uninstaller is missing and could not be downloaded from $BASE_URL" >&2; return 1; }
        chmod +x "$uninstaller"
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        lc_ui_printf '36' '[dry-run] retire the legacy server-backed install with: bash %q --keep-hosts\n' "$uninstaller"
        return 0
    fi
    echo "Migrating the legacy server-backed install to Workspaces Lite..."
    bash "$uninstaller" --keep-hosts \
        || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: could not retire the legacy local server/engine" >&2; return 1; }
}

cleanup() {
    [[ -z "$TEMP" ]] || rm -rf "$TEMP"
}
trap cleanup EXIT

if [[ "$FROM_BUILD" == "1" && "$DRY_RUN" != "1" ]]; then
    [[ -s "$RUNTIME_ARCHIVE" ]] \
        || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: Workspaces artifact not found: $RUNTIME_ARCHIVE" >&2; exit 1; }
fi

migrate_legacy_local_runtime || exit $?

# Allocate any download temp directory in the parent shell so the EXIT trap owns
# and removes it. Command substitution would otherwise lose the TEMP assignment.
if [[ -z "$TEMP" && -z "$RUNTIME_HELPER" && ! -f "${SCRIPT_DIR:+$SCRIPT_DIR/runtime.sh}" ]]; then
    TEMP="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-local-runtime.XXXXXX")"
fi
runtime_helper="$(resolve_runtime_helper)" || exit 1

runtime_args=(--lite)
if [[ "$FROM_BUILD" == "1" ]]; then runtime_args+=(--from-build "$RUNTIME_ARCHIVE"); fi
[[ -n "$AGENT" ]] && runtime_args+=(--agent "$AGENT")
[[ "$DRY_RUN" == "1" ]] && runtime_args+=(--dry-run)
LEMONCROW_INSTALL_NESTED=1 bash "$runtime_helper" "${runtime_args[@]}"

if [[ "$DRY_RUN" == "1" ]]; then exit 0; fi

RUNTIME_CLIENT="$STATE_DIR/runtime/client/bin/lemoncrow-client"
RUNTIME_NAMED="$STATE_DIR/runtime/client/bin/lemoncrow-runtime-client"
[[ -x "$RUNTIME_CLIENT" && -x "$RUNTIME_NAMED" ]] \
    || { lc_ui_printf '1;31' '%s\n' "local_runtime.sh: Workspaces client was not installed" >&2; exit 1; }

# Review owns the public launchers when present; Runtime Lite owns only lc when
# installed alone. There is deliberately no Runtime-owned lcr.
REVIEW_LC="$STATE_DIR/review/client/bin/lc"
REVIEW_LCR="$STATE_DIR/review/client/bin/lcr"
mkdir -p "$BIN_DIR"
if [[ -x "$REVIEW_LC" && -x "$REVIEW_LCR" ]]; then
    ln -sfn "$REVIEW_LC" "$BIN_DIR/lc"
    ln -sfn "$REVIEW_LCR" "$BIN_DIR/lcr"
    review_state="preserved"
else
    ln -sfn "$RUNTIME_CLIENT" "$BIN_DIR/lc"
    rm -f "$BIN_DIR/lcr"
    review_state="not installed"
fi
ln -sfn "$RUNTIME_NAMED" "$BIN_DIR/lemoncrow-runtime-client"

if [[ "${LEMONCROW_INSTALL_NESTED:-0}" != 1 ]]; then
    lc_ui_printf '1;36' '\n◆ Verify installation\n'
    lc_ui_printf progress 'Checking Workspaces client...\n'
    LEMONCROW_HOME="$STATE_DIR" LEMONCROW_NO_LAZY_INSTALL=1 "$RUNTIME_CLIENT" --version >/dev/null
fi
lc_ui_printf '1;32' '\n✓ Workspaces Lite ready\n'
lc_ui_printf '36' '  hosts:    %s\n' "${AGENT:-none enabled by installer}"
lc_ui_printf '36' '  review:   %s\n' "$review_state"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) lc_ui_printf '36' '  Shell setup: add %s to PATH\n' "$BIN_DIR" ;; esac
lc_ui_printf '1;36' '\nNext:\n'
lc_ui_printf command '  Check Workspaces:' 'lc rt status'
if [[ -z "$AGENT" ]]; then
    lc_ui_printf '1;36' '\nOptional:\n'
    lc_ui_printf command '  Enable Workspaces:' 'lc runtime enable codex' '   # or claude / opencode / auto'
fi
