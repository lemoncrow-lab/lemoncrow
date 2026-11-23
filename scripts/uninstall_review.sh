#!/usr/bin/env bash
# uninstall_review.sh — Remove LemonCrow Review without touching Runtime or shared account state.
set -euo pipefail

STATE_DIR="${LEMONCROW_HOME:-${HOME}/.lemoncrow}"
BIN_DIR="${LEMONCROW_BIN_DIR:-${STATE_DIR}/bin}"
REVIEW_ROOT="${LEMONCROW_REVIEW_INSTALL_ROOT:-${STATE_DIR}/review/client}"
RUNTIME_CLIENT="${LEMONCROW_RUNTIME_INSTALL_ROOT:-${STATE_DIR}/runtime/client}/bin/lemoncrow-client"
DRY_RUN="${LEMONCROW_DRY_RUN:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help|help)
            echo "usage: uninstall_review.sh [--dry-run]"
            exit 0 ;;
        *) echo "uninstall_review.sh: unknown option: $1" >&2; exit 2 ;;
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
        review) next=none ;;
        both) next=runtime ;;
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
link_points_into "$BIN_DIR/lc" "$REVIEW_ROOT" && lc_owned=1 || true

for name in lcr lemoncrow-review-client; do
    if link_points_into "$BIN_DIR/$name" "$REVIEW_ROOT"; then
        run rm -f "$BIN_DIR/$name"
    fi
done

run rm -rf "$STATE_DIR/review"

adjust_local_server_owner

if [[ "$lc_owned" == "1" ]]; then
    if [[ -x "$RUNTIME_CLIENT" ]]; then
        run mkdir -p "$BIN_DIR"
        run ln -sfn "$RUNTIME_CLIENT" "$BIN_DIR/lc"
    else
        run rm -f "$BIN_DIR/lc"
    fi
fi

printf '✓ LemonCrow Review uninstalled\n'
printf '  Workspaces: %s\n' "$([[ -x "$RUNTIME_CLIENT" ]] && echo preserved || echo not installed)"
printf '  Account: preserved under %s/account\n' "$STATE_DIR"
