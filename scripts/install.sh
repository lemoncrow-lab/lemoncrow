#!/usr/bin/env bash
# install.sh — the public LemonCrow installer. It never prompts.
#
#   curl -fsSL https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download/install.sh | bash
#
# With no options it installs Local Review (`lc`, `lcr` and a loopback Review
# service): no account and no sign-in. Hosted Review is opt-in with --hosted.
# Runtime is fetched the first time `lc rt enable <host>` needs it.
# Flags are non-interactive overrides: --product review|runtime|both,
# --local|--hosted (alias --cloud), --url, --port, --agent, --dry-run. It
# delegates to the small auditable per-product installers (hosted.sh,
# runtime.sh, local_review.sh, local_runtime.sh) and never enables a
# coding-agent host unless --agent is given.
#
# Helper trust: a published copy carries the SHA-256 of every helper script and
# product archive of its own release (stamped by scripts/pin_release_installer.sh)
# and refuses anything else. A source checkout uses its sibling scripts. A piped
# copy never reads helpers from the current directory.
#
# Compatibility: LEMONCROW_ENGINE_INSTALL=1 (sent by older full-engine
# `lc update` clients) delegates to engine_install.sh, the legacy full local
# engine bootstrap, with the original arguments.
#
#   LEMONCROW_RELEASE_TAG       Release tag to install from (default: latest)
#   LEMONCROW_RELEASE_BASE_URL  Override the release asset base (file:// or http://),
#                               e.g. to rehearse against locally staged assets
#   LEMONCROW_PRODUCT           Same as --product
#   LEMONCROW_DEPLOYMENT        Same as --local/--hosted (local|hosted|cloud)
#   LEMONCROW_RUNTIME_AGENT     Same as --agent
#   LEMONCROW_HOSTED_URL        Same as --url
#   LEMONCROW_LOCAL_SERVER_PORT Same as --port
#   LEMONCROW_DRY_RUN           If set to 1, same as --dry-run

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

# Installed Python environments must not import an inherited source checkout.
unset PYTHONPATH

# Sibling helpers are trusted only beside a real script file. A piped copy
# (`curl … | bash`) has none and must never fall back to the current directory.
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    SCRIPT_DIR=""
fi

# Release pins, stamped into the published copy by scripts/pin_release_installer.sh:
# the release tag and name=sha256 for every helper script and product archive.
# A source checkout leaves both empty.
_PUBLIC_PINNED_TAG=""
_PUBLIC_PINNED_SHA256S=""
_PUBLIC_PINS_ACTIVE=0


# ---- public product installer ------------------------------------------------
_public_install_usage() {
    cat <<'USAGE'
LemonCrow installer

Usage:
  install.sh [--product review|runtime|both] [--local|--hosted] [OPTIONS]

Products:
  review    Review code changes with Reader, evidence, comments, and Review Ask
  runtime   Use LemonCrow code intelligence and tools inside coding agents
  both      Install Review and Runtime together

Deployment:
  --local   Review uses a loopback service; Runtime uses serverless Local Lite.
            No account or sign-in. (default)
  --hosted  Use Hosted LemonCrow at api.lemoncrow.com; needs an invited account
            and `lc auth login`. --cloud is an alias.

Options:
  --agent NAME   Enable Runtime for claude, codex, opencode, or auto after install
  --url URL      Hosted server URL (default https://api.lemoncrow.com; implies --hosted
                 unless it is a loopback address)
  --port PORT    Local Review server port (default 7420)
  --dry-run      Show the resolved install plan without changing anything
  --preview      Preview progress, verification, and next steps without changes
  --non-interactive  Accepted for compatibility; the installer never prompts
  -h, --help     Show this help

Examples:
  install.sh --product review
  install.sh --product runtime --agent codex
  install.sh --product both
  install.sh --hosted

With no options, install.sh asks nothing and installs Local Review (lc, lcr and
a loopback Review service); re-running it keeps an existing Hosted Review
install hosted. Runtime is fetched on first `lc rt enable <host>`. Runtime never modifies a coding-agent host unless --agent is
provided or you later run `lc rt enable <host>`.
USAGE
}

