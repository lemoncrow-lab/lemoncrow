#!/usr/bin/env bash
# install.sh — bootstrap Atelier from GitHub using a curl|bash-friendly flow.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
#
# Optional environment variables:
#   ATELIER_REPO_URL   Git URL (default: https://github.com/pankaj4u4m/atelier.git)
#   ATELIER_REF        Git ref to install (default: main)
#   ATELIER_INSTALL_DIR Install location (default: ~/.local/share/atelier)
#   ATELIER_BIN_DIR    Global bin dir for console scripts (default: ~/.local/bin)
#   ATELIER_TOOL_DIR   uv tool environment dir (default: ~/.local/share/uv/tools)
#   ATELIER_NO_HOSTS   If set to 1, skip agent-host integration install scripts
#   ATELIER_NO_SERVICECTL If set to 1, skip starting the background service controller
#   ATELIER_SERVICECTL_INTERVAL_SECONDS Poll interval for servicectl (default: 60)
#   ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS Periodic maintenance interval (default: 21600)
#   ATELIER_DRY_RUN    If set to 1, print planned actions and exit
#   ATELIER_NO_STACK   If set to 1, skip starting the visualization stack (service + frontend)
#
# Notes:
#   Codex host install manages its Atelier AGENTS block with explicit START/END
#   sentinels so re-install can replace that block without overwriting user content.

set -euo pipefail

if [[ -t 1 ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
else
    C_RESET=""
    C_BOLD=""
    C_RED=""
    C_YELLOW=""
fi

ATELIER_REPO_URL="${ATELIER_REPO_URL:-https://github.com/pankaj4u4m/atelier.git}"
ATELIER_REF="${ATELIER_REF:-main}"
ATELIER_INSTALL_DIR="${ATELIER_INSTALL_DIR:-${HOME}/.local/share/atelier}"
ATELIER_BIN_DIR="${ATELIER_BIN_DIR:-${HOME}/.local/bin}"
ATELIER_TOOL_DIR="${ATELIER_TOOL_DIR:-${HOME}/.local/share/uv/tools}"
ATELIER_INSTALL_RECORD="${HOME}/.atelier/install_dir"
ATELIER_NO_HOSTS="${ATELIER_NO_HOSTS:-0}"
ATELIER_NO_SERVICECTL="${ATELIER_NO_SERVICECTL:-0}"
ATELIER_SERVICECTL_INTERVAL_SECONDS="${ATELIER_SERVICECTL_INTERVAL_SECONDS:-60}"
ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS="${ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS:-21600}"
ATELIER_DRY_RUN="${ATELIER_DRY_RUN:-0}"
ATELIER_NO_STACK="${ATELIER_NO_STACK:-0}"
ATELIER_LOCAL="${ATELIER_LOCAL:-0}"
ATELIER_USE_CURRENT_REPO="${ATELIER_USE_CURRENT_REPO:-}"
if [[ -z "$ATELIER_USE_CURRENT_REPO" ]]; then
    if [[ "$ATELIER_LOCAL" == "1" ]]; then
        ATELIER_USE_CURRENT_REPO=1
    elif [[ -f "uv.lock" && -d "src/atelier" && -f "scripts/install.sh" ]]; then
        ATELIER_USE_CURRENT_REPO=1
    else
        ATELIER_USE_CURRENT_REPO=0
    fi
fi
PASSTHROUGH=()
WARNINGS=()
ERRORS=()
FINAL_EXIT_CODE=0

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local) ATELIER_LOCAL=1; ATELIER_USE_CURRENT_REPO=1 ;;
        --no-local) ATELIER_LOCAL=0; ATELIER_USE_CURRENT_REPO=0 ;;
        --dry-run) ATELIER_DRY_RUN=1; PASSTHROUGH+=("$1") ;;
        --no-hosts) ATELIER_NO_HOSTS=1; PASSTHROUGH+=("$1") ;;
        --no-stack) ATELIER_NO_STACK=1; PASSTHROUGH+=("$1") ;;
        *) PASSTHROUGH+=("$1") ;;
    esac
    shift
