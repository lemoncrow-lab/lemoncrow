#!/usr/bin/env bash
# local_server.sh — lifecycle for the loopback LemonCrow server used by local installs.
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


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)/frontend/dist"

HOME_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
TOOL_DIR="${LEMONCROW_TOOL_DIR:-${HOME_DIR}/uv-tools}"
PYTHON_OVERRIDE="${LEMONCROW_SERVER_PYTHON:-}"
PORT="${LEMONCROW_LOCAL_SERVER_PORT:-7420}"
URL="http://127.0.0.1:${PORT}"
STATE_DIR="${LEMONCROW_LOCAL_SERVER_DIR:-${HOME_DIR}/server}"
FRONTEND_DIR="${LEMONCROW_FRONTEND_DIR:-${HOME_DIR}/install/frontend}"
TARGET_PRODUCT_FILE="${STATE_DIR}/target-product"
TARGET_PRODUCT="${LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT:-}"
if [[ -z "$TARGET_PRODUCT" && -f "$TARGET_PRODUCT_FILE" ]]; then
    TARGET_PRODUCT="$(head -n 1 "$TARGET_PRODUCT_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
fi
TARGET_PRODUCT="${TARGET_PRODUCT:-both}"
# Read-only migration for servers prepared before product-owned state existed.
# Never write the old shared ~/.lemoncrow/env again.
if [[ "$TARGET_PRODUCT" == "legacy" ]]; then
    TARGET_PRODUCT="both"
fi
case "$TARGET_PRODUCT" in
    review|runtime|both|none) ;;
    *) lc_ui_printf '1;31' '%s\n' "LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT must be review, runtime, both, or none" >&2; exit 2 ;;
esac
SERVER_PRODUCTS="${LEMONCROW_LOCAL_SERVER_PRODUCTS:-}"
if [[ -z "$SERVER_PRODUCTS" ]]; then
    # Runtime tools run in Runtime Lite; the server keeps Runtime's Dashboard.
    case "$TARGET_PRODUCT" in
        review) SERVER_PRODUCTS="review" ;;
        runtime) SERVER_PRODUCTS="dashboard" ;;
        none) SERVER_PRODUCTS="none" ;;
        *) SERVER_PRODUCTS="review,dashboard" ;;
    esac
fi
case ",$SERVER_PRODUCTS," in
    ,review,|,dashboard,|,runtime,|,review,dashboard,|,dashboard,review,|,review,runtime,|,runtime,review,|,none,) ;;
    *) lc_ui_printf '1;31' '%s\n' "LEMONCROW_LOCAL_SERVER_PRODUCTS must be review, dashboard, review,dashboard, or none" >&2; exit 2 ;;
esac
export LEMONCROW_LOCAL_SERVER_PRODUCTS="$SERVER_PRODUCTS"
REVIEW_ENGINE_PYTHON="${LEMONCROW_REVIEW_ENGINE_PYTHON:-${HOME_DIR}/review/engine/bin/python}"
RUNTIME_ENGINE_PYTHON="${LEMONCROW_RUNTIME_ENGINE_PYTHON:-${TOOL_DIR}/lemoncrow/bin/python}"
if [[ -n "$PYTHON_OVERRIDE" ]]; then
    PYTHON="$PYTHON_OVERRIDE"
else
    case "$TARGET_PRODUCT" in
        review)
            if [[ -x "$REVIEW_ENGINE_PYTHON" ]]; then PYTHON="$REVIEW_ENGINE_PYTHON"; else PYTHON="$RUNTIME_ENGINE_PYTHON"; fi
            ;;
        runtime|both) PYTHON="$RUNTIME_ENGINE_PYTHON" ;;
        none)
            if [[ -x "$RUNTIME_ENGINE_PYTHON" ]]; then PYTHON="$RUNTIME_ENGINE_PYTHON"; else PYTHON="$REVIEW_ENGINE_PYTHON"; fi
            ;;
    esac
fi
TOKEN_FILE="${LEMONCROW_LOCAL_TOKEN_FILE:-${HOME_DIR}/token}"
PID_FILE="${STATE_DIR}/server.pid"
LOG_FILE="${STATE_DIR}/server.log"
RESTART_LOG_FILE="${STATE_DIR}/restart.log"
RESTART_STATUS_FILE="${STATE_DIR}/restart.status"
RESTART_HANDOFF_SECONDS="${LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF:-1}"
SUPERVISOR="${LEMONCROW_LOCAL_SERVER_SUPERVISOR:-auto}"
HEALTH_TIMEOUT_SECONDS="${LEMONCROW_LOCAL_SERVER_START_TIMEOUT:-120}"
# A non-default LemonCrow home (oss-dev sandbox, oss-test, tests) gets
# its own unit and launchd label, so it can never rewrite, restart or stop the
# live install's server.
SERVICE_SUFFIX=""
if [[ "${HOME_DIR%/}" != "${HOME}/.lemoncrow" ]]; then
    SERVICE_SUFFIX="-$(printf '%s' "${HOME_DIR%/}" | cksum | awk '{print $1}')"
fi
SYSTEMD_UNIT="lemoncrow-local-server${SERVICE_SUFFIX}.service"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SYSTEMD_UNIT_FILE="${SYSTEMD_USER_DIR}/${SYSTEMD_UNIT}"
LAUNCHD_LABEL="com.lemoncrow.local-server${SERVICE_SUFFIX}"
LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
LAUNCHD_PLIST="${LAUNCHD_DIR}/${LAUNCHD_LABEL}.plist"

