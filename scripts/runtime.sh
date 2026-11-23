#!/usr/bin/env bash
# runtime.sh — Install LemonCrow Workspaces independently of Review.
#
# Runtime lives in ~/.lemoncrow/runtime/client. It does not replace a Review-owned
# lc launcher. If no lc exists yet, Runtime exposes its own lc until Review is installed.
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
RUNTIME_URL="${LEMONCROW_HOSTED_URL:-${LEMONCROW_URL:-https://api.lemoncrow.com}}"
RELEASE_TAG="${LEMONCROW_RELEASE_TAG:-latest}"
STATE_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
BIN_DIR="${LEMONCROW_BIN_DIR:-${STATE_DIR}/bin}"
INSTALL_ROOT="${LEMONCROW_RUNTIME_INSTALL_ROOT:-${STATE_DIR}/runtime/client}"
ALLOW_UNVERIFIED="${LEMONCROW_ALLOW_UNVERIFIED:-0}"
FROM_BUILD=""
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
AGENT=""
LITE=0
usage() {
    cat <<'USAGE'
usage: runtime.sh [OPTIONS]

Install LemonCrow Workspaces independently of Review.

  --lite                 install Workspaces Lite: no server, daemon, listener, endpoint, or login dependency
  --url URL              LemonCrow server for hosted/thin-client Workspaces (default https://api.lemoncrow.com)
  --from-build [ARCHIVE] install local dist/lemoncrow-runtime-client.tar.gz
  --agent NAME           explicitly enable Workspaces for claude, codex, opencode, or auto
  --no-hosts             compatibility no-op; host integration is already opt-in
  --dry-run              print the resolved installation plan and exit

Without --agent, installation changes no coding-agent configuration. Workspaces Lite
is self-contained and uses only session-scoped stdio processes plus repo-local indexes.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --lite) LITE=1; shift ;;
        --url)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "runtime.sh: --url needs a value" >&2; exit 2; }
            RUNTIME_URL="$2"; shift 2 ;;
        --url=*) RUNTIME_URL="${1#--url=}"; shift ;;
        --from-build)
            if [[ $# -ge 2 && "$2" != --* ]]; then FROM_BUILD="$2"; shift 2; else FROM_BUILD="${SCRIPT_DIR}/../dist/lemoncrow-runtime-client.tar.gz"; shift; fi ;;
        --from-build=*) FROM_BUILD="${1#--from-build=}"; shift ;;
        --agent)
            [[ $# -ge 2 ]] || { lc_ui_printf '1;31' '%s\n' "runtime.sh: --agent needs a value" >&2; exit 2; }
            AGENT="$2"; shift 2 ;;
        --agent=*) AGENT="${1#--agent=}"; shift ;;
        --no-hosts) shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help|help) usage; exit 0 ;;
        *) lc_ui_printf '1;31' '%s\n' "runtime.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -n "$AGENT" ]]; then
    case "$AGENT" in auto|claude|codex|opencode) ;; *) lc_ui_printf '1;31' '%s\n' "runtime.sh: unsupported --agent: $AGENT" >&2; exit 2 ;; esac
fi
RUNTIME_MODE="lite"
if [[ "$LITE" != "1" ]]; then
    case "$RUNTIME_URL" in
        https://*) ;;
        http://localhost|http://localhost/*|http://localhost:*|http://127.0.0.1|http://127.0.0.1/*|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]/*|http://\[::1\]:*) ;;
        http://*) lc_ui_printf '1;31' '%s\n' "runtime.sh: plaintext HTTP is allowed only for literal loopback; use HTTPS: $RUNTIME_URL" >&2; exit 2 ;;
        *) lc_ui_printf '1;31' '%s\n' "runtime.sh: Workspaces server URL must be absolute HTTPS (or loopback HTTP): $RUNTIME_URL" >&2; exit 2 ;;
    esac
    RUNTIME_MODE="hosted"
    case "${LEMONCROW_DEPLOYMENT:-}:$RUNTIME_URL" in
        # An explicit cloud deployment keeps a loopback server hosted (a local enterprise server).
        cloud:*|hosted:*) ;;
        # The local server executes no Runtime tools: local Runtime is Runtime Lite.
        *:http://localhost|*:http://localhost/*|*:http://localhost:*|*:http://127.0.0.1|*:http://127.0.0.1/*|*:http://127.0.0.1:*|*:http://\[::1\]|*:http://\[::1\]/*|*:http://\[::1\]:*)
            lc_ui_printf '1;33' '%s\n' "runtime.sh: a loopback endpoint now installs Workspaces Lite (no server needed)" >&2
            RUNTIME_MODE="lite" ;;
    esac