done

info() { echo "[atelier-install] $*"; }
warn() {
    WARNINGS+=("$*")
    printf "%b[atelier-install] WARN:%b %s\n" "$C_YELLOW" "$C_RESET" "$*" >&2
}
error() {
    ERRORS+=("$*")
    printf "%b[atelier-install] ERROR:%b %s\n" "$C_RED" "$C_RESET" "$*" >&2
}
fail() { error "$*"; exit 1; }

collect_issues_from_output() {
    local output="$1"
    local line
    while IFS= read -r line; do
        case "$line" in
            *"] WARN:"*)
                WARNINGS+=("${line#*WARN: }")
                ;;
            *"] ERROR:"*)
                ERRORS+=("${line#*ERROR: }")
                ;;
        esac
    done <<<"$output"
}

print_issue_group() {
    local title="$1"
    local color="$2"
    shift 2
    local entries=("$@")
    local -A counted=()
    local -A printed=()
    local entry
    local count=0

    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" && -z "${counted[$entry]+x}" ]] || continue
        counted["$entry"]=1
        count=$((count + 1))
    done

    [[ $count -gt 0 ]] || return 0
    printf "%b%s (%d)%b\n" "$color" "$title" "$count" "$C_RESET"
    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" && -z "${printed[$entry]+x}" ]] || continue
        printed["$entry"]=1
        printf "  %b-%b %s\n" "$color" "$C_RESET" "$entry"
    done
}