if [[ ! "$HEALTH_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    lc_ui_printf '1;31' '%s\n' "LEMONCROW_LOCAL_SERVER_START_TIMEOUT must be a positive integer" >&2
    exit 2
fi
if [[ ! "$RESTART_HANDOFF_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    lc_ui_printf '1;31' '%s\n' "LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF must be a non-negative number" >&2
    exit 2
fi

host_path() {
    # launchd hands jobs its own default PATH (/usr/bin:/bin:/usr/sbin:/sbin) and
    # systemd --user hands them the same four directories, so every agent the user
    # installed under ~/.local/bin, ~/.opencode/bin, ~/.lemoncrow/bin or Homebrew
    # reads as "not installed" inside the supervised server. Compose the PATH the
    # user's own login shell would give them: this shell's PATH first, then the
    # login shell's, then the known install directories that exist here.
    local login_path="" entry candidate joined=""
    local -a resolved=() inherited=() login=()
    local -a extras=(
        /opt/homebrew/bin /usr/local/bin
        "${HOME}/.local/bin" "${HOME}/.lemoncrow/bin" "${HOME}/.opencode/bin"
        "${HOME}/.bun/bin" "${HOME}/.npm-global/bin" "${HOME}/.cargo/bin"
        "${HOME}/.docker/bin" "/Applications/Docker.app/Contents/Resources/bin"
    )
    if [[ -n "${SHELL:-}" && -x "${SHELL}" ]]; then
        login_path="$("${SHELL}" -l -c 'printf "%s" "$PATH"' 2>/dev/null || true)"
    elif [[ -x /bin/zsh ]]; then
        # A systemd-run or launchd-spawned restart worker inherits no $SHELL, so
        # fall back to the shell macOS and Linux users actually log in with.
        login_path="$(/bin/zsh -l -c 'printf "%s" "$PATH"' 2>/dev/null || true)"
    fi
    IFS=: read -r -a inherited <<< "${PATH:-}"
    IFS=: read -r -a login <<< "$login_path"
    for candidate in "${inherited[@]:-}" "${login[@]:-}" "${extras[@]}"; do
        entry="${candidate/#\~/$HOME}"
        [[ -n "$entry" && -d "$entry" ]] || continue
        case ":${joined}:" in
            *":$entry:"*) continue ;;
        esac
        joined="${joined:+$joined:}$entry"
        resolved+=("$entry")
    done
    (IFS=:; printf '%s' "${resolved[*]-}")
}

SERVER_PATH=""
resolve_server_path() {
    # Every start form resolves the supervised server's PATH once: systemd and
    # launchd write it into their unit, and the fallback child inherits it here.
    [[ -n "$SERVER_PATH" ]] && return 0
    SERVER_PATH="$(host_path)"
    [[ -n "$SERVER_PATH" ]] || SERVER_PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    export PATH="$SERVER_PATH"
}

_write_target_file() {
    local env_file="$1" tmp
    mkdir -p "$(dirname "$env_file")"
    chmod 700 "$HOME_DIR" "$(dirname "$env_file")" 2>/dev/null || true
    tmp="${env_file}.tmp.$$"
    if [[ -f "$env_file" ]]; then
        grep -Ev '^(LEMONCROW_URL|LEMONCROW_INSTALL_MODE|LEMONCROW_LOCAL_FS|LEMONCROW_TOKEN_FILE)=' "$env_file" >"$tmp" || true
    else
        : >"$tmp"
    fi
    printf 'LEMONCROW_URL=%s\n' "$URL" >>"$tmp"
    printf 'LEMONCROW_INSTALL_MODE=local\n' >>"$tmp"
    printf 'LEMONCROW_LOCAL_FS=1\n' >>"$tmp"
    printf 'LEMONCROW_TOKEN_FILE=%s\n' "$TOKEN_FILE" >>"$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$env_file"
}

write_target() {
    mkdir -p "$HOME_DIR" "$STATE_DIR"
    # Only Review targets this server. Runtime runs as Runtime Lite and never
    # reads an endpoint from it, so runtime/env is not written.
    case "$TARGET_PRODUCT" in
        review|both) _write_target_file "$HOME_DIR/review/env" ;;
        runtime|none) ;;
    esac
    printf '%s\n' "$TARGET_PRODUCT" >"$TARGET_PRODUCT_FILE"
    chmod 600 "$TARGET_PRODUCT_FILE"
}

sync_source_frontend() {
    # When this lifecycle script is invoked directly from a source checkout,
    # `npm --prefix frontend run build` writes to <repo>/frontend/dist while
    # the installed local server normally serves ~/.lemoncrow/install/frontend.
    # Keep that common dev workflow intuitive: a restart after a successful
    # frontend build publishes that exact bundle before launching the server.
    #
    # Installed/bundled copies of this script have no sibling frontend/dist,
    # and an explicit LEMONCROW_FRONTEND_DIR is caller-owned, so both cases are
    # deliberately left untouched.
    [[ -z "${LEMONCROW_FRONTEND_DIR:-}" ]] || return 0
    [[ -f "${SOURCE_FRONTEND_DIR}/index.html" ]] || return 0
    [[ -d "${SOURCE_FRONTEND_DIR}/assets" ]] || return 0

    local destination_parent tmp
    destination_parent="$(dirname "$FRONTEND_DIR")"
    mkdir -p "$destination_parent"
    tmp="$(mktemp -d "${destination_parent}/.frontend-sync.XXXXXX")"
    cp -a "${SOURCE_FRONTEND_DIR}/." "$tmp/"
    rm -rf "$FRONTEND_DIR"
    mv "$tmp" "$FRONTEND_DIR"
    lc_ui_printf '1;36' 'Synced LemonCrow frontend from %s\n' "$SOURCE_FRONTEND_DIR"
}



server_graph_importable() {
    [[ -x "$PYTHON" ]] || return 1
    # Import the same bootstrap graph `python -m lemoncrow_server_core up` loads
    # before binding the socket. Importing only the package root is too shallow:
    # a release can have lemoncrow_server_core installed while a transitive
    # startup dependency (for example FastAPI used by the Review surface) is
    # missing, which otherwise turns the supervisor into a silent crash loop.
    "$PYTHON" -c 'from lemoncrow_server_core.bootstrap import build_local_server'
}

health() {
    [[ -x "$PYTHON" ]] || return 1
    # HTTP 200 from a long-lived process is not enough: a dev/source reinstall
    # can replace the uv-tool environment underneath that process. Require the
    # exact startup import graph the service would use on its next restart.
    server_graph_importable >/dev/null 2>&1 || return 1
    "$PYTHON" - "$URL" <<'PY' >/dev/null 2>&1
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1] + "/healthz", timeout=0.5) as r:
    raise SystemExit(0 if r.status == 200 else 1)
