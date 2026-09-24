#!/usr/bin/env bash
# local_server.sh — lifecycle for the loopback LemonCrow server used by local installs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)/frontend/dist"

HOME_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
TOOL_DIR="${LEMONCROW_TOOL_DIR:-${HOME}/.lemoncrow/uv-tools}"
PYTHON="${LEMONCROW_SERVER_PYTHON:-${TOOL_DIR}/lemoncrow/bin/python}"
PORT="${LEMONCROW_LOCAL_SERVER_PORT:-7420}"
URL="http://127.0.0.1:${PORT}"
STATE_DIR="${LEMONCROW_LOCAL_SERVER_DIR:-${HOME_DIR}/server}"
FRONTEND_DIR="${LEMONCROW_FRONTEND_DIR:-${HOME_DIR}/install/frontend}"
ENV_FILE="${HOME_DIR}/env"
TOKEN_FILE="${LEMONCROW_LOCAL_TOKEN_FILE:-${HOME_DIR}/token}"
PID_FILE="${STATE_DIR}/server.pid"
LOG_FILE="${STATE_DIR}/server.log"
RESTART_LOG_FILE="${STATE_DIR}/restart.log"
RESTART_STATUS_FILE="${STATE_DIR}/restart.status"
RESTART_HANDOFF_SECONDS="${LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF:-1}"
SUPERVISOR="${LEMONCROW_LOCAL_SERVER_SUPERVISOR:-auto}"
HEALTH_TIMEOUT_SECONDS="${LEMONCROW_LOCAL_SERVER_START_TIMEOUT:-120}"
SYSTEMD_UNIT="lemoncrow-local-server.service"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
SYSTEMD_UNIT_FILE="${SYSTEMD_USER_DIR}/${SYSTEMD_UNIT}"
# The persistent remote MCP tunnel (`lc mcp serve --persistent`, see
# _mcp_service.py) runs two more systemd --user units that proxy into this
# same backend on $PORT. Neither is supervised by this script; bounce them
# alongside the backend below so a reinstall/restart here cannot leave them
# holding a connection to a backend PID that no longer exists.
SHARED_MCP_GATEWAY_UNIT="lemoncrow-mcp-gateway.service"
SHARED_MCP_TUNNEL_UNIT="lemoncrow-mcp-tunnel.service"
LAUNCHD_LABEL="com.lemoncrow.local-server"
LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
LAUNCHD_PLIST="${LAUNCHD_DIR}/${LAUNCHD_LABEL}.plist"

if [[ ! "$HEALTH_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "LEMONCROW_LOCAL_SERVER_START_TIMEOUT must be a positive integer" >&2
    exit 2
fi
if [[ ! "$RESTART_HANDOFF_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "LEMONCROW_LOCAL_SERVER_RESTART_HANDOFF must be a non-negative number" >&2
    exit 2
fi

write_target() {
    mkdir -p "$HOME_DIR"
    local tmp="${ENV_FILE}.tmp.$$"
    if [[ -f "$ENV_FILE" ]]; then
        grep -Ev '^(LEMONCROW_URL|LEMONCROW_INSTALL_MODE|LEMONCROW_LOCAL_FS|LEMONCROW_TOKEN_FILE)=' "$ENV_FILE" >"$tmp" || true
    else
        : >"$tmp"
    fi
    printf 'LEMONCROW_URL=%s\n' "$URL" >>"$tmp"
    printf 'LEMONCROW_INSTALL_MODE=local\n' >>"$tmp"
    printf 'LEMONCROW_LOCAL_FS=1\n' >>"$tmp"
    printf 'LEMONCROW_TOKEN_FILE=%s\n' "$TOKEN_FILE" >>"$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ENV_FILE"
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
    printf 'Synced LemonCrow frontend from %s\n' "$SOURCE_FRONTEND_DIR"
}



health() {
    [[ -x "$PYTHON" ]] || return 1
    # HTTP 200 from a long-lived process is not enough: a dev/source reinstall
    # can replace the uv-tool environment underneath that process. Require the
    # environment the service would restart from to still contain its server
    # package, otherwise report unhealthy and force a clean restart/repair.
    "$PYTHON" -c 'import lemoncrow_server_core' >/dev/null 2>&1 || return 1
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

stop_pid_file_process() {
    if pid_file_alive; then
        local pid
        pid="$(cat "$PID_FILE")"
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 50); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
}

systemd_available() {
    command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

launchd_available() {
    [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1
}

bounce_shared_mcp_tunnel() {
    systemd_available || return 0
    [[ -f "${SYSTEMD_USER_DIR}/${SHARED_MCP_GATEWAY_UNIT}" && -f "${SYSTEMD_USER_DIR}/${SHARED_MCP_TUNNEL_UNIT}" ]] \
        || return 0
    systemctl --user restart "$SHARED_MCP_GATEWAY_UNIT" "$SHARED_MCP_TUNNEL_UNIT" >/dev/null 2>&1 \
        || echo "warning: could not restart persistent MCP tunnel units after local server restart; run: lc mcp service repair" >&2
}

supervisor_kind() {
    case "$SUPERVISOR" in
        none) echo none ;;
        systemd)
            systemd_available || { echo "requested systemd user supervisor is unavailable" >&2; return 1; }
            echo systemd
            ;;
        launchd)
            launchd_available || { echo "requested launchd supervisor is unavailable" >&2; return 1; }
            echo launchd
            ;;
        auto)
            if systemd_available; then echo systemd
            elif launchd_available; then echo launchd
            else echo none
            fi
            ;;
        *) echo "LEMONCROW_LOCAL_SERVER_SUPERVISOR must be auto, systemd, launchd, or none" >&2; return 2 ;;
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
    mkdir -p "$SYSTEMD_USER_DIR" "$STATE_DIR"
    cat >"$SYSTEMD_UNIT_FILE" <<EOF
[Unit]
Description=LemonCrow Local Server
After=network.target

[Service]
Type=simple
ExecStart=$(systemd_quote "$PYTHON") -m lemoncrow_server_core up --directory $(systemd_quote "$STATE_DIR/data") --port $PORT --token-file $(systemd_quote "$TOKEN_FILE") --frontend-dir $(systemd_quote "$FRONTEND_DIR") --allow-local-fs
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=1
# The server drains for 20s on stop; the systemd default (90s) only delays a SIGKILL.
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF
}

write_launchd_plist() {
    mkdir -p "$LAUNCHD_DIR" "$STATE_DIR"
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
        for _ in $(seq 1 300); do
            state="$(systemctl --user show "$SYSTEMD_UNIT" -p ActiveState --value 2>/dev/null || true)"
            [[ "$state" == "inactive" || "$state" == "failed" || -z "$state" ]] && break
            sleep 0.1
        done
        if [[ "$state" != "inactive" && "$state" != "failed" && -n "$state" ]]; then
            systemctl --user kill --signal=KILL "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
            for _ in $(seq 1 50); do
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
    rm -f "$PID_FILE"
}

stop_launchd() {
    if launchd_available && [[ -f "$LAUNCHD_PLIST" ]]; then
        launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    fi
    rm -f "$LAUNCHD_PLIST" "$PID_FILE"
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
    while (( SECONDS < deadline )); do
        if health; then
            case "$kind" in
                systemd) refresh_systemd_pid || true ;;
                launchd) refresh_launchd_pid || true ;;
            esac
            printf 'LemonCrow local server ready at %s\n' "$URL"
            return 0
        fi
        case "$kind" in
            systemd)
                systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null || break
                ;;
        esac
        sleep 0.2
    done
    echo "LemonCrow local server did not become healthy at $URL" >&2
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