_public_release_base_url() {
    local tag="${LEMONCROW_RELEASE_TAG:-latest}"
    if [[ -n "${LEMONCROW_RELEASE_BASE_URL:-}" ]]; then
        printf '%s\n' "${LEMONCROW_RELEASE_BASE_URL%/}"
    elif [[ "$tag" == "latest" ]]; then
        printf '%s\n' "https://github.com/lemoncrow-lab/lemoncrow/releases/latest/download"
    else
        printf '%s\n' "https://github.com/lemoncrow-lab/lemoncrow/releases/download/${tag}"
    fi
}

_public_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    else lc_ui_printf '1;31' '%s\n' "install.sh: sha256sum or shasum is required" >&2; return 1
    fi
}

_public_pinned_sha256() {
    local entry
    local -a pins
    read -r -a pins <<<"$_PUBLIC_PINNED_SHA256S"
    for entry in "${pins[@]}"; do
        if [[ "${entry%%=*}" == "$1" ]]; then printf '%s\n' "${entry#*=}"; return 0; fi
    done
    return 1
}

# Pins apply to the release they were stamped for. Nested helpers inherit that
# tag (so `latest` cannot move mid-install) and the archive pins, which replace
# the same-origin checksum sidecars.
_public_resolve_pins() {
    unset LEMONCROW_RELEASE_PINS
    local requested="${LEMONCROW_RELEASE_TAG:-latest}"
    if [[ -n "$_PUBLIC_PINNED_SHA256S" ]]; then
        if [[ -z "$_PUBLIC_PINNED_TAG" || "$requested" == latest || "$requested" == "$_PUBLIC_PINNED_TAG" ]]; then
            _PUBLIC_PINS_ACTIVE=1
            [[ -z "$_PUBLIC_PINNED_TAG" ]] || export LEMONCROW_RELEASE_TAG="$_PUBLIC_PINNED_TAG"
            export LEMONCROW_RELEASE_PINS="$_PUBLIC_PINNED_SHA256S"
            return 0
        fi
        lc_ui_printf '1;33' '%s\n' "install.sh: this installer pins release $_PUBLIC_PINNED_TAG; $requested is verified only by its own release checksums" >&2
    elif [[ -z "$SCRIPT_DIR" ]]; then
        # An unreleased piped copy has nothing to verify downloads against.
        if [[ -z "${LEMONCROW_RELEASE_BASE_URL:-}" && "$requested" == latest ]]; then
            lc_ui_printf '1;31' '%s\n' "install.sh: this copy is not a published release and carries no release pins, so it will not download helpers." \
                "  Use the published installer: curl -fsSL https://lemoncrow.com/install.sh | bash" \
                "  or run it from a source checkout: bash scripts/install.sh" >&2
            return 1
        fi
        lc_ui_printf '1;33' '%s\n' "install.sh: unpinned copy; helpers from $(_public_release_base_url) are verified only by their release checksums" >&2
    fi
    return 0
}