fi

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
    else lc_ui_printf '1;31' '%s\n' "runtime.sh: sha256sum or shasum is required" >&2; return 1
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
    else lc_ui_printf '1;31' '%s\n' "runtime.sh: curl or wget is required" >&2; return 1
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
            fetch_to "$BASE_URL/$name" "$target" || { lc_ui_printf '1;31' '%s\n' "runtime.sh: could not install $name" >&2; return 1; }
        fi
        chmod +x "$target"
    done
}

verify_archive() {
    local archive="$1" checksum_file="$2" expected actual
    expected="$(grep -oE '[0-9a-fA-F]{64}' "$checksum_file" 2>/dev/null | head -1 | tr 'A-F' 'a-f' || true)"
    if [[ -z "$expected" ]]; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then
            lc_ui_printf '1;33' '%s\n' "runtime.sh: WARNING: no valid SHA-256 checksum; proceeding because LEMONCROW_ALLOW_UNVERIFIED=1" >&2
            return 0
        fi
        lc_ui_printf '1;31' '%s\n' "runtime.sh: missing or invalid SHA-256 checksum; refusing Workspaces install" >&2
        return 1
    fi
    actual="$(sha256_file "$archive")"
    [[ "$actual" == "$expected" ]] || { lc_ui_printf '1;31' '%s\n' "runtime.sh: checksum mismatch; refusing Workspaces install" >&2; return 1; }
}

write_env() {
    local target="$STATE_DIR/runtime/env" tmp
    mkdir -p "$STATE_DIR/runtime"
    chmod 700 "$STATE_DIR" "$STATE_DIR/runtime" 2>/dev/null || true
    tmp="$(mktemp "${target}.XXXXXX")"
    if [[ -f "$target" ]]; then
        grep -Ev '^(LEMONCROW_URL|LEMONCROW_HOSTED_URL|LEMONCROW_INSTALL_MODE)=' "$target" >"$tmp" || true
    fi
    printf 'LEMONCROW_INSTALL_MODE=%s\n' "$RUNTIME_MODE" >>"$tmp"
    if [[ "$RUNTIME_MODE" != "lite" ]]; then printf 'LEMONCROW_URL=%s\n' "$RUNTIME_URL" >>"$tmp"; fi
    if [[ "$RUNTIME_MODE" == "hosted" ]]; then printf 'LEMONCROW_HOSTED_URL=%s\n' "$RUNTIME_URL" >>"$tmp"; fi
    chmod 600 "$tmp"
    mv -f "$tmp" "$target"
}

PYTHON="$(find_python || true)"
[[ -n "$PYTHON" ]] || { lc_ui_printf '1;31' '%s\n' "runtime.sh: Python 3.12 or 3.13 is required" >&2; exit 1; }

if [[ -n "${LEMONCROW_RELEASE_BASE_URL:-}" ]]; then
    BASE_URL="${LEMONCROW_RELEASE_BASE_URL%/}"
elif [[ "$RELEASE_TAG" == "latest" ]]; then
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
else
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/download/${RELEASE_TAG}"
fi
ASSET="lemoncrow-runtime-client.tar.gz"