PY
}

pid_alive() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 0 )) && kill -0 "$pid" 2>/dev/null
}

pid_file_alive() {
    [[ -s "$PID_FILE" ]] || return 1
    pid_alive "$(cat "$PID_FILE" 2>/dev/null || true)"
}

pid_holds_state_root() {
    local pid="${1:-}" lock_file="${STATE_DIR}/data/server.lock"
    [[ "$pid" =~ ^[0-9]+$ && -f "$lock_file" ]] || return 1
    grep -Fxq "pid=${pid} port=${PORT}" "$lock_file" 2>/dev/null
}

stop_pid_file_process() {
    if pid_file_alive; then
        local pid
        pid="$(cat "$PID_FILE")"
        # A stale pid file can name a recycled pid; never signal a process that
        # is not this server.
        if [[ "$(ps -p "$pid" -o args= 2>/dev/null || true)" == *"lemoncrow_server_core"* ]]; then
            kill "$pid" 2>/dev/null || true
            local i
            for ((i = 0; i < 50; i++)); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.1
            done
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
}

systemd_available() {
    command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

systemd_nofile_limit() {
    local hard="" kernel=""
    hard="$(systemctl --user show -p DefaultLimitNOFILE --value 2>/dev/null || true)"
    [[ "$hard" =~ ^[1-9][0-9]*$ ]] || hard="$(ulimit -Hn)"
    if [[ -r /proc/sys/fs/nr_open ]]; then
        IFS= read -r kernel </proc/sys/fs/nr_open || true
    fi
    [[ "$hard" != "unlimited" ]] || hard="$kernel"
    if [[ "$hard" =~ ^[1-9][0-9]*$ && "$kernel" =~ ^[1-9][0-9]*$ ]] && (( hard > kernel )); then
        hard="$kernel"
    fi
    [[ "$hard" =~ ^[1-9][0-9]*$ ]] || {
        lc_ui_printf '1;31' '%s\n' "Cannot detect the systemd user manager's open-file limit" >&2
        return 1
    }
    printf '%s\n' "$hard"
}

report_systemd_nofile_limit() {
    local limit="${1:-}"
    [[ -n "$limit" ]] || limit="$(systemd_nofile_limit)" || return 1
    # Nested installs suppress routine details in lc_ui_printf; an explicit
    # local_server.sh limits request still reports the healthy value.
    lc_ui_printf '36' 'LemonCrow server open-file limit: %s (systemd user manager ceiling)\n' "$limit"
    if (( limit < 1048576 )); then
        if [[ -f /etc/systemd/system/user@.service.d/lemoncrow.conf \
            && -f /etc/systemd/user.conf.d/lemoncrow.conf ]]; then
            lc_ui_printf '1;33' 'warning: systemd open-file limit is configured but user manager open-file ceiling is %s; reboot and rerun installation.\n' "$limit" >&2
        else
            lc_ui_printf '1;33' 'warning: user manager open-file ceiling is %s, below 1048576; raise user@.service LimitNOFILE and systemd user DefaultLimitNOFILE, ensure fs.nr_open permits it, then reboot and rerun installation.\n' "$limit" >&2
        fi
    fi
}

report_server_nofile_limit() {
    # Only a systemd user manager imposes the ceiling reported above; launchd
    # and a plain process keep the session's limit, which needs no systemd fix.
    local kind
    kind="$(supervisor_kind 2>/dev/null)" || return 0
    [[ "$kind" == "systemd" ]] || return 0
    report_systemd_nofile_limit
}

configure_systemd_nofile() {
    [[ "$(uname -s)" == "Linux" && "${LEMONCROW_NON_INTERACTIVE:-0}" != "1" \
        && "${LEMONCROW_DRY_RUN:-0}" != "1" ]] || return 0
    command -v systemctl >/dev/null 2>&1 || return 0
    local limit service_limit kernel
    limit="$(systemctl --user show -p DefaultLimitNOFILE --value 2>/dev/null || true)"
    limit="${limit##*:}"
    [[ "$limit" =~ ^[0-9]+$ ]] || limit="$(ulimit -Hn)"
    service_limit="$(systemctl show "user@$(id -u).service" -p LimitNOFILE --value 2>/dev/null || true)"
    service_limit="${service_limit##*:}"
    if [[ "$limit" =~ ^[0-9]+$ ]] && (( limit >= 1048576 )) \
        && [[ "$service_limit" =~ ^[0-9]+$ ]] && (( service_limit >= 1048576 )); then
        return 0
    fi
    local conf_root="${LEMONCROW_SYSTEMD_CONF_ROOT:-/etc/systemd}"
    local service_conf="$conf_root/system/user@.service.d/lemoncrow.conf"
    local manager_conf="$conf_root/user.conf.d/lemoncrow.conf"
    # An earlier install wrote these; the user manager applies them only after
    # a reboot, so asking for sudo again would just rewrite the same files.
    if [[ "$(cat "$service_conf" 2>/dev/null)" == $'[Service]\nLimitNOFILE=1048576' \
        && "$(cat "$manager_conf" 2>/dev/null)" == $'[Manager]\nDefaultLimitNOFILE=1048576' ]]; then
        echo "LemonCrow's systemd open-file limits are configured; reboot to activate them (current: ${limit:-unknown})."
        return 0
    fi
    if ! { : </dev/tty; } 2>/dev/null; then
        lc_ui_printf '1;33' '%s\n' "warning: administrator authentication needs a terminal; skipping the systemd open-file limit update." >&2
        return 0
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        lc_ui_printf '1;33' '%s\n' "warning: sudo is unavailable; skipping the systemd open-file limit update." >&2
        return 0
    fi
    echo "Increase LemonCrow's open-file limit: ${limit:-unknown} -> 1048576 (user manager)."
    echo "Increase user@$(id -u).service LimitNOFILE: ${service_limit:-unknown} -> 1048576."
    echo "This updates systemd settings, may raise fs.nr_open, and requires a reboot to take effect."
    echo "sudo may ask for your password now."
    if ! sudo -v </dev/tty; then
        lc_ui_printf '1;33' '%s\n' "warning: administrator authentication was declined; the server will use the current open-file limit." >&2
        return 0
    fi
    if ! sudo install -d "${service_conf%/*}" "${manager_conf%/*}"; then
        lc_ui_printf '1;33' '%s\n' "warning: could not create systemd configuration directories." >&2
        return 0
    fi
    if ! printf '[Service]\nLimitNOFILE=1048576\n' | sudo tee "$service_conf" >/dev/null; then
        lc_ui_printf '1;33' '%s\n' "warning: could not update user@.service LimitNOFILE." >&2
        return 0
    fi
    if ! printf '[Manager]\nDefaultLimitNOFILE=1048576\n' | sudo tee "$manager_conf" >/dev/null; then
        lc_ui_printf '1;33' '%s\n' "warning: could not update systemd user DefaultLimitNOFILE." >&2
        return 0
    fi
    if [[ -r /proc/sys/fs/nr_open ]]; then
        IFS= read -r kernel </proc/sys/fs/nr_open || true
        if [[ "$kernel" =~ ^[0-9]+$ ]] && (( kernel < 1048576 )); then
            printf 'fs.nr_open = 1048576\n' | sudo tee /etc/sysctl.d/90-lemoncrow-nofile.conf >/dev/null \
                && sudo sysctl -w fs.nr_open=1048576 >/dev/null \
                || lc_ui_printf '1;33' '%s\n' "warning: could not raise fs.nr_open." >&2
        fi
    fi
    sudo systemctl daemon-reload || lc_ui_printf '1;33' '%s\n' "warning: could not reload systemd configuration." >&2
    echo "Systemd limits configured. Reboot after installation to activate the new user-manager limit."
}

launchd_available() {
    [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1
}

supervisor_kind() {
    case "$SUPERVISOR" in
        none) echo none ;;
        systemd)
            systemd_available || { lc_ui_printf '1;31' '%s\n' "requested systemd user supervisor is unavailable" >&2; return 1; }
            echo systemd
            ;;
        launchd)
            launchd_available || { lc_ui_printf '1;31' '%s\n' "requested launchd supervisor is unavailable" >&2; return 1; }
            echo launchd
            ;;
        auto)
            if systemd_available; then echo systemd
            elif launchd_available; then echo launchd
            else echo none
            fi
            ;;
        *) lc_ui_printf '1;31' '%s\n' "LEMONCROW_LOCAL_SERVER_SUPERVISOR must be auto, systemd, launchd, or none" >&2; return 2 ;;
    esac
}