_public_download() {
    # _public_download URL TARGET [URL TARGET ...]: one curl call reuses the connection.
    local -a curl_args=()
    if command -v curl >/dev/null 2>&1; then
        while (( $# >= 2 )); do curl_args+=("$1" -o "$2"); shift 2; done
        curl -fLsS --retry 3 --retry-delay 2 --connect-timeout 15 "${curl_args[@]}"
    elif command -v wget >/dev/null 2>&1; then
        while (( $# >= 2 )); do wget -qO "$2" "$1" || return 1; shift 2; done
    else
        lc_ui_printf '1;31' '%s\n' "install.sh: curl or wget is required to download installer helpers" >&2
        return 1
    fi
}

_PUBLIC_HELPER_DIR_OWNED=0
_public_prepare_helpers() {
    if [[ -z "${LEMONCROW_INSTALL_HELPER_DIR:-}" ]]; then
        LEMONCROW_INSTALL_HELPER_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-install.XXXXXX")" || return 1
        _PUBLIC_HELPER_DIR_OWNED=1
        export LEMONCROW_INSTALL_HELPER_DIR
    fi
    _public_resolve_pins || return 1
    [[ "$_PUBLIC_PINS_ACTIVE" == 1 ]] || return 0
    # Pinned: materialize every pinned helper script beside the ones we run, so
    # nested helpers find verified siblings instead of fetching their own.
    local entry name base
    local -a pins downloads=()
    base="$(_public_release_base_url)"
    read -r -a pins <<<"$_PUBLIC_PINNED_SHA256S"
    for entry in "${pins[@]}"; do
        name="${entry%%=*}"
        [[ "$name" == *.sh && ! -s "$LEMONCROW_INSTALL_HELPER_DIR/$name" ]] || continue
        if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/$name" ]]; then
            cp "$SCRIPT_DIR/$name" "$LEMONCROW_INSTALL_HELPER_DIR/$name" || return 1
        else
            downloads+=("$base/$name" "$LEMONCROW_INSTALL_HELPER_DIR/$name")
        fi
    done
    if (( ${#downloads[@]} )); then
        lc_ui_printf progress 'Fetching installer helpers...\n'
        _public_download "${downloads[@]}" || {
            lc_ui_printf '1;31' '%s\n' "install.sh: could not download installer helpers from $base" >&2; return 1;
        }
    fi
    for entry in "${pins[@]}"; do
        name="${entry%%=*}"
        [[ "$name" == *.sh ]] || continue
        _public_verify_helper "$LEMONCROW_INSTALL_HELPER_DIR/$name" || return 1
        chmod +x "$LEMONCROW_INSTALL_HELPER_DIR/$name"
    done
}

_public_verify_helper() {
    local path="$1" name="${1##*/}" expected actual
    expected="$(_public_pinned_sha256 "$name")" || {
        lc_ui_printf '1;31' '%s\n' "install.sh: $name is not pinned by this release; refusing to run it" >&2; return 1;
    }
    actual="$(_public_sha256 "$path")" || return 1
    [[ "$actual" == "$expected" ]] || {
        lc_ui_printf '1;31' '%s\n' "install.sh: $name does not match the SHA-256 pinned in this installer; refusing to run it" >&2; return 1;
    }
}
_public_cleanup() {
    if [[ "$_PUBLIC_HELPER_DIR_OWNED" == "1" ]]; then
        rm -rf "$LEMONCROW_INSTALL_HELPER_DIR"
    fi
}
trap _public_cleanup EXIT

_public_fetch_helper() {
    local name="$1" target
    [[ -n "${LEMONCROW_INSTALL_HELPER_DIR:-}" ]] || {
        lc_ui_printf '1;31' '%s\n' "install.sh: helper directory was not initialized" >&2; return 1;
    }
    target="$LEMONCROW_INSTALL_HELPER_DIR/$name"
    if [[ "$_PUBLIC_PINS_ACTIVE" == 1 ]]; then
        # _public_prepare_helpers already placed and verified it; check again
        # right before use.
        _public_verify_helper "$target" || return 1
        printf '%s\n' "$target"
        return 0
    fi
    if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/$name" ]]; then
        printf '%s\n' "$SCRIPT_DIR/$name"
        return 0
    fi
    if [[ ! -s "$target" ]]; then
        _public_download "$(_public_release_base_url)/$name" "$target" || return 1
        chmod +x "$target"
    fi
    printf '%s\n' "$target"
}


# Persist activation separately from command hints. A sandbox never changes the
# default shell profile; it can be selected by sourcing its activation file.
_public_configure_shell() {
    local install_home="$1" bin_dir="$2" python
    if [[ -x "$install_home/review/client/bin/python" ]]; then
        python="$install_home/review/client/bin/python"
    elif [[ -x "$install_home/runtime/client/bin/python" ]]; then
        python="$install_home/runtime/client/bin/python"
    else
        python="$(command -v python3)" || return 1
    fi
    "$python" - "$install_home" "$bin_dir" <<'PY_SHELL'
import os
import re
import shlex
import sys
from pathlib import Path

install_home, bin_dir = map(Path, sys.argv[1:3])
install_home.mkdir(parents=True, exist_ok=True)
activation = install_home / "shell-env.sh"
activation.write_text(
    "# LemonCrow installation environment\n"
    + f"export LEMONCROW_HOME={shlex.quote(str(install_home))}\n"
    + f"export LEMONCROW_BIN_DIR={shlex.quote(str(bin_dir))}\n"
    + f'case "$PATH" in {shlex.quote(str(bin_dir))}|{shlex.quote(str(bin_dir))}:*) ;;\n'
    + f'    *) export PATH={shlex.quote(str(bin_dir))}:"$PATH" ;;\n'
    + "esac\n"
    + "unset LEMONCROW_URL PYTHONPATH\n",
    encoding="utf-8",
)
if install_home.resolve() != (Path.home() / ".lemoncrow").resolve():
    raise SystemExit(0)
shell = Path(os.environ.get("SHELL", "")).name
if shell == "zsh":
    profiles = [Path(os.environ.get("ZDOTDIR") or str(Path.home())) / ".zshrc"]
elif shell == "bash":
    login = next((Path.home() / name for name in (".bash_profile", ".bash_login", ".profile")
                  if (Path.home() / name).exists()), Path.home() / ".profile")
    profiles = [Path.home() / ".bashrc", login]
else:
    # Other shells can use the explicit activation hint without changing their rc.
    raise SystemExit(0)
start, end = "# >>> lemoncrow path setup >>>", "# <<< lemoncrow path setup <<<"
block = f"{start}\n. {shlex.quote(str(activation))}\n{end}\n"
for profile in profiles:
    text = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if (start in text) != (end in text):
        raise RuntimeError(f"incomplete LemonCrow block in {profile}; leaving it unchanged")
    updated = re.sub(r"(?m)^" + re.escape(start) + r"\n.*?^" + re.escape(end) + r"\n?",
                     "", text, flags=re.DOTALL)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(updated.rstrip("\n") + ("\n\n" if updated else "") + block, encoding="utf-8")
PY_SHELL
}

# Shared by a real install and --preview. Preview renders checks without running them.
# The caller owns product/deployment and the resolved home/bin values.
_public_verify_install() {
    lc_ui_printf '1;36' '\n◆ Verify installation\n'
    local exe helper elapsed=0 timeout="${LEMONCROW_LOCAL_SERVER_START_TIMEOUT:-120}"
    local -a checks=(lc)
    [[ "$product" == runtime ]] || checks+=(lcr)
    for exe in "${checks[@]}"; do
        lc_ui_printf progress 'Checking %s...\n' "$exe"
        [[ "$preview" == 1 ]] && continue
        if [[ ! -x "$bin_hint/$exe" ]]; then
            lc_ui_printf '1;31' 'Installed command is missing: %s\n' "$exe" >&2; return 1
        fi
        if [[ "$exe" == lc ]]; then
            LEMONCROW_HOME="$home_hint" LEMONCROW_NO_LAZY_INSTALL=1 "$bin_hint/$exe" --version >/dev/null || {
                lc_ui_printf '1;31' 'lc verification failed\n' >&2; return 1;
            }
        else
            LEMONCROW_HOME="$home_hint" LEMONCROW_NO_LAZY_INSTALL=1 "$bin_hint/$exe" --help >/dev/null || {
                lc_ui_printf '1;31' 'lcr verification failed\n' >&2; return 1;
            }
        fi
    done
    if [[ "$product" != review ]]; then
        lc_ui_printf progress 'Checking Runtime client...\n'
        if [[ "$preview" != 1 ]]; then
            exe="$home_hint/runtime/client/bin/lemoncrow-client"
            [[ -x "$exe" ]] && LEMONCROW_HOME="$home_hint" LEMONCROW_NO_LAZY_INSTALL=1 "$exe" --version >/dev/null || {
                lc_ui_printf '1;31' 'Runtime client verification failed\n' >&2; return 1;
            }
        fi
    fi
    if [[ "$product" != runtime && "$deployment" == local ]]; then
        lc_ui_printf progress 'Checking local Review server on port %s...\n' "$port"
        if [[ "$preview" != 1 ]]; then
            helper="$home_hint/install/scripts/local_server.sh"
            if [[ ! -f "$helper" ]]; then
                lc_ui_printf '1;31' 'Local server lifecycle helper is missing\n' >&2; return 1
            fi
            until LEMONCROW_HOME="$home_hint" LEMONCROW_LOCAL_SERVER_PORT="$port" bash "$helper" status >/dev/null 2>&1; do
                if [[ "$elapsed" -ge "$timeout" ]]; then
                    lc_ui_printf '1;31' 'Review server did not remain healthy on port %s after %s seconds\n' "$port" "$timeout" >&2
                    LEMONCROW_HOME="$home_hint" LEMONCROW_LOCAL_SERVER_PORT="$port" bash "$helper" diagnose || true
                    return 1
                fi
                if (( elapsed % 10 == 0 )); then lc_ui_printf progress 'Waiting for Review server: %s of %s seconds\n' "$elapsed" "$timeout"; fi
                sleep 1; elapsed=$((elapsed + 1))
            done
        fi
    elif [[ "$product" != runtime ]]; then
        lc_ui_printf progress 'Checking hosted Review configuration...\n'
        if [[ "$preview" != 1 ]] && ! grep -qx "LEMONCROW_INSTALL_MODE=hosted" "$home_hint/review/env"; then
            lc_ui_printf '1;31' 'Hosted Review configuration verification failed\n' >&2; return 1
        fi
    fi
    if [[ "$preview" == 1 ]]; then
        lc_ui_printf '36' 'Verification steps previewed; no checks were run.\n'
    else
        lc_ui_printf '1;32' '✓ Installation verified\n'
    fi
}

_public_show_summary() {
    local product_label
    case "$deployment:$product" in
        local:review) product_label="Local Review" ;;
        cloud:review) product_label="Hosted Review" ;;
        local:runtime) product_label="Runtime Lite" ;;
        cloud:runtime) product_label="Hosted Runtime" ;;
        local:both) product_label="Local Review and Runtime Lite" ;;
        cloud:both) product_label="Hosted Review and Runtime" ;;
    esac
    if [[ "$preview" == 1 ]]; then
        lc_ui_printf '1;36' '\n◆ Expected result: %s (not installed)\n' "$product_label"
    else
        lc_ui_printf '1;32' '\n✓ %s ready\n' "$product_label"
    fi
    if [[ "$product" == review || "$product" == both ]]; then
        if [[ "$deployment" == local ]]; then
            lc_ui_printf '36' '  Review endpoint: http://127.0.0.1:%s\n' "$port"
        else
            lc_ui_printf '36' '  Review endpoint: %s\n' "$cloud_url"
        fi
    fi
    if [[ "$preview" != 1 && "${home_hint%/}" == "$HOME/.lemoncrow" && "${SHELL:-}" =~ (^|/)(bash|zsh)$ ]]; then
        lc_ui_printf '36' '  Shell profile configured; new terminals are ready.\n'
    fi
    if [[ "$preview" == 1 ]]; then
        lc_ui_printf '1;36' '\nAfter a real installation:\n'
    else
        lc_ui_printf '1;36' '\nNext:\n'
    fi
    lc_ui_printf '36' '  Activate in this terminal: source %q/shell-env.sh\n' "$home_hint"
    if [[ "$deployment" == cloud ]]; then
        lc_ui_printf command '  Sign in:' 'lc auth login' '   # Hosted needs an invited account'
    fi
    if [[ "$product" == review || "$product" == both ]]; then
        lc_ui_printf command '  Open Review:' 'lcr'
    fi
    if [[ "$product" != review ]]; then
        lc_ui_printf command '  Check Runtime:' 'lc rt status'
    fi
    if [[ -z "$agent" || "$deployment" == local ]]; then
        lc_ui_printf '1;36' '\nOptional:\n'
    fi
    if [[ -z "$agent" ]]; then
        lc_ui_printf command '  Enable Runtime for an agent:' 'lc runtime enable codex' '   # or claude / opencode / auto'
    fi
    if [[ "$deployment" == local ]]; then
        lc_ui_printf command '  Use Hosted LemonCrow instead:' 'curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --hosted' '   # invited account'
    fi
    lc_ui_printf '36' '\nTroubleshooting: lc status · https://docs.lemoncrow.com/setup/troubleshooting\n'
}

# Print the URL of an existing hosted Review/Runtime install under HOME; fail if none.
_public_recorded_hosted_url() {
    local env_file line mode url
    for env_file in "$1/review/env" "$1/runtime/env"; do
        [[ -f "$env_file" ]] || continue
        mode="" url=""
        while IFS= read -r line || [[ -n "$line" ]]; do
            case "$line" in
                LEMONCROW_INSTALL_MODE=*) mode="${line#*=}" ;;
                LEMONCROW_URL=*) url="${line#*=}" ;;
            esac
        done <"$env_file"
        # The first recorded product decides; a local Review is never overridden.
        [[ "$mode" == hosted ]] || return 1
        printf '%s\n' "${url:-https://api.lemoncrow.com}"
        return 0
    done
    return 1
}