if [[ "$DRY_RUN" == "1" ]]; then
    lc_ui_printf '36' 'Product: LemonCrow Workspaces\n'
    lc_ui_printf '36' 'Workspaces mode: %s\n' "$RUNTIME_MODE"
    if [[ "$RUNTIME_MODE" == "lite" ]]; then lc_ui_printf '36' 'Workspaces server: none (session-scoped stdio only)\n'; else lc_ui_printf '1;34' 'Workspaces endpoint: %s\n' "$RUNTIME_URL"; fi
    lc_ui_printf '36' 'Python: %s\n' "$PYTHON"
    lc_ui_printf '36' 'Install root: %s\n' "$INSTALL_ROOT"
    lc_ui_printf '36' 'CLI bin dir: %s\n' "$BIN_DIR"
    if [[ -n "$FROM_BUILD" ]]; then lc_ui_printf '36' 'Artifact: %s (local build)\n' "$FROM_BUILD"; else lc_ui_printf '36' 'Artifact: %s/%s (SHA-256 required)\n' "$BASE_URL" "$ASSET"; fi
    lc_ui_printf '36' 'Agent registration: %s\n' "${AGENT:-none}"
    exit 0
fi

TEMP="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-runtime-install.XXXXXX")"
trap 'rm -rf "${TEMP:-}"' EXIT
archive="$TEMP/$ASSET"
checksum="$TEMP/$ASSET.sha256"

if [[ -n "$FROM_BUILD" ]]; then
    [[ -s "$FROM_BUILD" ]] || { lc_ui_printf '1;31' '%s\n' "runtime.sh: local Workspaces artifact not found: $FROM_BUILD" >&2; exit 1; }
    cp "$FROM_BUILD" "$archive"
    if [[ -s "${FROM_BUILD}.sha256" ]]; then cp "${FROM_BUILD}.sha256" "$checksum"; else printf '%s\n' "$(sha256_file "$archive")" >"$checksum"; fi
else
    lc_ui_printf progress 'Fetching Workspaces client...\n'
    fetch_to "$BASE_URL/$ASSET" "$archive" || { lc_ui_printf '1;31' '%s\n' "runtime.sh: could not download $BASE_URL/$ASSET" >&2; exit 1; }
    if pin="$(pinned_sha256 "$ASSET")"; then
        printf '%s\n' "$pin" >"$checksum"
    elif ! fetch_to "$BASE_URL/$ASSET.sha256" "$checksum"; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then : >"$checksum"; else lc_ui_printf '1;31' '%s\n' "runtime.sh: checksum sidecar is required: $BASE_URL/$ASSET.sha256" >&2; exit 1; fi
    fi
fi
verify_archive "$archive" "$checksum"

tar -xzf "$archive" -C "$TEMP"
wheel="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_runtime_client-*-py3-none-any.whl' -print -quit 2>/dev/null || true)"
wheel_count="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_runtime_client-*-py3-none-any.whl' -print 2>/dev/null | wc -l | tr -d ' ')"
[[ "$wheel_count" == "1" && -n "$wheel" ]] || { lc_ui_printf '1;31' '%s\n' "runtime.sh: artifact must contain exactly one lemoncrow-runtime-client wheel" >&2; exit 1; }

rm -rf "$INSTALL_ROOT"
mkdir -p "$(dirname "$INSTALL_ROOT")" "$BIN_DIR"
"$PYTHON" -m venv "$INSTALL_ROOT" || { lc_ui_printf '1;31' '%s\n' "runtime.sh: Python venv support is required" >&2; exit 1; }
VENV_PY="$INSTALL_ROOT/bin/python"
"$VENV_PY" -m pip install --disable-pip-version-check --no-index --no-deps --force-reinstall "$wheel" >/dev/null
RUNTIME_CLIENT="$INSTALL_ROOT/bin/lemoncrow-client"
RUNTIME_NAMED="$INSTALL_ROOT/bin/lemoncrow-runtime-client"
[[ -x "$RUNTIME_CLIENT" && -x "$RUNTIME_NAMED" ]] || { lc_ui_printf '1;31' '%s\n' "runtime.sh: Workspaces wheel did not provide its client" >&2; exit 1; }
ln -sfn "$RUNTIME_NAMED" "$BIN_DIR/lemoncrow-runtime-client"
# Runtime-only installations still get the stable lc identity. Review owns the
# public launcher whenever it is already installed, and delegates back here.
if [[ -x "$STATE_DIR/review/client/bin/lc" ]]; then
    ln -sfn "$STATE_DIR/review/client/bin/lc" "$BIN_DIR/lc"