systemd_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}

xml_escape() {
    local value="$1"
    value="${value//&/&amp;}"
    value="${value//</&lt;}"
    value="${value//>/&gt;}"
    value="${value//\"/&quot;}"
    value="${value//\'/&apos;}"
    printf '%s' "$value"
}

refresh_systemd_pid() {
    local pid
    pid="$(systemctl --user show "$SYSTEMD_UNIT" -p MainPID --value 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 0 )); then
        printf '%s\n' "$pid" >"$PID_FILE"
        return 0
    fi
    rm -f "$PID_FILE"
    return 1
}

refresh_launchd_pid() {
    local pid
    pid="$(launchctl list "$LAUNCHD_LABEL" 2>/dev/null | awk '/"PID"/ {gsub(/[^0-9]/, "", $0); print $0; exit}')"
    if [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 0 )); then
        printf '%s\n' "$pid" >"$PID_FILE"
        return 0
    fi
    rm -f "$PID_FILE"
    return 1
}

write_systemd_unit() {
    local nofile_limit
    nofile_limit="$(systemd_nofile_limit)" || return 1
    mkdir -p "$SYSTEMD_USER_DIR" "$STATE_DIR"
    resolve_server_path
    cat >"$SYSTEMD_UNIT_FILE" <<EOF
[Unit]
Description=LemonCrow Local Server
After=network.target
# Nothing else restarts a unit that hit systemd's default start limit, so a
# crash loop must never end in a permanently dead backend.
StartLimitIntervalSec=0

[Service]
Type=simple
# Repository indexes retain SQLite connections across requests; use the user
# manager's available ceiling and report when it should be raised.
LimitNOFILE=${nofile_limit}
# A killed predecessor can still hold the listen socket for a moment. Wait for
# the port instead of failing with EADDRINUSE and burning restarts.
ExecStartPre=/bin/bash -c 'for ((i = 0; i < 30; i++)); do (exec 3<>/dev/tcp/127.0.0.1/${PORT}) 2>/dev/null || exit 0; sleep 1; done; exit 0'
ExecStart=$(systemd_quote "$PYTHON") -m lemoncrow_server_core up --directory $(systemd_quote "$STATE_DIR/data") --port $PORT --token-file $(systemd_quote "$TOKEN_FILE") --frontend-dir $(systemd_quote "$FRONTEND_DIR") --allow-local-fs
Environment=PYTHONUNBUFFERED=1
Environment=LEMONCROW_LOCAL_SERVER_PRODUCTS=$(systemd_quote "$SERVER_PRODUCTS")
# The user manager hands the job the four base directories only, so every agent
# the user installed under ~/.local/bin or Homebrew reads as "not installed".
Environment=PATH=$(systemd_quote "$SERVER_PATH")
# always, not on-failure: a clean exit after a stray SIGTERM must not leave the
# backend down. An explicit systemctl stop is still honoured.
Restart=always
RestartSec=3
# The server drains for 20s on stop; the systemd default (90s) only delays a SIGKILL.
# Worst case is wait_drained(20) + AppRunner.cleanup(20) + the index-worker close
# thread, so 30s SIGKILLed a server that was still shutting down cleanly.
TimeoutStopSec=60

[Install]
WantedBy=default.target
EOF
    report_systemd_nofile_limit "$nofile_limit"
}

