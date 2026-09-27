#!/usr/bin/env bash
# hosted.sh — Install the auditable LemonCrow hosted thin client.
#
# This path intentionally does NOT call install.sh or bundle.sh. It installs one
# verified architecture-neutral lemoncrow-client wheel into a private venv,
# persists only the hosted endpoint under ~/.lemoncrow, and asks supported agent
# CLIs to register `lemoncrow-client mcp` using their native configuration APIs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
HOSTED_URL="${LEMONCROW_HOSTED_URL:-${LEMONCROW_URL:-https://api.lemoncrow.com}}"
RELEASE_TAG="${LEMONCROW_RELEASE_TAG:-latest}"
STATE_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
BIN_DIR="${LEMONCROW_BIN_DIR:-${STATE_DIR}/bin}"
INSTALL_ROOT="${LEMONCROW_HOSTED_INSTALL_ROOT:-${STATE_DIR}/hosted}"
ALLOW_UNVERIFIED="${LEMONCROW_ALLOW_UNVERIFIED:-0}"
FROM_BUILD=""
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
NO_HOSTS="${LEMONCROW_NO_HOSTS:-0}"
AGENT="auto"

usage() {
    cat <<'EOF'
usage: hosted.sh [OPTIONS]

  --url URL              LemonCrow server (default https://api.lemoncrow.com)
  --from-build [ARCHIVE] install local dist/lemoncrow-hosted-client.tar.gz
  --agent NAME           auto (default), claude, codex, or opencode
  --no-hosts             do not register coding-agent MCP integrations
  --dry-run              print the resolved installation plan and exit

Environment overrides:
  LEMONCROW_RELEASE_TAG, LEMONCROW_HOME, LEMONCROW_BIN_DIR,
  LEMONCROW_HOSTED_INSTALL_ROOT, LEMONCROW_ALLOW_UNVERIFIED
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url)
            [[ $# -ge 2 ]] || { echo "hosted.sh: --url needs a value" >&2; exit 2; }
            HOSTED_URL="$2"; shift 2 ;;
        --url=*) HOSTED_URL="${1#--url=}"; shift ;;
        --from-build)
            if [[ $# -ge 2 && "$2" != --* ]]; then FROM_BUILD="$2"; shift 2; else FROM_BUILD="${SCRIPT_DIR}/../dist/lemoncrow-hosted-client.tar.gz"; shift; fi ;;
        --from-build=*) FROM_BUILD="${1#--from-build=}"; shift ;;
        --agent)
            [[ $# -ge 2 ]] || { echo "hosted.sh: --agent needs a value" >&2; exit 2; }
            AGENT="$2"; shift 2 ;;
        --agent=*) AGENT="${1#--agent=}"; shift ;;
        --no-hosts) NO_HOSTS=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help|help) usage; exit 0 ;;
        --local) echo "hosted.sh: --local was renamed to --from-build" >&2; exit 2 ;;
        *) echo "hosted.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$AGENT" in auto|claude|codex|opencode) ;; *) echo "hosted.sh: unsupported --agent: $AGENT" >&2; exit 2 ;; esac
case "$HOSTED_URL" in
    https://*) ;;
    http://localhost|http://localhost/*|http://localhost:*|http://127.0.0.1|http://127.0.0.1/*|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]/*|http://\[::1\]:*) ;;
    http://*) echo "hosted.sh: plaintext HTTP is allowed only for literal loopback; use HTTPS: $HOSTED_URL" >&2; exit 2 ;;
    *) echo "hosted.sh: hosted URL must be absolute HTTPS (or loopback HTTP): $HOSTED_URL" >&2; exit 2 ;;
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
    else echo "hosted.sh: sha256sum or shasum is required" >&2; return 1
    fi
}

fetch_to() {
    local url="$1" target="$2"
    if command -v curl >/dev/null 2>&1; then curl -fLsS --retry 3 --retry-delay 2 --connect-timeout 15 "$url" -o "$target"
    elif command -v wget >/dev/null 2>&1; then wget -qO "$target" "$url"
    else echo "hosted.sh: curl or wget is required" >&2; return 1
    fi
}

verify_archive() {
    local archive="$1" checksum_file="$2" expected actual
    expected="$(grep -oE '[0-9a-fA-F]{64}' "$checksum_file" 2>/dev/null | head -1 | tr 'A-F' 'a-f' || true)"
    if [[ -z "$expected" ]]; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then
            echo "hosted.sh: WARNING: no valid SHA-256 checksum; proceeding because LEMONCROW_ALLOW_UNVERIFIED=1" >&2
            return 0
        fi
        echo "hosted.sh: missing or invalid SHA-256 checksum; refusing unverified hosted install" >&2
        return 1
    fi
    actual="$(sha256_file "$archive")"
    [[ "$actual" == "$expected" ]] || { echo "hosted.sh: checksum mismatch; refusing hosted install" >&2; return 1; }
}