_public_install() {
    local product="${LEMONCROW_PRODUCT:-}"
    local deployment="${LEMONCROW_DEPLOYMENT:-}"
    local agent="${LEMONCROW_RUNTIME_AGENT:-}"
    local cloud_url="${LEMONCROW_HOSTED_URL:-${LEMONCROW_URL:-}}"
    local port="${LEMONCROW_LOCAL_SERVER_PORT:-7420}"
    local dry_run="${LEMONCROW_DRY_RUN:-0}" preview=0
    local non_interactive="${LEMONCROW_NON_INTERACTIVE:-0}"
    local explicit_deployment=0
    local -a args=("$@")
    local i=0 arg

    while (( i < ${#args[@]} )); do
        arg="${args[$i]}"
        case "$arg" in
            --product)
                (( i + 1 < ${#args[@]} )) || { lc_ui_printf '1;31' '%s\n' "install.sh: --product needs a value" >&2; return 2; }
                product="${args[$((i+1))]}"; i=$((i+2));;
            --product=*) product="${arg#--product=}"; i=$((i+1));;
            --cloud|--hosted) deployment="cloud"; explicit_deployment=1; i=$((i+1));;
            --local) deployment="local"; explicit_deployment=1; i=$((i+1));;
            --agent)
                (( i + 1 < ${#args[@]} )) || { lc_ui_printf '1;31' '%s\n' "install.sh: --agent needs a value" >&2; return 2; }
                agent="${args[$((i+1))]}"; i=$((i+2));;
            --agent=*) agent="${arg#--agent=}"; i=$((i+1));;
            --url)
                (( i + 1 < ${#args[@]} )) || { lc_ui_printf '1;31' '%s\n' "install.sh: --url needs a value" >&2; return 2; }
                cloud_url="${args[$((i+1))]}"; i=$((i+2));;
            --url=*) cloud_url="${arg#--url=}"; i=$((i+1));;
            --port)
                (( i + 1 < ${#args[@]} )) || { lc_ui_printf '1;31' '%s\n' "install.sh: --port needs a value" >&2; return 2; }
                port="${args[$((i+1))]}"; i=$((i+2));;
            --port=*) port="${arg#--port=}"; i=$((i+1));;
            --preview) preview=1; i=$((i+1));;
            --dry-run) dry_run=1; i=$((i+1));;
            --non-interactive) non_interactive=1; i=$((i+1));;
            -h|--help|help) _public_install_usage; return 0;;
            *) lc_ui_printf '1;31' '%s\n' "install.sh: unknown option: $arg" >&2; _public_install_usage >&2; return 2;;
        esac
    done

    case "$product" in review|runtime|both|"") ;; *) lc_ui_printf '1;31' '%s\n' "install.sh: --product must be review, runtime, or both" >&2; return 2;; esac
    case "$deployment" in cloud|hosted) deployment="cloud";; local|"") ;; *) lc_ui_printf '1;31' '%s\n' "install.sh: deployment must be local or hosted" >&2; return 2;; esac
    case "$agent" in ""|auto|claude|codex|opencode) ;; *) lc_ui_printf '1;31' '%s\n' "install.sh: --agent must be claude, codex, opencode, or auto" >&2; return 2;; esac

    # No prompts: bare install is Local Review (no account). Runtime is fetched
    # the first time `lc rt enable <host>` needs it. Hosted is opt-in: --hosted,
    # a non-loopback --url, or an existing hosted install being re-run.
    [[ -z "$product" ]] && product="review"
    local home_hint="${LEMONCROW_HOME:-$HOME/.lemoncrow}"
    local bin_hint="${LEMONCROW_BIN_DIR:-$home_hint/bin}"
    if [[ -z "$deployment" && -z "$cloud_url" ]]; then
        cloud_url="$(_public_recorded_hosted_url "$home_hint")" && deployment="cloud" \
            && lc_ui_printf '36' 'Keeping the existing Hosted install at %s (use --local to switch).\n' "$cloud_url"
    fi
    if [[ -z "$deployment" ]]; then
        case "$cloud_url" in
            ""|http://localhost|http://localhost/*|http://localhost:*|http://127.0.0.1|http://127.0.0.1/*|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]/*|http://\[::1\]:*) deployment="local";;
            *) deployment="cloud";;
        esac
    fi
    [[ -n "$cloud_url" ]] || cloud_url="https://api.lemoncrow.com"

    if [[ -n "$agent" && "$product" == "review" ]]; then
        lc_ui_printf '1;31' '%s\n' "install.sh: --agent requires Runtime; choose --product runtime or --product both" >&2
        return 2
    fi
    if [[ "$deployment" == "local" && ( "$product" == "review" || "$product" == "both" ) ]]; then
        [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && (( port <= 65535 )) || { lc_ui_printf '1;31' '%s\n' "install.sh: invalid --port: $port" >&2; return 2; }
    fi

    if [[ "$preview" == 1 ]]; then
        lc_ui_printf '36' 'Preview only: no downloads, installation, or shell changes.\n'
        lc_ui_printf '1;36' '\n◆ LemonCrow install\n'
        if [[ "$deployment" == local && "$product" != runtime ]]; then
            lc_ui_printf progress 'Installing Review engine...\n'
            lc_ui_printf progress 'Installing engine dependencies...\n'
        fi
        if [[ "$product" != runtime ]]; then
            lc_ui_printf progress 'Installing Review client...\n'
        fi
        if [[ "$product" != review ]]; then
            lc_ui_printf progress 'Installing Runtime client...\n'
        fi
        if [[ "$deployment" == local && "$product" != runtime ]]; then
            lc_ui_printf progress 'Starting local Review server...\n'
        fi
        _public_verify_install || return $?
        lc_ui_printf progress 'Configuring shell activation...\n'
        _public_show_summary
        return 0
    fi
    lc_ui_printf '1;36' '\n◆ LemonCrow install\n'
    if [[ "${LEMONCROW_VERBOSE:-0}" == 1 || "$dry_run" == 1 ]]; then
        lc_ui_printf '36' '  Products:   %s\n' "$product"
        lc_ui_printf '36' '  Deployment: %s\n' "$deployment"
    fi
    if [[ "${LEMONCROW_VERBOSE:-0}" == 1 || "$dry_run" == 1 ]]; then
        if [[ "$deployment" == local && "$product" != review ]]; then
            lc_ui_printf '36' '  Runtime:    Local Lite (no server/daemon)\n'
        fi
        if [[ "$product" != review ]]; then
            lc_ui_printf '36' '  Agent:      %s\n' "${agent:-not enabled}"
        fi
    fi

    _public_prepare_helpers || return 1
    local review_helper runtime_helper
    local -a review_args runtime_args
    if [[ "$deployment" == "cloud" ]]; then
        # LEMONCROW_DEPLOYMENT=cloud keeps a loopback --url hosted (a local
        # enterprise server) instead of the helpers' loopback-means-local default.
        if [[ "$product" == "review" || "$product" == "both" ]]; then
            lc_ui_printf progress 'Installing Review client...\n'
            review_helper="$(_public_fetch_helper hosted.sh)" || return 1
            review_args=(--url "$cloud_url")
            [[ "$dry_run" == "1" ]] && review_args+=(--dry-run)
            LEMONCROW_INSTALL_NESTED=1 LEMONCROW_DEPLOYMENT=cloud bash "$review_helper" "${review_args[@]}" || return $?
        fi
        if [[ "$product" == "runtime" || "$product" == "both" ]]; then
            lc_ui_printf progress 'Installing Runtime client...\n'
            runtime_helper="$(_public_fetch_helper runtime.sh)" || return 1
            runtime_args=(--url "$cloud_url")
            [[ -n "$agent" ]] && runtime_args+=(--agent "$agent")
            [[ "$dry_run" == "1" ]] && runtime_args+=(--dry-run)
            LEMONCROW_INSTALL_NESTED=1 LEMONCROW_DEPLOYMENT=cloud bash "$runtime_helper" "${runtime_args[@]}" || return $?
        fi
    else
        if [[ "$product" == "review" || "$product" == "both" ]]; then
            review_helper="$(_public_fetch_helper local_review.sh)" || return 1
            review_args=(--port "$port")
            [[ "$dry_run" == "1" ]] && review_args+=(--dry-run)
            LEMONCROW_INSTALL_NESTED=1 bash "$review_helper" "${review_args[@]}" || return $?
        fi
        if [[ "$product" == "runtime" || "$product" == "both" ]]; then
            lc_ui_printf progress 'Installing Runtime Lite...\n'
            runtime_helper="$(_public_fetch_helper local_runtime.sh)" || return 1
            runtime_args=()
            [[ -n "$agent" ]] && runtime_args+=(--agent "$agent")
            [[ "$dry_run" == "1" ]] && runtime_args+=(--dry-run)
            LEMONCROW_INSTALL_NESTED=1 bash "$runtime_helper" "${runtime_args[@]}" || return $?
        fi
    fi

    [[ "$dry_run" == "1" ]] && return 0
    _public_verify_install || return $?
    lc_ui_printf progress 'Configuring shell activation...\n'
    _public_configure_shell "$home_hint" "$bin_hint" || return $?
    _public_show_summary
}

# Older full-engine clients use this shim with the original arguments.
if [[ "${LEMONCROW_ENGINE_INSTALL:-0}" == "1" ]]; then
    _public_prepare_helpers || exit 1
    _engine_installer="$(_public_fetch_helper engine_install.sh)" \
        || { lc_ui_printf '1;31' '%s\n' "install.sh: could not obtain engine_install.sh from $(_public_release_base_url)" >&2; exit 1; }
    bash "$_engine_installer" "$@"
    exit $?
fi

_public_install "$@"