write_launchd_plist() {
    mkdir -p "$LAUNCHD_DIR" "$STATE_DIR"
    resolve_server_path
    cat >"$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$PYTHON")</string>
    <string>-m</string><string>lemoncrow_server_core</string><string>up</string>
    <string>--directory</string><string>$(xml_escape "$STATE_DIR/data")</string>
    <string>--port</string><string>$PORT</string>
    <string>--token-file</string><string>$(xml_escape "$TOKEN_FILE")</string>
    <string>--frontend-dir</string><string>$(xml_escape "$FRONTEND_DIR")</string>
    <string>--allow-local-fs</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$STATE_DIR")</string>
  <!-- launchd's default PATH holds only the four base directories, so every
       agent installed under ~/.local/bin or Homebrew would read as missing. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml_escape "$SERVER_PATH")</string>
    <key>LEMONCROW_LOCAL_SERVER_PRODUCTS</key><string>$(xml_escape "$SERVER_PRODUCTS")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG_FILE")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG_FILE")</string>
</dict>
</plist>
EOF
}

stop_systemd() {
    # Only manage a unit owned by this HOME. Tests and alternate HOME installs
    # must never stop another login's real lemoncrow-local-server.service.
    local managed=0
    [[ -e "$SYSTEMD_UNIT_FILE" || -L "$SYSTEMD_UNIT_FILE" || -L "${SYSTEMD_USER_DIR}/default.target.wants/${SYSTEMD_UNIT}" ]] && managed=1
    # A previous interrupted installer may have removed the unit file while its
    # process remained loaded in the user manager. Recognize that orphan only
    # when its command line points at this install's state directory; alternate
    # HOME test installs must never stop the login's real server.
    if [[ "$managed" == "0" ]] && command -v systemctl >/dev/null 2>&1; then
        local loaded_pid="" loaded_command=""
        loaded_pid="$(systemctl --user show "$SYSTEMD_UNIT" -p MainPID --value 2>/dev/null || true)"
        if [[ "$loaded_pid" =~ ^[0-9]+$ ]] && (( loaded_pid > 0 )) && [[ -r "/proc/${loaded_pid}/cmdline" ]]; then
            loaded_command="$(tr '\0' ' ' <"/proc/${loaded_pid}/cmdline" 2>/dev/null || true)"
            if [[ "$loaded_command" == *"lemoncrow_server_core"* && "$loaded_command" == *"${STATE_DIR}/data"* ]]; then
                managed=1
            fi
        fi
    fi
    if [[ "$managed" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
        # Do not let a stale server binary hold the installer behind systemd's
        # default 90-second stop job. Ask it to drain asynchronously, give it the
        # unit's own 30-second budget, then kill the old process before the uv
        # environment underneath it is replaced. A failed stop used to be
        # ignored; start_systemd then saw the old process as healthy and skipped
        # loading the package that had just been installed.
        systemctl --user disable "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
        systemctl --user stop --no-block "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
        local state=""
        local i
        for ((i = 0; i < 300; i++)); do
            state="$(systemctl --user show "$SYSTEMD_UNIT" -p ActiveState --value 2>/dev/null || true)"
            [[ "$state" == "inactive" || "$state" == "failed" || -z "$state" ]] && break
            sleep 0.1
        done
        if [[ "$state" != "inactive" && "$state" != "failed" && -n "$state" ]]; then
            systemctl --user kill --signal=KILL "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
            for ((i = 0; i < 50; i++)); do
                state="$(systemctl --user show "$SYSTEMD_UNIT" -p ActiveState --value 2>/dev/null || true)"
                [[ "$state" == "inactive" || "$state" == "failed" || -z "$state" ]] && break
                sleep 0.1
            done
        fi
    fi
    rm -f "$SYSTEMD_UNIT_FILE" "${SYSTEMD_USER_DIR}/default.target.wants/${SYSTEMD_UNIT}"
    if systemd_available; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user reset-failed "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
    fi
    # Leave PID_FILE for stop_pid_file_process(). A process-supervised install
    # may run on a host where systemd is available; deleting the shared PID here
    # used to orphan that fallback server during uninstall.
}

stop_launchd() {
    if launchd_available && [[ -f "$LAUNCHD_PLIST" ]]; then
        launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    fi
    rm -f "$LAUNCHD_PLIST"
    # PID_FILE is shared with fallback mode and is removed only after the
    # recorded process has been checked/stopped by stop_pid_file_process().
}

retire_legacy_review_workspaces() {
    [[ -x "$PYTHON" ]] || return 0
    "$PYTHON" - "$HOME_DIR" <<'PY' >/dev/null 2>&1 || true
import sys
from pathlib import Path

try:
    from lemoncrow.pro.capabilities.review.workspace import retire_legacy_workspaces
except Exception:
    raise SystemExit(0)
retire_legacy_workspaces(Path(sys.argv[1]))
PY
}

stop_server() {
    # Stop every local-server launch form so switching local -> hosted cannot
    # leave a supervisor capable of resurrecting the loopback server later.
    stop_systemd
    stop_launchd
    stop_pid_file_process
    retire_legacy_review_workspaces
}

wait_for_health() {
    local kind="$1"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    local started=$SECONDS next_progress=$((SECONDS + 10))
    while (( SECONDS < deadline )); do
        if (( SECONDS >= next_progress )); then
            lc_ui_printf progress 'Waiting for local Review server... (%ss elapsed)\n' "$((SECONDS - started))"
            next_progress=$((SECONDS + 10))
        fi
        # In process-supervised mode, a stale server on the same port must not
        # make a newly spawned (and already failing) process look healthy. The
        # backend claims STATE_DIR/data/server.lock before binding its socket;
        # require the PID we just launched to own that exact claim before a 200
        # health response can satisfy this restart.
        if [[ "$kind" == "none" ]]; then
            pid_file_alive || break
            pid_holds_state_root "$(cat "$PID_FILE" 2>/dev/null || true)" || { sleep 0.2; continue; }
        fi
        if health; then
            case "$kind" in
                systemd) refresh_systemd_pid || true ;;
                launchd) refresh_launchd_pid || true ;;
            esac
            lc_ui_printf '1;32' 'LemonCrow local server ready at %s\n' "$URL"
            return 0
        fi
        case "$kind" in
            systemd)
                systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null || break
                ;;
        esac
        sleep 0.2
    done
    lc_ui_printf '1;31' '%s\n' "LemonCrow local server did not become healthy at $URL" >&2
    case "$kind" in
        systemd) journalctl --user -u "$SYSTEMD_UNIT" -n 40 --no-pager >&2 2>/dev/null || true ;;
        *) tail -40 "$LOG_FILE" >&2 2>/dev/null || true ;;
    esac
    return 1
}