write_env() {
    local target="$STATE_DIR/env" tmp
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR" 2>/dev/null || true
    tmp="$(mktemp "${target}.XXXXXX")"
    if [[ -f "$target" ]]; then
        grep -Ev '^(LEMONCROW_URL|LEMONCROW_HOSTED_URL|LEMONCROW_INSTALL_MODE)=' "$target" >"$tmp" || true
    fi
    printf 'LEMONCROW_URL=%s\nLEMONCROW_HOSTED_URL=%s\nLEMONCROW_INSTALL_MODE=hosted\n' "$HOSTED_URL" "$HOSTED_URL" >>"$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$target"
}

PYTHON="$(find_python || true)"
[[ -n "$PYTHON" ]] || { echo "hosted.sh: Python 3.12 or 3.13 is required" >&2; exit 1; }

if [[ "$RELEASE_TAG" == "latest" ]]; then
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
else
    BASE_URL="https://github.com/lemoncrow-lab/lemoncrow/releases/download/${RELEASE_TAG}"
fi
ASSET="lemoncrow-hosted-client.tar.gz"

if [[ "$DRY_RUN" == "1" ]]; then
    printf 'Hosted endpoint: %s\n' "$HOSTED_URL"
    printf 'Python: %s\n' "$PYTHON"
    printf 'Install root: %s\n' "$INSTALL_ROOT"
    printf 'CLI bin dir: %s\n' "$BIN_DIR"
    if [[ -n "$FROM_BUILD" ]]; then printf 'Artifact: %s (local build)\n' "$FROM_BUILD"; else printf 'Artifact: %s/%s (SHA-256 required)\n' "$BASE_URL" "$ASSET"; fi
    printf 'Agent registration: %s\n' "$([[ "$NO_HOSTS" == "1" ]] && echo disabled || echo "$AGENT")"
    exit 0
fi

TEMP="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-hosted-install.XXXXXX")"
trap 'rm -rf "${TEMP:-}"' EXIT
archive="$TEMP/$ASSET"
checksum="$TEMP/$ASSET.sha256"

if [[ -n "$FROM_BUILD" ]]; then
    [[ -s "$FROM_BUILD" ]] || { echo "hosted.sh: local hosted artifact not found: $FROM_BUILD" >&2; exit 1; }
    cp "$FROM_BUILD" "$archive"
    if [[ -s "${FROM_BUILD}.sha256" ]]; then cp "${FROM_BUILD}.sha256" "$checksum"; else printf '%s\n' "$(sha256_file "$archive")" >"$checksum"; fi
else
    fetch_to "$BASE_URL/$ASSET" "$archive" || { echo "hosted.sh: could not download $BASE_URL/$ASSET" >&2; exit 1; }
    if ! fetch_to "$BASE_URL/$ASSET.sha256" "$checksum"; then
        if [[ "$ALLOW_UNVERIFIED" == "1" ]]; then : >"$checksum"; else echo "hosted.sh: checksum sidecar is required: $BASE_URL/$ASSET.sha256" >&2; exit 1; fi
    fi
fi
verify_archive "$archive" "$checksum"

tar -xzf "$archive" -C "$TEMP"
wheel="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_client-*-py3-none-any.whl' -print -quit 2>/dev/null || true)"
wheel_count="$(find "$TEMP/vendor" -maxdepth 1 -type f -name 'lemoncrow_client-*-py3-none-any.whl' -print 2>/dev/null | wc -l | tr -d ' ')"
[[ "$wheel_count" == "1" && -n "$wheel" ]] || { echo "hosted.sh: artifact must contain exactly one lemoncrow-client py3-none-any wheel" >&2; exit 1; }

rm -rf "$INSTALL_ROOT/venv"
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
"$PYTHON" -m venv "$INSTALL_ROOT/venv" || { echo "hosted.sh: Python venv support is required (install the venv/ensurepip package for $PYTHON)" >&2; exit 1; }
VENV_PY="$INSTALL_ROOT/venv/bin/python"
"$VENV_PY" -m pip install --disable-pip-version-check --no-index --no-deps --force-reinstall "$wheel" >/dev/null
CLIENT="$INSTALL_ROOT/venv/bin/lemoncrow-client"
[[ -x "$CLIENT" ]] || { echo "hosted.sh: installed wheel did not provide lemoncrow-client" >&2; exit 1; }
ln -sfn "$CLIENT" "$BIN_DIR/lemoncrow-client"
ln -sfn "$CLIENT" "$BIN_DIR/lc"
write_env

if [[ "$NO_HOSTS" != "1" ]]; then
    if [[ "$AGENT" == "auto" ]] && ! command -v claude >/dev/null 2>&1 && ! command -v codex >/dev/null 2>&1 && ! command -v opencode >/dev/null 2>&1; then
        echo "Hosted client installed; no supported coding-agent CLI was detected for MCP registration."
    else
        PATH="$BIN_DIR:$PATH" "$CLIENT" install --agent "$AGENT"
    fi
fi

printf '✓ LemonCrow hosted client installed\n'
printf '  endpoint: %s\n' "$HOSTED_URL"
printf '  client:   %s\n' "$CLIENT"
printf '  command:  %s/lc\n' "$BIN_DIR"
printf '  sign in:  %s/lc auth login\n' "$BIN_DIR"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) printf '  PATH:     add %s to PATH for the lc shortcut\n' "$BIN_DIR" ;; esac