start_systemd() {
    if managed_file_uses_machine_auth "$SYSTEMD_UNIT_FILE" \
        && systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null && health; then
        refresh_systemd_pid || true
        printf 'LemonCrow local server already running at %s\n' "$URL"
        return 0
    fi
    stop_pid_file_process
    write_systemd_unit
    systemctl --user daemon-reload
    systemctl --user enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
        || { systemctl --user enable "$SYSTEMD_UNIT" >&2; exit 1; }
    systemctl --user restart "$SYSTEMD_UNIT"
    wait_for_health systemd || { stop_systemd; return 1; }
    bounce_shared_mcp_tunnel
}

start_launchd() {
    if managed_file_uses_machine_auth "$LAUNCHD_PLIST" \
        && launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1 && health; then
        refresh_launchd_pid || true
        printf 'LemonCrow local server already running at %s\n' "$URL"
        return 0
    fi
    stop_pid_file_process
    launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    write_launchd_plist
    launchctl load "$LAUNCHD_PLIST"
    wait_for_health launchd || { stop_launchd; return 1; }
}

start_fallback() {
    if pid_file_alive && pid_uses_machine_auth "$(cat "$PID_FILE")" && health; then
        printf 'LemonCrow local server already running at %s\n' "$URL"
        return 0
    fi
    stop_pid_file_process
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
    [[ -x "$PYTHON" ]] || { echo "local server Python not found: $PYTHON" >&2; exit 1; }
    "$PYTHON" -c 'import lemoncrow_server_core' >/dev/null 2>&1 || {
        echo "local server package is not installed in the LemonCrow tool environment" >&2
        exit 1
    }
    write_target
    mkdir -p "$STATE_DIR"
}

start_server() {
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
                bounce_shared_mcp_tunnel
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
            echo "systemd-run is required for self-safe LemonCrow restarts" >&2
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
            /bin/bash "$SCRIPT_DIR/local_server.sh" _restart-worker "$kind" \
            >>"$RESTART_LOG_FILE" 2>&1 </dev/null 9>&- &
    fi

    printf 'LemonCrow local server restart handed off to supervisor=%s; status=%s\n' "$kind" "$RESTART_STATUS_FILE"
}

restart_server() {
    local kind
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
                echo "running $URL pid=$(cat "$PID_FILE" 2>/dev/null || echo unknown) supervisor=systemd"
                return 0
            fi
            ;;
        launchd)
            if launchctl list "$LAUNCHD_LABEL" >/dev/null 2>&1 && health; then
                refresh_launchd_pid || true
                echo "running $URL pid=$(cat "$PID_FILE" 2>/dev/null || echo unknown) supervisor=launchd"
                return 0
            fi
            ;;
        none)
            if pid_file_alive && health; then
                echo "running $URL pid=$(cat "$PID_FILE") supervisor=process"
                return 0
            fi
            ;;
    esac
    echo "stopped"
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
    status) status_server ;;
    *) echo "usage: $0 {start|stop|restart|restart-status|status}" >&2; exit 2 ;;
esac