managed_file_uses_machine_auth() {
    local file="$1"
    [[ -f "$file" ]] || return 1
    grep -Fq -- "--token-file" "$file" && ! grep -Fq -- "--local-no-auth" "$file"
}

pid_uses_machine_auth() {
    local pid="$1" command=""
    pid_alive "$pid" || return 1
    command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    [[ "$command" == *"lemoncrow_server_core up"* && "$command" == *"--token-file"* && "$command" == *"$TOKEN_FILE"* ]]
}

# Refuse a port owned by another installation before reporting its health as ours.
ensure_port_available() {
    "$PYTHON" - "$PORT" <<'PY_PORT'
import errno
import socket
import sys

port = int(sys.argv[1])
with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"Port {port} is already in use by another process. "
                  "Use make oss-dev for the OSS sandbox (port 7428), "
                  "or select the intended installation before running oss-prod.", file=sys.stderr)
        else:
            print(f"Cannot bind local server port {port}: {exc}", file=sys.stderr)
        raise SystemExit(1)
PY_PORT
}

start_systemd() {
    if managed_file_uses_machine_auth "$SYSTEMD_UNIT_FILE" \
        && systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null \
        && refresh_systemd_pid && pid_holds_state_root "$(cat "$PID_FILE" 2>/dev/null || true)" && health; then
        lc_ui_printf '1;32' 'LemonCrow local server already running at %s\n' "$URL"
        return 0
    fi
    stop_pid_file_process
    # A restart may replace our own unhealthy listener; never stop another unit.
    if systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null; then
        systemctl --user stop "$SYSTEMD_UNIT"
    fi
    ensure_port_available || return 1
    write_systemd_unit
    systemctl --user daemon-reload
    systemctl --user enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
        || { systemctl --user enable "$SYSTEMD_UNIT" >&2; exit 1; }
    systemctl --user restart "$SYSTEMD_UNIT"
    wait_for_health systemd || { stop_systemd; return 1; }
}

