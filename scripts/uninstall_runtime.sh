#!/usr/bin/env bash
# uninstall_runtime.sh — Remove LemonCrow Workspaces without touching Review or shared account state.
set -euo pipefail

STATE_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
BIN_DIR="${LEMONCROW_BIN_DIR:-${STATE_DIR}/bin}"
RUNTIME_ROOT="${LEMONCROW_RUNTIME_INSTALL_ROOT:-${STATE_DIR}/runtime/client}"
RUNTIME_CLIENT="$RUNTIME_ROOT/bin/lemoncrow-client"
REVIEW_CLIENT="${LEMONCROW_REVIEW_INSTALL_ROOT:-${STATE_DIR}/review/client}/bin/lc"
TOOL_DIR="${LEMONCROW_TOOL_DIR:-${STATE_DIR}/uv-tools}"
RUNTIME_ENGINE_ROOT="$TOOL_DIR/lemoncrow"
RUNTIME_LOCAL=0
grep -qx 'LEMONCROW_INSTALL_MODE=local' "$STATE_DIR/runtime/env" 2>/dev/null && RUNTIME_LOCAL=1 || true
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"
KEEP_HOSTS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --keep-hosts|--no-hosts) KEEP_HOSTS=1; shift ;;
        -h|--help|help)
            cat <<'USAGE'
usage: uninstall_runtime.sh [--dry-run] [--keep-hosts]

By default LemonCrow removes host MCP entries and its Pi host and extension
before removing Workspaces. --keep-hosts skips that step.
USAGE
            exit 0 ;;
        *) echo "uninstall_runtime.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run]'; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

link_points_into() {
    local link="$1" root="$2" target
    [[ -L "$link" ]] || return 1
    target="$(readlink "$link" 2>/dev/null || true)"
    [[ "$target" == "$root" || "$target" == "$root/"* ]]
}

adjust_local_server_owner() {
    local target_file="${LEMONCROW_LOCAL_SERVER_DIR:-${STATE_DIR}/server}/target-product"
    local server_script="${LEMONCROW_LOCAL_SERVER_SCRIPT:-${STATE_DIR}/install/scripts/local_server.sh}"
    local current="" next=""
    [[ -f "$target_file" ]] || return 0
    current="$(head -n 1 "$target_file" 2>/dev/null | tr -d '[:space:]' || true)"
    case "$current" in
        runtime) next=none ;;
        both) next=review ;;
        *) return 0 ;;
    esac
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] local-server owner %s -> %s\n' "$current" "$next"
        if [[ -x "$server_script" ]]; then
            printf '[dry-run] bash %q stop\n' "$server_script"
            [[ "$next" == "none" ]] || printf '[dry-run] bash %q start\n' "$server_script"
        fi
        return 0
    fi
    mkdir -p "$(dirname "$target_file")"
    printf '%s\n' "$next" > "$target_file"
    chmod 600 "$target_file"
    if [[ -x "$server_script" ]]; then
        bash "$server_script" stop >/dev/null 2>&1 || true
        if [[ "$next" != "none" ]] && ! bash "$server_script" start >/dev/null 2>&1; then
            echo "$(basename "$0"): WARNING: local server could not start on surviving $next product" >&2
        fi
    fi
}

lc_owned=0
link_points_into "$BIN_DIR/lc" "$RUNTIME_ROOT" && lc_owned=1 || true

if [[ "$KEEP_HOSTS" != "1" && -x "$RUNTIME_CLIENT" ]]; then
    for host in claude codex opencode; do
        if [[ "$DRY_RUN" == "1" ]]; then
            printf '[dry-run] %q runtime disable %q\n' "$RUNTIME_CLIENT" "$host"
        else
            "$RUNTIME_CLIENT" runtime disable "$host" >/dev/null 2>&1 || true
        fi
    done
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] %q runtime host remove --engine pi --yes\n' "$RUNTIME_CLIENT"
    else
        "$RUNTIME_CLIENT" runtime host remove --engine pi --yes >/dev/null 2>&1 || true
    fi
fi

# Persistent remote MCP units run this Runtime's Python; remove them with it.
# Connector bindings and pairing state stay in $STATE_DIR/mcp for a reinstall.
if [[ -x "$RUNTIME_CLIENT" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] %q runtime mcp service uninstall\n' "$RUNTIME_CLIENT"
    else
        "$RUNTIME_CLIENT" runtime mcp service uninstall >/dev/null 2>&1 || true
    fi
fi

if link_points_into "$BIN_DIR/lemoncrow-runtime-client" "$RUNTIME_ROOT"; then
    run rm -f "$BIN_DIR/lemoncrow-runtime-client"
fi

# Move a surviving Review server onto its own engine before deleting the full
# Runtime uv-tool environment that may currently host the shared process.
adjust_local_server_owner

run rm -rf "$STATE_DIR/runtime"
if [[ "$RUNTIME_LOCAL" == "1" && -d "$RUNTIME_ENGINE_ROOT" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] remove local Workspaces engine %q\n' "$RUNTIME_ENGINE_ROOT"
    elif command -v uv >/dev/null 2>&1; then
        UV_TOOL_BIN_DIR="$BIN_DIR" UV_TOOL_DIR="$TOOL_DIR" uv tool uninstall lemoncrow >/dev/null 2>&1 || rm -rf "$RUNTIME_ENGINE_ROOT"
    else
        rm -rf "$RUNTIME_ENGINE_ROOT"
    fi
    # uv may remove legacy root launchers. Product clients restore lc below; no
    # full-engine command should survive Runtime uninstall.
    rm -f "$BIN_DIR/lemoncrow" "$BIN_DIR/lemoncrow-mcp"
fi

if [[ "$lc_owned" == "1" ]]; then
    if [[ -x "$REVIEW_CLIENT" ]]; then
        run mkdir -p "$BIN_DIR"
        run ln -sfn "$REVIEW_CLIENT" "$BIN_DIR/lc"
    else
        run rm -f "$BIN_DIR/lc"
    fi
fi

printf '✓ LemonCrow Workspaces uninstalled\n'
printf '  Review:  %s\n' "$([[ -x "$REVIEW_CLIENT" ]] && echo preserved || echo not installed)"
printf '  Account: preserved under %s/account\n' "$STATE_DIR"