print_final_report() {
    echo ""
    echo "══════════════════════════════════════════════"
    echo " Atelier Install Report"
    echo "══════════════════════════════════════════════"
    if [[ ${#ERRORS[@]} -eq 0 && ${#WARNINGS[@]} -eq 0 ]]; then
        echo "  No warnings or errors detected."
        return
    fi
    print_issue_group "Errors" "$C_RED" "${ERRORS[@]+"${ERRORS[@]}"}"
    print_issue_group "Warnings" "$C_YELLOW" "${WARNINGS[@]+"${WARNINGS[@]}"}"
}

run() {
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

install_uv_if_needed() {
    if command -v uv >/dev/null 2>&1; then
        info "Found uv: $(uv --version 2>/dev/null || echo unknown)"
        return
    fi

    need_cmd curl
    info "Installing uv (official installer)..."
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] curl -LsSf https://astral.sh/uv/install.sh | sh"
    else
        # shellcheck disable=SC2016
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi

    if [[ -x "${HOME}/.local/bin/uv" ]]; then
        export PATH="${HOME}/.local/bin:${PATH}"
    fi

    command -v uv >/dev/null 2>&1 || fail "uv install completed but uv is still not on PATH"
    info "Installed uv: $(uv --version 2>/dev/null || echo unknown)"
}

prepare_repo() {
    local dir
    dir="$(dirname "$ATELIER_INSTALL_DIR")"
    run mkdir -p "$dir"

    if [[ -d "$ATELIER_INSTALL_DIR/.git" ]]; then
        info "Updating existing repository in $ATELIER_INSTALL_DIR"
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR fetch --tags --prune origin"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR checkout $ATELIER_REF"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR pull --ff-only origin $ATELIER_REF"
        else
            git -C "$ATELIER_INSTALL_DIR" fetch --tags --prune origin
            git -C "$ATELIER_INSTALL_DIR" checkout "$ATELIER_REF"
            git -C "$ATELIER_INSTALL_DIR" pull --ff-only origin "$ATELIER_REF"
        fi
    else
        info "Cloning $ATELIER_REPO_URL into $ATELIER_INSTALL_DIR"
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] git clone --depth=1 --branch $ATELIER_REF $ATELIER_REPO_URL $ATELIER_INSTALL_DIR"
        else
            git clone --depth=1 --branch "$ATELIER_REF" "$ATELIER_REPO_URL" "$ATELIER_INSTALL_DIR"
        fi
    fi
}

install_console_scripts() {
    local extras="mcp,memory,smart,cloud,repo-map,api,postgres,vector,parsers,telemetry"
    local package_spec="${ATELIER_INSTALL_DIR}[${extras}]"
    local install_args=(tool install --force)

    if [[ "$ATELIER_LOCAL" == "1" ]]; then
        install_args+=(--editable)
    fi
    install_args+=("$package_spec")

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        printf '[dry-run] UV_TOOL_BIN_DIR=%q UV_TOOL_DIR=%q uv' "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
        printf ' %q' "${install_args[@]}"
        printf '\n'
        return
    fi

    mkdir -p "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
    UV_TOOL_BIN_DIR="$ATELIER_BIN_DIR" \
        UV_TOOL_DIR="$ATELIER_TOOL_DIR" \
        uv "${install_args[@]}"

    rm -f \
        "$ATELIER_BIN_DIR/atelier-api" \
        "$ATELIER_BIN_DIR/atelier-codex" \
        "$ATELIER_BIN_DIR/atelier-bench"
}

persist_install_record() {
    local record_dir
    record_dir="$(dirname "$ATELIER_INSTALL_RECORD")"

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] mkdir -p $record_dir"
        echo "[dry-run] printf '%s\\n' '$ATELIER_INSTALL_DIR' > '$ATELIER_INSTALL_RECORD'"
        return
    fi

    mkdir -p "$record_dir"
    printf '%s\n' "$ATELIER_INSTALL_DIR" > "$ATELIER_INSTALL_RECORD"
}

main() {
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) fail "Unsupported OS: $(uname -s). This installer supports Linux/macOS." ;;
    esac

    need_cmd git
    need_cmd bash
    install_uv_if_needed

    if [[ "$ATELIER_USE_CURRENT_REPO" == "1" ]]; then
        if [[ "$ATELIER_LOCAL" == "1" ]]; then
            info "Local mode: using current directory as an editable install source"
        else
            info "Using current directory as the install source"
        fi
        ATELIER_INSTALL_DIR="$(pwd)"
    else
        prepare_repo
    fi
    export ATELIER_INSTALL_DIR

    info "Installing Atelier console commands..."
    install_console_scripts
    persist_install_record

    if command -v npm >/dev/null 2>&1; then
        info "Installing codeburn (token/cost reporting)..."
        run npm install -g codeburn
        info "Installing tokscale (token/cost reporting)..."
        run npm install -g tokscale
    else
        warn "npm not found — skipping codeburn and tokscale (install Node.js 20+ to enable)"
    fi

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] ln -sf $ATELIER_INSTALL_DIR/bin/atelier-status $ATELIER_BIN_DIR/atelier-status"
    elif [[ -f "$ATELIER_INSTALL_DIR/bin/atelier-status" ]]; then
        run ln -sf "$ATELIER_INSTALL_DIR/bin/atelier-status" "$ATELIER_BIN_DIR/atelier-status"
    else
        warn "atelier-status helper not found at $ATELIER_INSTALL_DIR/bin/atelier-status"
    fi

    if [[ ":$PATH:" != *":$ATELIER_BIN_DIR:"* ]]; then
        warn "$ATELIER_BIN_DIR is not currently on PATH"
        echo ""
        echo "Add this to your shell profile, then restart your shell:"
        echo "  export PATH=\"$ATELIER_BIN_DIR:\$PATH\""
        echo ""
    fi

    info "Initializing Atelier runtime store..."
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $ATELIER_BIN_DIR/atelier init"
    else
        "$ATELIER_BIN_DIR/atelier" init >/dev/null
    fi

    if [[ "$ATELIER_NO_HOSTS" != "1" ]]; then
        info "Installing Atelier host integrations (skip if host CLI is missing)..."
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] bash $ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
        else
            local host_output host_ret
            set +e
            host_output="$(bash "$ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} 2>&1)"
            host_ret=$?
            set -e
            printf "%s\n" "$host_output"
            collect_issues_from_output "$host_output"
            if [[ $host_ret -ne 0 ]]; then
                ERRORS+=("One or more host integrations failed")
                FINAL_EXIT_CODE=1
            fi
        fi
        # Persist host detection results for the Docker service
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write 2>/dev/null || true
        fi
    else
        info "Skipping host integrations because ATELIER_NO_HOSTS=1"
        # Still persist current detection state even when skipping install
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write 2>/dev/null || true
        fi
    fi

    if [[ "$ATELIER_NO_SERVICECTL" != "1" ]]; then
        info "Starting Atelier background service controller..."
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] $ATELIER_BIN_DIR/atelier servicectl start --interval-seconds $ATELIER_SERVICECTL_INTERVAL_SECONDS --maintenance-interval-seconds $ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS"
        else
            "$ATELIER_BIN_DIR/atelier" servicectl start \
                --interval-seconds "$ATELIER_SERVICECTL_INTERVAL_SECONDS" \
                --maintenance-interval-seconds "$ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS" >/dev/null
        fi
    else
        info "Skipping background service controller because ATELIER_NO_SERVICECTL=1"
    fi

    STACK_STARTED=0
    if [[ "$ATELIER_NO_STACK" != "1" ]]; then
        if command -v docker >/dev/null 2>&1; then
            info "Starting Atelier visualization stack (service + frontend)..."
            if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $ATELIER_BIN_DIR/atelier stack start"
            else
                "$ATELIER_BIN_DIR/atelier" stack start \
                    && STACK_STARTED=1 \
                    || warn "Visualization stack did not start (Docker daemon may not be running)"
            fi
        else
            info "Skipping visualization stack because Docker is not installed"
        fi
    else
        info "Skipping visualization stack because ATELIER_NO_STACK=1"
    fi

    if [[ "$STACK_STARTED" == "1" ]]; then
        echo "  Visualization stack is running:"
        echo "    frontend: http://localhost:3125"
        echo "    service:  http://localhost:8787"
        echo ""
    fi
    echo "  Commands:"
    echo "    atelier --version           - Check core CLI version"
    echo "    atelier-mcp --version       - Check MCP server version"
    echo "    atelier servicectl status   - View background service and systemctl status"
    echo "    atelier stack start         - Start production API and Frontend (requires Docker)"
    echo "    atelier stack stop          - Stop the visualization stack"
    echo "    atelier stack logs          - View stack logs"
    echo "    atelier-status              - Show one-line status of the active reasoning run"

    if [[ "$ATELIER_DRY_RUN" != "1" ]]; then
        echo ""
        info "Importing agent sessions (all available history)..."
        "$ATELIER_BIN_DIR/atelier" import \
            && info "Session import complete." \
            || warn "Session import failed or no sessions found (non-fatal)."

        echo ""
        info "Collecting external reports (codeburn: month, tokscale: month)..."
        "$ATELIER_BIN_DIR/atelier" external-report --tool codeburn --period month \
            && info "codeburn report collected." \
            || warn "codeburn not installed or failed (non-fatal)."
        "$ATELIER_BIN_DIR/atelier" external-report --tool "codeburn:optimize" --period month \
            && info "codeburn optimization report collected." \
            || warn "codeburn optimization report failed (non-fatal)."
        "$ATELIER_BIN_DIR/atelier" external-report --tool tokscale --period month \
            && info "tokscale report collected." \
            || warn "tokscale not installed or failed (non-fatal)."
    fi

    print_final_report
    if [[ ${#ERRORS[@]} -gt 0 ]]; then
        info "${C_BOLD}${C_RED}Completed with errors.${C_RESET}"
    elif [[ ${#WARNINGS[@]} -gt 0 ]]; then
        info "${C_BOLD}${C_YELLOW}Completed with warnings.${C_RESET}"
    else
        info "Installation complete."
    fi

    return "$FINAL_EXIT_CODE"
}

main "$@"