start_launchd() {
    if managed_file_uses_machine_auth "$LAUNCHD_PLIST" \
        && launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1 && health; then
        refresh_launchd_pid || true
        lc_ui_printf '1;32' 'LemonCrow local server already running at %s\n' "$URL"
        return 0
    fi
    stop_pid_file_process
    launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    ensure_port_available || return 1
    write_launchd_plist
    launchctl load "$LAUNCHD_PLIST"
    wait_for_health launchd || { stop_launchd; return 1; }
}

start_fallback() {
    if pid_file_alive && pid_uses_machine_auth "$(cat "$PID_FILE")" && health; then
        lc_ui_printf '1;32' 'LemonCrow local server already running at %s\n' "$URL"
        return 0
    fi
    resolve_server_path
    stop_pid_file_process
    ensure_port_available || return 1
    mkdir -p "$STATE_DIR"
    : >>"$LOG_FILE"
    # fd 9 is install.sh's flock. Close it explicitly so this child can never
    # keep the installer lock alive after its parent exits.
    nohup "$PYTHON" -m lemoncrow_server_core up \
        --directory "$STATE_DIR/data" --port "$PORT" --token-file "$TOKEN_FILE" \
        --frontend-dir "$FRONTEND_DIR" --allow-local-fs \
        >>"$LOG_FILE" 2>&1 </dev/null 9>&- &
    local pid=$!
    printf '%s\n' "$pid" >"$PID_FILE"
    wait_for_health none || { stop_pid_file_process; return 1; }
}

prepare_server() {
    sync_source_frontend
    [[ -x "$PYTHON" ]] || { lc_ui_printf '1;31' '%s\n' "local server Python not found: $PYTHON" >&2; exit 1; }
    if ! server_graph_importable; then
        lc_ui_printf '1;31' '%s\n' "local server dependencies are not importable in the selected product engine" >&2
        exit 1
    fi
    write_target
    mkdir -p "$STATE_DIR"
}

start_server() {
    if [[ "$TARGET_PRODUCT" == "none" || "$SERVER_PRODUCTS" == "none" ]]; then
        lc_ui_printf '1;31' '%s\n' "no local LemonCrow product is enabled; install local Review or local Workspaces before starting the server" >&2
        return 2
    fi
    prepare_server
    case "$(supervisor_kind)" in
        systemd) start_systemd ;;
        launchd) start_launchd ;;
        none) start_fallback ;;
    esac
    retire_legacy_review_workspaces
}

write_restart_status() {
    local state="$1"
    shift || true
    mkdir -p "$STATE_DIR"
    local tmp="${RESTART_STATUS_FILE}.tmp.$$"
    printf '%s\t%s\t%s\n' "$state" "$(date -Iseconds 2>/dev/null || date)" "$*" >"$tmp"
    mv "$tmp" "$RESTART_STATUS_FILE"
}

restart_worker() {
    local kind="${1:-}"
    sleep "$RESTART_HANDOFF_SECONDS"
    write_restart_status restarting "supervisor=$kind"

    case "$kind" in
        systemd)
            if systemctl --user restart "$SYSTEMD_UNIT" && wait_for_health systemd; then
                refresh_systemd_pid || true
            else
                write_restart_status failed "supervisor=systemd"
                return 1
            fi
            ;;
        launchd)
            launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
            if launchctl load "$LAUNCHD_PLIST" && wait_for_health launchd; then
                refresh_launchd_pid || true
            else
                write_restart_status failed "supervisor=launchd"
                return 1
            fi
            ;;
        none)
            stop_pid_file_process
            if ! start_fallback; then
                write_restart_status failed "supervisor=process"
                return 1
            fi
            ;;
        *)
            write_restart_status failed "unknown supervisor=$kind"
            return 2
            ;;
    esac

    retire_legacy_review_workspaces
    write_restart_status ready "supervisor=$kind pid=$(cat "$PID_FILE" 2>/dev/null || echo unknown)"
}