else
    ln -sfn "$RUNTIME_CLIENT" "$BIN_DIR/lc"
fi
write_env
persist_uninstall_suite

# Persistent remote MCP (`lc runtime mcp serve --persistent`) runs from this
# client. Rewrite existing gateway/tunnel units for the fresh install, including
# units still pointing at the retired server gateway. No connectors: no-op.
if [[ -d "$STATE_DIR/mcp/connectors" ]]; then
    LEMONCROW_HOME="$STATE_DIR" "$RUNTIME_CLIENT" runtime mcp service repair >/dev/null 2>&1 \
        || lc_ui_printf '1;33' '%s\n' "warning: could not migrate the persistent remote MCP service; run: lc runtime mcp service repair" >&2
fi

# Migrate only existing LC connections; a fresh install still enables no hosts
# unless --agent was explicitly requested. Use the installed wheel, not checkout imports.
repair_status=0
repair_output="$(LEMONCROW_HOME="$STATE_DIR" PYTHONPATH= "$VENV_PY" -m lemoncrow_client.host_repair)" || repair_status=$?
while IFS= read -r repair_line; do
    [[ -z "$repair_line" ]] || lc_ui_printf progress '%s\n' "$repair_line"
done <<<"$repair_output"
[[ "$repair_status" -eq 0 ]] || exit "$repair_status"
if [[ -n "$AGENT" ]]; then
    PYTHONPATH= "$RUNTIME_CLIENT" runtime enable "$AGENT"
fi

# Check the real stdio entrypoint, not just --version. An import-only success can
# conceal a missing Runtime module or an incompatible launcher in release wheels.
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
    | LEMONCROW_HOME="$STATE_DIR" PYTHONPATH= "$RUNTIME_CLIENT" runtime lite-mcp --host install-check \
    >"$TEMP/mcp-check.jsonl"
"$VENV_PY" -c 'import json,sys; assert any(json.loads(line).get("result", {}).get("serverInfo", {}).get("name") == "lemoncrow-runtime-lite" for line in open(sys.argv[1]))' "$TEMP/mcp-check.jsonl"

if [[ "${LEMONCROW_INSTALL_NESTED:-0}" != 1 ]]; then
    lc_ui_printf '1;36' '\n◆ Verify installation\n'
    lc_ui_printf progress 'Checking Workspaces client...\n'
    LEMONCROW_HOME="$STATE_DIR" LEMONCROW_NO_LAZY_INSTALL=1 "$RUNTIME_CLIENT" --version >/dev/null
fi
lc_ui_printf '1;32' '\n✓ Workspaces ready\n'
if [[ "$RUNTIME_MODE" == "lite" ]]; then
    lc_ui_printf '36' '  mode:     Lite\n'
    lc_ui_printf '36' '  server:   none (stdio processes only)\n'
else
    lc_ui_printf '36' '  mode:     %s\n' "$RUNTIME_MODE"
    lc_ui_printf '1;34' '  endpoint: %s\n' "$RUNTIME_URL"
fi
lc_ui_printf '36' '  hosts:    %s\n' "${AGENT:-none enabled by installer}"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) lc_ui_printf '36' '  Shell setup: add %s to PATH\n' "$BIN_DIR" ;; esac
lc_ui_printf '1;36' '\nNext:\n'
lc_ui_printf command '  Check Workspaces:' 'lc rt status'
if [[ -z "$AGENT" ]]; then
    lc_ui_printf '1;36' '\nOptional:\n'
    lc_ui_printf command '  Enable Workspaces:' 'lc runtime enable codex' '   # or claude / opencode / auto'
fi