schedule_restart_worker() {
    local kind="$1"
    write_restart_status scheduled "supervisor=$kind"

    if [[ "$kind" == "systemd" ]]; then
        command -v systemd-run >/dev/null 2>&1 || {
            lc_ui_printf '1;31' '%s\n' "systemd-run is required for self-safe LemonCrow restarts" >&2
            return 1
        }
        local helper_unit="lemoncrow-local-restart-$$-${RANDOM}.service"
        systemd-run --user --quiet --collect \
            --unit="$helper_unit" \
            --property=Type=oneshot \
            --setenv="LEMONCROW_HOME=$HOME_DIR" \
            --setenv="LEMONCROW_TOOL_DIR=$TOOL_DIR" \
            --setenv="LEMONCROW_SERVER_PYTHON=$PYTHON" \
            --setenv="LEMONCROW_LOCAL_SERVER_PORT=$PORT" \
            --setenv="LEMONCROW_LOCAL_SERVER_DIR=$STATE_DIR" \
            --setenv="LEMONCROW_FRONTEND_DIR=$FRONTEND_DIR" \
            --setenv="LEMONCROW_LOCAL_TOKEN_FILE=$TOKEN_FILE" \
            --setenv="LEMONCROW_LOCAL_SERVER_SUPERVISOR=systemd" \
            --setenv="LEMONCROW_LOCAL_SERVER_START_TIMEOUT=$HEALTH_TIMEOUT_SECONDS" \
            --setenv="LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF=$RESTART_HANDOFF_SECONDS" \
            --setenv="LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT=$TARGET_PRODUCT" \
            --setenv="LEMONCROW_LOCAL_SERVER_PRODUCTS=$SERVER_PRODUCTS" \
            /bin/bash "$SCRIPT_DIR/local_server.sh" _restart-worker systemd
    else
        # In process mode only the recorded server PID is stopped, so this
        # detached helper survives. launchd owns the server independently too.
        nohup env \
            LEMONCROW_HOME="$HOME_DIR" \
            LEMONCROW_TOOL_DIR="$TOOL_DIR" \
            LEMONCROW_SERVER_PYTHON="$PYTHON" \
            LEMONCROW_LOCAL_SERVER_PORT="$PORT" \
            LEMONCROW_LOCAL_SERVER_DIR="$STATE_DIR" \
            LEMONCROW_FRONTEND_DIR="$FRONTEND_DIR" \
            LEMONCROW_LOCAL_TOKEN_FILE="$TOKEN_FILE" \
            LEMONCROW_LOCAL_SERVER_SUPERVISOR="$kind" \
            LEMONCROW_LOCAL_SERVER_START_TIMEOUT="$HEALTH_TIMEOUT_SECONDS" \
            LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF="$RESTART_HANDOFF_SECONDS" \
            LEMONCROW_LOCAL_SERVER_TARGET_PRODUCT="$TARGET_PRODUCT" \
            LEMONCROW_LOCAL_SERVER_PRODUCTS="$SERVER_PRODUCTS" \
            /bin/bash "$SCRIPT_DIR/local_server.sh" _restart-worker "$kind" \
            >>"$RESTART_LOG_FILE" 2>&1 </dev/null 9>&- &
    fi

    lc_ui_printf '1;36' 'LemonCrow local server restart handed off to supervisor=%s; status=%s\n' "$kind" "$RESTART_STATUS_FILE"
}

restart_server() {
    local kind
    if [[ "$TARGET_PRODUCT" == "none" || "$SERVER_PRODUCTS" == "none" ]]; then
        lc_ui_printf '1;31' '%s\n' "no local LemonCrow product is enabled; install local Review or local Workspaces before restarting the server" >&2
        return 2
    fi
    prepare_server
    kind="$(supervisor_kind)" || return 1

    case "$kind" in
        systemd)
            # Do not stop, disable, or delete the running unit from inside its
            # own MCP request. Prepare first, then an external user-systemd unit
            # owns the restart and health verification.
            write_systemd_unit
            systemctl --user daemon-reload
            systemctl --user enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
                || { systemctl --user enable "$SYSTEMD_UNIT" >&2; return 1; }
            ;;
        launchd)
            write_launchd_plist
            ;;
        none)
            ;;
    esac

    schedule_restart_worker "$kind"
}

status_server() {
    local kind
    kind="$(supervisor_kind)" || return 1
    case "$kind" in
        systemd)
            if systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null && health; then
                refresh_systemd_pid || true
                lc_ui_printf '1;32' 'running %s\n' "$URL pid=$(cat "$PID_FILE" 2>/dev/null || echo unknown) supervisor=systemd"
                return 0
            fi
            ;;
        launchd)
            if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1 && health; then
                refresh_launchd_pid || true
                lc_ui_printf '1;32' 'running %s\n' "$URL pid=$(cat "$PID_FILE" 2>/dev/null || echo unknown) supervisor=launchd"
                return 0
            fi
            ;;
        none)
            if pid_file_alive && health; then
                lc_ui_printf '1;32' 'running %s\n' "$URL pid=$(cat "$PID_FILE") supervisor=process"
                return 0
            fi
            ;;
    esac
    lc_ui_printf '1;33' 'stopped\n'
    return 1
}

case "${1:-start}" in
    start) start_server ;;
    stop) stop_server ;;
    restart) restart_server ;;
    restart-status)
        [[ -f "$RESTART_STATUS_FILE" ]] && cat "$RESTART_STATUS_FILE" || echo "no restart recorded"
        ;;
    _restart-worker) restart_worker "${2:-}" ;;
    _prepare) prepare_server ;;
    status) status_server ;;
    diagnose)
        status_server || true
        case "$(supervisor_kind)" in
            systemd) journalctl --user -u "$SYSTEMD_UNIT" -n 20 --no-pager >&2 ;;
            *) [[ ! -f "$LOG_FILE" ]] || tail -20 "$LOG_FILE" >&2 ;;
        esac
        ;;
    limits) report_server_nofile_limit ;;
    configure-nofile) configure_systemd_nofile ;;
    *) lc_ui_printf '1;31' '%s\n' "usage: $0 {start|stop|restart|restart-status|status|diagnose|limits|configure-nofile}" >&2; exit 2 ;;
esac
